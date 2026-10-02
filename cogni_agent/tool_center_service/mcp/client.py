"""
M5-07/08:MCP(Model Context Protocol)客户端——真实的 JSON-RPC 2.0 对接。

**传输选择**:MCP 规范定义了 stdio 与 HTTP 两类传输。本平台是服务端进程,对接的是
"部署在别处的远端 MCP Server",所以只实现 HTTP 传输(Streamable HTTP):向同一个
端点 POST JSON-RPC 请求,响应可能是 `application/json`,也可能是 `text/event-stream`
(服务端选择用 SSE 回包),两种都要能解。stdio 传输面向的是"本机拉起一个子进程当
MCP Server"的桌面客户端场景,与这里的部署形态不符,不做。

**握手顺序**(规范要求,不能省):
    initialize            -> 交换协议版本与能力,服务端可能通过 Mcp-Session-Id 头给出会话 id
    notifications/initialized -> 通知服务端握手完成(这是通知,没有响应)
    tools/list            -> 列工具,可能分页(nextCursor)
    tools/call            -> 调用

**为什么不复用 httpx 的默认行为直接打**:MCP 要求后续请求都带上 initialize 阶段
拿到的 `Mcp-Session-Id`,否则服务端会拒绝(或者当成一个全新会话)。所以这里用一个
持有会话状态的 client 对象,而不是一堆无状态函数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from cogni_agent.common.logging import get_logger

log = get_logger("tool_center_service.mcp.client")

# 客户端声明支持的协议版本。服务端若返回不同版本,只要能继续对话就按服务端的来
# (规范允许协商),真正不兼容时它会直接报错。
PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "cogni-agent", "version": "1.0"}


class McpError(RuntimeError):
    """MCP 交互失败(连接不上/握手失败/服务端返回 JSON-RPC error)。"""


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: dict

    def as_import_spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class McpClient:
    url: str
    auth_header: str | None = None
    timeout: float = 30.0
    session_id: str | None = field(default=None, init=False)
    server_info: dict = field(default_factory=dict, init=False)
    _next_id: int = field(default=0, init=False)

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            # 同时声明两种,让服务端自行决定用普通 JSON 还是 SSE 回包
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.auth_header:
            h["Authorization"] = self.auth_header
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _post(self, client: httpx.AsyncClient, payload: dict, *, expect_response: bool = True) -> Any:
        try:
            resp = await client.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise McpError(f"连接 MCP Server 失败:{exc}") from exc

        # 服务端可能在 initialize 的响应头里下发会话 id,后续请求必须带上
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

        if resp.status_code >= 400:
            raise McpError(f"MCP Server 返回 HTTP {resp.status_code}:{resp.text[:300]}")
        if not expect_response or resp.status_code == 202 or not resp.content:
            # 通知类请求(如 notifications/initialized)没有响应体,202 也是合法的
            return None

        body = self._parse_body(resp)
        if body is None:
            return None
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            raise McpError(f"MCP 调用出错 [{err.get('code')}]:{err.get('message')}")
        return body.get("result") if isinstance(body, dict) else body

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict | None:
        """解析响应体。服务端可能用普通 JSON,也可能用 SSE 逐事件回包。"""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            # SSE:取最后一个带 data 的事件作为 JSON-RPC 响应
            last: dict | None = None
            for block in resp.text.split("\n\n"):
                data = "".join(
                    line[5:].strip() for line in block.splitlines() if line.startswith("data:")
                )
                if not data:
                    continue
                try:
                    last = json.loads(data)
                except json.JSONDecodeError:
                    continue
            return last
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise McpError(f"MCP Server 返回的不是合法 JSON:{resp.text[:200]}") from exc

    async def handshake(self, client: httpx.AsyncClient) -> dict:
        """initialize + notifications/initialized。规范要求先握手再做任何实质调用。"""
        result = await self._post(client, {
            "jsonrpc": "2.0", "id": self._rpc_id(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},           # 我们不提供 roots/sampling 等回调能力
                "clientInfo": _CLIENT_INFO,
            },
        })
        self.server_info = (result or {}).get("serverInfo", {}) if isinstance(result, dict) else {}
        # 这是通知(没有 id、没有响应),失败也不该阻断——部分实现并不严格要求
        try:
            await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                             expect_response=False)
        except McpError as exc:
            log.debug(f"notifications/initialized 未被接受(忽略):{exc}")
        return self.server_info

    async def list_tools(self, client: httpx.AsyncClient) -> list[McpTool]:
        """tools/list,自动翻页(nextCursor)。"""
        tools: list[McpTool] = []
        cursor: str | None = None
        for _ in range(50):  # 翻页上限,防服务端游标不收敛导致死循环
            params = {"cursor": cursor} if cursor else {}
            result = await self._post(client, {
                "jsonrpc": "2.0", "id": self._rpc_id(), "method": "tools/list", "params": params,
            }) or {}
            for item in result.get("tools") or []:
                name = item.get("name")
                if not name:
                    continue
                tools.append(McpTool(
                    name=name,
                    description=item.get("description") or "",
                    # MCP 的 inputSchema 就是一份标准 JSON Schema,可直接用作
                    # function-calling 的 parameters,不需要转换
                    input_schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                ))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, client: httpx.AsyncClient, tool_name: str, arguments: dict) -> dict:
        """tools/call。返回规范化后的结果。

        MCP 的返回是 `{content: [{type, text|data|...}], isError, structuredContent?}`。
        这里把它压成对上层友好的结构:优先用 structuredContent(新版规范里的结构化
        返回),否则把 text 类型的 content 拼起来。`isError=true` 是**业务级错误**
        (工具自己报错),不是协议错误,所以不抛异常,而是原样带回去让模型看到并自纠。
        """
        result = await self._post(client, {
            "jsonrpc": "2.0", "id": self._rpc_id(), "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }) or {}

        if "structuredContent" in result:
            payload: Any = result["structuredContent"]
        else:
            chunks = []
            for item in result.get("content") or []:
                if item.get("type") == "text":
                    chunks.append(item.get("text", ""))
                else:
                    # 图片/资源等非文本内容不塞进上下文,只标注类型,避免 base64 撑爆窗口
                    chunks.append(f"[{item.get('type')} 类型内容,已省略]")
            payload = "\n".join(chunks)
            # 文本本身是 JSON 时解开一层,省得模型再解析一次字符串里的 JSON
            if isinstance(payload, str) and payload.strip().startswith(("{", "[")):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    pass

        return {"ok": not result.get("isError", False), "result": payload}


async def _with_client(url: str, auth_header: str | None, timeout: float, fn):
    mcp = McpClient(url=url, auth_header=auth_header, timeout=timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        await mcp.handshake(http)
        return await fn(mcp, http)


async def discover_tools(server_url: str, auth_header: str | None = None, timeout: float = 30.0) -> list[dict]:
    """按 MCP 协议 tools/list 拉取工具清单,返回可直接入库的 spec 列表。"""
    async def _run(mcp: McpClient, http: httpx.AsyncClient):
        tools = await mcp.list_tools(http)
        log.info(f"MCP Server {server_url} 发现 {len(tools)} 个工具"
                 f"(server={mcp.server_info.get('name', '?')})")
        return [t.as_import_spec() for t in tools]

    return await _with_client(server_url, auth_header, timeout, _run)


async def call_remote_tool(server_url: str, tool_name: str, arguments: dict,
                           auth_header: str | None = None, timeout: float = 30.0) -> dict:
    """调用远端 MCP 工具。

    每次调用都重新握手:MCP 会话是有状态的,但把长连接缓存在进程里会带来会话过期、
    多实例各自持有不同会话、服务端重启后状态错乱等一堆问题。对"一次工具调用"这种
    粒度,重新握手的开销(一次额外往返)远小于维护连接池的复杂度。
    """
    async def _run(mcp: McpClient, http: httpx.AsyncClient):
        return await mcp.call_tool(http, tool_name, arguments)

    return await _with_client(server_url, auth_header, timeout, _run)


async def ping(server_url: str, auth_header: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """连通性自检:握手 + 列一次工具。用于管理后台展示 connect_status。"""
    try:
        async def _run(mcp: McpClient, http: httpx.AsyncClient):
            tools = await mcp.list_tools(http)
            name = mcp.server_info.get("name") or "未命名"
            return True, f"{name},{len(tools)} 个工具"

        return await _with_client(server_url, auth_header, timeout, _run)
    except McpError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
