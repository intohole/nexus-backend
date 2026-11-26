from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ironman.agents.collaboration import CollabSession
from ironman.agents.react_agent import ReActAgent
from ironman.agents.sub_agent import SubAgent, SubAgentSpec, SubAgentTeam
from ironman.agents.super_agent import SuperAgent
from ironman.context_assembly import ContextAssembler
from ironman.memory.layers import LayeredMemory, MemoryItem, MemoryScope
from ironman.prompt.instructions import InstructionBundle, InstructionFile, load_instruction_chain
from ironman.prompt.sections import PromptAssembler, PromptSection
from ironman.skills.source import SkillCatalog
from ironman.tools.registry import ToolRegistry
from ironman.types import AgentConfig
from nexus.bee_memory import BeeMemoryBackend
from nexus.memory import render_memory_context_bounded
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
    memory_scope: MemoryScope = MemoryScope.SESSION
    tools: ToolRegistry = field(default_factory=ToolRegistry)


class AgentHarness:
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
    ) -> None:
        self._profile = profile
        self._prompt_assembler = prompt_assembler
        self._context_assembler = context_assembler
        self._memory = memory
        self._catalog = catalog
        self._registry = registry
        self._instructions = instructions
        self._tools = tools
        self._collab = collab
        self._memory_backend = memory_backend
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

    def build_system_prompt(
        self,
        extra_sections: Sequence[PromptSection] = (),
        memory_payload: dict[str, object] | None = None,
        memory_query: str = "",
        max_memory_chars: int = 1200,
    ) -> str:
        sections = list(self._profile.sections) + list(extra_sections)
        if self._instructions.text:
            sections.append(
                PromptSection(
                    name="project_instructions",
                    content=self._instructions.text,
                    order=40,
                    priority=80,
                )
            )
        catalog_text = self._catalog.render_catalog()
        if catalog_text:
            sections.append(PromptSection(name="skills", content=catalog_text, order=50, priority=70))
        memory_text = ""
        if memory_payload is not None:
            memory_text, _readout = render_memory_context_bounded(
                memory_payload,
                self_app=self._profile.app_name,
                budget_chars=max_memory_chars,
            )
        elif memory_query:
            memory_text = self._memory.render(memory_query, budget_chars=max_memory_chars)
        if memory_text:
            sections.append(PromptSection(name="memory", content=memory_text, order=60, priority=60))
        return self._prompt_assembler.assemble(sections).text

    async def build_system_prompt_with_memory(
        self,
        extra_sections: Sequence[PromptSection] = (),
        query: str = "",
        top_k: int = 10,
        max_memory_chars: int = 1200,
    ) -> str:
        if self._memory_backend is not None:
            payload = await self._memory_backend.snapshot(top_k=top_k, max_chars=max_memory_chars)
            return self.build_system_prompt(
                extra_sections=extra_sections,
                memory_payload=payload,
                max_memory_chars=max_memory_chars,
            )
        return self.build_system_prompt(extra_sections=extra_sections, memory_query=query, max_memory_chars=max_memory_chars)

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
        return SubAgentTeam(members, collab=self._collab)


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
) -> AgentHarness:
    skill_registry = registry or SkillRegistry(
        [SkillDirSpec(path=directory, source=profile.app_name) for directory in profile.skill_dirs]
    )
    if registry is None and profile.skill_dirs:
        skill_registry.reload()
    catalog = SkillCatalog([NexusSkillSource(skill_registry, source=profile.app_name)])
    instructions = _load_instructions(profile)
    memory = LayeredMemory(
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
    )
