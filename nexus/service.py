"""服务基类：业务服务层的通用基类。"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class BaseService:
    """Service 层基类，提供 session 和 repo 注册能力"""

    def __init__(self, session: AsyncSession) -> None:
        self.session: AsyncSession = session
        self._repos: dict[str, object] = {}

    def register_repo(self, name: str, repo: object) -> None:
        self._repos[name] = repo

    def get_repo(self, name: str) -> object:
        if name not in self._repos:
            raise KeyError(f"repo '{name}' not registered")
        return self._repos[name]

    @property
    def repos(self) -> dict[str, object]:
        return self._repos

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()

    async def get_or_create(
        self,
        finder: Callable[[], Awaitable[Optional[T]]],
        creator: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """创建前查重样板：命中返回 (obj, False)，否则创建返回 (obj, True)。"""
        obj = await finder()
        if obj is not None:
            return obj, False
        created = await creator()
        return created, True
