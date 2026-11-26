"""AgentHarness：r260 B1 薄壳化——spawn/team/记忆读写面上收 ironman.kernel.KernelHarness。

nexus 侧只留四缝：①working 上下文读写（接 nexus.harness_working+bee payload 方言）
②payload 渲染缝（render_memory_context_bounded，围栏镜像契约 7 nexus 侧守底）
③B5 旗读缝（memory_injection_enabled 接 nexus feature_flags；旗关=素版逐字节零漂移）
④executor 网关缺省（gateway_executor 三道门）。金样等价不变式=tests/test_r260_kernel_equivalence.py。
"""
from __future__ import annotations

from typing import Sequence

from ironman.context_assembly import ContextAssembler
from ironman.kernel.capabilities import KernelHarness
from ironman.memory.layers import LayeredMemory, MemoryItem, MemoryScope
from ironman.memory.summarizer import StructuredSummarizer
from ironman.prompt.instructions import InstructionBundle
from ironman.prompt.lifecycle import PromptLifecycle
from ironman.prompt.sections import PromptAssembler, PromptSection
from ironman.skills.evolution import SkillEvolutionReview
from ironman.skills.source import SkillCatalog
from ironman.tools.registry import ToolRegistry

from nexus.bee_memory import BeeMemoryBackend
from nexus.harness_llm import LLMExecutor, gateway_executor
from nexus.harness_profile import HarnessProfile
from nexus.harness_working import (
    read_working_context as _read_working_context,
)
from nexus.harness_working import (
    restore_working_memory as _restore_working_memory,
)
from nexus.memory import render_memory_context_bounded
from nexus.memory_contract import MemoryBudget
from nexus.skill_source import NexusSkillSource
from nexus.skills import SkillDirSpec, SkillRegistry


class AgentHarness(KernelHarness):
    def __init__(
        self,
        profile: HarnessProfile,
        prompt_assembler: PromptAssembler,
        context_assembler: ContextAssembler,
        memory: LayeredMemory,
        catalog: SkillCatalog,
        registry: SkillRegistry,
        instructions: InstructionBundle,
        tools: ToolRegistry,
        collab,
        memory_backend: BeeMemoryBackend | None = None,
        evolution: SkillEvolutionReview | None = None,
        lifecycle: PromptLifecycle | None = None,
        llm_executor: LLMExecutor | None = None,
    ) -> None:
        super().__init__(
            profile=profile,
            prompt_assembler=prompt_assembler,
            context_assembler=context_assembler,
            memory=memory,
            catalog=catalog,
            instructions=instructions,
            tools=tools,
            collab=collab,
            skill_registry=registry,
            memory_backend=memory_backend,
            evolution=evolution,
            lifecycle=lifecycle,
            llm_executor=llm_executor,
            memory_payload_renderer=render_memory_context_bounded,
        )
        self._nexus_registry = registry

    @property
    def profile(self) -> HarnessProfile:
        return self._profile  # type: ignore[return-value]

    @property
    def registry(self) -> SkillRegistry:
        return self._nexus_registry

    @property
    def memory_backend(self) -> BeeMemoryBackend | None:
        return self._memory_backend  # type: ignore[return-value]

    def resolve_llm_executor(self) -> LLMExecutor:
        if self._llm_executor is not None:
            return self._llm_executor
        return gateway_executor(self._profile.app_name)

    def memory_injection_enabled(self) -> bool:
        from nexus.feature_flags import get_feature_flags

        return get_feature_flags().harness_memory_injection

    # ---- working 上下文缝（nexus.harness_working 接线，kernel 缺省空实现的覆写）----

    async def read_working_context(
        self,
        history_turns: int = 12,
        top_k: int = 10,
        max_chars: int = 1200,
        history_max_chars: int | None = None,
        budget: MemoryBudget | None = None,
    ):
        from ironman.kernel.ports import KernelWorkingContext

        context = await _read_working_context(
            self._memory_backend,
            self._profile.app_name,
            history_turns=history_turns,
            top_k=top_k,
            max_chars=max_chars,
            history_max_chars=history_max_chars,
            budget=budget,
        )
        return KernelWorkingContext(
            history=context.history,
            text=context.text,
            history_count=context.history_count,
            truncated=context.truncated,
        )

    async def restore_working_memory(
        self,
        history_turns: int = 12,
        top_k: int = 10,
        max_chars: int = 1200,
        history_max_chars: int | None = None,
        budget: MemoryBudget | None = None,
    ) -> int:
        return await _restore_working_memory(
            self._memory,
            self._memory_backend,
            self._profile.app_name,
            history_turns=history_turns,
            top_k=top_k,
            max_chars=max_chars,
            history_max_chars=history_max_chars,
            budget=budget,
        )

    # ---- 显式类型面（kernel 侧鸭子协议的 nexus 签名投影）----

    async def remember_image(
        self,
        image: str | bytes,
        prompt: str = "",
        channel: str = "image",
        session_id: str = "",
        budget: MemoryBudget | None = None,
    ) -> dict[str, object] | None:
        if self._memory_backend is None:
            return None
        return await self._memory_backend.add_image(
            image, prompt=prompt, channel=channel, session_id=session_id, budget=budget
        )

    async def recall_image(
        self,
        image: str | bytes,
        prompt: str = "",
        limit: int = 5,
        scope: MemoryScope | None = None,
        budget: MemoryBudget | None = None,
    ) -> list[MemoryItem]:
        if self._memory_backend is None:
            return []
        return await self._memory_backend.recall_image(
            image, prompt=prompt, limit=limit, scope=scope, budget=budget
        )

    # ---- spawn 兼容缝（r259 形态：旗读缝在 nexus，kernel 收显式参数）----

    async def spawn_react(
        self,
        name: str = "agent",
        system_prompt: str | None = None,
        tools: ToolRegistry | None = None,
        max_steps: int = 10,
        extra_sections: Sequence[PromptSection] = (),
    ):
        return await super().spawn_react(
            name=name,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=max_steps,
            extra_sections=extra_sections,
            inject_memory=self.memory_injection_enabled(),
        )

    async def spawn_super(
        self,
        name: str = "super_agent",
        system_prompt: str | None = None,
        extra_sections: Sequence[PromptSection] = (),
    ):
        return await super().spawn_super(
            name=name,
            system_prompt=system_prompt,
            extra_sections=extra_sections,
            inject_memory=self.memory_injection_enabled(),
        )

    async def _build_spawn_prompt(self, extra_sections: Sequence[PromptSection] = ()) -> str:
        """兼容缝（r259 形态）：=_resolve_spawn_prompt 的旗读包装。"""
        return await self._resolve_spawn_prompt(
            None, extra_sections, inject_memory=self.memory_injection_enabled()
        )


