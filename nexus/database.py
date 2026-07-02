from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from nexus.config import NexusConfig, get_settings
from nexus.errors import DatabaseError


class Base(DeclarativeBase):
    pass


class DatabaseManager:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def config(self) -> NexusConfig:
        return self._config

    async def init(self) -> None:
        if self._engine is not None:
            return
        async with self._lock:
            if self._engine is not None:
                return
            db_url: str = self._config.database.url
            if db_url.startswith("sqlite://"):
                db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
            elif db_url.startswith("postgresql://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

            engine_kwargs: dict[str, object] = {
                "echo": self._config.database.echo,
            }
            if not db_url.startswith("sqlite"):
                engine_kwargs["pool_size"] = self._config.database.pool_size
                engine_kwargs["max_overflow"] = self._config.database.max_overflow
                engine_kwargs["pool_recycle"] = self._config.database.pool_recycle

            self._engine = create_async_engine(db_url, **engine_kwargs)
            self._session_factory = async_sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            if self._config.database.sqlite_pragma and "sqlite" in db_url:
                await self._apply_sqlite_pragma()

    async def _apply_sqlite_pragma(self) -> None:
        if self._engine is None:
            return
        async with self._engine.begin() as conn:
            from sqlalchemy import text

            pragmas: list[str] = [
                "PRAGMA journal_mode=WAL",
                "PRAGMA busy_timeout=5000",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA cache_size=-64000",
                "PRAGMA foreign_keys=ON",
            ]
            for pragma in pragmas:
                await conn.execute(text(pragma))

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise DatabaseError("Database not initialized. Call init() first.")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise DatabaseError("Database not initialized. Call init() first.")
        return self._session_factory

    async def create_tables(self) -> None:
        if self._engine is None:
            await self.init()
        if self._engine is None:
            raise DatabaseError("Database engine initialization failed.")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def add_missing_columns(self) -> None:
        if self._engine is None:
            await self.init()
        if self._engine is None:
            raise DatabaseError("Database engine initialization failed.")
        async with self._engine.begin() as conn:
            from sqlalchemy import inspect, text

            def _sync_add_columns(sync_conn) -> None:
                insp = inspect(sync_conn)
                for table_name, table_obj in Base.metadata.tables.items():
                    if not insp.has_table(table_name):
                        continue
                    existing_cols: set[str] = {col["name"] for col in insp.get_columns(table_name)}
                    for col in table_obj.columns:
                        if col.name in existing_cols:
                            continue
                        col_type: str = str(col.type.compile(dialect=sync_conn.dialect))
                        null_clause: str = "" if col.nullable else " NOT NULL"
                        default_clause: str = ""
                        if col.server_default is not None:
                            default_clause = f" DEFAULT {col.server_default.arg}"
                        sql: str = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}{null_clause}{default_clause}'
                        sync_conn.execute(text(sql))

            await conn.run_sync(_sync_add_columns)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        if self._session_factory is None:
            await self.init()
        if self._session_factory is None:
            raise DatabaseError("Database session factory initialization failed.")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


db_manager: DatabaseManager = DatabaseManager()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async for session in db_manager.session():
        yield session


async def init_db() -> None:
    await db_manager.init()
    await db_manager.create_tables()


async def close_db() -> None:
    await db_manager.close()
