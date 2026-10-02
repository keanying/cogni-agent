"""M-链路可视化:给定任务,还原它的执行图拓扑与逐步执行链。

数据源是 Task 表——result.timeline 记录了完整执行步骤(mode_decision /
intent_check / knowledge_search / generation / tool_call / advance_skill …),
前端据此绘制 langgraph 节点链路图 + 步骤时间线,用于排查
"这一轮到底走到哪、卡在哪"(格式错误、工具停用、审批挂起等)。

v1.1 新增 session-info:给定会话 id 聚合该会话全部任务的"有用信息"
(状态/意图分诊/模式/错误/卡死检测),供 AI 助手与运维侧快速定位问题。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import NotFound
from cogni_agent.common.logging import get_logger
from cogni_agent.control_plane_service.models import Conversation, LLMPromptLog, Task
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user

log = get_logger("gateway_service.routes_graph")
router = APIRouter(prefix="/admin/graph", tags=["管理后台 · 链路可视化"])

# timeline step → 图节点 id 的映射(用于标注实际执行过的节点)
_STEP_TO_NODE = {
    "mode_decision": "intent_check",
    "intent_check": "intent_check",
    "knowledge_retrieval": "knowledge",
    "generation": "agent",
    "tool_call": "tools",
    "tool_call_skipped": "tools",
    "tool_start": "tools",
    "tool_executing": "tools",
    "tool_disabled": "tools",
    "stall_finalize": "agent",
    "advance_skill": "advance_skill",
    "skill": "advance_skill",
}


def _graph_template(executor_type: str, mode: str, timeline: list) -> dict:
    """按执行模式返回 langgraph 拓扑模板 + 实际执行过的节点标注。

    模板是静态的图结构(与 graphs.py 的 StateGraph 节点对应),executed 由
    timeline 实际步骤映射,前端据此高亮真实走过的节点与边。
    """
    executed = set()
    for item in timeline:
        node = _STEP_TO_NODE.get(item.get("step") or "")
        if node:
            executed.add(node)

    if executor_type in ("tool", "mcp"):
        # 单工具/MCP 直调:START → run → END
        nodes = [
            {"id": "START", "label": "开始"},
            {"id": "run", "label": "执行工具"},
            {"id": "END", "label": "结束"},
        ]
        edges = [
            {"from": "START", "to": "run"},
            {"from": "run", "to": "END"},
        ]
    elif executor_type == "skill":
        # Skill DAG:层间执行 + 结果合成
        nodes = [
            {"id": "START", "label": "开始"},
            {"id": "intent_check", "label": "意图判定"},
            {"id": "knowledge", "label": "知识检索"},
            {"id": "layer", "label": "技能层执行"},
            {"id": "synthesize", "label": "结果合成"},
            {"id": "END", "label": "结束"},
        ]
        edges = [
            {"from": "START", "to": "intent_check"},
            {"from": "intent_check", "to": "knowledge"},
            {"from": "knowledge", "to": "layer"},
            {"from": "layer", "to": "synthesize"},
            {"from": "synthesize", "to": "END"},
        ]
    else:
        # Agent 型(ReAct 循环):agent ⇄ tools,可升级/切换技能
        nodes = [
            {"id": "START", "label": "开始"},
            {"id": "intent_check", "label": "意图判定"},
            {"id": "knowledge", "label": "知识检索"},
            {"id": "agent", "label": "Agent 推理"},
            {"id": "tools", "label": "工具调用"},
            {"id": "advance_skill", "label": "技能升级"},
            {"id": "END", "label": "结束"},
        ]
        edges = [
            {"from": "START", "to": "intent_check"},
            {"from": "intent_check", "to": "knowledge"},
            {"from": "knowledge", "to": "agent"},
            {"from": "agent", "to": "tools"},
            {"from": "tools", "to": "agent"},
            {"from": "agent", "to": "advance_skill"},
            {"from": "advance_skill", "to": "agent"},
            {"from": "agent", "to": "END"},
        ]
    return {"nodes": nodes, "edges": edges, "executed": sorted(executed)}


@router.get("/trace/{task_id}")
async def graph_trace(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回任务的执行图拓扑与逐步执行链。

    - graph.nodes / graph.edges:该执行模式的 langgraph 拓扑模板;
    - graph.executed:实际执行过的节点 id 集合(timeline 步骤映射);
    - steps:原始 timeline(逐步执行链,含 step/状态/工具/耗时字段)。

    隔离规则与任务中心一致:景区角色强制 scenic_id 精确匹配;
    集团级角色(scenic_id=None)可跨景区查看(运维/排查用途)。
    """
    stmt = select(Task).where(Task.id == task_id)
    if user.scenic_id is not None:
        stmt = stmt.where(Task.scenic_id == user.scenic_id)
    task = (await db.execute(stmt)).scalar_one_or_none()
    if not task:
        raise NotFound("任务不存在或无权限访问")

    result = task.result or {}
    payload = task.payload or {}
    timeline = result.get("timeline") or []

    # mode 优先取 timeline 首步 mode_decision,兜底 payload.mode
    mode = ""
    for item in timeline:
        if item.get("step") == "mode_decision":
            mode = item.get("mode") or ""
            break
    if not mode:
        mode = payload.get("mode") or ""

    error = result.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("error") or str(error)

    return {
        "task_id": task.id,
        "status": task.status,
        "executor_type": task.executor_type,
        "executor_id": task.executor_id,
        "conversation_id": task.conversation_id or "",
        "mode": mode or "unknown",
        "error": error or "",
        "usage": result.get("usage"),
        "graph": _graph_template(task.executor_type, mode, timeline),
        "steps": timeline,
    }


