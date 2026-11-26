from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ironman.memory.layers import LayeredMemory, MemoryItem, MemoryScope
from ironman.prompt.instructions import InstructionBundle
from ironman.prompt.lifecycle import PromptLifecycle
from ironman.prompt.sections import PromptAssembler, PromptSection
from ironman.skills.evolution import (
    EvolutionTurn,
    SkillEvolutionReport,
    SkillEvolutionReview,
)
from ironman.skills.source import SkillCatalog

from nexus.bee_memory import BeeMemoryBackend
from nexus.harness_working import (
    WorkingContext,
)
from nexus.harness_working import (
    read_working_context as _read_working_context,
)
from nexus.harness_working import (
    restore_working_memory as _restore_working_memory,
)
from nexus.memory import render_memory_context_bounded
from nexus.memory_contract import MemoryBudget

if TYPE_CHECKING:
    from nexus.harness_profile import HarnessProfile


class HarnessCapabilities:
    def __init__(
        self,
        profile: HarnessProfile,
        prompt_assembler: PromptAssembler,
        memory: LayeredMemory,
        catalog: SkillCatalog,
        instructions: InstructionBundle,
        memory_backend: BeeMemoryBackend | None = None,
        evolution: SkillEvolutionReview | None = None,
        lifecycle: PromptLifecycle | None = None,
    ) -> None:
        self._profile = profile
        self._prompt_assembler = prompt_assembler
        self._memory = memory
        self._catalog = catalog
        self._instructions = instructions
        self._memory_backend = memory_backend
        self._evolution = evolution
        self._lifecycle = lifecycle or PromptLifecycle()

    async def read_working_context(
        self,
        history_turns: int = 12,
        top_k: int = 10,
        max_chars: int = 1200,
        history_max_chars: int | None = None,
        budget: MemoryBudget | None = None,
    ) -> WorkingContext:
        return await _read_working_context(
            self._memory_backend,
            self._profile.app_name,
            history_turns=history_turns,
            top_k=top_k,
            max_chars=max_chars,
            history_max_chars=history_max_chars,
            budget=budget,
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

    def _collect_sections(
        self,
        extra_sections: Sequence[PromptSection] = (),
        memory_payload: dict[str, object] | None = None,
        memory_query: str = "",
        max_memory_chars: int = 1200,
    ) -> list[PromptSection]:
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
        return sections

    def build_system_prompt(
        self,
        extra_sections: Sequence[PromptSection] = (),
        memory_payload: dict[str, object] | None = None,
        memory_query: str = "",
        max_memory_chars: int = 1200,
    ) -> str:
        sections = self._collect_sections(extra_sections, memory_payload, memory_query, max_memory_chars)
        return self._prompt_assembler.assemble(sections).text

    async def build_system_prompt_with_memory(
        self,
        extra_sections: Sequence[PromptSection] = (),
        query: str = "",
        top_k: int = 10,
        max_memory_chars: int = 1200,
        budget: MemoryBudget | None = None,
    ) -> str:
        if self._memory_backend is not None:
            payload = await self._memory_backend.snapshot(
                top_k=top_k, max_chars=max_memory_chars, budget=budget
            )
            return self.build_system_prompt(
                extra_sections=extra_sections,
                memory_payload=payload,
                max_memory_chars=max_memory_chars,
            )
        return self.build_system_prompt(
            extra_sections=extra_sections,
            memory_query=query,
            max_memory_chars=max_memory_chars,
        )

    def build_system_prompt_with_notices(
        self,
        session_id: str,
        extra_sections: Sequence[PromptSection] = (),
        memory_payload: dict[str, object] | None = None,
        memory_query: str = "",
        max_memory_chars: int = 1200,
        version: int = 0,
    ) -> str:
        sections = self._collect_sections(extra_sections, memory_payload, memory_query, max_memory_chars)
        notices = self._lifecycle.observe(session_id, sections, version=version)
        notice_text = self._lifecycle.render(notices)
        if notice_text:
            sections.append(
                PromptSection(name="prompt_notices", content=notice_text, order=90, priority=95)
            )
        return self._prompt_assembler.assemble(sections).text

    async def build_system_prompt_with_working_context(
        self,
        extra_sections: Sequence[PromptSection] = (),
        history_turns: int = 12,
        top_k: int = 10,
        max_memory_chars: int = 1200,
        history_max_chars: int | None = None,
        session_id: str | None = None,
        version: int = 0,
        budget: MemoryBudget | None = None,
    ) -> str:
        context = await self.read_working_context(
            history_turns=history_turns,
            top_k=top_k,
            max_chars=max_memory_chars,
            history_max_chars=history_max_chars,
            budget=budget,
        )
        sections = list(extra_sections)
        if context.text:
            sections.append(
                PromptSection(name="working_context", content=context.text, order=65, priority=55)
            )
        if session_id is not None:
            return self.build_system_prompt_with_notices(
                session_id=session_id,
                extra_sections=sections,
                max_memory_chars=max_memory_chars,
                version=version,
            )
        return self.build_system_prompt(extra_sections=sections, max_memory_chars=max_memory_chars)

    async def review_skill_evolution(
        self,
        user_text: str,
        assistant_text: str,
        session_id: str = "",
        turn_index: int = 0,
    ) -> SkillEvolutionReport | None:
        if self._evolution is None:
            return None
        turn = EvolutionTurn(
            session_id=session_id,
            turn_index=turn_index,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        return await self._evolution.review(turn)

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


__all__ = ["HarnessCapabilities"]
