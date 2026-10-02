"""M5-10:工具调用统一入口——runtime 只准通过这里调用工具,禁止绕过。"""
from __future__ import annotations

from cogni_agent.common.logging import get_logger
from cogni_agent.governance_event_bus_service.events.outbox import emit_event
from cogni_agent.tool_center_service.mcp.authz import check_tool_authz
from cogni_agent.tool_center_service.mcp.vault import inject_credential
from cogni_agent.tool_center_service.sandbox.base import get_sandbox
from cogni_agent.tool_center_service.tool.models import Tool, ToolCredential

log = get_logger("tool_center_service.mcp")


async def _tool_snapshot_dict(db, tool: Tool) -> dict:
    """工具内容快照兜底(最新已发布 > 最新草稿)。v1.2(0051):主表已无内容列,
    调用配置只在版本快照里。"""
    from cogni_agent.tool_center_service import versioning as ver

    pub = await ver.latest_published_version(db, "tool", tool.id)
    if pub is not None:
        return pub.snapshot or {}
    draft = await ver.latest_draft(db, "tool", tool.id)
    return (draft.snapshot or {}) if draft is not None else {}


async def invoke_tool(
    db,
    tool_id: str,
    scenic_id: str,
    role: str,
    params: dict,
    *,
    user_id: str | None = None,
    scope: str | None = None,
    type_config: dict | None = None,
    tool_type: str | None = None,
    extra_vars: dict | None = None,
) -> dict:
    """
    M5-10 三元鉴权(scenic_id × role × tool_id)-> 取工具配置(type/type_config)->
    凭证注入 -> 沙箱执行。风险分级(high 转 HITL)在 runtime_service 侧的 tools_adapter
    里处理(那里才有 interrupt() 的调用权),这里只管"放行之后怎么真正跑起来"。

    v1.1.10:执行配置默认收口到「解析层已解析的配置」(type_config/tool_type 注入,
    tools_adapter 传 spec 值 —— spec 来源 = pin 版本快照),不再无条件重读主表 ——
    修工具「发布新版本后执行仍用主表创建初值配置」的漂移。
    v1.2(0051):主表内容列已删除,与 Skill/Agent 同构 —— 未注入时从**版本快照**
    (最新已发布 > 最新草稿)兜底配置,不再是读主表实时值(主表已无列)。

    §10.1 M0(v0.1.4):凭证必属具体用户,注入按 (user_id, scope) 走两级降级链
    (见 vault.inject_credential);无凭证/无身份时不注入,由模型引导用户交互提供。
    """
    if not await check_tool_authz(db, tool_id, scenic_id, role):
        raise PermissionError(f"tool {tool_id} not authorized for scenic_id={scenic_id} role={role}")

    tool = await db.get(Tool, tool_id)
    if tool is None:
        raise ValueError(f"tool {tool_id} 不存在")

    cfg = dict(type_config) if type_config is not None else None
    tt = tool_type
    if cfg is None or tt is None:
        snap = await _tool_snapshot_dict(db, tool)
        if cfg is None:
            cfg = dict(snap.get("type_config") or {})
        if tt is None:
            tt = snap.get("type")
    if not tt:
        raise ValueError(f"tool {tool.id} 未解析到工具类型(type),不可调用(请先发布或检查版本快照)")

    type_config, params = await inject_credential(
        tool_id, cfg, params, user_id=user_id, scope=scope,
    )

    if tt == "mcp":
        # MCP 工具**不能**走沙箱。沙箱面向的是"在本机执行一段东西"(HTTP 请求 /
        # 只读 SQL / Python 子进程),而 MCP 工具的语义是"请远端 MCP Server 按协议
        # 帮我执行",必须发 JSON-RPC tools/call。
        #
        # 早先的实现把 MCP 发现来的工具当作 type="http" 导入,这是错的:即便发现
        # 环节能跑通,调用时也会退化成"对 MCP 端点发一个任意 HTTP 请求",服务端
        # 根本不认——MCP 端点只接受 JSON-RPC 报文。
        result = await _invoke_mcp_tool(db, tool, type_config, params, extra_vars=extra_vars)
    elif tt == "quickbi":
        # Quick BI 智能小Q:同 MCP,语义是「请远端服务执行」,不走本机沙箱。
        # 连接凭证在 quickbi 模块内部加密保管,这里不做 {{credential}} 注入。
        from cogni_agent.tool_center_service.quickbi.tool_type import invoke as _invoke_quickbi

        result = await _invoke_quickbi(tool.id, type_config, params, scenic_id=scenic_id or None,
                                       role=role, user_id=user_id)
    else:
        sandbox = get_sandbox()
        # 执行上下文传下去:模板里的 {{scenic_id}} / {{user_id}} / {{today}} 由沙箱渲染。
        # 不传的话 HTTP 工具的 URL 里会留着字面量 {{scenic_id}} 打出去、
        # DB 工具会把它绑成 NULL —— 查出来永远是空集,而且不报错。
        result = await sandbox.execute(
            tool_id=tool_id, tool_type=tt, type_config=type_config, params=params,
            context={"scenic_id": scenic_id, "user_id": user_id, "role": role,
                     "org_id": scenic_id,
                     # 系统参数:模板里写 {{参数标识}} 引用(见 engine/scenic_params)
                     "extra_vars": extra_vars or {}},
        )

    await emit_event(db, event_type="com.zhilv.tool.called", scenic_id=scenic_id, data={"tool_id": tool_id, "tool_type": tt})
    # invoke_tool 是这个共享 db 会话(整条任务执行期间由 executor.py 打开)上唯一会写
    # evt_outbox 的地方,这里提交,避免事件随任务级会话的其它只读操作一直悬空到最后才落盘。
    await db.commit()
    return result


