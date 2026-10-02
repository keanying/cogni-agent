"""M7-04/10:审计日志——月分区表(此脚手架用普通表+索引简化,生产建议按月分区)。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name


class AuditLog(Base):
    __tablename__ = table_name("gov_audit")

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)  # 如 UnauthorizedAccess / CrossOrgAccess
    # 可空:集团级操作没有所属景区(与 outbox / cost 保持一致)
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


async def write_audit(event_type: str, scenic_id: str, actor_user_id: str | None, detail: dict) -> None:
    import uuid

    async with get_sessionmaker()() as db:
        db.add(AuditLog(id=f"aud_{uuid.uuid4().hex}", event_type=event_type, scenic_id=scenic_id, actor_user_id=actor_user_id, detail=detail))
        await db.commit()


async def audit_subscriber(cloudevent: dict) -> None:
    """M7-02 订阅者:凡是敏感事件类型都落一条审计记录。"""
    sensitive_types = {"com.zhilv.tool.published", "com.zhilv.approval.granted", "com.zhilv.user.created"}
    if cloudevent.get("type") in sensitive_types:
        await write_audit(cloudevent["type"], cloudevent.get("tenantid", ""), None, cloudevent.get("data", {}))
