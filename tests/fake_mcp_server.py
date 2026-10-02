"""
符合 MCP 规范的假 Server,用于在没有真实 MCP 服务的情况下验证客户端实现。

和假模型服务器一样,这个也**严格校验协议**:握手顺序、JSON-RPC 报文格式、
Mcp-Session-Id 回传。宽容的假服务器只会让"本地全绿、一接真服务就挂"重演一次。
"""
from __future__ import annotations

import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = "2025-03-26"

TOOLS = [
    {"name": "get_weather", "description": "查询指定城市天气",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "search_docs", "description": "检索内部文档",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
]


class Handler(BaseHTTPRequestHandler):
    sessions: set[str] = set()
    violations: list[str] = []
    calls: list[dict] = []
    require_auth: str | None = None
    paginate: bool = False
    use_sse: bool = False

    def _send_json(self, code: int, payload: dict | None, extra_headers: dict | None = None) -> None:
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        if payload is not None:
            ctype = "text/event-stream" if Handler.use_sse else "application/json"
            self.send_header("Content-Type", ctype)
            if Handler.use_sse:
                raw = b"event: message\ndata: " + raw + b"\n\n"
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if Handler.require_auth and self.headers.get("Authorization") != Handler.require_auth:
            self._send_json(401, {"jsonrpc": "2.0", "id": body.get("id"),
                                  "error": {"code": -32001, "message": "unauthorized"}})
            return

        if body.get("jsonrpc") != "2.0":
            Handler.violations.append("缺少 jsonrpc:2.0")
        method = body.get("method")

        if method == "initialize":
            sid = uuid.uuid4().hex
            Handler.sessions.add(sid)
            self._send_json(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }}, extra_headers={"Mcp-Session-Id": sid})
            return

        # 握手之后的请求必须带会话 id —— 这正是最容易被漏掉的一条
        sid = self.headers.get("Mcp-Session-Id")
        if method != "notifications/initialized" and sid not in Handler.sessions:
            Handler.violations.append(f"{method} 未携带有效 Mcp-Session-Id")
            self._send_json(400, {"jsonrpc": "2.0", "id": body.get("id"),
                                  "error": {"code": -32600, "message": "missing session"}})
            return

        if method == "notifications/initialized":
            self._send_json(202, None)   # 通知无响应体
            return

        if method == "tools/list":
            cursor = (body.get("params") or {}).get("cursor")
            if Handler.paginate:
                if not cursor:
                    self._send_json(200, {"jsonrpc": "2.0", "id": body["id"],
                                          "result": {"tools": TOOLS[:1], "nextCursor": "p2"}})
                else:
                    self._send_json(200, {"jsonrpc": "2.0", "id": body["id"],
                                          "result": {"tools": TOOLS[1:]}})
            else:
                self._send_json(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": TOOLS}})
            return

        if method == "tools/call":
            params = body.get("params") or {}
            Handler.calls.append(params)
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "get_weather":
                self._send_json(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps({"city": args.get("city"), "temp": 28}, ensure_ascii=False)}],
                    "isError": False}})
            elif name == "boom":
                # 业务级错误:isError=true 而不是 JSON-RPC error
                self._send_json(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                    "content": [{"type": "text", "text": "上游系统维护中"}], "isError": True}})
            else:
                self._send_json(200, {"jsonrpc": "2.0", "id": body["id"],
                                      "error": {"code": -32602, "message": f"unknown tool {name}"}})
            return

        self._send_json(200, {"jsonrpc": "2.0", "id": body.get("id"),
                              "error": {"code": -32601, "message": f"method not found: {method}"}})

    def log_message(self, *a):
        pass


def make_server(port: int, *, require_auth: str | None = None,
                paginate: bool = False, use_sse: bool = False) -> ThreadingHTTPServer:
    Handler.sessions = set()
    Handler.violations = []
    Handler.calls = []
    Handler.require_auth = require_auth
    Handler.paginate = paginate
    Handler.use_sse = use_sse
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
