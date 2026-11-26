from __future__ import annotations

import logging

from ironman.memory.layers import MemoryItem, MemoryScope
from ironman.memory.writeback import (
    MemoryGate,
    MemoryTurn,
    MemoryWriteback,
    MemoryWriteReport,
)

from nexus.bee_memory_items import payload_items
from nexus.bm_sdk import BeeMemorySDK, get_bm_sdk
from nexus.memory import render_memory_context_bounded
from nexus.memory_contract import MemoryBudget
from nexus.memory_media import (
    DEFAULT_IMAGE_MEMORY_MODEL,
    MediaDescriber,
    VisionDescriber,
    compose_image_memory,
)

logger = logging.getLogger(__name__)

IMAGE_DESCRIPTION_MAX_CHARS = 2000


class BeeMemoryBackend:
    def __init__(
        self,
        user_id: str,
        app_name: str,
        sdk: BeeMemorySDK | None = None,
        session_id: str = "",
        write_gate: MemoryGate | None = None,
        min_write_chars: int = 32,
        describer: MediaDescriber | None = None,
        image_model: str = DEFAULT_IMAGE_MEMORY_MODEL,
        budget: MemoryBudget | None = None,
    ) -> None:
        self._user_id = user_id
        self._app_name = app_name
        self._sdk = sdk or get_bm_sdk(app_name=app_name)
        self._session_id = session_id
        self._describer = describer
        self._image_model = image_model
        self._budget = budget or MemoryBudget()
        self._writeback = MemoryWriteback(
            writer=self._write_turn,
            gate=write_gate,
            min_chars=min_write_chars,
        )

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def budget(self) -> MemoryBudget:
        return self._budget

    async def recall(
        self,
        query: str,
        scope: MemoryScope | None = None,
        limit: int = 5,
        budget: MemoryBudget | None = None,
    ) -> list[MemoryItem]:
        if not self._user_id or not query.strip() or limit <= 0:
            return []
        contract = budget or self._budget
        try:
            payload = await self._sdk.recall_memories(
                content=query,
                user_id=self._user_id,
                app_name=self._app_name,
                top_k=limit,
                smart=True,
                max_chars=contract.max_chars or None,
            )
        except Exception as exc:
            logger.warning("beeMemory recall 失败: %s", exc)
            contract.on_failure(str(exc))
            return []
        return payload_items(payload if isinstance(payload, dict) else {}, scope or MemoryScope.USER)

    async def snapshot(
        self,
        top_k: int = 10,
        max_chars: int = 1200,
        budget: MemoryBudget | None = None,
    ) -> dict[str, object]:
        contract = budget or self._budget
        try:
            payload = await self._sdk.snapshot_memories(
                user_id=self._user_id,
                app_name=self._app_name,
                top_k=top_k,
                max_chars=contract.max_chars or max_chars,
            )
        except Exception as exc:
            logger.warning("beeMemory snapshot 失败: %s", exc)
            contract.on_failure(str(exc))
            return {}
        return payload if isinstance(payload, dict) else {}

    async def render_snapshot(
        self,
        top_k: int = 10,
        max_chars: int = 1200,
        budget: MemoryBudget | None = None,
    ) -> str:
        contract = budget or self._budget
        payload = await self.snapshot(top_k=top_k, max_chars=max_chars, budget=contract)
        text, readout = render_memory_context_bounded(
            payload,
            self_app=self._app_name,
            kind="snapshot",
            budget_chars=contract.max_chars or max_chars,
        )
        contract.ensure(readout)
        return text

    async def context(
        self,
        history_turns: int = 12,
        top_k: int = 10,
        max_chars: int = 1200,
        history_max_chars: int | None = None,
        budget: MemoryBudget | None = None,
    ) -> dict[str, object]:
        contract = budget or self._budget
        try:
            payload = await self._sdk.get_context(
                user_id=self._user_id,
                app_name=self._app_name,
                session_id=self._session_id,
                history_turns=history_turns,
                top_k=top_k,
                max_chars=contract.max_chars or max_chars,
                history_max_chars=history_max_chars,
            )
        except Exception as exc:
            logger.warning("beeMemory context 失败: %s", exc)
            contract.on_failure(str(exc))
            return {}
        return payload if isinstance(payload, dict) else {}

    async def add(
        self,
        content: str,
        scope: MemoryScope | None = None,
        session_id: str = "",
        channel: str = "",
    ) -> dict[str, object]:
        if not self._user_id or not content.strip():
            return {"success": False, "message": "missing user_id or content"}
        try:
            payload = await self._sdk.add_memory(
                content=content,
                user_id=self._user_id,
                app_name=self._app_name,
                session_id=session_id or self._session_id,
                channel=channel,
            )
        except Exception as exc:
            logger.warning("beeMemory add 失败，降级跳过: %s", exc)
            return {"success": False, "message": str(exc)}
        return payload if isinstance(payload, dict) else {}

    async def add_image(
        self,
        image: str | bytes,
        prompt: str = "",
        channel: str = "image",
        session_id: str = "",
        budget: MemoryBudget | None = None,
    ) -> dict[str, object]:
        description = await self._describe(image, prompt, budget)
        content = compose_image_memory(description[:IMAGE_DESCRIPTION_MAX_CHARS], prompt)
        if not content:
            return {"success": False, "message": "empty_image_description"}
        payload = await self.add(content, session_id=session_id, channel=channel)
        result = dict(payload)
        result.setdefault("description", description)
        return result

    async def recall_image(
        self,
        image: str | bytes,
        prompt: str = "",
        limit: int = 5,
        scope: MemoryScope | None = None,
        budget: MemoryBudget | None = None,
    ) -> list[MemoryItem]:
        description = await self._describe(image, prompt, budget)
        if not description:
            return []
        query = f"{prompt}\n{description}" if prompt.strip() else description
        return await self.recall(query, scope=scope, limit=limit, budget=budget)

    async def _describe(
        self,
        image: str | bytes,
        prompt: str,
        budget: MemoryBudget | None,
    ) -> str:
        contract = budget or self._budget
        try:
            return (await self._resolve_describer().describe(image, prompt)).strip()
        except Exception as exc:
            logger.warning("beeMemory 图片描述失败: %s", exc)
            contract.on_failure(f"图片描述失败: {exc}")
            return ""

    def _resolve_describer(self) -> MediaDescriber:
        if self._describer is None:
            self._describer = VisionDescriber(model=self._image_model)
        return self._describer

    async def remember_turn(self, turn: MemoryTurn) -> MemoryWriteReport:
        scoped = MemoryTurn(
            session_id=turn.session_id or self._session_id,
            turn_index=turn.turn_index,
            user_text=turn.user_text,
            assistant_text=turn.assistant_text,
            app_name=turn.app_name or self._app_name,
            kind=turn.kind,
            metadata=turn.metadata,
        )
        return await self._writeback.run(scoped)

    async def _write_turn(self, turn: MemoryTurn, content: str) -> dict[str, object]:
        return await self.add(content, session_id=turn.session_id, channel=turn.kind)

    async def save_history(self, messages: list[dict[str, str]], channel: str = "") -> dict[str, object]:
        try:
            payload = await self._sdk.save_history(
                user_id=self._user_id,
                messages=messages,
                app_name=self._app_name,
                session_id=self._session_id,
                channel=channel,
            )
        except Exception as exc:
            logger.warning("beeMemory save_history 失败: %s", exc)
            return {"success": False, "message": str(exc)}
        return payload if isinstance(payload, dict) else {}

    async def health(self) -> bool:
        try:
            return await self._sdk.health_check()
        except Exception:
            return False


__all__ = ["BeeMemoryBackend"]
