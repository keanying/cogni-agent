"""
M3-02/03/04/07:任务队列——建任务、SKIP LOCKED 认领、老化提升、查询/取消。
这里是「Pull 模式调度」的核心实现,worker/loops.py 里的认领循环直接调用 claim_one()。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_sessionmaker, table_name
from cogni_agent.runtime_service.engine import chart_view
from cogni_agent.common.errors import NotFound
from cogni_agent.common.logging import get_logger, get_trace_id, new_trace_id
from cogni_agent.config import get_settings
from cogni_agent.control_plane_service.models import ACTIVE_STATUSES, Task
from cogni_agent.governance_event_bus_service.events.outbox import emit_event

log = get_logger("control_plane_service.queue")


async def create_task(scenic_id: str | None, source: str, payload: dict, priority: int = 5,
                       executor_type: str = "agent", executor_id: str = "",
                       conversation_id: str | None = None, user_id: str | None = None) -> dict:
    """
    M3-02:内部对话与开放平台调用统一收口到这一个方法,来源只在 source 字段区分。
    scenic_id=None 表示集团级角色发起的任务(不归属具体景区);
    executor_type/executor_id 对应「对话」页的执行目标选择器(agent/skill/tool/mcp);
    user_id 是发起任务的账号——同一个 scenic_id 可能被多个账号绑定(如一个景区
    多个运营人员),任务/成本需要按账号归因,所以落库一列;未显式传时从
    payload.user_id 兜底(兼容历史调用),都没有则存 NULL。
    """
    payload = dict(payload or {})  # 浅拷贝,不把 user_id 回写到调用方的 dict
    # trace_id 链路固化:HTTP 入口(中间件)生成的 trace_id 经 payload 透传到 executor
    # (executor 执行时 set_trace_context(payload.trace_id)),worker 内部 intent/graphs/llm
    # 日志才能按 trace_id 串起全链路;非 HTTP 上下文(如评测内部入队)则生成独立 trace,
    # 避免内部任务日志 trace_id 恒为 "-" 无法检索。
    tid = get_trace_id()
    if not payload.get("trace_id") or payload.get("trace_id") == "-":
        payload["trace_id"] = tid if tid and tid != "-" else new_trace_id()
    user_id = user_id or payload.get("user_id") or ""
    if user_id and not payload.get("user_id"):
        # executor 执行时从 payload 取账号做权限校验(G-AUTH 四级可见性/工具越权),
        # 保证任务元数据与执行上下文里的账号一致。
        payload["user_id"] = user_id
    async with get_sessionmaker()() as db:
        task = Task(scenic_id=scenic_id, source=source, payload=payload, priority=priority, status="QUEUED",
                    executor_type=executor_type, executor_id=executor_id, conversation_id=conversation_id,
                    user_id=user_id or None)
        db.add(task)
        await db.flush()
        await emit_event(db, event_type="com.zhilv.task.state_changed", scenic_id=scenic_id,
                          data={"task_id": task.id, "to": "QUEUED"})
        await db.commit()
        log.info(f"task {task.id} created org={scenic_id} source={source} user={user_id or '-'}")
        return {"id": task.id, "status": task.status}


async def claim_one(worker_id: str) -> dict | None:
    """
    M3-03:SKIP LOCKED 认领一条任务。使用原生 SQL 是因为 SQLAlchemy ORM 的
    `with_for_update(skip_locked=True)` 在异步驱动下语义等价但这里显式写出
    便于团队理解真实生成的 SQL(评审/排障都方便)。
    """
    settings = get_settings()
    lease_seconds = settings.get("control_plane", "lease_seconds", default=600)
    table = table_name("task_queue")

    async with get_sessionmaker()() as db:
        sql = text(
            f"""
            SELECT id FROM {table}
            WHERE status = 'QUEUED'
            ORDER BY priority DESC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        row = (await db.execute(sql)).first()
        if row is None:
            return None
        task_id = row[0]
        lease_until = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="RUNNING", lease_until=lease_until, version=Task.version + 1)
        )
        await emit_event(db, event_type="com.zhilv.task.claimed", scenic_id="", data={"task_id": task_id, "worker": worker_id})
        await db.commit()
        log.info(f"task {task_id} claimed by {worker_id}")
        return {"id": task_id}


async def heartbeat_lease(task_id: str) -> None:
    """M3-05:Worker 每 30s 续租一次,防止租约到期被其它 Worker 重新抢占。"""
    settings = get_settings()
    lease_seconds = settings.get("control_plane", "lease_seconds", default=600)
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Task).where(Task.id == task_id).values(lease_until=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))
        )
        await db.commit()