# ── v1.1: 会话信息聚合(AI 助手/运维排查用) ───────────────────────────────

# 任务状态分布统计时用的"进行中"集合,与 models.ACTIVE_STATUSES 保持一致
_ACTIVE_STATUSES = ("CREATED", "QUEUED", "RUNNING", "WAITING_HUMAN")


def _trim(text: str | None, limit: int = 300) -> str:
    """长文本截断,避免聚合接口把整段回答/整条消息都吐出来。"""
    t = (text or "").strip()
    return (t[:limit] + "…") if len(t) > limit else t


def _task_mode(timeline: list, payload: dict) -> str:
    """优先取 timeline 首步 mode_decision,兜底 payload.mode。"""
    for item in timeline:
        if item.get("step") == "mode_decision":
            return item.get("mode") or ""
    return payload.get("mode") or ""


def _task_error(result: dict) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("error") or str(error)
    return _trim(error or "", 300)


def _intent_info(timeline: list) -> dict:
    """从 timeline 提取 Triager/意图判定信息(intent_check 步骤的 payload)。"""
    for item in timeline:
        if item.get("step") == "intent_check":
            return {
                k: item.get(k) for k in (
                    "action", "intent", "target_skills", "missing", "reply", "degraded",
                    "reason", "params_filled",
                )
                if item.get(k) not in (None, "")
            }
    return {}


def _timeline_summary(timeline: list) -> list[dict]:
    """每步只留关键字段(step/状态/工具/耗时/错误),丢弃大 payload。"""
    out = []
    for item in timeline or []:
        out.append({
            "step": item.get("step"),
            "status": item.get("status") or item.get("state") or "",
            # tool 保留内部工具 id 以便定位;tool_display_name 是展示名(中文),
            # 缺失时回落 tool 字段,避免管理后台时间线只剩 tool_e8ede329 这类内部 ID。
            "tool": item.get("tool") or item.get("tool_name") or "",
            "tool_display_name": item.get("tool_display_name")
            or item.get("tool") or item.get("tool_name") or "",
            "duration": item.get("duration"),
            "error": _trim(item.get("error") or "", 120),
        })
    return out


