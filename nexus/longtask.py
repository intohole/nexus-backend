"""长任务检查点原语（S211 LR2 收编）：journal 追加+fsync / 排空与可用性闸门 / 启动续跑编排。

收编边界（council.py 同款纪律）：只收与业务域无关的机械骨架——
arcade c3cda6f 三件套中 gen_journal 的 JSONL 追加+fsync 崩溃安全/归档/
孤儿清扫、runtime_guard 的 draining+llm_ready+inflight+wait_idle、
gen_recovery 的「枚举→判定→续跑或诚实结算→清扫」编排骨架。任务键语义、
resume 载荷字段映射、活动状态判定、续跑执行器、失败文案是业务域资产，
经参数注入不进共享仓。

三契约：
- 崩溃安全：单行 JSON + os.fsync；进程中途死亡残留的残行在读取侧跳过、
  在写入侧换行隔离（修复 arcade 原实现「崩溃后首条记录与残行粘连被整体
  吞掉」的潜伏丢失——残行无换行结尾时新记录落新行）。
- 闸门三态：draining 排空标记（停机不再接新活、等在飞归零）、llm_ready
  两段判定（未探活前不拦截、探活后可用性反转）、wait_idle 有界等待。
- 恢复编排：逐行（枚举由消费方供 DB 行）判 resume_payload——缺失/不活跃
  →按 needs_settle 决定诚实结算或静默清理；续跑次数到帽→诚实结算；
  其余→记 resume 事件并回调续跑；收尾孤儿 journal 清扫。

射程外（诚实边界）：DB 会话与行查询归消费方（crontab.py 同款）；结算
文案、resume 后执行器（如何续）、结算落库（patch 到什么状态）全部业务域。
供给形态档位=强契约工具档（显式 import 显式调用）。

r273 增 make_journal 工厂档：目录惰性解析+同路径实例缓存单例，收编
arcade gen_journal / piying run_journal / MiaoBi deck_journal 三仓
同构 wrapper 的机械面（目录来源+缓存不变量），业务转发面签名不变。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Optional

from nexus.logging import get_logger
from nexus.utils.time import TimeUtils

logger = get_logger("nexus.longtask")

TaskId = object
_SETTLE_EVENTS = ("settle", "settle_start")
_ORPHAN_CASE = "orphan"
_CAP_CASE = "cap"


class TaskJournal:
    """任务级 JSONL 检查点日志：追加/fsync/归档/清扫，任务键任意 int|str。

    目录布局：<root>/<task_id>.jsonl 现役，结算后 clear 归档到
    <root>/<archive_dir_name>/<task_id>_<时间戳>.jsonl；读取侧残行跳过
    不抛错（崩溃残留可续用）。
    """

    def __init__(self, root_dir: str | Path, *, archive_dir_name: str = "archive") -> None:
        self._root = Path(root_dir)
        self._archive_name = archive_dir_name
        self._seq: dict[object, int] = {}

    def _dir(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def _path(self, task_id: object) -> Path:
        return self._dir() / f"{task_id}.jsonl"

    def _archive_dir(self) -> Path:
        path = self._dir() / self._archive_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record(self, key: object, event: str, **fields: object) -> None:
        kid = str(key)
        if key not in self._seq:
            self._seq[key] = len(self._read(self._path(kid)))
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        payload: dict[str, object] = {
            "seq": seq, "event": event, "ts": TimeUtils.now_utc_naive().isoformat()}
        payload.update(fields)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        target = self._path(kid)
        needs_newline = False
        if target.is_file() and target.stat().st_size > 0:
            with target.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_newline = fh.read(1) != b"\n"
        if needs_newline:
            line = "\n" + line
        fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read(self, path: Path) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if not path.is_file():
            return rows
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(raw)
            except Exception as exc:
                logger.warning("longtask journal 残行已跳过：%s", str(exc)[:120])
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def events(self, key: object, include_archive: bool = True) -> list[dict[str, object]]:
        kid = str(key)
        rows = self._read(self._path(kid))
        if rows or not include_archive:
            return rows
        archived = sorted(self._archive_dir().glob(f"{kid}_*.jsonl"))
        return self._read(archived[-1]) if archived else []

    def resume_payload(self, key: object) -> Optional[dict[str, object]]:
        rows = self.events(key)
        start = next((row for row in reversed(rows) if row.get("event") == "start"), None)
        if start is None:
            return None
        payload: dict[str, object] = {
            k: v for k, v in start.items() if k not in ("seq", "event", "ts")}
        payload["resume_count"] = sum(1 for row in rows if row.get("event") == "resume")
        return payload

    def clear(self, key: object) -> None:
        kid = str(key)
        target = self._path(kid)
        rows = self._read(target)
        self._seq.pop(key, None)
        if target.is_file() and any(
                row.get("event") in _SETTLE_EVENTS for row in rows):
            stamp = TimeUtils.now_utc_naive().strftime("%Y%m%dT%H%M%S")
            target.replace(self._archive_dir() / f"{kid}_{stamp}.jsonl")
            return
        target.unlink(missing_ok=True)

    def sweep(self, active_ids: set[object]) -> int:
        cleared = 0
        for target in self._dir().glob("*.jsonl"):
            if target.stem.isdigit() and int(target.stem) not in active_ids:
                target.unlink(missing_ok=True)
                cleared += 1
        return cleared


class LazyJournal:
    """make_journal 工厂档：目录惰性解析 + 同路径实例缓存单例。

    目录来源（settings 属性/env var/默认路径）经 resolver 注入，每次取
    实例时才求值——测试缝等效保留（改 env/patch settings 后下一次取数
    即生效，不要求声明期目录已存在）。同路径缓存同一 TaskJournal 实例：
    _seq 连续性是 TaskJournal 自身不变量，原语不再摊派给消费方手搓
    （r208 get_asset_gateway 按 key 单例同款）。reset() 供测试隔离清
    缓存；运行期调用行为仍等价（新实例首笔 record 会重读全量补齐 seq），
    仅多一次全量读。

    契约 hazard（自 piying run_journal 升入单源）：同仓多个恢复面禁止
    共用 journal 目录——sweep/孤儿清扫按目录进行，混用会互删对方不在册
    文件（blueprint_job 恢复面互删案）。
    """

    def __init__(
        self,
        dir_resolver: Callable[[], str | Path],
        *,
        archive_dir_name: str = "archive",
    ) -> None:
        self._resolver = dir_resolver
        self._archive_name = archive_dir_name
        self._instances: dict[str, TaskJournal] = {}

    def current(self) -> TaskJournal:
        path = str(self._resolver())
        instance = self._instances.get(path)
        if instance is None:
            instance = TaskJournal(path, archive_dir_name=self._archive_name)
            self._instances[path] = instance
        return instance

    def reset(self) -> int:
        cleared = len(self._instances)
        self._instances.clear()
        return cleared

    def record(self, key: object, event: str, **fields: object) -> None:
        self.current().record(key, event, **fields)

    def events(
        self, key: object, include_archive: bool = True
    ) -> list[dict[str, object]]:
        return self.current().events(key, include_archive)

    def resume_payload(self, key: object) -> Optional[dict[str, object]]:
        return self.current().resume_payload(key)

    def clear(self, key: object) -> None:
        self.current().clear(key)

    def sweep(self, active_ids: set[object]) -> int:
        return self.current().sweep(active_ids)


def make_journal(
    dir_resolver: Callable[[], str | Path],
    *,
    archive_dir_name: str = "archive",
) -> LazyJournal:
    """journal 工厂：仓接入面=一行声明（r273 扩容判据，禁再手搓 wrapper）。"""
    return LazyJournal(dir_resolver, archive_dir_name=archive_dir_name)


class RuntimeGate:
    """长任务运行闸门：排空标记 + LLM 可用性两段判定 + 在飞计数有界等待。

    availability 默认懒取 nexus.is_ironman_available（ironman 网关探活）；
    inflight 由消费方注入（在飞任务计数来源是业务域状态）。
    """

    def __init__(
        self,
        *,
        availability: Optional[Callable[[], bool]] = None,
        inflight: Optional[Callable[[], int]] = None,
    ) -> None:
        self._availability = availability
        self._inflight_fn = inflight or (lambda: 0)
        self._draining = False
        self._llm_checked = False

    def _available(self) -> bool:
        if self._availability is not None:
            return self._availability()
        from nexus import is_ironman_available
        return is_ironman_available()

    def mark_llm_checked(self) -> None:
        self._llm_checked = True

    def llm_blocked(self) -> bool:
        return self._llm_checked and not self._available()

    def mark_draining(self) -> None:
        self._draining = True

    def is_draining(self) -> bool:
        return self._draining

    def inflight(self) -> int:
        return self._inflight_fn()

    def reset(self) -> None:
        self._draining = False
        self._llm_checked = False

    async def wait_idle(self, timeout_seconds: float) -> int:
        waited = 0.0
        while self._inflight_fn() and waited < timeout_seconds:
            await asyncio.sleep(0.5)
            waited += 0.5
        return self._inflight_fn()


async def recover_interrupted(
    journal: TaskJournal,
    rows: Iterable[tuple[TaskId, str]],
    *,
    max_resumes: int,
    is_resumable: Callable[[str, dict[str, object]], bool],
    needs_settle: Callable[[str], bool],
    on_fail: Callable[[TaskId, Optional[dict[str, object]], str], Awaitable[None]],
    on_resume: Callable[[TaskId, dict[str, object]], None],
) -> tuple[int, int]:
    """启动恢复编排骨架：逐行判定 续跑/诚实结算/静默清理，返回（续跑数，结算数）。

    rows 由消费方从 DB 枚举（(任务键, 状态) 二元组）；判定用 journal 的
    resume_payload——缺失或不满足 is_resumable(状态, 载荷) 时按
    needs_settle(状态) 决定 on_fail（case=orphan）或静默 clear；
    续跑次数 ≥ max_resumes 时 on_fail（case=cap）；其余记 resume 事件
    （resume_no=旧次数+1）并同步回调 on_resume（续跑任务排程是消费方
    职责，create_task 自理）。收尾 sweep 不在枚举行列的
    孤儿 journal 文件。on_fail 抛错会中断整批（与 arcade 原语义一致，
    消费方自行决定是否兜底）。
    """
    resumed = 0
    settled = 0
    seen: set[object] = set()
    rows = list(rows)
    for task_id, status in rows:
        seen.add(task_id)
        payload = journal.resume_payload(task_id)
        if payload is None or not is_resumable(status, payload):
            if needs_settle(status):
                await on_fail(task_id, payload, _ORPHAN_CASE)
                settled += 1
            journal.clear(task_id)
            continue
        count = int(payload.get("resume_count") or 0)
        if count >= max_resumes:
            await on_fail(task_id, payload, _CAP_CASE)
            settled += 1
            journal.clear(task_id)
            continue
        journal.record(task_id, "resume", resume_no=count + 1)
        on_resume(task_id, payload)
        resumed += 1
    # r272 刀22（狩猎A P2）：枚举瞬态空集防线——rows 为空时 seen=∅，sweep 会把
    # 全部数字键 journal 当孤儿 unlink，崩溃任务的唯一续跑载荷蒸发、任务行与
    # journal 从此脱钩。空枚举跳过 sweep 并 warning fail-visible（claude SDK
    # session_store GC「删除集合必须来自已核实孤儿判定，不接受空集全删」同律）。
    if rows:
        swept = journal.sweep(seen)
        if swept:
            logger.info("orphan journal files swept: %s", swept)
    else:
        logger.warning(
            "recover_interrupted: empty task enumeration, skip orphan sweep "
            "(transient enum failure would erase all journals)"
        )
    return resumed, settled