async def requeue_expired_leases() -> int:
    """M3-05:巡检协程调用,回收超时租约任务,返回回收的任务数。

    以前是把租约过期的 RUNNING 重新置回 QUEUED 等待重跑;配合「心跳只在有进展时
    续租」(worker/loops._heartbeat_until_done)之后,租约过期意味着任务确实卡死/
    异常(而非 worker 只是慢)——此时直接置回 QUEUED 会:① 重复执行工具副作用与
    重复计费;② 多轮会话里重复追加 HumanMessage;③ 若卡死根因(如 Redis 不可用)
    未消失还会再次卡死。因此这里改为直接置为 FAILED 并广播 error 事件:
    前端 SSE 立刻收到终态解锁,用户重新发送即可(Checkpointer 按 task_id 区分,
    重发是全新一轮,语义清晰)。
    """
    rows = []
    async with get_sessionmaker()() as db:
        result = await db.execute(
            select(Task.id, Task.conversation_id)
            .where(Task.status == "RUNNING", Task.lease_until < datetime.now(timezone.utc))
        )
        rows = [(r[0], r[1]) for r in result.fetchall()]
        if rows:
            for task_id, _conv_id in rows:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.status == "RUNNING")
                    .values(
                        status="FAILED",
                        version=Task.version + 1,
                        result={
                            "error": "任务执行中断(租约已过期,可能因执行卡死或进程异常),已自动终止,请重新发送。",
                            "kind": "TASK_INTERRUPTED",
                        },
                    )
                )
            await db.commit()
            log.warning(f"reclaimed expired leases as FAILED: {[r[0] for r in rows]}")
    if rows:
        # 广播 error 事件,让订阅该任务 SSE 的前端立刻解锁(延迟 import 避免循环依赖)
        from cogni_agent.common.schemas import TaskErrorKind, task_error
        from cogni_agent.runtime_service import streaming

        for task_id, _conv_id in rows:
            try:
                await streaming.publish(task_id, "error", task_error(
                    TaskErrorKind.TASK_TIMEOUT,
                    message="任务执行中断(租约已过期),已自动终止,请重新发送",
                ))
            except Exception:  # noqa: BLE001 - 事件广播失败不影响回收本身
                log.warning(f"task {task_id} 回收后广播 error 事件失败", exc_info=True)
    return len(rows)


async def promote_aged_tasks() -> int:
    """M3-04:老化提升——等待超阈值的低优先级任务自动提升一级,防止饿死。"""
    wait_seconds = get_settings().get("control_plane", "aging_wait_seconds", default=300)
    threshold = datetime.now(timezone.utc) - timedelta(seconds=wait_seconds)
    async with get_sessionmaker()() as db:
        result = await db.execute(
            update(Task)
            .where(Task.status == "QUEUED", Task.created_at < threshold, Task.priority < 9)
            .values(priority=Task.priority + 1)
            .returning(Task.id)
        )
        ids = [r[0] for r in result.fetchall()]
        await db.commit()
        return len(ids)


async def get_task(task_id: str, scenic_id: str | None) -> dict:
    """
    M3-07 + G-ISO-01:景区角色查询强制带 scenic_id 过滤(RLS 兜底见迁移脚本)。
    集团级角色(group_admin/super_admin)传入 scenic_id=None 时放开过滤,可查看跨景区任务——
    与管理端任务列表 list_tasks 的口径一致(列表不过滤、详情却看不了,是任务中心页
    "列表 4 条、点开全部 404"的直接原因)。授权边界由路由层角色校验保证:
    scenic_id=None 只可能来自集团级角色(景区角色 token 必带 scenic_id)。
    """
    async with get_sessionmaker()() as db:
        stmt = select(Task).where(Task.id == task_id)
        if scenic_id:
            stmt = stmt.where(Task.scenic_id == scenic_id)
        task = (await db.execute(stmt)).scalar_one_or_none()
        if not task:
            raise NotFound("任务不存在或无权限访问")
        # 读时补全图表的两种形态(option 给 APP / 行列表给管理端)。SSE 断线后前端
        # 走这个接口补终态,那时实时 chart 事件已经错过了 —— 拿到的 result 里图表
        # 缺哪一种形态,对应那端就是空的。补在这里,历史任务同样受益。
        result = task.result
        if isinstance(result, dict) and result.get("structured") is not None:
            result = {**result,
                      "structured": chart_view.normalize_structured(result["structured"])}
        return {"id": task.id, "status": task.status, "user_id": task.user_id or "",
                "payload": task.payload, "result": result}


