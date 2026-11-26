from __future__ import annotations

from dataclasses import dataclass, field

from ironman.agents.sub_agent import SubAgentSpec
from ironman.memory.layers import MemoryScope
from ironman.prompt.sections import PromptSection
from ironman.tools.registry import ToolRegistry


@dataclass
class HarnessProfile:
    app_name: str
    model: str | None = None
    task_type: str = ""
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


__all__ = ["HarnessProfile"]
