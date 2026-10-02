"""统一审批记录——工作台待审批卡片 与 审计中心·审批中心 共用同一张表。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name


class Approval(Base):
    __tablename__ = table_name("gov_approvals")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String(32))  # 高风险工具调用 / 资源发布审批 等
    related_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_resource_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # tool/skill/mcp/agent
    related_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="medium")
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applicant_user_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected
    opinion: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def create_approval(type_: str, risk_level: str, applicant_user_id: str, scenic_id: str | None,
                           related_task_id: str | None = None, related_resource_type: str | None = None,
                           related_resource_id: str | None = None) -> str:
    async with get_sessionmaker()() as db:
        approval = Approval(type=type_, risk_level=risk_level, applicant_user_id=applicant_user_id, scenic_id=scenic_id,
                             related_task_id=related_task_id, related_resource_type=related_resource_type,
                             related_resource_id=related_resource_id)
        db.add(approval)
        await db.commit()
        return approval.id


async def resolve_approval(approval_id: str, approved: bool, opinion: str, resolved_by: str,
                           params: dict | None = None) -> None:
    """通过 -> 若关联任务,调用 runtime_service.engine.hitl.resume_after_approval() 恢复执行;
    驳回 -> 必须有 opinion(路由层已校验非空)。"""
    async with get_sessionmaker()() as db:
        approval = await db.get(Approval, approval_id)
        if not approval:
            return
        approval.status = "approved" if approved else "rejected"
        approval.opinion = opinion
        approval.resolved_by = resolved_by
        from datetime import datetime as _dt, timezone as _tz

        approval.resolved_at = _dt.now(_tz.utc)
        related_task_id = approval.related_task_id
        await db.commit()

    if related_task_id:
        from cogni_agent.runtime_service.engine.hitl import resume_after_approval

        # params 非空 = 改参数后批准:带着审批人改过的参数恢复执行
        await resume_after_approval(related_task_id, approved, opinion, params=params)
