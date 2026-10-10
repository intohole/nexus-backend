"""SQLite 幂等迁移原语：建表后补列（PRAGMA 比对判定，不依赖异常吞没）。

三族入口，消费方按迁移形态选用：
- ensure_column / ensure_columns：清单驱动（表名, 列名, 列定义 三元组）
- ensure_model_columns：模型驱动（ORM metadata 与库比对自动补列）
- 同步形态后缀 _sync（run_sync 内 / 同步引擎直接用）
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import MetaData

logger = logging.getLogger("nexus.sqlite_migrate")

ColumnSpec = Tuple[str, str, str]


def ensure_column_sync(conn: Connection, table: str, column: str, col_ddl: str) -> bool:
    """列不存在则 ALTER TABLE 补列（col_ddl 为列名之后的定义片段），返回是否新增。

    表不存在时静默跳过（create_all 先行的常见启动序）。
    """
    insp = inspect(conn)
    if not insp.has_table(table):
        return False
    if column in {c["name"] for c in insp.get_columns(table)}:
        return False
    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_ddl}'))
    logger.info("sqlite_migrate: added %s.%s", table, column)
    return True


def ensure_columns_sync(conn: Connection, migrations: Sequence[ColumnSpec]) -> List[Tuple[str, str]]:
    added: List[Tuple[str, str]] = []
    for table, column, col_ddl in migrations:
        if ensure_column_sync(conn, table, column, col_ddl):
            added.append((table, column))
    return added


def ensure_model_columns_sync(
    conn: Connection, metadata: MetaData, tables: Optional[Iterable[str]] = None
) -> List[Tuple[str, str]]:
    """ORM metadata 与库现列比对，缺失列自动补（类型/NOT NULL/server_default 编译）。

    NOT NULL 且无 server_default 的列补 DEFAULT ''（SQLite 对有行数的表加
    NOT NULL 无 DEFAULT 列会直接报错）。
    """
    wanted: Optional[set] = set(tables) if tables is not None else None
    added: List[Tuple[str, str]] = []
    insp = inspect(conn)
    for table_name, table_obj in metadata.tables.items():
        if wanted is not None and table_name not in wanted:
            continue
        if not insp.has_table(table_name):
            continue
        existing: set = {c["name"] for c in insp.get_columns(table_name)}
        for col in table_obj.columns:
            if col.name in existing:
                continue
            col_type: str = str(col.type.compile(dialect=conn.dialect))
            default_clause: str
            if col.server_default is not None:
                default_clause = f" DEFAULT {col.server_default.arg}"
            elif col.nullable is False:
                default_clause = " DEFAULT ''"
            else:
                default_clause = ""
            null_clause: str = "" if col.nullable else " NOT NULL"
            sql: str = (
                f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}"'
                f" {col_type}{null_clause}{default_clause}"
            )
            conn.execute(text(sql))
            logger.info("sqlite_migrate: added %s.%s (from model)", table_name, col.name)
            added.append((table_name, col.name))
    return added


async def ensure_column(conn: AsyncConnection, table: str, column: str, col_ddl: str) -> bool:
    return await conn.run_sync(lambda sc: ensure_column_sync(sc, table, column, col_ddl))


async def ensure_columns(conn: AsyncConnection, migrations: Sequence[ColumnSpec]) -> List[Tuple[str, str]]:
    return await conn.run_sync(lambda sc: ensure_columns_sync(sc, migrations))


async def ensure_model_columns(
    conn: AsyncConnection, metadata: MetaData, tables: Optional[Iterable[str]] = None
) -> List[Tuple[str, str]]:
    return await conn.run_sync(lambda sc: ensure_model_columns_sync(sc, metadata, tables))
