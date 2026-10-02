"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    phone: str
    password: str
    sms_code: str | None = None  # 首次登录/新设备登录时必填(M2-03)
    rsa_encrypt: bool = False    # true 时 password 字段为 RSA 加密后的 Base64 密文


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    must_change_password: bool


class CreateUserRequest(BaseModel):
    """M2-06:仅管理后台可调用。scenic_id 留空 = 创建集团级账号(role 需传
    super_admin/group_admin/group_developer/group_viewer 之一);景区账号必须传 scenic_id
    (role 传 scenic_admin/scenic_operator)。"""

    # 初始密码。留空用默认规则「{手机号后4位}@123」;需要更强初始密码时管理员可显式指定。
    password: str | None = None
    name: str
    account: str | None = None   # v1.0: 不再必填，留空自动生成(纳秒时间戳后8位)
    phone: str
    scenic_id: str | None = None
    role: str = "scenic_operator"
    position: str | None = None


class UpdateUserRequest(BaseModel):
    """M2-07。"""

    name: str | None = None
    scenic_id: str | None = None
    role: str | None = None
    position: str | None = None
    is_active: bool | None = None


# ═══════════════════════════════════════════════════════════════
# 用户扩展 Schema(从当前项目补充)
# ═══════════════════════════════════════════════════════════════

class UserListQuery(BaseModel):
    """用户列表筛选参数。"""
    keyword: str | None = None
    role: str | None = None
    scenic_id: str | None = None
    is_active: bool | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class UserInfo(BaseModel):
    """用户信息返回体(不含 password_hash)。"""
    id: str
    name: str
    account: str
    phone: str
    scenic_id: str | None = None
    scenic_name: str | None = None
    role: str
    position: str | None = None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class BatchRoleRequest(BaseModel):
    """批量角色分配。"""
    user_ids: list[str]
    role: str


class RoleInfo(BaseModel):
    """角色信息。"""
    role: str
    label: str
    permissions: dict[str, Any] | None = None


# ═══════════════════════════════════════════════════════════════
# 模型厂商与配置 Schema(从当前项目补充)
# ═══════════════════════════════════════════════════════════════

class ModelProviderCreate(BaseModel):
    """新增模型厂商。"""
    name: str
    provider_type: str
    base_url: str
    api_key: str = ""
    description: str = ""
    is_enabled: bool = True


class ModelProviderUpdate(BaseModel):
    """更新模型厂商。"""
    name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    description: str | None = None
    is_enabled: bool | None = None


class ModelProviderResponse(BaseModel):
    """模型厂商响应(api_key 脱敏)。"""
    id: str
    name: str
    provider_type: str
    base_url: str
    api_key: str = ""
    description: str = ""
    is_enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None

    class Config:
        from_attributes = True


class ModelConfigCreate(BaseModel):
    """新增模型配置。"""
    name: str
    provider_id: str
    model_name: str
    description: str = ""
    is_enabled: bool = True
    is_default: bool = False
    extra_params: dict[str, Any] = Field(default_factory=dict)


class ModelConfigUpdate(BaseModel):
    """更新模型配置。"""
    name: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    description: str | None = None
    is_enabled: bool | None = None
    is_default: bool | None = None
    extra_params: dict[str, Any] | None = None


class ModelConfigResponse(BaseModel):
    """模型配置响应(含 provider 名称)。"""
    id: str
    name: str
    provider_id: str
    provider_name: str = ""
    provider_type: str = ""
    model_name: str
    description: str = ""
    is_enabled: bool = True
    is_default: bool = False
    extra_params: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    class Config:
        from_attributes = True
