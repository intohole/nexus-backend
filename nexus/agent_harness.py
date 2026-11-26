from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ironman.agents.collaboration import CollabSession
from ironman.agents.react_agent import ReActAgent
from ironman.agents.sub_agent import SubAgent, SubAgentSpec
from ironman.agents.sub_agent_team import SubAgentTeam
from ironman.agents.super_agent import SuperAgent
from ironman.context_assembly import ContextAssembler
from ironman.memory.buffer_memory import BufferMemory
from ironman.memory.compaction import CompactionStrategy
from ironman.memory.layers import LayeredMemory, MemoryItem, MemoryScope
from ironman.memory.summarizer import StructuredSummarizer
from ironman.memory.writeback import MemoryTurn, MemoryTurnKind, MemoryWriteReport
from ironman.prompt.instructions import InstructionBundle, InstructionFile, load_instruction_chain
from ironman.prompt.lifecycle import PromptLifecycle
from ironman.prompt.sections import PromptAssembler, PromptSection
from ironman.skills.evolution import SkillEvolutionReview
from ironman.skills.source import SkillCatalog
from ironman.tools.registry import ToolRegistry
from ironman.types import AgentConfig

from nexus.bee_memory import BeeMemoryBackend
from nexus.harness_capabilities import HarnessCapabilities
from nexus.skill_source import NexusSkillSource
from nexus.skills import SkillDirSpec, SkillRegistry


@dataclass
class HarnessProfile:
    app_name: str
    model: str | None = None
    max_context_tokens: int = 32000
    reserve_tokens: int = 512
    keep_recent_tokens: int = 0
    max_tool_result_chars: int | None = None
    max_prompt_chars: int = 16000
    memory_tokens: int = 8000
    episode_limit: int = 50
    instruction_dirs: tuple[str, ...] = ()
    instruction_filenames: tuple[str, ...] = ("AGENTS.md",)
    max_instruction_bytes: int = 32768
    skill_dirs: tuple[str, ...] = ()
    sections: tuple[PromptSection, ...] = ()
    subagents: tuple[SubAgentSpec, ...] = ()
    max_parallel_subagents: int = 0
    memory_scope: MemoryScope = MemoryScope.SESSION
    tools: ToolRegistry = field(default_factory=ToolRegistry)


