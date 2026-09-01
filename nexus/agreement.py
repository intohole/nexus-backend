"""统一用户协议：协议内容下发、用户同意记录与状态查询。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.types import DateTime, String

from nexus.auth import get_current_user_id_required
from nexus.database import Base, get_db
from nexus.response import success_response

AGREEMENT_VERSION: str = "1.0.0"

DEFAULT_TERMS: str = """欢迎使用本应用。使用前请阅读以下要点：

1. 数据归属：您在本应用中创建和保存的所有内容，始终归您所有。
2. 数据用途：您的数据仅用于向您提供服务，我们不会挪作他用。
3. 数据删除：您可随时在应用内删除自己的内容，删除后不可恢复。
4. AI 生成内容：由 AI 生成的内容仅供参考，重要决策请自行核实。
5. 服务变更：服务如有重大调整，我们会提前告知您。

继续使用即表示您已理解并同意以上条款。"""

DEFAULT_PRIVACY: str = """我们非常重视您的隐私：

1. 收集信息：仅收集为您提供服务所必要的信息（如账号、您主动输入的内容）。
2. 信息保护：您的数据在传输和存储过程中均受到加密保护。
3. 不对外提供：未经您的同意，我们不会向任何第三方出售或共享您的个人信息。
4. 您的权利：您可随时查看、更正或删除自己的个人信息。

如有任何疑问，欢迎随时与我们联系。"""


class AgreementRecord(Base):
    __tablename__ = "agreement_records"
    __table_args__ = (
        UniqueConstraint("user_id", "app_name", "version", name="uq_agreement_user_app_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    app_name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    agreed_at: Mapped[datetime] = mapped_column(DateTime)


class AcceptRequest(BaseModel):
    version: Optional[str] = None


async def _get_record(
    session: AsyncSession, user_id: str, app_name: str, version: str
) -> Optional[AgreementRecord]:
    stmt = (
        select(AgreementRecord)
        .where(
            AgreementRecord.user_id == user_id,
            AgreementRecord.app_name == app_name,
            AgreementRecord.version == version,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def create_agreement_router(
    prefix: str = "/api/agreement",
    app_name: str = "",
    version: str = AGREEMENT_VERSION,
    terms_content: Optional[str] = None,
    privacy_content: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> APIRouter:
    if not app_name:
        raise ValueError("app_name is required")
    terms: str = terms_content or DEFAULT_TERMS
    privacy: str = privacy_content or DEFAULT_PRIVACY
    router = APIRouter(prefix=prefix, tags=tags or ["用户协议"])

    @router.get("/current")
    async def current() -> object:
        return success_response(
            {
                "app_name": app_name,
                "version": version,
                "terms": terms,
                "privacy": privacy,
            },
            "获取成功",
        )

    @router.get("/status")
    async def status(
        user_id: str = Depends(get_current_user_id_required),
        session: AsyncSession = Depends(get_db),
    ) -> object:
        record: Optional[AgreementRecord] = await _get_record(session, user_id, app_name, version)
        return success_response(
            {
                "accepted": record is not None,
                "version": version,
                "accepted_at": record.agreed_at.isoformat() if record else None,
            },
            "获取成功",
        )

    @router.post("/accept")
    async def accept(
        request: AcceptRequest,
        user_id: str = Depends(get_current_user_id_required),
        session: AsyncSession = Depends(get_db),
    ) -> object:
        target: str = request.version or version
        if target != version:
            raise HTTPException(status_code=400, detail="协议版本不存在")
        record: Optional[AgreementRecord] = await _get_record(session, user_id, app_name, target)
        if record is None:
            session.add(
                AgreementRecord(
                    user_id=user_id,
                    app_name=app_name,
                    version=target,
                    agreed_at=datetime.now(),
                )
            )
        return success_response({"accepted": True, "version": target}, "已同意")

    return router
