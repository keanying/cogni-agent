"""外部 MCP Server 接入记录(独立于 tc_tools;发现导入的工具复用 tc_tools 并标记 source_mcp_server_id)。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class McpServer(Base):
    __tablename__ = table_name("tc_mcp_servers")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))
    auth_type: Mapped[str] = mapped_column(String(16), default="none")  # none/apikey/oauth2
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 关联 tc_credentials
    connect_status: Mapped[str] = mapped_column(String(16), default="pending")  # connected/failed/pending
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="group")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
