"""agent_harness 守底：技能适配/系统段装配/记忆作用域/子 agent 团队。"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.skill_source import NexusSkillSource
from nexus.skills import SkillDirSpec, SkillRegistry


def _write_skill(base: Path, dirname: str, name: str, description: str, body: str) -> Path:
    skill_dir = base / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_nexus_skill_source_lists_and_loads(tmp_path: Path):
    _write_skill(tmp_path, "deploy", "deploy", "部署灰度与回滚", "先灰度后全量")
    registry = SkillRegistry([SkillDirSpec(path=str(tmp_path), source="test")])
    registry.reload()
    source = NexusSkillSource(registry)
    metas = source.list_skills()
    assert [meta.name for meta in metas] == ["deploy"]
    assert source.load("deploy") == "先灰度后全量"
    assert source.suggest("灰度部署")[0].name == "deploy"


def test_nexus_skill_source_read_resource_rejects_traversal(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "deploy", "deploy", "部署", "正文")
    (skill_dir / "notes.md").write_text("资源", encoding="utf-8")
    registry = SkillRegistry([SkillDirSpec(path=str(tmp_path), source="test")])
    registry.reload()
    source = NexusSkillSource(registry)
    assert source.read_resource("deploy", "notes.md") == "资源"
    with pytest.raises(ValueError):
        source.read_resource("deploy", "../secret.md")


def test_build_agent_harness_assembles_system_prompt(tmp_path: Path):
    skills = tmp_path / "skills"
    _write_skill(skills, "deploy", "deploy", "部署灰度与回滚", "先灰度后全量")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("项目规则：先测试后提交", encoding="utf-8")
    profile = HarnessProfile(
        app_name="demo",
        model="test-model",
        skill_dirs=(str(skills),),
        instruction_dirs=(str(project),),
    )
    harness = build_agent_harness(profile)
    prompt = harness.build_system_prompt()
    assert "项目规则：先测试后提交" in prompt
    assert "deploy" in prompt
    assert prompt.index("project_instructions") < prompt.index("skills")


@pytest.mark.asyncio
async def test_harness_memory_scope_and_recall(tmp_path: Path):
    profile = HarnessProfile(app_name="demo")
    harness = build_agent_harness(profile)
    await harness.remember("用户偏好中文", importance=1.0)
    hits = await harness.recall("中文")
    assert [item.content for item in hits] == ["用户偏好中文"]
    prompt = harness.build_system_prompt(memory_payload={"memories": [{"content": "历史偏好中文"}]})
    assert "历史偏好中文" in prompt
    await harness.close()


def test_harness_spawns_react_and_team(tmp_path: Path):
    from ironman.agents.sub_agent import SubAgentSpec
    from ironman.tools.function_tool import FunctionTool

    async def handler() -> str:
        return "ok"

    profile = HarnessProfile(
        app_name="demo",
        model="test-model",
        subagents=(
            SubAgentSpec(name="reviewer", description="审查", tools=("lookup",)),
        ),
    )
    profile.tools.register(FunctionTool(name="lookup", description="查询", handler=handler))
    harness = build_agent_harness(profile)
    agent = harness.spawn_react(name="lead")
    assert agent.name == "lead"
    assert agent.model == "test-model"
    assert agent._system_prompt
    assert "reviewer" in harness.team.members
    assert harness.team.members["reviewer"].inner.tools.list_tools() == ["lookup"]
    assert agent._steering is harness.collab.steering


def test_nexus_top_level_reexport():
    import nexus

    assert nexus.AgentHarness is not None
    assert nexus.build_agent_harness is not None


@pytest.mark.asyncio
async def test_harness_team_end_to_end_with_executor(tmp_path: Path):
    from ironman.agents.base import BaseAgent
    from ironman.agents.sub_agent import SubAgentSpec
    from ironman.tools.function_tool import FunctionTool
    from ironman.types import LLMRequest, LLMResponse, TokenUsage

    seen_prompts: list[str] = []

    class _Executor:
        async def __call__(self, request: LLMRequest) -> LLMResponse:
            system = next((m.content for m in request.messages if m.role.value == "system"), "")
            seen_prompts.append(system if isinstance(system, str) else "")
            last = request.messages[-1].content
            return LLMResponse(
                content=str(last),
                model="test-model",
                usage=TokenUsage(prompt_tokens=3, completion_tokens=3, total_tokens=6),
            )

    async def handler() -> str:
        return "ok"

    skills = tmp_path / "skills"
    _write_skill(skills, "deploy", "deploy", "部署灰度与回滚", "先灰度后全量")
    profile = HarnessProfile(
        app_name="demo",
        model="test-model",
        skill_dirs=(str(skills),),
        subagents=(SubAgentSpec(name="reviewer", description="审查", tools=("lookup",)),),
    )
    profile.tools.register(FunctionTool(name="lookup", description="查询", handler=handler))
    harness = build_agent_harness(profile)
    BaseAgent.set_llm_executor(_Executor())
    try:
        results = await harness.team.dispatch([("reviewer", "检查部署方案")], parallel=True)
    finally:
        BaseAgent.set_llm_executor(None)
    assert results[0].success is True
    assert results[0].content == "检查部署方案"
    assert any("deploy" in prompt for prompt in seen_prompts)
    await harness.close()