@router.get("/session-info/{conversation_id}")
async def session_info(
    conversation_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """聚合一个会话的全部有用信息,供 AI 助手/运维快速定位问题。

    输入会话 id,返回:
    - conversation:会话元信息(标题/执行目标/轮数/归属);
    - tasks:该会话每一轮任务的摘要(状态/意图分诊/模式/错误/耗时/是否卡死);
    - summary:快速诊断要点(状态分布/进行中/卡死/失败/最新回答)。

    隔离规则:景区角色强制 scenic_id 精确匹配;集团级角色(scenic_id=None,
    super_admin/group_admin 等)可跨景区查看——本接口是运维/排查用途,超管
    需要能看到任意景区的会话才能定位问题,景区角色权限边界不变。
    """
    conv_stmt = select(Conversation).where(Conversation.id == conversation_id)
    if user.scenic_id is not None:
        conv_stmt = conv_stmt.where(Conversation.scenic_id == user.scenic_id)
    conv = (await db.execute(conv_stmt)).scalar_one_or_none()
    if not conv:
        raise NotFound("会话不存在或无权限访问")

    tasks_stmt = select(Task).where(Task.conversation_id == conversation_id)
    if user.scenic_id is not None:
        tasks_stmt = tasks_stmt.where(Task.scenic_id == user.scenic_id)
    tasks_stmt = tasks_stmt.order_by(Task.created_at.asc())
    tasks = (await db.execute(tasks_stmt)).scalars().all()

    now = datetime.now(timezone.utc)
    task_items = []
    status_dist: dict[str, int] = {}
    stuck: list[str] = []
    failed: list[str] = []
    last_answer = ""

    for t in tasks:
        result = t.result or {}
        payload = t.payload or {}
        timeline = result.get("timeline") or []
        in_progress = t.status in _ACTIVE_STATUSES
        # 卡死检测:RUNNING 且租约已过期(worker 心跳停更,等待巡检回收)
        lease_stale = bool(
            t.status == "RUNNING" and t.lease_until and t.lease_until.replace(tzinfo=timezone.utc) < now
        )
        if lease_stale:
            stuck.append(t.id)
        if t.status == "FAILED":
            failed.append(t.id)
        status_dist[t.status] = status_dist.get(t.status, 0) + 1

        answer = result.get("answer")
        if answer:
            last_answer = answer  # 取最新一帧(按 created_at 升序,自然落在最后)

        task_items.append({
            "task_id": t.id,
            "status": t.status,
            "source": t.source,
            "executor_type": t.executor_type,
            "executor_id": t.executor_id,
            "retry_count": t.retry_count,
            "message": _trim(payload.get("message"), 200),
            "mode": _task_mode(timeline, payload) or "unknown",
            "intent": _intent_info(timeline),
            "answer": _trim(answer, 300),
            "error": _task_error(result),
            "usage": result.get("usage"),
            "timeline": _timeline_summary(timeline),
            "in_progress": in_progress,
            "lease_stale": lease_stale,
            "lease_until": t.lease_until,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        })

    latest = task_items[-1] if task_items else None
    return {
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "executor_type": conv.executor_type,
            "executor_id": conv.executor_id,
            "scenic_id": conv.scenic_id,
            "user_id": conv.user_id,
            "turn_count": conv.turn_count,
            "archived": conv.archived,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        },
        "tasks": task_items,
        "summary": {
            "total_turns": len(task_items),
            "status_distribution": status_dist,
            "active_count": sum(1 for i in task_items if i["in_progress"]),
            "stuck_tasks": stuck,     # RUNNING 且租约过期(卡死,等巡检回收)
            "failed_tasks": failed,
            "latest_status": latest["status"] if latest else None,
            "latest_error": latest["error"] if latest else "",
            "last_answer": _trim(last_answer, 300),
        },
    }


