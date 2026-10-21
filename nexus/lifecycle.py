"""数据生命周期原语（S29）：温表 TTL / 冷表归档 / keep-N 收敛的统一执行器。

背景：全舰队 SQLite 单文件库无 DBA，日志/观测/事件流类表只增不减——单仓已发生
≥5 起手工清洗（notifyCenter r22 / jinnang r4 / beeMemory r15 / LifeCompass r8 /
wanxiang r3），教训#48 god 磁盘 100% 满 20 分钟实锤磁盘代价。本模块把两判例收归单源：
- notifyCenter shelf_life（r22）：周期流顶新 + 归档不删除 + 注册表卫生；
- usercenter audit_service.cleanup_old_logs（r13 系）：retention_days TTL + keep-N
  per action + 动态配置 + startup/周期双接线。

契约（S29 数据生命周期契约，handoff/24）：
- 分级四类：热（用户资产，永不动）/ 温（日志观测流，TTL purge）/ 冷（审计留痕，
  归档 JSONL 后删）/ 污（测试残留，精确圈定一次性配方）；
- 消费面先行：立 TTL 前必须登记「谁在读+读的窗口形态」，消费面不明一律豁免；
- 五护栏：dry_run 默认；删除前待删行导出 JSONL.gz（备份失败=跳过该表 fail-closed）；
  min_age_hours 保同日（顶新判例，不删今天的数据）；keep_N 分组收敛（快照类）；
  结构化留痕（表/删数/备份路径）。

用法（仓内 async 接线）：
    from nexus.lifecycle import LifecyclePolicy, enforce_lifecycle_async
    policies = [LifecyclePolicy(table="gateway_usage_log", ttl_days=90)]
    report = await enforce_lifecycle_async(session, policies, dry_run=False)

离线/脚本：enforce_lifecycle(db_path, policies)（sqlite3 文件层直连，只读模式探测）。
"""
from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("nexus.lifecycle")

MODE_PURGE = "purge"
MODE_ARCHIVE = "archive"
DEFAULT_MIN_AGE_HOURS = 24
DEFAULT_ARCHIVE_DIR = "data/archives"


@dataclass(frozen=True)
class LifecyclePolicy:
    """单表生命周期策略。time_column 必须是可比较的时间列（含 epoch 整数形态）。"""

    table: str
    ttl_days: int = 0
    time_column: str = "created_at"
    mode: str = MODE_PURGE
    keep_n: int = 0
    group_column: str | None = None
    min_age_hours: int = DEFAULT_MIN_AGE_HOURS
    archive_dir: str = DEFAULT_ARCHIVE_DIR
    id_column: str = "id"
    extra_where: str = ""
    """附加圈定条件（SQL 片段），如 "archived_at IS NOT NULL" 或 "channel='e2e'"。"""
    cascade_from: str = ""
    cascade_fk: str = ""
    """级联跟随：本表删除集=前序表（cascade_from）本次删除行的 cascade_fk 外键指向集。

    关联表与主表时间轨常不同步（关联 created_at 回填统一写、主表 publish_time 是
    业务时间），按各自 TTL 删会留悬挂外键。声明级联后本表不再需要 ttl/keep_n，
    删除集完全跟随前序表（前序表必须出现在本 policy 之前且带 id_column）。
    """

    def __post_init__(self) -> None:
        if self.mode not in (MODE_PURGE, MODE_ARCHIVE):
            raise ValueError(f"lifecycle policy {self.table}: mode 必须 purge/archive，得到 {self.mode}")
        if self.cascade_from and not self.cascade_fk:
            raise ValueError(f"lifecycle policy {self.table}: cascade_from 需同时声明 cascade_fk")
        if self.ttl_days <= 0 and self.keep_n <= 0 and not self.cascade_from:
            raise ValueError(f"lifecycle policy {self.table}: ttl_days 与 keep_n 至少一个 > 0")
        if self.keep_n > 0 and self.group_column is not None:
            # 分组 keep-N 依赖窗口函数（SQLite >= 3.25）
            pass


@dataclass
class TableReport:
    table: str
    mode: str = ""
    scanned: int = 0
    eligible: int = 0
    deleted: int = 0
    backup_path: str = ""
    dry_run: bool = True
    skipped_reason: str = ""
    oldest_kept: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table, "mode": self.mode, "scanned": self.scanned,
            "eligible": self.eligible, "deleted": self.deleted, "backup_path": self.backup_path,
            "dry_run": self.dry_run, "skipped_reason": self.skipped_reason,
            "oldest_kept": self.oldest_kept,
        }


