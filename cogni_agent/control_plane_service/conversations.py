"""
多轮会话服务。

会话 = LangGraph Checkpointer 的一个 thread。同一 conversation_id 下的多个任务
共享同一份对话历史,这样"帮我规划武夷山两日游"之后再说"预算再低一点"才有意义
(在此之前每条消息都是一次全新的图运行,上一句说了什么完全不知道)。

**并发保护是这里最关键的一件事**:同一个会话如果同时跑两个任务,两次运行会对同一个
thread 的 checkpoint 交错读写,轻则消息顺序错乱,重则把对方写的状态整段覆盖掉。
LangGraph 的 checkpointer 没有为"同 thread 并发运行"提供隔离保证,所以这里在入口
就挡住:会话里还有未结束的任务时,拒绝新一轮。这比事后修复错乱的历史便宜得多。
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, update

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import Forbidden, NotFound
from cogni_agent.common.logging import get_logger
from cogni_agent.runtime_service.engine import chart_view
from cogni_agent.control_plane_service.models import (
    ACTIVE_STATUSES,
    AppFollowedQuestion,
    AppMessageFeedback,
    Conversation,
    Task,
)

log = get_logger("control_plane.conversations")

_TITLE_MAX = 40


def _make_title(text: str) -> str:
    t = (text or "").strip().replace("\n", " ")
    return (t[:_TITLE_MAX] + "…") if len(t) > _TITLE_MAX else (t or "新会话")


async def get_or_create(
    *, conversation_id: str | None, scenic_id: str | None, user_id: str,
    executor_type: str, executor_id: str, first_message: str,
) -> tuple[Conversation, list[str]]:
    """取回或新建会话。返回 (会话, 诊断信息)。

    诊断信息用于把"你换了执行目标"这类事实带回给调用方——不阻断,但要让人知道
    历史消息是上一个 Agent 产生的。
    """
    diagnostics: list[str] = []
    async with get_sessionmaker()() as db:
        if conversation_id:
            conv = await db.get(Conversation, conversation_id)
            if conv is None:
                raise NotFound(f"会话 {conversation_id} 不存在")
            # 归属校验:会话历史里可能有敏感内容,不能靠传对 id 就读到别人的会话。
            # 只按 owner(user_id)判定可见性——scenic_id 是会话创建时的景区属性,不是访问
            # 控制条件。用户换绑景区后仍应能查看/续聊自己的历史会话(执行上下文沿用会话
            # 自身景区,由调用方用 conv.scenic_id 传递),否则"绑定错景区→换绑"就会丢历史。
            if conv.user_id != user_id:
                raise Forbidden("无权访问该会话")
            if conv.archived:
                raise Forbidden("会话已归档,请新建会话")
            if conv.executor_type != executor_type or conv.executor_id != executor_id:
                # 允许中途换执行目标(换个 Agent 接着聊是合理需求),但要留痕:
                # 历史里的工具调用记录属于上一个目标,当前目标未必绑定了那些工具。
                diagnostics.append(
                    f"本会话创建时绑定的是 {conv.executor_type}:{conv.executor_id},"
                    f"本轮切换为 {executor_type}:{executor_id};历史消息仍会作为上下文带入"
                )
            return conv, diagnostics

        conv = Conversation(
            id=f"conv_{uuid.uuid4().hex}", scenic_id=scenic_id, user_id=user_id,
            executor_type=executor_type, executor_id=executor_id, title=_make_title(first_message),
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv, diagnostics


async def assert_no_active_turn(conversation_id: str) -> None:
    """会话里还有未结束的任务时拒绝新一轮(含 WAITING_HUMAN——确认/审批没处理完不能继续聊)。

    僵尸任务豁免:某个 RUNNING 任务若租约已过期(心跳已停、执行侧认定卡死),说明上一轮
    实际上已经死了,只是状态没落库——若仍按 ACTIVE 拦截,用户会被一个"永远 RUNNING"
    的任务锁死:前端光标一直闪、消息发不出去,且没有任何 UI 能解锁。这里把它回收为
    FAILED 并放行,用户重新发送即恢复(检查点在 task 维度,重发是全新一轮)。
    """
    async with get_sessionmaker()() as db:
        stmt = select(Task.id, Task.status, Task.lease_until).where(
            Task.conversation_id == conversation_id, Task.status.in_(ACTIVE_STATUSES)
        ).order_by(Task.created_at.desc()).limit(1)
        row = (await db.execute(stmt)).first()
    if row:
        task_id, status, lease_until = row
        if status == "WAITING_HUMAN":
            raise Forbidden(f"本会话有一个任务正在等待确认(task={task_id[:8]}),请先在对话中完成确认后再继续对话")
        if status == "RUNNING" and _is_stale_lease(lease_until):
            await _abandon_stale_task(task_id)
            log.warning(f"conversation {conversation_id}: 回收僵尸任务 {task_id}(RUNNING 但租约已过期),放行新一轮")
            return
        raise Forbidden(f"本会话上一轮尚未结束(task={task_id[:8]},状态 {status}),请等待完成后再发送")


def _is_stale_lease(lease_until: datetime | None) -> bool:
    """RUNNING 任务的租约是否已过期(naive/aware 时区都兼容)。"""
    if lease_until is None:
        return False
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    return lease_until < datetime.now(timezone.utc)


async def _abandon_stale_task(task_id: str) -> None:
    """把僵尸任务标记为 FAILED(乐观锁:仅当仍是 RUNNING 时才生效,避免误伤重跑)。"""
    async with get_sessionmaker()() as db:
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


async def bump_turn(conversation_id: str) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Conversation).where(Conversation.id == conversation_id)
            .values(turn_count=Conversation.turn_count + 1, updated_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def set_scenic(conversation_id: str, scenic_id: str | None) -> None:
    """记下这段会话在用哪个景区(集团账号点名/回答澄清之后;景区账号归属变了之后)。

    单独一个写入口,是因为 get_or_create 返回的会话对象来自它自己的 session,
    在调用方身上改 conv.scenic_id 不会落库 —— 改了等于没改,下一轮又被问一遍「哪个景区」。
    """
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Conversation).where(Conversation.id == conversation_id)
            .values(scenic_id=scenic_id, updated_at=datetime.now(timezone.utc)))
        await db.commit()


async def list_conversations(*, user_id: str, limit: int = 50) -> list[dict]:
    """会话列表只按 owner(user_id)过滤,不按景区过滤。

    换绑景区后用户仍要能看到自己的全部历史会话(scenic_id 降级为会话属性,
    由 _as_dict 带出供前端标注所属景区)。景区间的数据隔离由 owner 校验承担——
    会话的 user_id 在创建时固化,换绑不会改变归属。
    """
    async with get_sessionmaker()() as db:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.archived.is_(False))
            .order_by(Conversation.updated_at.desc()).limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()
    return [_as_dict(c) for c in rows]


async def get_conversation(conversation_id: str, *, user_id: str) -> dict:
    async with get_sessionmaker()() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            raise NotFound(f"会话 {conversation_id} 不存在")
        if conv.user_id != user_id:
            raise Forbidden("无权访问该会话")
        return _as_dict(conv)


async def archive(conversation_id: str, *, user_id: str) -> None:
    async with get_sessionmaker()() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            raise NotFound(f"会话 {conversation_id} 不存在")
        if conv.user_id != user_id:
            raise Forbidden("无权访问该会话")
        conv.archived = True
        await db.commit()


_CURSOR_SEP = "|"
_CURSOR_MAX_PAGE = 50
_DEFAULT_PAGE = 10


def _encode_cursor(created_at: datetime, task_id: str) -> str:
    """把 (created_at, task_id) 编码为不透明分页游标。

    客户端把响应里的 next_cursor 当不透明字符串回传即可,无需解析内容。
    """
    raw = f"{created_at.isoformat()}{_CURSOR_SEP}{task_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解码不透明游标为 keyset 边界 (created_at, task_id)。

    非法游标抛 ValueError,由路由层转成 HTTP 400。
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except Exception:
        raise ValueError("invalid cursor")
    ts, sep, task_id = raw.rpartition(_CURSOR_SEP)
    if not sep or not task_id:
        raise ValueError("invalid cursor")
    try:
        return datetime.fromisoformat(ts), task_id
    except ValueError:
        raise ValueError("invalid cursor")


async def list_turns(conversation_id: str, *, user_id: str) -> list[dict]:
    """会话内的历轮任务(用于刷新页面后重建对话列表)。

    这里读的是 task_queue 而不是 Checkpointer:任务表里有每一轮的输入(payload.message)
    与输出(result.answer),结构稳定、查询方便;Checkpointer 的快照是给引擎恢复用的,
    不适合当作展示层的数据源。
    """
    async with get_sessionmaker()() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            raise NotFound(f"会话 {conversation_id} 不存在")
        if conv.user_id != user_id:
            raise Forbidden("无权访问该会话")
        stmt = select(Task).where(Task.conversation_id == conversation_id).order_by(Task.created_at.asc())
        tasks = (await db.execute(stmt)).scalars().all()
        task_ids = [t.id for t in tasks]

        # 一次性批量查点赞状态与关注状态，避免每轮一条查询（N+1）
        feedback_map: dict[str, str] = {}
        if task_ids:
            fb_stmt = select(AppMessageFeedback).where(
                AppMessageFeedback.user_id == user_id,
                AppMessageFeedback.message_id.in_(task_ids),
            )
            for fb in (await db.execute(fb_stmt)).scalars().all():
                feedback_map[fb.message_id] = fb.feedback_type

        followed_map: dict[str, bool] = {}
        if task_ids:
            fq_stmt = select(AppFollowedQuestion).where(
                AppFollowedQuestion.user_id == user_id,
                AppFollowedQuestion.source_message_id.in_(task_ids),
                AppFollowedQuestion.deleted_at.is_(None),
            )
            for fq in (await db.execute(fq_stmt)).scalars().all():
                followed_map[fq.source_message_id] = True
    from cogni_agent.control_plane_service.models import ACTIVE_STATUSES

    out = []
    for t in tasks:
        result = t.result or {}
        # 取消是正常业务终态(用户主动停止),不是错误:不回传 error(内部异常串/任务ID
        # 不该出网),前端据 cancelled 标记渲染「已停止生成」+ answer 部分内容。
        _is_cancelled = t.status == "CANCELLED" or bool(result.get("cancelled"))
        out.append({
            "task_id": t.id, "status": t.status,
            "message": (t.payload or {}).get("message", ""),
            "answer": result.get("answer"),
            "error": None if _is_cancelled else result.get("error"),
            "usage": result.get("usage"),
            # 执行链:让人一眼看出这一轮到底干了什么(调了哪些工具、走了哪个模式)
            "timeline": result.get("timeline") or [],
            # 仍在执行中的轮次。前端据此**重新挂回 SSE 流**继续接收,
            # 而不是显示"(该轮未产生回答)"——那条文案在任务还在跑的时候是错的,
            # 它描述的是"跑完了但没产出",两种情况完全不同。
            # 例外:RUNNING 但租约已过期的僵尸任务(执行已死、状态未落库)不视为进行中,
            # 否则前端会永远挂流/锁输入框,和"卡死"时用户看到的"光标闪但发不出"一致。
            "in_progress": t.status in ACTIVE_STATUSES and not (t.status == "RUNNING" and _is_stale_lease(t.lease_until)),
            # 用户主动停止的轮次:前端显示「已停止生成」+ 已产出的部分回答,而非凭空消失
            "cancelled": _is_cancelled,
            # 点赞/踩状态回显(null/like/dislike)：刷新/重进对话后高亮保持一致
            "my_feedback": feedback_map.get(t.id),
            # 该轮是否被用户关注(关注的是问题,按来源任务 id 关联)
            "is_followed": followed_map.get(t.id, False),
            # 引用溯源:知识检索来源明细(回答里 [n] 标注对应),刷新/重进后仍可核验
            "citations": result.get("citations") or [],
            # 结构化问数结果(cards/charts/insight/recommends):与 SSE 流式结构化事件同构,
            # 历史回放据此渲染卡片/图表/解读/关联推荐;无结构化时为 None(纯文本回答/旧数据)
            # 读时补全图表的两种形态:历史记录里有一批图只有 ECharts option、没有
            # 中性行列表(模型自己写结构化块画的那些),管理端吃不了 option,于是
            # 刷新后那些图就是空的。补在读取侧,已落库的旧记录不用迁移也能正常显示。
            "structured": chart_view.normalize_structured(result.get("structured")),
            "created_at": t.created_at,
        })
    return out


async def list_turns_page(
    conversation_id: str, *, user_id: str, limit: int = _DEFAULT_PAGE, cursor: str | None = None,
) -> dict:
    """游标分页版的 list_turns(APP 会话历史用);list_turns 保持全量语义供管理端使用。

    分页对象是「会话内轮次(tasks)」,排序键 (created_at, id)(同毫秒用 id 决胜)。
    keyset 分页稳定不漂移——聊天流是 append-only,新轮次插入不会让翻页重复/漏条
    (offset 页码会漂,所以这里不用 page/offset)。
    - 无 cursor:取「最新」一页(时间倒序取 limit 条);
    - 有 cursor:取比该游标更早的 limit 条(next_cursor 继续往前翻);
    - 服务端倒序取 limit+1 条判断 has_more,再翻转成时间正序返回(UI 直接插顶部即可);
    - 进行中的轮次必为最新一轮(同会话并发在入口被挡),一定落在最近一页,
      断网恢复场景(17 号文档 S4/S7)只看最近页就能找到它。
    """
    limit = max(1, min(int(limit), _CURSOR_MAX_PAGE))
    async with get_sessionmaker()() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None:
            raise NotFound(f"会话 {conversation_id} 不存在")
        if conv.user_id != user_id:
            raise Forbidden("无权访问该会话")

        stmt = select(Task).where(Task.conversation_id == conversation_id)
        if cursor is not None:
            # ValueError 上抛,由路由层转成 400(非法游标)
            ts, task_id = _decode_cursor(cursor)
            stmt = stmt.where(or_(
                Task.created_at < ts,
                and_(Task.created_at == ts, Task.id < task_id),
            ))
        rows_desc = (
            await db.execute(
                stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows_desc) > limit
        tasks = list(reversed(rows_desc[:limit]))  # 时间正序,UI 直接渲染
        task_ids = [t.id for t in tasks]

        # 点赞/关注状态批量富化(与 list_turns 一致,避免每轮一条的 N+1)
        feedback_map: dict[str, str] = {}
        if task_ids:
            fb_stmt = select(AppMessageFeedback).where(
                AppMessageFeedback.user_id == user_id,
                AppMessageFeedback.message_id.in_(task_ids),
            )
            for fb in (await db.execute(fb_stmt)).scalars().all():
                feedback_map[fb.message_id] = fb.feedback_type

        followed_map: dict[str, bool] = {}
        if task_ids:
            fq_stmt = select(AppFollowedQuestion).where(
                AppFollowedQuestion.user_id == user_id,
                AppFollowedQuestion.source_message_id.in_(task_ids),
                AppFollowedQuestion.deleted_at.is_(None),
            )
            for fq in (await db.execute(fq_stmt)).scalars().all():
                followed_map[fq.source_message_id] = True

    from cogni_agent.control_plane_service.models import ACTIVE_STATUSES

    out = []
    for t in tasks:
        result = t.result or {}
        # 取消是正常业务终态:不回传 error(与 list_turns 同口径)
        _is_cancelled = t.status == "CANCELLED" or bool(result.get("cancelled"))
        out.append({
            "task_id": t.id, "status": t.status,
            "message": (t.payload or {}).get("message", ""),
            "answer": result.get("answer"),
            "error": None if _is_cancelled else result.get("error"),
            "usage": result.get("usage"),
            "timeline": result.get("timeline") or [],
            "in_progress": t.status in ACTIVE_STATUSES and not (t.status == "RUNNING" and _is_stale_lease(t.lease_until)),
            # 用户主动停止的轮次:前端显示「已停止生成」+ 已产出的部分回答
            "cancelled": _is_cancelled,
            "my_feedback": feedback_map.get(t.id),
            "is_followed": followed_map.get(t.id, False),
            "citations": result.get("citations") or [],
            # 结构化问数结果(cards/charts/insight/recommends):与 SSE 流式结构化事件同构,
            # APP 上滑加载历史据此回放卡片/图表/解读/关联推荐;纯文本/旧轮次为 None
            # 读时补全图表的两种形态:历史记录里有一批图只有 ECharts option、没有
            # 中性行列表(模型自己写结构化块画的那些),管理端吃不了 option,于是
            # 刷新后那些图就是空的。补在读取侧,已落库的旧记录不用迁移也能正常显示。
            "structured": chart_view.normalize_structured(result.get("structured")),
            # 0074:这一轮实际打给谁(agent / skill)。同一个会话里可能混着两种 ——
            # 固定问题命中会把这轮切到技能执行,历史回放要能看出来。
            "target_type": t.executor_type or "agent",
            "target_id": t.executor_id,
            "created_at": t.created_at,
        })

    next_cursor = None
    if has_more and tasks:
        oldest = tasks[0]  # 正序列表第一条 = 本页最早 → 下一页(更早)的 keyset 边界
        next_cursor = _encode_cursor(oldest.created_at, oldest.id)
    return {"items": out, "has_more": has_more, "next_cursor": next_cursor}


async def get_recent_turns(
    conversation_id: str, *, exclude_task_id: str | None = None, limit: int = 0,
) -> list[dict]:
    """取该会话「本轮之前」最近 limit 轮的用户/助手文本(供 Triager 多轮历史注入)。

    与 list_turns 同源(tasks 表),但不做点赞/关注等展示层装配,只返回归一化消息:
      [{"role": "user", "content": ...}, {"role": "assistant", "content": ...}, ...]
    每轮 = 用户原话(payload.message) + 助手回答(result.answer),时间正序。

    归一化规则(设计文档 §5.2):
    - 无 answer(失败/中断/被熔断):保留 user,assistant 标注"(该轮未完成)",防止
      Triager 把失败的追问/回答当参考;
    - message 为空的系统任务轮:跳过(不占轮数);
    - exclude_task_id(本轮):跳过(不占轮数)。

    limit <= 0 直接返回空(history_rounds=0 时调用方短路,不进 DB)。
    """
    if limit <= 0:
        return []
    async with get_sessionmaker()() as db:
        stmt = (
            select(Task)
            .where(Task.conversation_id == conversation_id)
            .order_by(Task.created_at.desc())
        )
        tasks = (await db.execute(stmt)).scalars().all()

    collected: list[list[dict]] = []  # 每轮 = [user条目, assistant条目](轮内保序)
    turns_seen = 0
    for t in tasks:
        if exclude_task_id is not None and t.id == exclude_task_id:
            continue  # 本轮:不占轮数
        message = (t.payload or {}).get("message") or ""
        if not str(message).strip():
            continue  # 系统任务(无用户消息):不占轮数
        result = t.result or {}
        answer = result.get("answer")
        if answer is not None and not isinstance(answer, str):
            answer = str(answer)
        collected.append([
            {"role": "user", "content": str(message)},
            {"role": "assistant", "content": answer or "(该轮未完成)"},
        ])
        turns_seen += 1
        if turns_seen >= limit:
            break
    # 降序取最近 N 轮后整轮反转为时间正序;按轮反转而非整体反转,
    # 保证每轮内部 user -> assistant 顺序不被倒置。
    collected.reverse()
    return [item for turn in collected for item in turn]


def _as_dict(c: Conversation) -> dict:
    return {
        "id": c.id, "title": c.title, "executor_type": c.executor_type, "executor_id": c.executor_id,
        "turn_count": c.turn_count, "scenic_id": c.scenic_id,
        "created_at": c.created_at, "updated_at": c.updated_at,
    }
