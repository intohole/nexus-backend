"""文件生命周期原语（S104 五护栏单源：TTL+keep-N 顶新+清理前归档+白名单+结构化 Report）.

业界口径（2025 共识）：纯时间/计数规则的清理归 OS 层（logrotate/systemd-tmpfiles），
语义级保留（分组 keep-N/动态消费面 skip_predicate/配额水位信号）无成熟 PyPI 包，
应用内小工具+包管理器单源分发是共识形态。S104 模板 v2 六仓接线（handoff/81）
经 r198 收归本原语：指纹制单源（文档级）升级为运行时单源，仓内仅存 4 行 shim。
五护栏契约见 handoff/81 §三；仓内配置面（file_faces）在各仓 services/file_gc.py。
"""
from __future__ import annotations

import fnmatch
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

PRIMITIVE_VERSION = "nexus/file_lifecycle.py@r198"


@dataclass
class FileFace:
    name: str
    root: Path
    tier: str  # hot | warm | cold（hot 拒绝自动清理）
    ttl_hours: float = 336.0  # 14d 默认，温类可覆写
    keep_n: int = 3
    group_by: str | None = None  # None=整目录一组；"parent"=按父目录分组
    include_globs: tuple[str, ...] = ("*",)
    consumer_globs: tuple[str, ...] = ()  # 消费面命中即跳过
    never_globs: tuple[str, ...] = ()
    skip_predicate: object = None  # Callable[[str], bool]|None：动态消费面（如 DB 引用集）True=跳过
    archive_dir: Path | None = None  # None = root/_archive
    archive_keep_n: int = 3
    quota_warn_mb: float = 0.0  # 0=不检查
    clock: float = 0.0  # 注入测试时钟；0=用真实时间


def _now(face: FileFace) -> float:
    return face.clock if face.clock else time.time()


def _hit(rel: str, globs: tuple[str, ...]) -> bool:
    name = Path(rel).name
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g) for g in globs)


def _group_key(rel: str, group_by: str | None) -> str:
    if group_by == "parent":
        return str(Path(rel).parent)
    return "__all__"


def scan_face(face: FileFace) -> dict:
    """only scan：返回候选/保留/跳过分桶（零副作用）。"""
    now = _now(face)
    root = face.root
    items: dict[str, list[tuple[float, str]]] = {}
    skipped_consumer: list[str] = []
    skipped_never: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel.startswith("_archive"):
            continue
        if not _hit(rel, face.include_globs):
            continue
        if _hit(rel, face.never_globs):
            skipped_never.append(rel)
            continue
        if _hit(rel, face.consumer_globs):
            skipped_consumer.append(rel)
            continue
        if face.skip_predicate is not None and face.skip_predicate(rel):
            skipped_consumer.append(rel)
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        items.setdefault(_group_key(rel, face.group_by), []).append((mtime, rel))
    expired: list[str] = []
    kept_top: list[str] = []
    for group, members in items.items():
        members.sort(reverse=True)  # 新→旧
        for i, (mtime, rel) in enumerate(members):
            if i < face.keep_n:
                kept_top.append(rel)
            elif now - mtime > face.ttl_hours * 3600:
                expired.append(rel)
    return {"expired": expired, "kept_top": kept_top,
            "skipped_consumer": skipped_consumer, "skipped_never": skipped_never}


def run_face(face: FileFace, execute: bool = False) -> dict:
    """run：dry-run 默认；execute=True 时 先归档后删原（move 语义）+ 归档目录 keep-N。"""
    if face.tier == "hot":
        return {"face": face.name, "error": "hot tier 拒绝自动清理（契约§一）"}
    plan = scan_face(face)
    report = {"face": face.name, "dry": not execute,
              "would_delete": plan["expired"], "kept_top": plan["kept_top"],
              "skipped_consumer": plan["skipped_consumer"], "skipped_never": plan["skipped_never"],
              "archived": [], "deleted": [], "bytes_freed": 0,
              "reasons": {rel: "ttl_expired_not_top_n" for rel in plan["expired"]}}
    if not execute:
        report["bytes_would_free"] = sum(
            (face.root / r).stat().st_size for r in plan["expired"] if (face.root / r).exists())
        return report
    archive = face.archive_dir or (face.root / "_archive")
    archive.mkdir(parents=True, exist_ok=True)
    for rel in plan["expired"]:
        src = face.root / rel
        if not src.exists():
            continue
        dst = archive / f"{int(time.time() * 1000)}_{Path(rel).name}"
        try:
            shutil.move(str(src), str(dst))
        except OSError as e:
            report.setdefault("errors", []).append(f"{rel}: {e}")
            continue
        report["archived"].append(rel)
        report["deleted"].append(rel)
        report["bytes_freed"] += dst.stat().st_size
    _prune_archive(archive, face.archive_keep_n, report)
    return report


def _prune_archive(archive: Path, keep_n: int, report: dict) -> None:
    arch = sorted(((p.stat().st_mtime, p) for p in archive.iterdir() if p.is_file()), reverse=True)
    for _, p in arch[max(keep_n, 0):]:
        try:
            p.unlink()
            report.setdefault("archive_pruned", []).append(p.name)
        except OSError:
            pass


def quota_check(face: FileFace) -> dict:
    """quota：水位观测（信号不是刀），超限只报不删。"""
    total = sum(p.stat().st_size for p in face.root.rglob("*") if p.is_file() and not str(p.relative_to(face.root)).startswith("_archive"))
    warn = face.quota_warn_mb > 0 and total > face.quota_warn_mb * 1e6
    return {"face": face.name, "bytes": total, "quota_warn_mb": face.quota_warn_mb,
            "warn": warn, "message": "quota_exceeded_signal_only" if warn else "ok"}


def render_log(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))
