"""M1-01、M3-07:任务查询/取消/重试,供管理后台任务中心页调用。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.control_plane_service.models import Task
from cogni_agent.control_plane_service.queue import cancel_task, get_task
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/admin/tasks", tags=["任务中心"])


@router.get("")
async def list_tasks(
    status: str | None = None, executor_type: str | None = None, limit: int = 50,
    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """M5-01:任务中心列表——默认只读,强制按当前用户的 scenic_id 过滤(集团角色 scenic_id=None 看集团全部)。"""
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if user.scenic_id:
        stmt = stmt.where(Task.scenic_id == user.scenic_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if executor_type:
        stmt = stmt.where(Task.executor_type == executor_type)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [
        {"id": t.id, "scenic_id": t.scenic_id, "executor_type": t.executor_type, "executor_id": t.executor_id,
         "source": t.source, "status": t.status, "priority": t.priority, "created_at": t.created_at, "updated_at": t.updated_at}
        for t in rows
    ]}


@router.get("/{task_id}/debug")
async def task_debug(task_id: str, format: str = "json",
                     user: CurrentUser = Depends(get_current_user)):
    """导出这一次执行的**完整链条**:任务 → 版本溯源 → 执行链(含工具入参/结果摘要)
    → 每一步的提示词 → 用量成本 → 最终回答。

    做给「跑出问题了,把执行过程整个发给研发」这件事用的。Quick BI 那边早有
    `/runs/{id}/debug`,自己这条链一直没有 —— 排障只能靠截图,而截图里恰恰缺最要紧的
    那几样(提示词、工具入参、结果摘要),只能靠猜。

    `format=md` 直接返回可粘贴的 Markdown。**凭证已脱敏**,可以直接转发。
    """
    from cogni_agent.control_plane_service import task_debug as td

    bundle = await td.task_debug(task_id, user_id=user.user_id, role=user.role,
                                 scenic_id=user.scenic_id)
    if (format or "").lower() in ("md", "markdown", "text"):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(td.task_debug_markdown(bundle),
                                 media_type="text/markdown; charset=utf-8")
    return bundle


@router.get("/{task_id}")
async def read_task(task_id: str, user: CurrentUser = Depends(get_current_user)):
    return await get_task(task_id, scenic_id=user.scenic_id)


@router.post("/{task_id}/cancel")
async def cancel(task_id: str, user: CurrentUser = Depends(get_current_user)):
    result = await cancel_task(task_id, scenic_id=user.scenic_id)
    if result.get("status") == "CANCELLED":
        # 真取消:登记进程内取消标记,执行侧感知后中断 LLM 交互/工具调用(APP 同口径);
        # 对终态任务的幂等空转不标记不广播。
        from cogni_agent.runtime_service import streaming
        streaming.mark_user_cancelled(task_id)
        await streaming.publish(task_id, "cancelled", {"message": "任务已取消"})
    return result
