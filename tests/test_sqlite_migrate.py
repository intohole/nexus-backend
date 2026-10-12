from __future__ import annotations

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, func, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.sqlite_migrate import (
    ensure_column,
    ensure_column_sync,
    ensure_columns,
    ensure_columns_sync,
    ensure_model_columns,
    ensure_model_columns_sync,
)


def _make_db():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, note TEXT)"))
    return engine


def _columns(engine, table: str) -> set:
    with engine.connect() as conn:
        return {c["name"] for c in inspect(conn).get_columns(table)}


def test_ensure_column_sync_adds_then_idempotent() -> None:
    engine = _make_db()
    with engine.begin() as conn:
        assert ensure_column_sync(conn, "orders", "status", "VARCHAR(20) DEFAULT 'open'") is True
        assert ensure_column_sync(conn, "orders", "status", "VARCHAR(20) DEFAULT 'open'") is False
    assert _columns(engine, "orders") == {"id", "note", "status"}


def test_ensure_column_sync_missing_table_skipped() -> None:
    engine = _make_db()
    with engine.begin() as conn:
        assert ensure_column_sync(conn, "ghosts", "x", "INTEGER") is False


def test_ensure_columns_sync_batch() -> None:
    engine = _make_db()
    specs = [
        ("orders", "carrier", "VARCHAR(50)"),
        ("orders", "shipped_at", "DATETIME"),
    ]
    with engine.begin() as conn:
        added = ensure_columns_sync(conn, specs)
        assert added == [("orders", "carrier"), ("orders", "shipped_at")]
        assert ensure_columns_sync(conn, specs) == []


def test_ensure_model_columns_sync_compiles_defaults() -> None:
    engine = _make_db()
    md = MetaData()
    Table(
        "orders", md,
        Column("id", Integer, primary_key=True),
        Column("note", String),
        Column("region", String(16), nullable=False, server_default=text("'cn'")),
        Column("priority", Integer, nullable=False),
        Column("extra", String(50)),
    )
    with engine.begin() as conn:
        added = ensure_model_columns_sync(conn, md)
    assert set(added) == {("orders", "region"), ("orders", "priority"), ("orders", "extra")}
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders (id, note) VALUES (1, 'a')"))
    assert _columns(engine, "orders") >= {"region", "priority", "extra"}


def test_ensure_model_columns_sync_table_filter() -> None:
    engine = _make_db()
    md = MetaData()
    Table("orders", md, Column("id", Integer, primary_key=True), Column("batch", String(8)))
    Table("others", md, Column("id", Integer, primary_key=True))
    with engine.begin() as conn:
        ensure_model_columns_sync(conn, md)
    with engine.begin() as conn:
        assert ensure_model_columns_sync(conn, md, tables=["nonexistent"]) == []
    assert "batch" in _columns(engine, "orders")


def test_ensure_model_columns_sync_skips_missing_tables() -> None:
    engine = _make_db()
    md = MetaData()
    Table("never_created", md, Column("id", Integer, primary_key=True))
    with engine.begin() as conn:
        assert ensure_model_columns_sync(conn, md) == []


def test_ensure_model_columns_sync_sql_expr_default_nullable_downgrades() -> None:
    engine = _make_db()
    md = MetaData()
    Table(
        "orders", md,
        Column("id", Integer, primary_key=True),
        Column("created_at", DateTime, server_default=func.now()),
        Column("origin", String(16), nullable=False, server_default="cn"),
    )
    with engine.begin() as conn:
        added = ensure_model_columns_sync(conn, md)
        assert set(added) == {("orders", "created_at"), ("orders", "origin")}
        conn.execute(text("INSERT INTO orders (id) VALUES (1)"))
    with engine.connect() as conn:
        nullables = {c["name"]: c["nullable"] for c in inspect(conn).get_columns("orders")}
    assert nullables["created_at"] is True
    assert nullables["origin"] is False


def test_ensure_model_columns_sync_sql_expr_default_not_null_rejected() -> None:
    engine = _make_db()
    md = MetaData()
    Table("orders", md, Column("stamp", DateTime, nullable=False, server_default=func.now()))
    with engine.begin() as conn:
        with pytest.raises(ValueError, match="非常量默认"):
            ensure_model_columns_sync(conn, md)


@pytest.mark.asyncio
async def test_async_family_smoke() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    md = MetaData()
    Table(
        "orders", md,
        Column("id", Integer, primary_key=True),
        Column("note", String),
        Column("tag", String(16)),
    )
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
        assert await ensure_column(conn, "orders", "flag", "INTEGER DEFAULT 0") is True
        assert await ensure_column(conn, "orders", "flag", "INTEGER DEFAULT 0") is False
        added = await ensure_columns(conn, [("orders", "a1", "TEXT"), ("orders", "a2", "TEXT")])
        assert added == [("orders", "a1"), ("orders", "a2")]
        assert await ensure_model_columns(conn, md) == []
    async with engine.connect() as conn:
        cols = {c["name"] for c in await conn.run_sync(
            lambda sc: inspect(sc).get_columns("orders")
        )}
    assert cols == {"id", "note", "tag", "flag", "a1", "a2"}
    await engine.dispose()