async def _invoke_mcp_tool(db, tool: Tool, type_config: dict, params: dict,
                           extra_vars: dict | None = None) -> dict:
    """把一次工具调用转成远端 MCP Server 的 tools/call。"""
    from cogni_agent.tool_center_service.mcp.client import McpError, call_remote_tool
    from cogni_agent.tool_center_service.mcp.models import McpServer

    server_id = type_config.get("server_id")
    remote_name = type_config.get("remote_tool_name") or tool.name
    if not server_id:
        raise ValueError(f"MCP 工具 {tool.id} 未记录 server_id,无法定位远端 Server")

    server = await db.get(McpServer, server_id)
    if server is None:
        raise ValueError(f"MCP 工具 {tool.id} 关联的 Server {server_id} 已不存在")
    if not server.active:
        raise ValueError(f"MCP Server「{server.name}」已停用")

    auth_header = await _resolve_mcp_auth(db, server)
    # Server 地址里可以写 {{系统参数}}(按调用时的景区取值);未知的占位原样保留
    url = server.url
    if extra_vars and "{{" in (url or ""):
        import re as _re

        url = _re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
                      lambda m: str(extra_vars.get(m.group(1), m.group(0))), url)
    try:
        return await call_remote_tool(url, remote_name, params, auth_header=auth_header)
    except McpError as exc:
        # 远端不可用是"这次调用失败",不是平台故障:原样返回让模型看到并决定要不要
        # 换个工具/重试,而不是把整条任务直接炸掉。
        log.warning(f"MCP 工具 {tool.id} 调用失败:{exc}")
        return {"ok": False, "error": f"远端 MCP 工具调用失败:{exc}"}


async def _resolve_mcp_auth(db, server) -> str | None:
    """按 auth_type 组装 Authorization 头。凭证密文存 tc_credentials,这里解密后使用,
    绝不落日志。"""
    if server.auth_type in (None, "", "none") or not server.credential_id:
        return None
    from cogni_agent.tool_center_service.mcp.vault import decrypt_credential

    cred = await db.get(ToolCredential, server.credential_id)
    if cred is None:
        log.warning(f"MCP Server {server.id} 声明了 auth_type={server.auth_type} 但凭证记录不存在")
        return None
    try:
        secret = decrypt_credential(cred.cipher_text)
    except Exception:  # noqa: BLE001
        log.exception(f"MCP Server {server.id} 的凭证解密失败")
        return None

    if server.auth_type == "bearer":
        return f"Bearer {secret}"
    if server.auth_type == "apikey":
        # 约定:apikey 直接作为 Authorization 头的完整值(需要自定义头名的场景
        # 由 MCP Server 侧适配,这里不引入更多可配置项徒增复杂度)
        return secret
    if server.auth_type == "basic":
        return f"Basic {secret}"
    return secret
