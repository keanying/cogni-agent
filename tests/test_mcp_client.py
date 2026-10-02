"""MCP JSON-RPC 客户端测试(对着严格校验协议的假 Server 跑)。"""
from __future__ import annotations

import threading

import pytest

from cogni_agent.tool_center_service.mcp.client import McpError, call_remote_tool, discover_tools, ping
from tests.fake_mcp_server import Handler, make_server

_PORT = 8321


@pytest.fixture
def server():
    srv = make_server(_PORT)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def paginated_server():
    srv = make_server(_PORT + 1, paginate=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def sse_server():
    srv = make_server(_PORT + 2, use_sse=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def auth_server():
    srv = make_server(_PORT + 3, require_auth="Bearer secret-token")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


URL = f"http://127.0.0.1:{_PORT}/mcp"


@pytest.mark.asyncio
async def test_discover_tools(server):
    tools = await discover_tools(URL)
    assert [t["name"] for t in tools] == ["get_weather", "search_docs"]
    # inputSchema 必须原样带出来——它就是 function-calling 的 parameters
    assert tools[0]["input_schema"]["required"] == ["city"]
    assert not Handler.violations, f"协议违规:{Handler.violations}"


@pytest.mark.asyncio
async def test_session_id_is_carried(server):
    """握手拿到的 Mcp-Session-Id 必须带到后续请求上,否则规范服务端会拒绝。"""
    await discover_tools(URL)
    assert not Handler.violations, f"协议违规:{Handler.violations}"
    assert Handler.sessions, "服务端应当已建立会话"


@pytest.mark.asyncio
async def test_call_tool_unwraps_content(server):
    r = await call_remote_tool(URL, "get_weather", {"city": "武夷山"})
    assert r["ok"] is True
    # content[].text 里是 JSON 字符串时应解开一层,省得模型再解析一次
    assert r["result"] == {"city": "武夷山", "temp": 28}
    assert Handler.calls[-1]["arguments"] == {"city": "武夷山"}


@pytest.mark.asyncio
async def test_business_error_is_not_exception(server):
    """isError=true 是业务级错误(工具自己报错),应原样带回让模型自纠,
    而不是抛异常把整条任务炸掉。"""
    r = await call_remote_tool(URL, "boom", {})
    assert r["ok"] is False
    assert "维护中" in str(r["result"])


@pytest.mark.asyncio
async def test_protocol_error_raises(server):
    """JSON-RPC error 才是协议级错误,应当抛出。"""
    with pytest.raises(McpError, match="unknown tool"):
        await call_remote_tool(URL, "no_such_tool", {})


@pytest.mark.asyncio
async def test_pagination(paginated_server):
    tools = await discover_tools(f"http://127.0.0.1:{_PORT + 1}/mcp")
    assert [t["name"] for t in tools] == ["get_weather", "search_docs"], "nextCursor 分页必须翻完"


@pytest.mark.asyncio
async def test_sse_response_is_parsed(sse_server):
    """服务端可以选择用 text/event-stream 回包,客户端必须能解。"""
    tools = await discover_tools(f"http://127.0.0.1:{_PORT + 2}/mcp")
    assert len(tools) == 2


@pytest.mark.asyncio
async def test_auth_header_is_sent(auth_server):
    url = f"http://127.0.0.1:{_PORT + 3}/mcp"
    with pytest.raises(McpError):
        await discover_tools(url)   # 不带凭证应失败
    tools = await discover_tools(url, auth_header="Bearer secret-token")
    assert len(tools) == 2


@pytest.mark.asyncio
async def test_ping_reports_failure_without_raising():
    ok, detail = await ping("http://127.0.0.1:59999/nope", timeout=2)
    assert ok is False and detail
