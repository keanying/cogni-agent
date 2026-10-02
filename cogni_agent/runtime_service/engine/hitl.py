"""
M4-07:HITL(Human-in-the-loop)——高风险动作 interrupt 中断,转 WAITING_HUMAN;
审批通过/驳回后从同一个 LangGraph Checkpoint 恢复/终止。

真正触发 interrupt() 的代码在 tools_adapter.py(节点内部才能调用 interrupt());
这个模块负责"中断发生后 task_queue/gov_approvals 两张表怎么联动":

- request_approval():executor.py 捕获到图暂停后调用,把 Task 转 WAITING_HUMAN
  并落一条 Approval 记录(审批中心/工作台待审批卡片读的就是这张表)。
- resume_after_approval():governance_event_bus_service.approvals.resolve_approval()
  审批通过/驳回后调用——不管通过还是驳回,都只是把 Task 重新置回 QUEUED 并带上
  "从检查点恢复"的标记,真正"通过->继续执行 / 驳回->任务取消"的判断留给
  executor.py 用 Command(resume=...) 实际驱动图恢复时处理(tools_adapter 里
  approved=False 会抛 ApprovalRejected,executor.py 捕获后才真正落 CANCELLED)。
  这样"审批表怎么更新"和"图怎么恢复"两件事只有一处代码判断审批结果,不会两边
  各写一份判断逻辑导致不一致。
"""
from __future__ import annotations

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.control_plane_service.models import Task
from cogni_agent.governance_event_bus_service.approvals import create_approval

log = get_logger("runtime_service.hitl")


async def request_approval(task_id: str, scenic_id: str | None, applicant_user_id: str, interrupt_payload: dict) -> str:
    """图在高风险工具调用处暂停后,executor.py 调用本函数落地 WAITING_HUMAN + Approval 记录。
    返回创建的 approval_id。"""
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task:
            task.status = "WAITING_HUMAN"
            await db.commit()
    approval_id = await create_approval(
        type_="高风险工具调用",
        risk_level=interrupt_payload.get("risk_level", "high"),
        applicant_user_id=applicant_user_id,
        scenic_id=scenic_id,
        related_task_id=task_id,
        related_resource_type="tool",
        related_resource_id=interrupt_payload.get("tool_id"),
    )
    log.info(f"task {task_id} awaiting approval {approval_id}: {interrupt_payload.get('tool_name')} (risk=high)")
    return approval_id


async def resume_after_approval(task_id: str, approved: bool, opinion: str | None = None,
                                params: dict | None = None) -> None:
    """审批通过或驳回后统一调用——重新入队,携带 resume 标记供 worker/executor 识别。
    真正的"继续执行"还是"取消"由 executor.py 里 Command(resume=approved) 驱动图决定
    (tools_adapter 在驳回时会抛 ApprovalRejected,executor.py 捕获后把 Task 落 CANCELLED)。
    注意:仅适用于「审批中心/高风险工具」类 HITL;对话内凭证确认的忽略/超时走
    finalize_credential_confirm()(B 方案,直接终结,不重新入队)。
    """
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if not task:
            log.warning(f"resume_after_approval: task {task_id} 不存在")
            return
        payload = dict(task.payload or {})
        payload["_resume"] = True
        payload["_resume_approved"] = approved
        payload["_resume_opinion"] = opinion
        # 第三种审批动作:**改参数后批准**。审批人看到「要取消订单 A」时,常见需求是
        # 「改成取消订单 B 再执行」—— 原先只能驳回让用户从头再问一遍。
        # 改过的参数在 tools_adapter 里要重新过 Schema 与租户校验(按**任务的**景区/角色,
        # 不是审批人的:任务恢复后仍以原用户身份执行)。
        if params:
            payload["_resume_params"] = params
        else:
            payload.pop("_resume_params", None)
        task.payload = payload
        task.status = "QUEUED"
        task.version = (task.version or 0) + 1
        await db.commit()
    log.info(f"task {task_id} 已重新入队等待 worker 恢复执行(approved={approved}"
             f"{',改参数 ' + ','.join(sorted(params)) if params else ''})")


async def finalize_credential_confirm(task_id: str, reason: str) -> None:
    """E 方案(B 修正):凭证确认「忽略 / 超时」后任务**直接终结**,不重新入队恢复执行。

    为什么不能照旧 resume_after_approval(approved=False):恢复执行时 agent 从
    checkpoint 继续,在没有用户新输入的情况下会**再次发出同一个「保存凭证」请求**
    → 又 interrupt → 又等待确认,形成无人值守的自动重试循环(直至步数耗尽 FAILED)。

    交互是一次一次的:本轮不保存就结束任务(COMPLETED),用户想再保存直接在对话里
    重新发起并提供账号密码即可。调用方:
    - credential-confirm 端点 action=ignore;
    - credential-confirm 端点检测到已超时;
    - 租约巡检 reap_expired_waiting_human 的凭证确认超时兜底;
    - executor 恢复入口的历史遗留兜底(带 _resume=False 标记却仍入队的)。
    """
    # 延迟 import 避免循环依赖(本模块顶部仅依赖 approvals)
    from cogni_agent.governance_event_bus_service.events.outbox import emit_event
    from cogni_agent.runtime_service import streaming
    from cogni_agent.runtime_service.engine import credential_guard

    credential_guard.cleanup_task(task_id)
    result = {
        "answer": "未保存该工具的账号凭证,本轮任务已结束(不保存、不重试)。如需保存,请重新发起并提供账号密码。",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "timeline": [{"step": "credential_confirm", "verdict": "ignored", "reason": reason}],
    }
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task is None or task.status != "WAITING_HUMAN":
            return
        payload = dict(task.payload or {})
        for key in ("_credential_confirm", "_resume", "_resume_approved", "_resume_opinion"):
            payload.pop(key, None)
        task.payload = payload
        task.status = "COMPLETED"
        task.result = result
        task.version = (task.version or 0) + 1
        await emit_event(db, event_type="com.zhilv.task.state_changed",
                         scenic_id=task.scenic_id or "",
                         data={"task_id": task_id, "to": "COMPLETED"})
        await db.commit()
    log.info(f"task {task_id} 凭证确认{reason},任务直接终结(COMPLETED)")
    await streaming.publish(task_id, "done", result)
