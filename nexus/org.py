"""组织可见性原语：B 端数据按「自有(user_id) 或同组织(org_id)」划界的单源谓词。

org 上下文来自 token claim（UC /organizations/{id}/enter 签发，登录时由
users.active_org_id 恢复），业务表以 org_id 列承接归属：写入时 stamp，
读取时全量过本谓词。业务仓零依赖可自行装配可见性 scope。

判例：adSmart advertiser_visibility（首例）、piying project_visibility（次例）。
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement


def org_visibility(
    user_col: object,
    user_id: str | int,
    org_col: object = None,
    org_id: str | int | None = None,
) -> ColumnElement[bool]:
    conditions = [user_col == user_id]
    if org_id is not None and org_col is not None:
        conditions.append(org_col == int(org_id))
    return or_(*conditions)


__all__ = ["org_visibility"]
