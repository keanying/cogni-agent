"""前置参数处理:统一入口 + 端注册表。

路由侧只调用 resolve_pre_params(source=...),不直接 import 具体端的模块——
这样「管理端按某个端的逻辑跑」(联调/调试)只需要换 source,不必改调用代码。

新增一个端:
  1. control_plane_service/models.py 加 SOURCE_* 常量(若还没有);
  2. 新建 <端>_pre_params.py,实现 `async def resolve(*, user, req, conv=None) -> ParamContext`;
  3. 在 _REGISTRY 注册一行。

未注册的 source 走 base.default_context 兜底(不解析端专属字段)而不抛异常——
避免将来某个入口漏注册时整条链路直接不可用。
"""
from __future__ import annotations

from typing import Any

from cogni_agent.control_plane_service.models import SOURCE_APP

from . import app_pre_params
from .base import ParamContext, default_context, initial_unresolved, resolve_scenic_id

__all__ = [
    "ParamContext",
    "app_pre_params",
    "default_context",
    "initial_unresolved",
    "resolve_pre_params",
    "resolve_scenic_id",
]

_REGISTRY: dict[str, Any] = {
    SOURCE_APP: app_pre_params,
}


async def resolve_pre_params(*, source: str, user: Any, req: Any,
                             conv: Any = None) -> ParamContext:
    """按端分发:source 取值见 control_plane_service.models.SOURCE_*。"""
    provider = _REGISTRY.get(source)
    if provider is None:
        return default_context(source=source, user=user, conv=conv,
                               message=getattr(req, "message", None) or "")
    return await provider.resolve(user=user, req=req, conv=conv)
