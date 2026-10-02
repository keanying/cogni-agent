"""
全局中间件:trace_id 注入(G-LOG-04)+ 基础请求日志(M1-07)+ ApiResponse 自动包裹。

网关层是 trace_id 的唯一生成点:若请求头带了 X-Trace-Id(如内部服务间调用/
压测工具透传),复用该值;否则新生成一个。生成后写入 contextvars,本请求
后续所有函数调用、日志、事件信封(CloudEvents traceparent 字段)、LLM 调用、
工具调用全部读取同一个 trace_id,响应头也回传方便前端/调用方对齐。

ApiResponseMiddleware:对所有 HTTP 200 JSON 响应自动包裹 {code,message,data,trace_id}。
异常路径由 exception_handler 自行返回 ApiResponse.fail,中间件不干预。
"""
from __future__ import annotations

import json
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from cogni_agent.common.logging import get_logger, get_trace_id, new_trace_id, set_trace_context

log = get_logger("gateway_service.middleware")


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
        scenic_id = request.headers.get("X-Org-Id", "-")
        set_trace_context(trace_id, scenic_id)

        started = time.perf_counter()
        log.info(f"→ {request.method} {request.url.path}")
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Trace-Id"] = trace_id
        log.info(f"← {request.method} {request.url.path} status={response.status_code} {elapsed_ms:.1f}ms")
        return response


class ApiResponseMiddleware:
    """对 HTTP 200 JSON 响应自动包一层 ApiResponse 信封（纯 ASGI 中间件）。

    为什么不用 BaseHTTPMiddleware:
    FastAPI 内部使用 _StreamingResponse 而非 JSONResponse,
    BaseHTTPMiddleware 依赖 response.body 会在 TestClient / ASGI 下报
    AttributeError。纯 ASGI 方案在 TestClient 和生产环境(uvi/uvicorn)均工作。

    包裹规则:
    1. 非 JSON Content-Type → 原样返回
    2. status_code >= 300 → 原样返回(exception_handler 已处理)
    3. body 已是 ApiResponse 形状(含 code+message+data) → 原样返回
    4. 其他 → 包裹: {code:0, message:"success", data:原值, trace_id:当前trace}
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 收集响应片段
        status_code = 200
        response_headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []
        completed = False
        # 透传模式:响应头判定为 SSE/非 JSON/错误后开启,之后所有消息边收边发、
        # 不再整段缓冲。**绝不能把 SSE 流也收集起来**:text/event-stream 的每个
        # chunk 都是 more_body=True,一旦开始收集,completed 永远为 False,事件会
        # 全部卡在中间件里直到流结束——前端收到的是"连接 200 但 body 空"。
        passthrough = False

        async def _capture(message):
            nonlocal status_code, response_headers, completed, passthrough
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = list(message.get("headers", []))
                headers_dict = {k.decode("latin-1"): v.decode("latin-1") for k, v in response_headers}
                content_type = headers_dict.get("content-type", "")
                if ("text/event-stream" in content_type
                        or status_code >= 300
                        or (content_type and "application/json" not in content_type)):
                    passthrough = True
                if passthrough:
                    await send(message)
            elif message["type"] == "http.response.body":
                if passthrough:
                    await send(message)
                    return
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    completed = True
            else:
                # http.disconnect / trailers 等:透传模式下原样转发
                if passthrough:
                    await send(message)

        await self.app(scope, receive, _capture)

        if passthrough or not completed:
            return

        raw_body = b"".join(body_chunks)

        # 解析 JSON body
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await send({"type": "http.response.start", "status": status_code, "headers": response_headers})
            await send({"type": "http.response.body", "body": raw_body, "more_body": False})
            return

        # 已是完整 ApiResponse 形状 → 透传
        if isinstance(body, dict) and {"code", "message", "data"}.issubset(body.keys()):
            await send({"type": "http.response.start", "status": status_code, "headers": response_headers})
            await send({"type": "http.response.body", "body": raw_body, "more_body": False})
            return

        # —— 包裹 ——
        from cogni_agent.common.schemas import ApiResponse

        wrapped = ApiResponse.ok(data=body, trace_id=get_trace_id())
        wrapped_raw = json.dumps(wrapped.model_dump(), ensure_ascii=False).encode("utf-8")

        # 重建 headers（去掉旧的 content-length，用新的）
        new_headers = [(k, v) for k, v in response_headers if k.lower() != b"content-length"]
        new_headers.append((b"content-length", str(len(wrapped_raw)).encode("ascii")))

        await send({"type": "http.response.start", "status": 200, "headers": new_headers})
        await send({"type": "http.response.body", "body": wrapped_raw, "more_body": False})
