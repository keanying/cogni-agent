"""M5-10:三元鉴权 scenic_id × role × tool_id(结合 G-AUTH 可见范围与授权表)。"""
from __future__ import annotations

from cogni_agent.tool_center_service.tool.models import Tool


async def check_tool_authz(db, tool_id: str, scenic_id: str, role: str) -> bool:
    """
    真正细粒度的可见范围/定向授权判定已经在 runtime_service.engine.resolver 解析
    ExecutionSpec 阶段做过一次(那里有 user_id,能跑完整的 test_permission 四级判定)。
    这里是执行前的最后一道闸——只做"任务从排队到真正执行之间,工具是否被禁用/删除"
    这类会随时间变化、resolver 阶段可能已经过时的检查,不重复完整的可见范围算法
    (调用方也没有把 user_id 传到这一层,按当前 mcp/protocol.py 的调用约定,细粒度
    判定的职责边界就在 resolver,这里保持轻量)。
    """
    tool = await db.get(Tool, tool_id)
    if tool is None or not tool.active:
        return False
    return True
