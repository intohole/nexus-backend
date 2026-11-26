"""r260 B1 薄壳化等价不变式（contracts.md §5 验收门，r256 三方不变式同款判例）。

四面对表：
- 金样等价：build_agent_harness 装配输出 == 上收前存档（tests/fixtures/kernel_golden_r260.json，
  逐字节）
- kernel 自足：ironman.kernel.build_kernel_harness 纯 ironman 件可独立装配出同构 prompt
- B5 旗关零漂移：spawn prompt == 素版 build_system_prompt；旗开+backend 在位=围栏注入+census 计数
- spawn/team 面：薄壳签名与 r259 async 形态一致（spawn 系 0.21.0 BREAKING 已登记）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from ironman.kernel import KernelProfile, build_kernel_harness
from ironman.skills.source import SkillCatalog
from ironman.types import LLMRequest, LLMResponse, TokenUsage

from nexus.agent_harness import HarnessProfile, build_agent_harness

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "kernel_golden_r260.json").read_text(encoding="utf-8")
)


def _write_skill(base: Path, dirname: str, name: str, description: str, body: str) -> Path:
    skill_dir = base / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def _golden_profile(base: Path) -> HarnessProfile:
    _write_skill(base, "skills/deploy", "deploy", "部署灰度与回滚", "先灰度后全量")
    project = base / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "AGENTS.md").write_text("项目规则：先测试后提交", encoding="utf-8")
    return HarnessProfile(
        app_name="demo",
        model="test-model",
        skill_dirs=(str(base / "skills"),),
        instruction_dirs=(str(project),),
    )


@pytest.mark.asyncio
async def test_golden_equivalence_after_kernel_uplift(tmp_path: Path):
    harness = build_agent_harness(_golden_profile(tmp_path))
    assert harness.build_system_prompt() == GOLDEN["system_prompt"]
    assert await harness._build_spawn_prompt() == GOLDEN["spawn_prompt"]
    assert (
        harness.build_system_prompt(memory_query="部署怎么做", max_memory_chars=200)
        == GOLDEN["memory_query_prompt"]
    )
    config = harness._agent_config("sysx")
    assert config.system_prompt == GOLDEN["config_system"]
    assert config.model == GOLDEN["config_model"]


@pytest.mark.asyncio
async def test_kernel_self_contained_without_nexus(tmp_path: Path):
    """kernel 纯 ironman 件自足装配（不含 nexus skills/bee）产出同构 section 序。"""
    profile = KernelProfile(
        app_name="kernel_only",
        instruction_dirs=(),
        skill_dirs=(),
        sections=(),
    )
    harness = build_kernel_harness(profile, skill_catalog=SkillCatalog([]))
    text = harness.build_system_prompt()
    assert text == ""  # 空 profile+空 catalog=空 prompt（金样空壳先例）
    assert harness.memory is not None and harness.collab is not None


@pytest.mark.asyncio
async def test_b5_flag_off_byte_identical_spawn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NEXUS_HARNESS_MEMORY_INJECTION", "")
    from nexus.feature_flags import reset_feature_flags

    reset_feature_flags()
    harness = build_agent_harness(_golden_profile(tmp_path))
    spawn_prompt = await harness._build_spawn_prompt()
    assert spawn_prompt == harness.build_system_prompt()
    assert spawn_prompt == GOLDEN["spawn_prompt"]
    census = harness.memory_injection_census()
    assert census == {"spawn_memory_injections": 0, "spawn_memory_hits": 0}


class _FakeBackend:
    """MemoryQueryPort 鸭子实现（围栏注入路径验证）。"""

    def __init__(self) -> None:
        self.budget = None

    async def snapshot(self, *, top_k=10, max_chars=1200, budget=None):
        return {
            "app_name": "demo",
            "memories": [
                {"content": "用户偏好深色主题", "source_app": "", "created_at": "2026-11-26T00:00:00Z"}
            ],
            "history_window": [],
        }

    async def add(self, content, scope=None):
        return {"ok": True}


@pytest.mark.asyncio
async def test_b5_flag_on_injects_memory_and_census(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NEXUS_HARNESS_MEMORY_INJECTION", "1")
    from nexus.feature_flags import reset_feature_flags

    reset_feature_flags()
    harness = build_agent_harness(_golden_profile(tmp_path), memory_backend=_FakeBackend())
    try:
        prompt = await harness._build_spawn_prompt()
    finally:
        reset_feature_flags()
    assert "<memory_context" in prompt
    assert "用户偏好深色主题" in prompt
    census = harness.memory_injection_census()
    assert census["spawn_memory_injections"] == 1
    assert census["spawn_memory_hits"] == 1


@pytest.mark.asyncio
async def test_spawn_faces_preserve_r259_async_signatures(tmp_path: Path):
    async def _executor(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="ok",
            model="test-model",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    harness = build_agent_harness(_golden_profile(tmp_path), llm_executor=_executor)
    agent = await harness.spawn_react(name="worker", max_steps=3)
    assert agent._max_steps == 3
    assert agent._name == "worker"
