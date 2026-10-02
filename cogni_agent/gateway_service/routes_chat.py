"""M1-01/05/06:内部会话入口,建任务并通过 SSE 流式返回。

对应「对话」页的执行目标选择器(Agent/Skill/Tool/MCP)+ 高级选项(参数覆盖/
模式覆盖)。真正的解析/判定/执行全部发生在 worker 认领任务后异步跑
(runtime_service.engine.executor.execute_task),这个路由只管两件事:
建任务、把 runtime_service.streaming 广播的事件原样转成 SSE 推给前端。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import Forbidden, NotFound
from cogni_agent.config import get_settings
from cogni_agent.control_plane_service import conversations as conv_svc
from cogni_agent.control_plane_service.models import Task
from cogni_agent.control_plane_service.queue import create_task, get_task
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.runtime_service import streaming
from cogni_agent.runtime_service.engine import hitl

from cogni_agent.common.logging import get_logger
from cogni_agent.common.schemas import TaskErrorKind, task_error

log = get_logger("gateway.chat")

router = APIRouter(prefix="/admin/chat", tags=["内部对话"])


class ChatRequest(BaseModel):
    executor_type: str = "agent"  # agent / skill / tool / mcp,对应对话页的执行目标选择器
    executor_id: str
    message: str | None = None  # agent/skill 场景:本轮用户输入
    params: dict | None = None  # tool/mcp 直调调试场景:直接传结构化参数,免去让调用方手工拼 JSON 字符串
    overrides: dict | None = None  # 高级选项:知识源/工具参数/允许的工具范围覆盖(见文档 1.3)
    override_mode: str | None = None  # 高级选项:强制指定 L0/L1/L2(联调用)
    executor_version_id: str | None = None  # Phase 5:显式指定执行目标(agent/skill)的 published 版本行 id(两层版本选择)
    skill_id: str | None = None  # 兼容旧字段:等价于 executor_type=skill,executor_id=skill_id
    # 多轮会话:传已有会话 id 则接着聊(带上历史上下文);传 null 且 new_conversation=true
    # 则开一个新会话;两者都不传 = 一次性任务(无记忆,旧行为)。
    conversation_id: str | None = None
    new_conversation: bool = False


@router.post("")
async def chat(req: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    """建任务,交由 control-plane 排队,runtime worker 异步认领执行。"""
    executor_type = req.executor_type
    executor_id = req.executor_id or req.skill_id or ""
    if req.skill_id and not req.executor_id:
        executor_type = "skill"

    if executor_type in ("tool", "mcp"):
        message_for_engine = json.dumps(req.params or {}, ensure_ascii=False)
    else:
        message_for_engine = req.message or ""

    conversation_id: str | None = None
    conv_diagnostics: list[str] = []
    conv = None
    conv_scenic_before: str | None = None
    if req.conversation_id or req.new_conversation:
        conv, conv_diagnostics = await conv_svc.get_or_create(
            conversation_id=req.conversation_id, scenic_id=user.scenic_id, user_id=user.user_id,
            executor_type=executor_type, executor_id=executor_id, first_message=message_for_engine,
        )
        conversation_id = conv.id
        conv_scenic_before = conv.scenic_id
        # 同一会话不允许并发跑:两次运行会对同一个 Checkpointer thread 交错读写,
        # 轻则消息顺序错乱,重则整段状态被覆盖。入口挡住比事后修复便宜得多。
        await conv_svc.assert_no_active_turn(conversation_id)

    payload = {
        "message": message_for_engine,
        "user_id": user.user_id,
        "role": user.role,
        "overrides": req.overrides,
        "override_mode": req.override_mode,
        "executor_version_id": req.executor_version_id,
    }
    # 本轮景区:景区账号一律用**当前**归属(改过归属的旧会话也跟着改 —— 否则会拿着
    # 已经不归他管的景区去取数);集团账号按「消息里点名 > 本会话此前指定」取,
    # 都没有则留空,由执行层在真用到景区时发起澄清。
    # (早先这里是「跟随会话自身景区、不随换绑漂移」,按产品口径改为以当前归属为准。)
    turn_scenic = user.scenic_id
    if executor_type not in ("tool", "mcp"):
        from cogni_agent.common.db import get_sessionmaker as _gsm
        from cogni_agent.gateway_service import scenic_binding as _sb

        async with _gsm()() as _db:
            turn_scenic, _src = await _sb.resolve_turn_scenic(
                _db, user=user, conv=conv, message=req.message or "")
        if conv is not None and turn_scenic and turn_scenic != conv_scenic_before:
            await conv_svc.set_scenic(conv.id, turn_scenic)
    task = await create_task(
        scenic_id=turn_scenic, source="internal", payload=payload,
        executor_type=executor_type, executor_id=executor_id, conversation_id=conversation_id,
        user_id=user.user_id,
    )
    return {"task_id": task["id"], "status": task["status"],
            "conversation_id": conversation_id, "diagnostics": conv_diagnostics}


# ── 会话管理(「操练台」左侧会话列表靠这几个接口持久化,不再是纯前端状态)──

@router.get("/conversations")
async def list_conversations(user: CurrentUser = Depends(get_current_user)):
    return {"items": await conv_svc.list_conversations(user_id=user.user_id)}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: CurrentUser = Depends(get_current_user)):
    return await conv_svc.get_conversation(conversation_id, user_id=user.user_id)


@router.get("/conversations/{conversation_id}/turns")
async def list_conversation_turns(conversation_id: str, user: CurrentUser = Depends(get_current_user)):
    """会话内历轮问答,用于刷新页面后重建对话。"""
    return {"items": await conv_svc.list_turns(conversation_id, user_id=user.user_id)}


@router.delete("/conversations/{conversation_id}")
async def archive_conversation(conversation_id: str, user: CurrentUser = Depends(get_current_user)):
    await conv_svc.archive(conversation_id, user_id=user.user_id)
    return {"ok": True}


# 终态:到了这几个状态就不会再有新事件了
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
# 多久没有新事件就去查一次任务状态。既当心跳(让前端知道还活着),
# 也是"事件丢了/任务在别处结束了"的兜底
_IDLE_PROBE_SECONDS = 5.0

# E 方案(v0.1.5):对话内凭证确认超时(秒)。超时仍未确认的,按「忽略」自动处理,
# 防止任务永久挂在 WAITING_HUMAN,也避免明文在内存里滞留过久。
# 与 control_plane.credential_confirm_ttl_seconds 同源,租约巡检兜底用的也是它。
_CREDENTIAL_CONFIRM_TTL_SECONDS = int(
    get_settings().get("control_plane", "credential_confirm_ttl_seconds", default=600)
)


def _credential_confirm_of(task: dict) -> dict | None:
    """任务正处于「对话内凭证确认」挂起时返回确认请求信息(不含明文),否则 None。"""
    if task.get("status") != "WAITING_HUMAN":
        return None
    cc = (task.get("payload") or {}).get("_credential_confirm")
    return cc if isinstance(cc, dict) else None


def _status_message(task: dict) -> str:
    if _credential_confirm_of(task):
        return "等待用户在对话中确认保存凭证"
    return {
        "QUEUED": "已排队,等待执行器认领…",
        "CREATED": "已创建,等待入队…",
        "RUNNING": "执行中…",
        "WAITING_HUMAN": "等待人工审批",
    }.get(task.get("status"), task.get("status") or "执行中…")


@router.get("/stream/{task_id}")
async def chat_stream(task_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    """
    M1-05/06:SSE 流式出口 + 断线续传。

    **这个端点对任务状态负责,不只是个被动管道。** 早期实现只是把 streaming.subscribe()
    的事件透传出去,于是下面几种情况前端会一直转圈到 900 秒超时,且没有任何提示:

    - 任务卡在 QUEUED 没被 worker 领取(worker 忙 / 刚重启)
    - 任务在**另一个进程**里执行并结束(未启用 Redis 时事件只在那个进程的内存流里)
    - 客户端连上来时任务其实早就结束了(刷新页面重连、执行很快的任务)
    - 某条异常路径漏发了 error 事件

    现在:连上先回报一次当前状态;之后每隔几秒没有新事件就查一次库,发现已是终态就
    补发终局事件并关闭。用户至少能明确看到"排队中 / 执行中 / 失败了",而不是无限等待。
    """
    last_id = request.headers.get("last-event-id")

    async def _snapshot() -> dict:
        return await get_task(task_id, scenic_id=user.scenic_id)

    def _final_event(task: dict) -> dict:
        status = task.get("status")
        result = task.get("result") or {}
        if status == "COMPLETED":
            return {"event": "done", "data": {"answer": result.get("answer", ""),
                                              "usage": result.get("usage"),
                                              "timeline": result.get("timeline", [])}}
        if status == "CANCELLED":
            # 用户主动停止:终态带已产出的部分回答,管理端断线/兜底同样显示「已停止+半截」
            return {"event": "cancelled", "data": {"message": "任务已取消",
                                                   "answer": result.get("answer", "")}}
        return {"event": "error", "data": task_error(
            result.get("kind") or TaskErrorKind.FAILED,
            message=result.get("error") or "任务已失败,但没有留下具体原因(请查看服务端日志)")}

    async def event_gen():
        # ① 连上先给一次现状:任务可能早就结束了(刷新重连/执行很快),
        #    也可能还在排队——两种情况都要立刻让前端知道,而不是先干等着。
        try:
            task = await _snapshot()
        except Exception:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps(
                task_error(TaskErrorKind.TASK_NOT_FOUND, message="任务不存在或无权访问"), ensure_ascii=False)}
            return

        if task.get("status") in _TERMINAL:
            ev = _final_event(task)
            yield {"event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}
            return

        yield {"event": "status", "data": json.dumps(
            {"status": task.get("status"),
             "message": _status_message(task),
             "credential_confirm": _credential_confirm_of(task)}, ensure_ascii=False)}

        # ② 订阅事件流,同时做空闲探测。
        #
        # **不能直接 asyncio.wait_for(iterator.__anext__(), ...)**:超时会取消这个
        # __anext__ 协程,而取消一个正在运行的异步生成器会让它进入损坏状态——之后再
        # 调用 __anext__ 直接抛 StopAsyncIteration。表现出来就是"心跳发了一次,流就断了"。
        # 正确做法是让订阅在独立任务里跑、把事件塞进队列,这边只对**取队列**做超时,
        # 生成器本身从头到尾不被打断。
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()
        # 终局事件是否已经推给客户端。pump 里推过之后,下面收尾兜底就不能再推一次,
        # 否则前端会收到两条 error / 两条 done。
        terminal_sent = False

        async def _pump() -> None:
            try:
                async for item in streaming.subscribe(task_id, last_id=last_id):
                    await queue.put(item)
            except Exception as exc:  # noqa: BLE001 - 订阅侧异常不能让请求挂死
                log.exception(f"task {task_id} 事件订阅异常")
                await queue.put(exc)
            finally:
                await queue.put(_DONE)

        pump = asyncio.create_task(_pump())
        try:
            while True:
                # request.is_disconnected() 在部分 Starlette/uvicorn 版本下会阻塞等待连接
                # 消息,长任务(LLM 生成几十秒~几分钟)期间会把主循环卡住 → 心跳、终局
                # 事件全部停发 → 前端 120s 无数据超时,消息停在"正在思考"。给它一个极短
                # 超时,只负责"顺带看一眼",永远不阻塞事件下发。
                try:
                    disconnected = await asyncio.wait_for(request.is_disconnected(), timeout=0.1)
                except asyncio.TimeoutError:
                    disconnected = False
                if disconnected:
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_IDLE_PROBE_SECONDS)
                except asyncio.TimeoutError:
                    # 一段时间没有新事件:查库确认任务是不是已经结束了(可能结束在别的
                    # 进程,也可能某条异常路径漏发了事件),同时把状态回报给前端当心跳。
                    try:
                        task = await _snapshot()
                    except Exception:  # noqa: BLE001
                        continue
                    if task.get("status") in _TERMINAL:
                        ev = _final_event(task)
                        yield {"event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}
                        return
                    yield {"event": "status", "data": json.dumps(
                        {"status": task.get("status"),
                         "message": _status_message(task),
                         "credential_confirm": _credential_confirm_of(task)}, ensure_ascii=False)}
                    continue
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    yield {"event": "error", "data": json.dumps(
                        task_error(TaskErrorKind.STREAM_ERROR, message=f"事件流异常:{item}"), ensure_ascii=False)}
                    return
                event_id, event_name, data = item
                if event_name in ("done", "error", "cancelled"):
                    terminal_sent = True
                yield {"event": event_name, "id": event_id,
                       "data": json.dumps(data, ensure_ascii=False, default=str)}
        finally:
            pump.cancel()

        # ③ 流关了但没收到终局事件(比如 worker 侧异常路径漏发),补一个,
        #    否则前端会停在"执行中"再也不动。
        if terminal_sent:
            return
        try:
            task = await _snapshot()
        except Exception:  # noqa: BLE001
            return
        if task.get("status") in _TERMINAL:
            ev = _final_event(task)
            yield {"event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}

    return EventSourceResponse(event_gen())


class CredentialConfirmRequest(BaseModel):
    action: str = "approve"  # approve / ignore


@router.post("/{task_id}/credential-confirm")
async def credential_confirm(task_id: str, req: CredentialConfirmRequest,
                             user: CurrentUser = Depends(get_current_user)):
    """E 方案(v0.1.5):对话内用户确认/忽略保存凭证。

    凭证保存不再走管理员审批中心,由发起对话的用户本人在对话内确认卡片上
    点「确认保存 / 忽略」。端点只回执、不携带任何凭证明文:
    - approve:恢复任务,图从 checkpoint 继续,credential_save 落库;
    - ignore:任务直接终结(COMPLETED,不保存、不重试),用户想再保存重新发起;
    - 超时(默认 10 分钟):自动按「忽略」直接终结,防止任务永久挂起。
    """
    if req.action not in ("approve", "ignore"):
        raise HTTPException(status_code=400, detail="action 只能是 approve 或 ignore")
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task is None or task.scenic_id != user.scenic_id:
            raise NotFound("任务不存在或无权限访问")
        if task.user_id != user.user_id:
            raise Forbidden("只能确认本人发起任务的凭证保存")
        if task.status != "WAITING_HUMAN":
            raise HTTPException(status_code=409, detail=f"任务当前状态 {task.status},不在等待凭证确认状态")
        cc = (task.payload or {}).get("_credential_confirm")
        if not cc:
            raise HTTPException(status_code=409, detail="该任务没有待确认的凭证保存请求")
        try:
            created = datetime.fromisoformat(str(cc.get("created_at") or ""))
        except ValueError:
            created = None
        if created is not None and (datetime.now(timezone.utc) - created).total_seconds() > _CREDENTIAL_CONFIRM_TTL_SECONDS:
            # 超时:B 方案直接终结任务,不再恢复执行(否则 agent 无人值守时反复重试同一保存请求)
            log.info(f"task {task_id} 凭证确认已超时(>{_CREDENTIAL_CONFIRM_TTL_SECONDS}s),自动忽略并终结任务")
            await hitl.finalize_credential_confirm(task_id, "确认已超时")
            return {"ok": True, "action": "expired", "message": "确认已超时,已自动忽略该凭证保存请求,任务已结束"}
    if req.action == "approve":
        await hitl.resume_after_approval(task_id, approved=True)
    else:
        await hitl.finalize_credential_confirm(task_id, "被用户忽略")
    return {"ok": True, "action": req.action,
            "message": "已确认保存该凭证" if req.action == "approve" else "已忽略,不保存该凭证,任务已结束"}


@router.get("/result/{task_id}")
async def chat_result(task_id: str, user: CurrentUser = Depends(get_current_user)):
    """非流式(sync)出口——需要完整结果一次性拿到的场景(脚本调用/批处理)直接轮询这个接口,
    与 SSE 走的是同一个任务/同一套执行引擎,只是"最后一步怎么把结果吐出去"不同(文档 1.2)。"""
    return await get_task(task_id, scenic_id=user.scenic_id)
