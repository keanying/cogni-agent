"""路由层共用:SSE 输出、首事件预取、上传读取、QuickBIError → HTTP 错误。"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import UploadFile
from sse_starlette.sse import EventSourceResponse

from cogni_agent.common.errors import ValidationError, ZhilvError
from cogni_agent.common.logging import get_logger
from cogni_agent.tool_center_service.quickbi import filesupport as fs
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError

log = get_logger("tool_center_service.quickbi.http")


class QuickBIHttpError(ZhilvError):
    """Quick BI 侧失败映射为 HTTP 错误:配置/入参 400,鉴权 424(上游鉴权失败,不是平台 401),其余 502。"""

    code = "internal_error"

    def __init__(self, exc: QuickBIError):
        status = {"config": 400, "input": 400, "auth": 424, "business": 422, "network": 504}.get(exc.kind, 502)
        super().__init__(str(exc), status_code=status, error_code=4100)
        self.code = "validation_error" if status == 400 else "internal_error"


async def guard(coro):
    """await 一个 service 调用,把 QuickBIError 转成平台统一错误。"""
    try:
        return await coro
    except QuickBIError as exc:
        raise QuickBIHttpError(exc) from exc


class QuickBIInternalError(ZhilvError):
    """执行链路里没预料到的异常(数据库、序列化、代码 bug…)。

    单独一类是为了**带上人话**:裸 500 到了前端只剩一句「请求失败 (500)」,
    用户既不知道问题出在哪一步,也不知道该找谁 —— 而这类故障恰恰是最需要留痕的。
    """

    code = "internal_error"

    def __init__(self, exc: BaseException):
        super().__init__(
            f"Quick BI 执行失败:{type(exc).__name__}: {exc}。"
            "这是平台内部错误,不是提问本身的问题;请把这条消息与「运行记录」里的这次记录发给管理员",
            status_code=500, error_code=4199)


async def primed(agen: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """先取出第一个事件再返回响应:这样鉴权/入参错误能以正常 HTTP 错误返回,而不是塞进 200 的事件流。

    首个事件之前的那段准备工作(读固定问题、解析连接、挑目标数据、落运行记录)一旦抛异常,
    这里是**唯一**的拦截点 —— 漏一种异常类型,前端收到的就是裸 500。所以这里兜全部异常:
    平台自己的错(ZhilvError)原样抛,让它按各自的状态码与文案返回;其余一律包成带人话的内部错误。
    """
    try:
        first = await agen.__anext__()
    except StopAsyncIteration:
        first = None
    except QuickBIError as exc:
        raise QuickBIHttpError(exc) from exc
    except ZhilvError:
        raise                      # 400/403/404 这类本来就有明确文案与状态码
    except Exception as exc:       # noqa: BLE001
        log.exception("quickbi 执行准备阶段异常(首事件之前)")
        raise QuickBIInternalError(exc) from exc

    async def gen():
        if first is not None:
            yield first
        async for e in agen:
            yield e
    return gen()


def sse(agen: AsyncIterator[dict]) -> EventSourceResponse:
    """事件流。流一旦开始,HTTP 状态码就定死是 200 了 —— 之后无论出什么事,
    唯一能告诉前端的方式就是**发一个 error 事件**。这里兜全部异常:漏掉的那种
    会让连接直接断开,前端只看到「答到一半没了」,连原因都没有。"""

    async def event_gen():
        try:
            async for e in agen:
                yield {"event": e.get("type", "message"), "data": json.dumps(e, ensure_ascii=False, default=str)}
        except (QuickBIError, ZhilvError) as exc:
            yield {"event": "error", "data": json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)}
        except asyncio.CancelledError:
            raise                  # 客户端断开:正常取消,不当故障报
        except Exception as exc:   # noqa: BLE001
            log.exception("quickbi 事件流异常")
            yield {"event": "error", "data": json.dumps(
                {"type": "error", "kind": "internal",
                 "message": f"Quick BI 执行中断:{type(exc).__name__}: {exc}。"
                            "这是平台内部错误;本次记录已留在「运行记录」里,可凭它排查"},
                ensure_ascii=False)}
    return EventSourceResponse(event_gen(), ping=15)


async def collect(agen: AsyncIterator[dict]) -> dict:
    """非流式:消费完事件流,返回 {run_id, done, events_count}。"""
    run_id, done, n = None, {}, 0
    try:
        async for e in agen:
            n += 1
            if e["type"] == "run":
                run_id = e.get("run_id")
            elif e["type"] == "done":
                done = e
    except QuickBIError as exc:
        raise QuickBIHttpError(exc) from exc
    return {"run_id": run_id, "done": done, "event_count": n}


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    name = file.filename or "upload"
    content = await file.read(fs.MAX_FILE_BYTES + 1)
    if len(content) > fs.MAX_FILE_BYTES:
        raise ValidationError("文件超过 5MB 上限(Quick BI OpenAPI 限制)")
    return name, content
