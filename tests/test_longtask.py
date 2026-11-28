"""长任务检查点原语三契约（S211 LR2）：journal 崩溃安全 / gate 排空与可用性 / 恢复编排。

崩溃安全 prove-red 用子进程真写半行后 os._exit(1) 模拟 kill -9——
父进程读 journal 必须跳过残行、续写记录必须落在新行上（修复 arcade
原实现「崩溃后首条记录与残行粘连被整体吞掉」的潜伏丢失）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.longtask import RuntimeGate, TaskJournal, recover_interrupted

_NEXUS_ROOT = str(Path(__file__).resolve().parent.parent)

_CRASH_CHILD = """
import os, sys
sys.path.insert(0, sys.argv[1])
from nexus.longtask import TaskJournal
j = TaskJournal(sys.argv[2])
j.record(1, "start", instruction="a", images=[])
j.record(1, "stage", name="prepare")
fd = os.open(j._path(1), os.O_APPEND | os.O_WRONLY)
os.write(fd, b'{"seq":3,"event":"stage","na')
os._exit(1)
"""


def _crash_child(root: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, _NEXUS_ROOT, str(root)],
        capture_output=True, timeout=30,
    )
    assert proc.returncode == 1, proc.stderr.decode()


def test_record_roundtrip(tmp_path: Path) -> None:
    j = TaskJournal(tmp_path / "j")
    j.record(7, "start", instruction="x", images=["a"])
    j.record(7, "stage", name="prepare")
    rows = j.events(7)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["instruction"] == "x"
    assert rows[0]["event"] == "start" and "ts" in rows[0]
    assert rows[1]["name"] == "prepare"


def test_crash_truncated_line_skipped_and_next_record_survives(tmp_path: Path) -> None:
    root = tmp_path / "j"
    _crash_child(root)
    j = TaskJournal(root)
    assert len(j.events(1)) == 2
    j.record(1, "resume", resume_no=1)
    rows = j.events(1)
    assert len(rows) == 3
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert rows[2]["event"] == "resume"


def test_resume_payload_counts_and_fields(tmp_path: Path) -> None:
    j = TaskJournal(tmp_path / "j")
    assert j.resume_payload(3) is None
    j.record(3, "stage", name="early")
    assert j.resume_payload(3) is None
    j.record(3, "start", instruction="t", images=["p"], target_status="ready")
    j.record(3, "resume", resume_no=1)
    j.record(3, "resume", resume_no=2)
    payload = j.resume_payload(3)
    assert payload is not None
    assert payload["instruction"] == "t"
    assert payload["images"] == ["p"]
    assert payload["resume_count"] == 2


def test_clear_archives_settled_unlinks_unsettled(tmp_path: Path) -> None:
    j = TaskJournal(tmp_path / "j")
    j.record(1, "start", instruction="a")
    j.clear(1)
    assert not (tmp_path / "j" / "1.jsonl").exists()
    assert j.events(1) == []
    j.record(2, "start", instruction="b")
    j.record(2, "settle", status="ready")
    j.clear(2)
    assert not (tmp_path / "j" / "2.jsonl").exists()
    archived = j.events(2)
    assert len(archived) == 2
    assert list((tmp_path / "j" / "archive").glob("2_*.jsonl"))


def test_sweep_orphans(tmp_path: Path) -> None:
    j = TaskJournal(tmp_path / "j")
    j.record(1, "start", instruction="a")
    j.record(2, "start", instruction="b")
    swept = j.sweep({2})
    assert swept == 1
    assert j.events(2) and j.events(1, include_archive=False) == []


def test_gate_draining_and_llm_states() -> None:
    gate = RuntimeGate(availability=lambda: False, inflight=lambda: 0)
    assert not gate.is_draining()
    assert not gate.llm_blocked()
    gate.mark_llm_checked()
    assert gate.llm_blocked()
    gate.mark_draining()
    assert gate.is_draining()
    gate.reset()
    assert not gate.is_draining() and not gate.llm_blocked()
    ok = RuntimeGate(availability=lambda: True, inflight=lambda: 0)
    ok.mark_llm_checked()
    assert not ok.llm_blocked()


def test_gate_wait_idle(tmp_path: Path) -> None:
    state = {"n": 1}
    gate = RuntimeGate(inflight=lambda: state["n"])
    async def drain_soon() -> None:
        await gate.wait_idle(0.05)
        state["n"] = 0
    import asyncio
    asyncio.run(drain_soon())
    assert gate.inflight() == 0
    stuck = RuntimeGate(inflight=lambda: 2)
    assert asyncio.run(stuck.wait_idle(0.1)) == 2


def _recorder(failed: list) -> object:
    async def _fail(task_id: object, payload: object, case: str) -> None:
        failed.append((task_id, case))
    return _fail


def _simple_journal(tmp_path: Path) -> TaskJournal:
    return TaskJournal(tmp_path / "j")


@pytest.mark.asyncio
async def test_recover_resumes_active(tmp_path: Path) -> None:
    j = _simple_journal(tmp_path)
    j.record(1, "start", instruction="a", target_status="ready")
    resumed_list: list[tuple[object, dict]] = []
    failed: list[tuple[object, str]] = []
    resumed, settled = await recover_interrupted(
        j, [(1, "generating")],
        max_resumes=3,
        is_resumable=lambda status, payload: bool(payload),
        needs_settle=lambda status: True,
        on_fail=_recorder(failed),
        on_resume=lambda tid, payload: resumed_list.append((tid, payload)),
    )
    assert (resumed, settled) == (1, 0)
    assert resumed_list[0][0] == 1
    assert resumed_list[0][1]["instruction"] == "a"
    assert j.events(1)[-1]["event"] == "resume"


@pytest.mark.asyncio
async def test_recover_settles_orphan_and_cap(tmp_path: Path) -> None:
    j = _simple_journal(tmp_path)
    failed: list[tuple[object, str]] = []
    j.record(1, "start", instruction="a")
    for i in range(3):
        j.record(1, "resume", resume_no=i + 1)
    j.record(2, "start", instruction="b")
    j.record(9, "start", instruction="ghost")
    resumed, settled = await recover_interrupted(
        j, [(1, "generating"), (2, "generating")],
        max_resumes=3,
        is_resumable=lambda status, payload: bool(payload),
        needs_settle=lambda status: True,
        on_fail=_recorder(failed),
        on_resume=lambda tid, payload: None,
    )
    assert (resumed, settled) == (1, 1)
    assert failed == [(1, "cap")]
    assert j.events(1, include_archive=False) == []
    assert j.events(2)[-1]["event"] == "resume"
    assert j.events(9, include_archive=False) == []


@pytest.mark.asyncio
async def test_recover_inactive_cleared_without_settle(tmp_path: Path) -> None:
    j = _simple_journal(tmp_path)
    j.record(5, "start", instruction="a")
    failed: list[object] = []
    resumed, settled = await recover_interrupted(
        j, [(5, "confirmed")],
        max_resumes=3,
        is_resumable=lambda status, payload: status == "generating" and bool(payload),
        needs_settle=lambda status: status == "generating",
        on_fail=_recorder(failed),
        on_resume=lambda tid, payload: None,
    )
    assert (resumed, settled) == (0, 0)
    assert not failed
    assert j.events(5, include_archive=False) == []


@pytest.mark.asyncio
async def test_recover_empty_rows_skips_orphan_sweep(tmp_path: Path) -> None:
    """r272 刀22（狩猎A P2）：枚举瞬态空集不得清空全部孤儿 journal。

    rows 为空（DB 枚举瞬态失败/返回空列表）时 sweep(seen=∅) 会把全部数字键
    journal 当孤儿 unlink——崩溃任务的唯一续跑载荷蒸发、任务行与 journal 脱钩。
    钉死：空枚举跳过 sweep 并 warning fail-visible；有枚举行时 sweep 照常。"""
    j = TaskJournal(tmp_path / "j")
    j.record(7, "start", instruction="a")

    async def _fail(task_id, payload, case):  # noqa: ANN001
        return None

    resumed, settled = await recover_interrupted(
        j,
        [],
        max_resumes=3,
        is_resumable=lambda status, payload: True,
        needs_settle=lambda status: False,
        on_fail=_fail,
        on_resume=lambda task_id, payload: None,
    )
    assert (resumed, settled) == (0, 0)
    assert j.events(7), "空枚举清掉了在役任务 journal（续跑载荷蒸发）"

    j.record(8, "start", instruction="b")
    resumed, settled = await recover_interrupted(
        j,
        [(8, "pending")],
        max_resumes=3,
        is_resumable=lambda status, payload: True,
        needs_settle=lambda status: False,
        on_fail=_fail,
        on_resume=lambda task_id, payload: None,
    )
    assert resumed == 1
    swept = j.sweep(set())
    assert swept >= 1
