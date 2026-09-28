from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from nexus.config import NexusConfig
from nexus.database import DatabaseManager


@pytest.mark.asyncio
async def test_sqlite_pragma_applied_on_every_connection(tmp_path: Path) -> None:
    cfg: NexusConfig = NexusConfig()
    cfg.database.url = f"sqlite:///{tmp_path}/pragma.db"
    mgr: DatabaseManager = DatabaseManager(cfg)
    await mgr.init()
    try:
        for _ in range(2):
            async with mgr.engine.connect() as conn:
                journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
                foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
                busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert str(journal_mode).lower() == "wal"
            assert int(foreign_keys) == 1
            assert int(busy_timeout) == 5000
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_sqlite_pragma_can_be_disabled(tmp_path: Path) -> None:
    cfg: NexusConfig = NexusConfig()
    cfg.database.url = f"sqlite:///{tmp_path}/no_pragma.db"
    cfg.database.sqlite_pragma = False
    mgr: DatabaseManager = DatabaseManager(cfg)
    await mgr.init()
    try:
        async with mgr.engine.connect() as conn:
            foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert int(foreign_keys) == 0
    finally:
        await mgr.close()
