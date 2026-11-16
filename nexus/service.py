"""服务基类：业务服务层的通用基类。"""
from __future__ import annotations

from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class BaseService:
    """Service 层基类，提供 session 和 repo 注册能力"""

    def __init__(self, session: AsyncSession) -> None:
        self.session: AsyncSession = session

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()

