"""
API Key 管理路由:创建、列表、吊销。

供管理后台调用,需要 JWT 鉴权 + admin 角色。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from cogni_agent.common.db import utcnow
from cogni_agent.common.logging import get_logger
from cogni_agent.gateway_service.api_key_service import create_api_key, list_api_keys, revoke_api_key
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user

log = get_logger("gateway_service.routes_api_keys")
router = APIRouter(prefix="/admin/api-keys", tags=["管理后台 · API Key"])


class CreateApiKeyRequest(BaseModel):
    agent_allowed_ids: list[str] = Field(default_factory=list, description="允许调用的 Agent ID 列表,为空表示全部")
    expires_in_days: int | None = Field(default=None, ge=1, le=365, description="有效期天数,不填则永不过期")


class CreateApiKeyResponse(BaseModel):
    id: str
    key: str  # 明文 Key,仅此一次返回
    prefix_display: str
    agent_allowed_ids: list[str]
    expires_at: str | None
    created_at: str | None


@router.post("")
async def create_key(
    req: CreateApiKeyRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """
    创建 API Key。
    返回完整明文 Key,只在创建时返回一次,请提示用户妥善保存。
    """
    from cogni_agent.common.errors import Forbidden

    # 校验 admin 角色
    if user.role not in ("super_admin", "group_admin", "group_developer"):
        raise Forbidden("需要 admin 或更高级别权限")

    expires_at = None
    if req.expires_in_days:
        expires_at = utcnow().replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=req.expires_in_days)

    key_data = await create_api_key(
        scenic_id=user.scenic_id,
        created_by=user.user_id,
        agent_allowed_ids=req.agent_allowed_ids,
        expires_at=expires_at,
    )

    return CreateApiKeyResponse(**key_data).model_dump()


@router.get("")
async def list_keys(
    user: CurrentUser = Depends(get_current_user),
):
    """列出当前 scenic_id 下的所有 API Keys(不含明文)。"""
    keys = await list_api_keys(scenic_id=user.scenic_id)
    return keys


@router.post("/{key_id}/revoke")
async def revoke_key(
    key_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """吊销指定的 API Key。"""
    ok = await revoke_api_key(key_id, scenic_id=user.scenic_id)
    if not ok:
        from cogni_agent.common.logging import get_trace_id
        from cogni_agent.common.schemas import ApiResponse, ErrorCode
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content=ApiResponse.fail(
                code=ErrorCode.NOT_FOUND,
                message="API Key 不存在或无权操作",
                trace_id=get_trace_id(),
            ).model_dump(),
        )
    return {"ok": True, "message": "API Key 已吊销"}
