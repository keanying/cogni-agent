"""APP 端前置参数处理(/api/app/chat 入口专用)。

端专属职责只有三件事,其余规则一律复用 base.py:
  1. 身份:APP 端 JWT → scenic_id / user_id / role(经 deps.get_current_app_user 解析);
  2. 请求结构:APP body 的 date(问数日期上下文)、question_id(固定问题入口);
  3. 端标识:source = SOURCE_APP(落 Task.source,统计口径按端可区分)。

为什么单独成文件:APP 端后续要加的「设备绑定 / 多景区切换 / APP 专属参数处理」都写在
这里,不再去改 deps.py 的公共鉴权,也不散落在路由函数里。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cogni_agent.control_plane_service.models import SOURCE_APP

from .base import ParamContext, initial_unresolved, resolve_scenic_id

if TYPE_CHECKING:
    from cogni_agent.gateway_service.deps import CurrentUser


async def resolve(*, user: "CurrentUser", req: Any, conv: Any = None) -> ParamContext:
    """APP 请求 → 标准上下文。

    req 刻意用鸭子类型读字段(不 import AppChatRequest):这样管理端要「按 APP 端逻辑
    跑」做联调时,同一个适配器可以直接接 ChatRequest,不必再写一套。
    """
    scenic_id = resolve_scenic_id(conv, user)
    date = getattr(req, "date", None)
    return ParamContext(
        source=SOURCE_APP,
        scenic_id=scenic_id,
        user_id=user.user_id,
        role=user.role,
        message=req.message or "",
        date=date.model_dump(exclude_none=True) if date is not None else None,
        question_id=getattr(req, "question_id", None),
        unresolved=initial_unresolved(scenic_id),
    )
