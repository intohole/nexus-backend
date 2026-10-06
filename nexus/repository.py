"""仓储基类：无状态数据访问基类。"""
from __future__ import annotations

from typing import Any, Generic, Optional, Type, TypeVar

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select

from nexus.errors import NotFoundError

ModelT = TypeVar("ModelT")


async def paginate(
    session: AsyncSession,
    stmt: Select,
    page: int = 1,
    page_size: int = 20,
    unique: bool = False,
) -> tuple[list[Any], int]:
    """通用分页：同一 stmt 二段执行 count + 页切片，返回 (items, total)。

    page 从 1 起；stmt 可带 where/order_by，count 走 subquery 不受影响；
    joined eager load（collection）场景传 unique=True 去重。
    """
    total: int | None = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    )
    result = await session.execute(
        stmt.offset((page - 1) * page_size).limit(page_size)
    )
    scalars = result.scalars()
    items = list(scalars.unique().all()) if unique else list(scalars.all())
    return items, int(total or 0)


class StatelessRepository(Generic[ModelT]):
    """无状态Repository基类，每次方法调用时传入session"""

    def __init__(self, model: Type[ModelT]) -> None:
        self._model: Type[ModelT] = model

    @property
    def model(self) -> Type[ModelT]:
        return self._model

    async def _scalar_one_or_none(self, session: AsyncSession, stmt: object) -> Optional[ModelT]:
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _scalars_all(self, session: AsyncSession, stmt: object) -> list[ModelT]:
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, session: AsyncSession, id: int | str) -> Optional[ModelT]:
        stmt = select(self._model).where(self._model.id == id)  # type: ignore[attr-defined]
        return await self._scalar_one_or_none(session, stmt)

    async def create(
        self,
        session: AsyncSession,
        obj_in: dict[str, object],
        auto_refresh: bool = True,
    ) -> ModelT:
        instance = self._model(**obj_in)  # type: ignore[call-arg]
        session.add(instance)
        await session.flush()
        if auto_refresh:
            await session.refresh(instance)
        return instance

    async def update(
        self,
        session: AsyncSession,
        id: int | str,
        obj_in: dict[str, object],
    ) -> Optional[ModelT]:
        obj: Optional[ModelT] = await self.get_by_id(session, id)
        if obj is None:
            return None
        for key, value in obj_in.items():
            if hasattr(obj, key) and key != "id":
                setattr(obj, key, value)
        await session.flush()
        await session.refresh(obj)
        return obj

    async def delete(self, session: AsyncSession, id: int | str) -> bool:
        obj: Optional[ModelT] = await self.get_by_id(session, id)
        if obj is None:
            return False
        await session.delete(obj)
        await session.flush()
        return True

    async def list_all(
        self,
        session: AsyncSession,
        skip: int = 0,
        limit: int = 20,
        order_by: object | None = None,
    ) -> list[ModelT]:
        stmt = select(self._model)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.offset(skip).limit(limit)
        return await self._scalars_all(session, stmt)

    async def count(
        self,
        session: AsyncSession,
        filters: Optional[dict[str, object]] = None,
    ) -> int:
        stmt = select(func.count()).select_from(self._model)  # type: ignore[arg-type]
        for key, value in (filters or {}).items():
            if hasattr(self._model, key):
                stmt = stmt.where(getattr(self._model, key) == value)
        result: int | None = await session.scalar(stmt)
        return result or 0

    async def exists(self, session: AsyncSession, id: int | str) -> bool:
        stmt = (
            select(literal(1))
            .where(self._model.id == id)  # type: ignore[attr-defined]
            .limit(1)
        )
        result: int | None = await session.scalar(stmt)
        return result is not None

    async def find_one_by(self, session: AsyncSession, **kwargs: object) -> Optional[ModelT]:
        stmt = select(self._model)
        for key, value in kwargs.items():
            if hasattr(self._model, key):
                stmt = stmt.where(getattr(self._model, key) == value)
        return await self._scalar_one_or_none(session, stmt)

    async def find_all_by(
        self,
        session: AsyncSession,
        skip: int = 0,
        limit: int = 20,
        order_by_attr: str = "id",
        descending: bool = True,
        **kwargs: object,
    ) -> list[ModelT]:
        stmt = select(self._model)
        for key, value in kwargs.items():
            if hasattr(self._model, key):
                stmt = stmt.where(getattr(self._model, key) == value)
        if hasattr(self._model, order_by_attr):
            col = getattr(self._model, order_by_attr)
            stmt = stmt.order_by(col.desc() if descending else col.asc())
        stmt = stmt.offset(skip).limit(limit)
        return await self._scalars_all(session, stmt)