@dataclass
class LifecycleReport:
    ran_at: str = ""
    tables: list[TableReport] = field(default_factory=list)
    total_deleted: int = 0
    ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "total_deleted": self.total_deleted,
            "ok": self.ok,
            "tables": [t.as_dict() for t in self.tables],
        }


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _time_lt_hours(col: str, hours: int) -> str:
    return (
        f"(datetime({col}) < datetime('now','localtime','-{hours} hours') "
        f"OR (typeof({col}) IN ('integer','real') AND {col} < "
        f"strftime('%s','now','localtime') - {hours} * 3600))"
    )


def eligible_where(policy: LifecyclePolicy) -> str:
    """待删行 WHERE 子句：删除集 =（TTL 到期 ∪ keep-N 圈外）∩ min_age 护栏 ∩ extra_where。

    keep-N 与 TTL 是独立删除语义（usercenter trim_login_logs 判例：分组收敛不以
    行龄为前提——组内最新 N 条之外即删）；min_age 护栏与 extra_where 对删除集
    全量生效（保同日/圈定）。全部子句缺失时返回恒假（0）——policy 校验已保证
    至少一个删除维度存在，此为防御。
    """
    col = _quote(policy.time_column)
    removal: list[str] = []
    if policy.ttl_days > 0:
        removal.append(_time_lt_hours(col, policy.ttl_days * 24))
    if policy.keep_n > 0:
        idc = _quote(policy.id_column)
        if policy.group_column:
            grp = _quote(policy.group_column)
            rank_src = (
                f"SELECT {idc} FROM (SELECT {idc}, ROW_NUMBER() OVER "
                f"(PARTITION BY {grp} ORDER BY {col} DESC, {idc} DESC) AS rn "
                f"FROM {_quote(policy.table)}) WHERE rn <= {policy.keep_n}"
            )
        else:
            rank_src = (
                f"SELECT {idc} FROM {_quote(policy.table)} "
                f"ORDER BY {col} DESC, {idc} DESC LIMIT {policy.keep_n}"
            )
        removal.append(f"{idc} NOT IN ({rank_src})")
    if not removal:
        return "0"
    guards: list[str] = ["(" + " OR ".join(removal) + ")"]
    if policy.min_age_hours > 0:
        guards.append(_time_lt_hours(col, policy.min_age_hours))
    if policy.extra_where:
        guards.append(f"({policy.extra_where})")
    return " AND ".join(guards)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _export_rows_jsonl(conn: sqlite3.Connection, policy: LifecyclePolicy, where: str,
                       backup_dir: Path, stamp: str, params: tuple[Any, ...] = ()) -> Path:
    """删除前护栏：待删行全量导出 JSONL.gz（purge 与 archive 模式同走此备份）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"{policy.table}-{stamp}.jsonl.gz"
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(policy.table)})")]
    sel = ", ".join(_quote(c) for c in cols)
    cursor = conn.execute(f"SELECT {sel} FROM {_quote(policy.table)} WHERE {where}", params)
    count = 0
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for row in cursor:
            payload = {c: _jsonable(v) for c, v in zip(cols, row)}
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            count += 1
    if count == 0 or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"backup empty for {policy.table} (rows={count}) — refuse to delete")
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def enforce_lifecycle(
    db_path: str | Path,
    policies: Sequence[LifecyclePolicy],
    *,
    dry_run: bool = True,
    archive_root: str | Path | None = None,
) -> LifecycleReport:
    """文件层执行器：sqlite3 直连（scripts/离线清理与本地验证用）。

    dry_run=True 只统计不动数据；实跑顺序=导出待删行→校验备份非空→DELETE→留痕。
    备份失败/表缺失/列缺失均跳过该表并记录 skipped_reason（fail-closed，不中断其余表）。
    """
    report = LifecycleReport(ran_at=datetime.now().isoformat(timespec="seconds"))
    path = Path(db_path)
    if not path.exists():
        report.ok = False
        report.tables.append(TableReport(table=str(db_path), skipped_reason=f"db not found: {path}"))
        return report
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    cascaded_ids: dict[str, list[Any]] = {}
    try:
        for policy in policies:
            tr = TableReport(table=policy.table, mode=policy.mode, dry_run=dry_run)
            try:
                if not _table_exists(conn, policy.table):
                    tr.skipped_reason = "table missing"
                    report.tables.append(tr)
                    continue
                cols = _columns(conn, policy.table)
                needed = set() if policy.cascade_from else {policy.time_column}
                if policy.keep_n > 0:
                    needed.add(policy.id_column)
                if policy.group_column:
                    needed.add(policy.group_column)
                missing = needed - cols
                if missing:
                    tr.skipped_reason = f"columns missing: {sorted(missing)}"
                    report.tables.append(tr)
                    continue
                tr.scanned = int(conn.execute(
                    f"SELECT COUNT(*) FROM {_quote(policy.table)}"
                ).fetchone()[0])
                if policy.cascade_from:
                    ids = cascaded_ids.get(policy.cascade_from)
                    if ids is None:
                        tr.skipped_reason = f"cascade source {policy.cascade_from} not seen"
                        report.tables.append(tr)
                        continue
                    if not ids:
                        report.tables.append(tr)
                        continue
                    marks = ",".join("?" for _ in ids)
                    where = f"{_quote(policy.cascade_fk)} IN ({marks})"
                    params: tuple[Any, ...] = tuple(ids)
                else:
                    where = eligible_where(policy)
                    params = ()
                tr.eligible = int(conn.execute(
                    f"SELECT COUNT(*) FROM {_quote(policy.table)} WHERE {where}", params
                ).fetchone()[0])
                if not policy.cascade_from:
                    kept = conn.execute(
                        f"SELECT MIN({_quote(policy.time_column)}) FROM {_quote(policy.table)} "
                        f"WHERE NOT ({where})"
                    ).fetchone()[0]
                    tr.oldest_kept = str(kept)[:19] if kept is not None else None
                    cascaded_ids[policy.table] = [
                        r[0] for r in conn.execute(
                            f"SELECT {_quote(policy.id_column)} FROM {_quote(policy.table)} WHERE {where}", params
                        )
                    ]
                if tr.eligible == 0 or dry_run:
                    report.tables.append(tr)
                    continue
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_dir = Path(archive_root or path.parent) / policy.archive_dir
                backup = _export_rows_jsonl(conn, policy, where, backup_dir, stamp, params)
                tr.backup_path = str(backup)
                if policy.mode == MODE_ARCHIVE:
                    logger.info("[lifecycle] archived %s rows of %s -> %s",
                                tr.eligible, policy.table, backup)
                cur = conn.execute(
                    f"DELETE FROM {_quote(policy.table)} WHERE {where}", params
                )
                conn.commit()
                tr.deleted = int(cur.rowcount or 0)
                report.total_deleted += tr.deleted
                logger.info("[lifecycle] %s %s rows deleted from %s (backup=%s)",
                            policy.mode, tr.deleted, policy.table, backup.name)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                tr.skipped_reason = f"error: {exc}"
                report.ok = False
                logger.warning("[lifecycle] skipped %s: %s", policy.table, exc)
            report.tables.append(tr)
    finally:
        conn.close()
    return report


async def enforce_lifecycle_async(
    session: Any,
    policies: Sequence[LifecyclePolicy],
    *,
    dry_run: bool = True,
    db_path: str | Path | None = None,
    archive_root: str | Path | None = None,
) -> LifecycleReport:
    """仓内 async 接线薄壳：从 AsyncSession/engine 解析 sqlite 文件路径后走文件层执行。

    db_path 显式传入优先；否则从 engine.url 解析（非 sqlite 后端拒绝执行——
    本原语只治理 SQLite 单文件库，业务在 PG/MySQL 的清理需仓内自建）。
    """
    import asyncio

    resolved = db_path
    if resolved is None:
        engine = getattr(session, "bind", None) or getattr(session, "get_bind", lambda: None)()
        if engine is None:
            raise ValueError("enforce_lifecycle_async: 无法解析 engine，请显式传 db_path")
        url = str(getattr(engine, "url", "") or "")
        if not url.startswith("sqlite"):
            raise ValueError(f"enforce_lifecycle_async: 仅支持 sqlite，得到 {url.split('://')[0]}")
        resolved = url.split("///")[-1] if "///" in url else url.split("://")[-1]
    return await asyncio.to_thread(
        enforce_lifecycle, resolved, policies, dry_run=dry_run, archive_root=archive_root
    )


def lifecycle_status(
    db_path: str | Path,
    policies: Sequence[LifecyclePolicy],
) -> dict[str, dict[str, Any]]:
    """水位观测：每表行数/最老行/已达 TTL 行数（供 /health 或 sentinel 探针消费）。"""
    path = Path(db_path)
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return {str(path): {"error": "db not found"}}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        for policy in policies:
            if not _table_exists(conn, policy.table):
                out[policy.table] = {"error": "table missing"}
                continue
            where = eligible_where(policy)
            rows = int(conn.execute(f"SELECT COUNT(*) FROM {_quote(policy.table)}").fetchone()[0])
            overdue = int(conn.execute(
                f"SELECT COUNT(*) FROM {_quote(policy.table)} WHERE {where}"
            ).fetchone()[0])
            oldest = conn.execute(
                f"SELECT MIN({_quote(policy.time_column)}) FROM {_quote(policy.table)}"
            ).fetchone()[0]
            out[policy.table] = {
                "rows": rows, "overdue": overdue,
                "oldest": str(oldest)[:19] if oldest is not None else None,
            }
    finally:
        conn.close()
    return out


__all__ = [
    "LifecyclePolicy", "LifecycleReport", "TableReport",
    "enforce_lifecycle", "enforce_lifecycle_async", "lifecycle_status",
    "eligible_where", "MODE_PURGE", "MODE_ARCHIVE",
]
