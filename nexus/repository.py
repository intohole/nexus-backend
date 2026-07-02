from __future__ import annotations

from typing import Generic, Optional, Type, TypeVar

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.errors import NotFoundError

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    def __init__(self, model: Type[ModelT], session: AsyncSession) -> None:
        self._model: Type[ModelT] = model
        self._session: AsyncSession = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    @property
    def model(self) -> Type[ModelT]:
        return self._model

    async def _scalar_one_or_none(self, stmt: object) -> Optional[ModelT]:
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def _scalars_all(self, stmt: object) -> list[ModelT]:
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, id: int | str) -> Optional[ModelT]:
        stmt = select(self._model).where(self._model.id == id)  # type: ignore[attr-defined]
        return await self._scalar_one_or_none(stmt)

    async def get_or_404(self, id: int | str) -> ModelT:
        obj: Optional[ModelT] = await self.get_by_id(id)
        if obj is None:
            raise NotFoundError(f"{self._model.__name__} with id={id} not found")  # type: ignore[attr-defined]
        return obj

    async def create(
        self,
        obj_in: dict[str, object] | ModelT,
        auto_commit: bool = False,
    ) -> ModelT:
        if isinstance(obj_in, dict):
            obj: ModelT = self._model(**obj_in)  # type: ignore[call-arg]
        else:
            obj = obj_in
        self._session.add(obj)
        await self._session.flush()
        if auto_commit:
            await self._session.commit()
        else:
            await self._session.refresh(obj)
        return obj

    async def update(
        self,
        id: int | str,
        obj_in: dict[str, object],
        auto_commit: bool = False,
    ) -> Optional[ModelT]:
        obj: Optional[ModelT] = await self.get_by_id(id)
        if obj is None:
            return None
        for key, value in obj_in.items():
            if hasattr(obj, key) and key != "id":
                setattr(obj, key, value)
        await self._session.flush()
        if auto_commit:
            await self._session.commit()
        else:
            await self._session.refresh(obj)
        return obj

    async def delete(self, id: int | str, auto_commit: bool = False) -> bool:
        obj: Optional[ModelT] = await self.get_by_id(id)
        if obj is None:
            return False
        await self._session.delete(obj)
        await self._session.flush()
        if auto_commit:
            await self._session.commit()
        return True

    async def list_all(
        self,
        skip: int = 0,
        limit: int = 20,
        order_by: Optional[object] = None,
    ) -> list[ModelT]:
        stmt = select(self._model)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.offset(skip).limit(limit)
        return await self._scalars_all(stmt)

    async def paginate(
        self,
        page: int = 1,
        page_size: int = 20,
        filters: Optional[dict[str, object]] = None,
        order_by: str = "id",
        order_desc: bool = True,
    ) -> dict[str, object]:
        stmt = select(self._model)
        countStmt = select(func.count()).select_from(self._model)  # type: ignore[arg-type]

        if filters:
            for key, value in filters.items():
                if hasattr(self._model, key):
                    col = getattr(self._model, key)
                    stmt = stmt.where(col == value)
                    countStmt = countStmt.where(col == value)

        total: int = await self._session.scalar(countStmt) or 0

        if hasattr(self._model, order_by):
            col = getattr(self._model, order_by)
            stmt = stmt.order_by(col.desc() if order_desc else col.asc())

        offsetVal: int = (page - 1) * page_size
        stmt = stmt.offset(offsetVal).limit(page_size)
        items: list[ModelT] = await self._scalars_all(stmt)

        totalPages: int = (total + page_size - 1) // page_size if page_size > 0 else 0
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": totalPages,
        }

    async def count(self, filters: Optional[dict[str, object]] = None) -> int:
        stmt = select(func.count()).select_from(self._model)  # type: ignore[arg-type]
        if filters:
            for key, value in filters.items():
                if hasattr(self._model, key):
                    stmt = stmt.where(getattr(self._model, key) == value)
        return await self._session.scalar(stmt) or 0

    async def exists(self, id: int | str) -> bool:
        stmt = (
            select(literal(1))
            .where(self._model.id == id)  # type: ignore[attr-defined]
            .limit(1)
        )
        result: Optional[int] = await self._session.scalar(stmt)
        return result is not None

    async def find_one_by(self, **kwargs: object) -> Optional[ModelT]:
        stmt = select(self._model)
        for key, value in kwargs.items():
            if hasattr(self._model, key):
                stmt = stmt.where(getattr(self._model, key) == value)
        return await self._scalar_one_or_none(stmt)

    async def find_all_by(
        self,
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
        return await self._scalars_all(stmt)

    async def bulk_create(self, objs_in: list[dict[str, object]]) -> list[ModelT]:
        objs: list[ModelT] = [self._model(**data) for data in objs_in]  # type: ignore[call-arg]
        self._session.add_all(objs)
        await self._session.flush()
        return objs

    async def bulk_delete(self, ids: list[int | str]) -> int:
        stmt = sa_delete(self._model).where(self._model.id.in_(ids))  # type: ignore[attr-defined]
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount or 0