class AgentHarness(HarnessCapabilities):
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
        collab: CollabSession,
        memory_backend: BeeMemoryBackend | None = None,
        evolution: SkillEvolutionReview | None = None,
        lifecycle: PromptLifecycle | None = None,
    ) -> None:
        super().__init__(
            profile=profile,
            prompt_assembler=prompt_assembler,
            memory=memory,
            catalog=catalog,
            instructions=instructions,
            memory_backend=memory_backend,
            evolution=evolution,
            lifecycle=lifecycle,
        )
        self._context_assembler = context_assembler
        self._registry = registry
        self._tools = tools
        self._collab = collab
        self._team = self._build_team()

    @property
    def profile(self) -> HarnessProfile:
        return self._profile

    @property
    def memory(self) -> LayeredMemory:
        return self._memory

    @property
    def skills(self) -> SkillCatalog:
        return self._catalog

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    @property
    def instructions(self) -> InstructionBundle:
        return self._instructions

    @property
    def context_assembler(self) -> ContextAssembler:
        return self._context_assembler

    @property
    def collab(self) -> CollabSession:
        return self._collab

    @property
    def memory_backend(self) -> BeeMemoryBackend | None:
        return self._memory_backend

    @property
    def team(self) -> SubAgentTeam:
        return self._team

    @property
    def lifecycle(self) -> PromptLifecycle:
        return self._lifecycle

    def spawn_react(
        self,
        name: str = "agent",
        system_prompt: str | None = None,
        tools: ToolRegistry | None = None,
        max_steps: int = 10,
        extra_sections: Sequence[PromptSection] = (),
    ) -> ReActAgent:
        config = AgentConfig(
            model=self._profile.model,
            system_prompt=system_prompt if system_prompt is not None else self.build_system_prompt(extra_sections),
            max_steps=max_steps,
        )
        return ReActAgent(
            name=name,
            config=config,
            memory=self._memory,
            tools=(tools or self._tools).copy(),
            steering=self._collab.steering,
        )

    def spawn_super(
        self,
        name: str = "super_agent",
        system_prompt: str | None = None,
        extra_sections: Sequence[PromptSection] = (),
    ) -> SuperAgent:
        config = AgentConfig(
            model=self._profile.model,
            system_prompt=system_prompt if system_prompt is not None else self.build_system_prompt(extra_sections),
        )
        workers = {worker_name: member for worker_name, member in self._team.members.items()}
        return SuperAgent(
            name=name,
            config=config,
            memory=self._memory,
            tools=self._tools.copy(),
            workers=workers or None,
        )

    async def remember(
        self,
        content: str,
        scope: MemoryScope | None = None,
        key: str = "",
        importance: float = 0.5,
    ) -> MemoryItem:
        item = await self._memory.remember(
            content,
            scope=scope or self._profile.memory_scope,
            key=key,
            importance=importance,
        )
        if self._memory_backend is not None:
            await self._memory_backend.add(content, scope=item.scope)
        return item

    async def remember_turn(
        self,
        user_text: str,
        assistant_text: str,
        session_id: str = "",
        turn_index: int = 0,
        kind: MemoryTurnKind = "turn_complete",
    ) -> MemoryWriteReport | None:
        if self._memory_backend is None:
            return None
        turn = MemoryTurn(
            session_id=session_id,
            turn_index=turn_index,
            user_text=user_text,
            assistant_text=assistant_text,
            app_name=self._profile.app_name,
            kind=kind,
        )
        return await self._memory_backend.remember_turn(turn)

    async def recall(self, query: str, scope: MemoryScope | None = None, limit: int = 5) -> list[MemoryItem]:
        return await self._memory.recall(query, scope=scope, limit=limit)

    async def close(self) -> None:
        await self._collab.close()

    def _build_team(self) -> SubAgentTeam:
        members: list[SubAgent] = []
        for spec in self._profile.subagents:
            tools = self._tools.subset(spec.tools) if spec.tools is not None else self._tools.copy()
            inner = ReActAgent(
                name=spec.name,
                config=AgentConfig(
                    model=spec.model or self._profile.model,
                    system_prompt=spec.system_prompt if spec.system_prompt is not None else self.build_system_prompt(),
                    max_steps=spec.max_steps or 10,
                ),
                tools=tools,
            )
            members.append(SubAgent(name=spec.name, description=spec.description, agent=inner, spec=spec))
        return SubAgentTeam(members, collab=self._collab, max_parallel=self._profile.max_parallel_subagents)


def _load_instructions(profile: HarnessProfile) -> InstructionBundle:
    files: list[InstructionFile] = []
    skipped: list[tuple[str, str]] = []
    texts: list[str] = []
    seen: set[str] = set()
    for directory in profile.instruction_dirs:
        bundle = load_instruction_chain(
            directory,
            filenames=profile.instruction_filenames,
            max_bytes_per_file=profile.max_instruction_bytes,
        )
        for instruction_file in bundle.files:
            if instruction_file.path in seen:
                continue
            seen.add(instruction_file.path)
            files.append(instruction_file)
            texts.append(instruction_file.text)
        skipped.extend(bundle.skipped)
    return InstructionBundle(files=files, skipped=skipped, text="\n\n".join(texts))


def build_agent_harness(
    profile: HarnessProfile,
    registry: SkillRegistry | None = None,
    memory_backend: BeeMemoryBackend | None = None,
    summarizer: StructuredSummarizer | None = None,
    evolution: SkillEvolutionReview | None = None,
    lifecycle: PromptLifecycle | None = None,
) -> AgentHarness:
    skill_registry = registry or SkillRegistry(
        [SkillDirSpec(path=directory, source=profile.app_name) for directory in profile.skill_dirs]
    )
    if registry is None and profile.skill_dirs:
        skill_registry.reload()
    catalog = SkillCatalog([NexusSkillSource(skill_registry, source=profile.app_name)])
    instructions = _load_instructions(profile)
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
        instructions=instructions,
        tools=profile.tools,
        collab=collab,
        memory_backend=memory_backend,
        evolution=evolution,
        lifecycle=lifecycle,
    )
