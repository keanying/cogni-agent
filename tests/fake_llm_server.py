"""
严格校验 OpenAI 消息协议的假模型服务器。

**为什么需要"严格"**:之前那版假服务器只管返回固定响应,不校验请求体,结果把一个
致命 bug 放过去了——引擎在回传历史时丢掉了 assistant.tool_calls 字段,导致 role="tool"
的消息变成孤儿。真实厂商(OpenAI/DeepSeek/通义/vLLM 兼容层)一律 400,但假服务器
照单全收,于是"本地全绿、一接真模型就挂"。

这个版本把厂商的校验规则搬了过来,任何违反协议的请求直接 400 + 明确原因。
宁可在本地测试里失败,也不要在生产上才发现。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ProtocolViolation(Exception):
    pass


def validate_messages(messages: list[dict]) -> None:
    """按 OpenAI Chat Completions 协议校验消息序列。"""
    if not messages:
        raise ProtocolViolation("messages 不能为空")

    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise ProtocolViolation(f"messages[{i}].role 非法: {role!r}")

        if role == "assistant":
            tcs = m.get("tool_calls")
            if tcs is not None:
                if not isinstance(tcs, list) or not tcs:
                    raise ProtocolViolation(f"messages[{i}].tool_calls 必须是非空数组")
                for j, tc in enumerate(tcs):
                    if not tc.get("id"):
                        raise ProtocolViolation(f"messages[{i}].tool_calls[{j}] 缺少 id")
                    if tc.get("type") != "function":
                        raise ProtocolViolation(f"messages[{i}].tool_calls[{j}].type 必须为 'function'")
                    fn = tc.get("function") or {}
                    if not fn.get("name"):
                        raise ProtocolViolation(f"messages[{i}].tool_calls[{j}].function.name 缺失")
                    if not isinstance(fn.get("arguments"), str):
                        raise ProtocolViolation(
                            f"messages[{i}].tool_calls[{j}].function.arguments 必须是字符串(JSON 文本)"
                        )

        if role == "tool":
            # 这就是当初漏掉的那条规则
            if not m.get("tool_call_id"):
                raise ProtocolViolation(f"messages[{i}] role='tool' 缺少 tool_call_id")
            prev = messages[i - 1] if i > 0 else None
            # 向前找最近的 assistant 消息(中间可以夹着其它 tool 消息)
            k = i - 1
            while k >= 0 and messages[k].get("role") == "tool":
                k -= 1
            prev = messages[k] if k >= 0 else None
            if not prev or prev.get("role") != "assistant" or not prev.get("tool_calls"):
                raise ProtocolViolation(
                    f"messages[{i}] role='tool' 必须响应前面一条带 tool_calls 的 assistant 消息"
                    f"(实际前置消息 role={prev.get('role') if prev else None},"
                    f"tool_calls={'有' if prev and prev.get('tool_calls') else '无'})"
                )
            valid_ids = {tc.get("id") for tc in (prev.get("tool_calls") or [])}
            if m["tool_call_id"] not in valid_ids:
                raise ProtocolViolation(
                    f"messages[{i}].tool_call_id={m['tool_call_id']!r} 不在前置 assistant.tool_calls 的 id 集合 {valid_ids} 中"
                )


class Handler(BaseHTTPRequestHandler):
    # 由启动方注入:一个 callable(body) -> dict(OpenAI 响应结构)
    responder = None
    received: list[dict] = []
    violations: list[str] = []

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        Handler.received.append(body)

        try:
            validate_messages(body.get("messages") or [])
        except ProtocolViolation as exc:
            Handler.violations.append(str(exc))
            self._send(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return

        resp = Handler.responder(body) if Handler.responder else _default_responder(body)
        if body.get("stream"):
            self._send_stream(resp)
        else:
            self._send(200, resp)

    def _send_stream(self, resp: dict) -> None:
        """把非流式响应拆成 SSE 分片,模拟真实流式协议(含 tool_calls 分片与 usage)。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        msg = resp["choices"][0]["message"]

        def emit(chunk: dict) -> None:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": resp.get("model", "fake")}
        for ch in (msg.get("content") or ""):
            emit({**base, "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}]})
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            emit({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": idx, "id": tc["id"], "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
            }]}, "finish_reason": None}]})
        emit({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": msg.get("finish_reason", "stop")}],
              "usage": resp.get("usage", {"prompt_tokens": 10, "completion_tokens": 5})})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def _default_responder(body: dict) -> dict:
    return {"model": body.get("model", "fake"), "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def make_server(port: int, responder=None) -> ThreadingHTTPServer:
    Handler.responder = responder
    Handler.received = []
    Handler.violations = []
    # 必须用 Threading 版本:单线程 HTTPServer 会把并发请求排队处理,
    # 导致"引擎有没有真正并发"这件事在测试里根本观察不到(串行的假象来自服务器,不是引擎)。
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