@router.get("/session-info/{conversation_id}/llm-prompts")
async def session_llm_prompts(
    conversation_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """v1.1.6:返回会话内每次大模型调用 LLM **真正收到的最终提示词**,供提示词质量分析。

    输入会话 id,按时间升序返回每次 LLM 调用的:
    - step: intent_check / generation / drift_check(调用类型);
    - system_prompt: 完整 system 层提示词(主 prompt + extra_systems 拼接);
    - history: user/assistant/tool 消息 JSON 数组(每条已截断,控制体积)。

    使用场景:排查「提示词里是否有业务无关噪音 / 内部信息泄漏」(如监督员提醒注入、
    工具禁用计数、版本运维信息误入 system)等提示词质量问题。
    隔离规则与 session-info 一致:景区角色强制 scenic_id 精确匹配。
    """
    conv_stmt = select(Conversation).where(Conversation.id == conversation_id)
    if user.scenic_id is not None:
        conv_stmt = conv_stmt.where(Conversation.scenic_id == user.scenic_id)
    conv = (await db.execute(conv_stmt)).scalar_one_or_none()
    if not conv:
        raise NotFound("会话不存在或无权限访问")

    stmt = select(LLMPromptLog).where(LLMPromptLog.conversation_id == conversation_id)
    if user.scenic_id is not None:
        stmt = stmt.where(LLMPromptLog.scenic_id == user.scenic_id)
    stmt = stmt.order_by(LLMPromptLog.created_at.asc(), LLMPromptLog.id.asc())
    logs = (await db.execute(stmt)).scalars().all()

    history = None
    if logs:
        history = []
        for lg in logs:
            try:
                parsed = json.loads(lg.history) if lg.history else []
            except (TypeError, ValueError):
                parsed = []
            history.append(parsed)
    return {
        "conversation_id": conversation_id,
        "total_calls": len(logs),
        "prompts": [
            {
                "task_id": lg.task_id,
                "step": lg.step,
                "model": lg.model,
                "prompt_tokens": lg.prompt_tokens,
                "created_at": lg.created_at,
                "system_prompt": lg.system_prompt,
                "history": (json.loads(lg.history) if lg.history else []),
            }
            for lg in logs
        ],
    }


@router.get("/task-info/{task_id}")
async def task_info(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按任务 id 聚合单任务的有用信息(技能 fetch_task.py 支撑)。

    输入任务 id(chat 返回的 task_id,或会话任务列表里的任务 id),返回:
    - task:该任务完整摘要(状态/意图分诊/模式/错误/耗时/timeline/是否卡死),
      与 session-info 中单轮任务字段同构,可直接复用其解读口径;
    - conversation:所属会话头(标题/景区/创建时间),便于反查上下文与多轮定位。
    使用场景:手头只有任务 id(而非 conv_ 会话 id)时,定位"这个任务走到哪、
    卡在哪、时间花在哪步"(timeline 每步带 duration,按步聚合即可还原耗时分布)。

    隔离规则与 session-info/trace 一致:景区角色强制 scenic_id 精确匹配;
    集团级角色(scenic_id=None)可跨景区查看(运维/排查用途)。
    """
    stmt = select(Task).where(Task.id == task_id)
    if user.scenic_id is not None:
        stmt = stmt.where(Task.scenic_id == user.scenic_id)
    task = (await db.execute(stmt)).scalar_one_or_none()
    if not task:
        raise NotFound("任务不存在或无权限访问")

    conv = None
    if task.conversation_id:
        conv = (await db.execute(
            select(Conversation).where(Conversation.id == task.conversation_id)
        )).scalar_one_or_none()

    result = task.result or {}
    payload = task.payload or {}
    timeline = result.get("timeline") or []
    now = datetime.now(timezone.utc)
    # 卡死检测与 session-info 同口径:RUNNING 且租约过期(worker 心跳停更)
    lease_stale = bool(
        task.status == "RUNNING" and task.lease_until
        and task.lease_until.replace(tzinfo=timezone.utc) < now
    )

    return {
        "task": {
            "task_id": task.id,
            "status": task.status,
            "source": task.source,
            "executor_type": task.executor_type,
            "executor_id": task.executor_id,
            "retry_count": task.retry_count,
            "message": _trim(payload.get("message"), 200),
            "mode": _task_mode(timeline, payload) or "unknown",
            "intent": _intent_info(timeline),
            "answer": _trim(result.get("answer"), 300),
            "error": _task_error(result),
            "usage": result.get("usage"),
            "timeline": _timeline_summary(timeline),
            "lease_stale": lease_stale,
            "lease_until": task.lease_until,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        },
        "conversation": (
            {
                "id": conv.id,
                "title": conv.title,
                "executor_type": conv.executor_type,
                "executor_id": conv.executor_id,
                "scenic_id": conv.scenic_id,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
            } if conv else None
        ),
    }
