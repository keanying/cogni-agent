"""数据库知识:数据源管理 + 表登记 + 自定义数据源。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class DbDatasource(Base):
    """数据源管理:按类型分类(mysql/selectdb/starrocks/clickhouse),支持修改/删除/启禁用/归属。"""

    __tablename__ = table_name("kb_db_datasources")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    db_type: Mapped[str] = mapped_column(String(16))  # mysql / selectdb / starrocks / clickhouse
    connection_config: Mapped[dict] = mapped_column(JSON)  # host/port/db/凭证引用等
    scope: Mapped[str] = mapped_column(String(16), default="group")  # group(集团级) / scenic(景区级)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DbCatalog(Base):
    """表登记:选择数据源 -> 展示表列表 -> 点击查看 DDL + 预览前100行(预览逻辑在 API 层实时查询,不落库)。"""

    __tablename__ = table_name("kb_db_catalog")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    datasource_id: Mapped[str] = mapped_column(String(36), index=True)
    table_name_ref: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(255))
    columns_doc: Mapped[dict] = mapped_column(JSON, default=dict)  # {字段名: 释义}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DbCustomSource(Base):
    """自定义数据源:基于已登记的源表,用自定义 SQL/视图逻辑构建虚拟数据源,同样纳入表登记体系。"""

    __tablename__ = table_name("kb_db_custom_sources")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    base_datasource_id: Mapped[str] = mapped_column(String(36))
    build_sql: Mapped[str] = mapped_column(Text)  # 自定义视图逻辑,如多表 JOIN
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
