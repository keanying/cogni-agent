"""M2-01/04:用户、登录设备、组织、模型配置表。表名统一带 numeri_core_ 前缀。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class User(Base):
    __tablename__ = table_name("usr_users")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    account: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # None=集团级角色
    role: Mapped[str] = mapped_column(String(32), default="scenic_operator")
    # role 六值枚举:super_admin / group_admin / group_developer / group_viewer / scenic_admin / scenic_operator
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credentials_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ↑ 改密/重置密码后更新，所有签发时间早于此值的 JWT 一律失效（含 access_token 和 refresh_token）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LoginDevice(Base):
    """M2-04:登录设备记录。"""

    __tablename__ = table_name("usr_login_devices")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey(table_name("usr_users") + ".id"), index=True)
    device_info: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(64))
    login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    # v1.0 新增：APP 端设备信息
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_os: Mapped[str | None] = mapped_column(String(32), nullable=True)       # iOS / Android
    push_token: Mapped[str | None] = mapped_column(String(255), nullable=True)     # APNs / FCM 推送 Token
    app_version: Mapped[str | None] = mapped_column(String(16), nullable=True)     # APP 版本号


class SmsCode(Base):
    """M2-03:短信验证码记录,含限流用的发送时间戳。"""

    __tablename__ = table_name("usr_sms_codes")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code: Mapped[str] = mapped_column(String(8))
    purpose: Mapped[str] = mapped_column(String(32))  # login / new_device / reset_password
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ═══════════════════════════════════════════════════════════════
# 组织管理(从当前项目补充)
# ═══════════════════════════════════════════════════════════════

class SysGroup(Base):
    """集团信息。"""

    __tablename__ = table_name("sys_group")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    name: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class SysScenic(Base):
    """景区信息。"""

    __tablename__ = table_name("sys_scenics")

    scenic_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenic_name: Mapped[str] = mapped_column(String(255))
    scenic_grade_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_config: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ═══════════════════════════════════════════════════════════════
# 模型厂商与配置(从当前项目补充)
# ═══════════════════════════════════════════════════════════════

class ModelProvider(Base):
    """模型厂商。"""

    __tablename__ = table_name("sys_model_providers")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    provider_type: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(512))
    api_key: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(String(255), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ModelConfig(Base):
    """模型配置。"""

    __tablename__ = table_name("sys_model_configs")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    provider_id: Mapped[str] = mapped_column(String(36), ForeignKey(table_name("sys_model_providers") + ".id"))
    model_name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(255), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ═══════════════════════════════════════════════════════════════
# v1.0 APP 端新增模型
# ═══════════════════════════════════════════════════════════════

class AppConfig(Base):
    """v1.0 APP 端：用户个人配置存储（JSONB 透传）。"""

    __tablename__ = table_name("app_config")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey(table_name("usr_users") + ".id"), index=True)
    config_type: Mapped[str] = mapped_column(String(64), index=True)   # ota_credentials / workbench
    config: Mapped[dict] = mapped_column(JSON, default=dict)            # 前端传入的完整 JSON，后端透传
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CompetitorRequest(Base):
    """v1.0 APP 端：竞品需求提交记录。业务字段由 extra JSONB 承载，schema_version 标记结构版本。"""

    __tablename__ = table_name("app_competitor_requests")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
