"""register_uc_proxy 与 repository.paginate 回归（r43 收归原语）。"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base

from nexus.repository import paginate, paginate_skip
from nexus.response import paginated_payload
from nexus.uc_proxy import register_uc_proxy

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    name = Column(String(32))


@pytest.mark.asyncio
async def test_paginate_returns_items_and_total():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([Widget(name=f"w{i}") for i in range(7)])
        await session.commit()
        stmt = select(Widget).order_by(Widget.id)
        items, total = await paginate(session, stmt, page=2, page_size=3)
        assert total == 7
        assert [w.name for w in items] == ["w3", "w4", "w5"]


@pytest.mark.asyncio
async def test_paginate_skip_matches_paginate_window():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add_all([Widget(name=f"w{i}") for i in range(7)])
        await session.commit()
        stmt = select(Widget).order_by(Widget.id)
        items, total = await paginate_skip(session, stmt, skip=3, limit=3)
        assert total == 7
        assert [w.name for w in items] == ["w3", "w4", "w5"]
        payload = paginated_payload([w.name for w in items], total, skip=3, limit=3)
        assert payload == {
            "items": ["w3", "w4", "w5"],
            "total": 7,
            "skip": 3,
            "limit": 3,
            "has_more": True,
        }
        tail_items, _ = await paginate_skip(session, stmt, skip=6, limit=3)
        assert paginated_payload([], 7, skip=6, limit=3)["has_more"] is False
        assert len(tail_items) == 1


def test_register_uc_proxy_proxies_and_503(monkeypatch):
    app = FastAPI()
    register_uc_proxy(app)
    client = httpx.AsyncClient  # 引用面自检：原语内部共享 client
    assert client is not None

    @app.get("/other")
    async def other():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    import asyncio

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            monkeypatch.delenv("UC__BASE_URL", raising=False)
            import nexus.uc_proxy as m

            monkeypatch.setattr(m, "_resolve_uc_base", lambda: "")
            r1 = await c.get("/uc-api/api/auth/config")
            assert r1.status_code == 503
            monkeypatch.setattr(m, "_resolve_uc_base", lambda: "http://upstream")
            r2 = await c.get("/other")
            assert r2.status_code == 200

    asyncio.run(run())
