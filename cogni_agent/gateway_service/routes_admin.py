"""
M1-01:管理后台聚合路由。

真实实现按模块拆分挂载(用户中心、工具中心、知识中心……各自的 admin 子路由),
此处作为脚手架先给一个健康检查与版本信息占位。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["管理后台"])


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "zhilv-agent-core"}