async def cancel_task(task_id: str, scenic_id: str | None) -> dict:
    """M3-07:取消需校验操作人权限(本人任务或 admin,路由层已做角色校验)。
    集团级角色(scenic_id=None)放开景区过滤,可取消跨景区任务——与 get_task 口径一致。
    只允许取消未结束的任务:终态任务(COMPLETED/FAILED/CANCELLED)返回当前状态幂等
    空转,不再翻状态——否则「对已完成任务调取消」会把 COMPLETED 改写成 CANCELLED。"""
    async with get_sessionmaker()() as db:
        stmt = (update(Task).where(Task.id == task_id, Task.status.in_(ACTIVE_STATUSES))
                .values(status="CANCELLED", version=Task.version + 1).returning(Task.id))
        if scenic_id:
            stmt = stmt.where(Task.scenic_id == scenic_id)
        result = await db.execute(stmt)
        if not result.first():
            # 不在活跃状态:存在则幂等返回当前状态(重复点停止很常见),不存在/无权限才 404
            sel = select(Task.id, Task.status).where(Task.id == task_id)
            if scenic_id:
                sel = sel.where(Task.scenic_id == scenic_id)
            row = (await db.execute(sel)).first()
            if row is None:
                raise NotFound("任务不存在或无权限访问")
            return {"id": task_id, "status": row[1]}
        await db.commit()
        return {"id": task_id, "status": "CANCELLED"}


async def reap_expired_waiting_human() -> int:
    """M3-08:WAITING_HUMAN 超时巡检——防「待审批/待凭证确认」任务永久挂起(逻辑闭环)。

    两种人工介入的挂起点,都只有"用户主动操作"一条出口,缺后台兜底:
    - 对话内凭证确认(payload._credential_confirm):超时检查原本只在用户调用
      credential-confirm 端点时触发,用户一直不操作(前端没收到确认卡片、
      人忘了)任务就永远卡在 WAITING_HUMAN;
    - 审批中心审批(关联 gov_approvals pending):无任何超时机制,审批人一直
      不审任务也永远卡住。

    本巡检由 lease_patrol_loop 周期调用,兜底处理:
    - 凭证确认超时 → 清理明文缓存 + 按「忽略」恢复任务(跳过保存凭证继续对话);
    - 审批超时 → 自动驳回,恢复任务使其取消(CANCELLED)。
    返回本次自动处理的任务数。
    """
    # 延迟 import 避免与 runtime_service/hitl 形成循环依赖
    from cogni_agent.governance_event_bus_service.approvals import Approval, resolve_approval
    from cogni_agent.runtime_service.engine import hitl

    settings = get_settings()
    confirm_ttl = int(settings.get("control_plane", "credential_confirm_ttl_seconds", default=600))
    approval_expire = int(settings.get("control_plane", "approval_expire_seconds", default=3600))
    now = datetime.now(timezone.utc)
    expired: list[tuple[Task, str, str | None]] = []

    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(Task).where(Task.status == "WAITING_HUMAN"))).scalars().all()
        for task in rows:
            cc = (task.payload or {}).get("_credential_confirm")
            if cc:
                created = None
                try:
                    created = datetime.fromisoformat(str(cc.get("created_at") or ""))
                except ValueError:
                    pass
                if created is not None and (now - created).total_seconds() > confirm_ttl:
                    expired.append((task, "credential_confirm", None))
                continue
            approval = (await db.execute(
                select(Approval)
                .where(Approval.related_task_id == task.id, Approval.status == "pending")
                .order_by(Approval.created_at.asc())
                .limit(1)
            )).scalar_one_or_none()
            if approval is not None and approval.created_at is not None \
                    and (now - approval.created_at).total_seconds() > approval_expire:
                expired.append((task, "approval", approval.id))

    for task, kind, approval_id in expired:
        if kind == "credential_confirm":
            log.info(f"task {task.id} 凭证确认已超时(>{confirm_ttl}s),巡检自动忽略并终结任务")
            await hitl.finalize_credential_confirm(task.id, "已超时(巡检兜底)")
        else:
            log.info(f"task {task.id} 关联审批 {approval_id} 已超时(>{approval_expire}s),巡检自动驳回")
            await resolve_approval(approval_id, approved=False, opinion="审批超时,系统自动驳回", resolved_by="system")
    return len(expired)