def build_agent_harness(
    profile: HarnessProfile,
    registry: SkillRegistry | None = None,
    memory_backend: BeeMemoryBackend | None = None,
    summarizer: StructuredSummarizer | None = None,
    evolution: SkillEvolutionReview | None = None,
    lifecycle: PromptLifecycle | None = None,
    llm_executor: LLMExecutor | None = None,
) -> AgentHarness:
    """薄壳组装：nexus 业务件（SkillRegistry/catalog）+kernel 基座件（构造序与 r259 逐行同形）。"""
    skill_registry = registry or SkillRegistry(
        [SkillDirSpec(path=directory, source=profile.app_name) for directory in profile.skill_dirs]
    )
    if registry is None and profile.skill_dirs:
        skill_registry.reload()
    catalog = SkillCatalog([NexusSkillSource(skill_registry, source=profile.app_name)])
    from ironman.agents.collaboration import CollabSession
    from ironman.kernel.builder import load_instruction_bundle
    from ironman.memory.buffer_memory import BufferMemory
    from ironman.memory.compaction import CompactionStrategy

    working = BufferMemory(
        max_tokens=profile.memory_tokens,
        strategy=CompactionStrategy.SUMMARY if summarizer else CompactionStrategy.SLIDING_WINDOW,
        summarizer=summarizer,
        keep_recent_tokens=profile.keep_recent_tokens,
    )
    memory = LayeredMemory(
        working=working,
        max_tokens=profile.memory_tokens,
        episode_limit=profile.episode_limit,
        retriever=memory_backend,
    )
    collab = CollabSession()
    return AgentHarness(
        profile=profile,
        prompt_assembler=PromptAssembler(max_chars=profile.max_prompt_chars),
        context_assembler=ContextAssembler(
            max_tokens=profile.max_context_tokens,
            reserve_tokens=profile.reserve_tokens,
            keep_recent_tokens=profile.keep_recent_tokens,
            max_tool_result_chars=profile.max_tool_result_chars,
        ),
        memory=memory,
        catalog=catalog,
        registry=skill_registry,
        instructions=load_instruction_bundle(profile),
        tools=profile.tools,
        collab=collab,
        memory_backend=memory_backend,
        evolution=evolution,
        lifecycle=lifecycle,
        llm_executor=llm_executor,
    )
