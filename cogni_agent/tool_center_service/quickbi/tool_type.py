"""工具中心 · quickbi 工具类型。

让 Agent / Skill 像绑定普通工具一样绑定智能小Q能力:
    type = "quickbi"
    type_config = {
        "connection_id": "...",          # 空 = 默认连接
        "capability": "dataset_qa",      # auto / dataset_qa / file_qa / dashboard_qa / insight / report
        "target": {"cube_id": "..."},    # 固定的目标数据(仪表板 asset_id / page_id / cube_id / file_id …)
        "fixed_question_id": "",         # 绑定固定问题时,模型只需(可选)提供 params
        "report_wait": false,            # 报告是否等待完成(默认只创建任务并返回 run_id)
        "max_rows": 30,                  # 返回给模型的每张表最大行数
    }

模型看到的参数(input schema)由 capability 决定,见 input_schema()。
"""
from __future__ import annotations

from typing import Any

from cogni_agent.tool_center_service.quickbi import capabilities as cap

QUICKBI_TEMPLATE = {
    "type": "quickbi",
    "label": "Quick BI",
    "summary": "调用 Quick BI:问数(数据集/上传文件)、报表(仪表板)、解读、报告。"
               "推荐在「工具中心 · Quick BI」里一键生成,这里也可以手工配置。",
    "fields": [
        # 这四项原先都是「让人自己填 ID」。而这些 ID 是 UUID —— 人得先去另一个页面找到它、
        # 复制、切回来粘上。填错了不会当场报错,要等到发起提问才炸一句「资产不存在」。
        # 能选就不让填:下面四个 widget 都是选择器,值仍然是同样的 ID,只是不用人去记。
        {"key": "connection_id", "label": "Quick BI 连接", "widget": "qbi-connection-select",
         "required": False, "section": "basic", "placeholder": "留空使用默认连接",
         "description": "留空时使用默认连接(或环境变量连接)。列表只列当前账号能用的连接。"},
        {"key": "capability", "label": "Quick BI 能力", "widget": "select", "required": True, "section": "basic",
         "options": ["dataset_qa", "dashboard_qa", "file_qa", "insight", "report", "auto"], "default": "dataset_qa",
         # 下拉里显示中文:dataset_qa 这种值只有开发看得懂,运营选的时候全靠猜
         "option_labels": {
             "dataset_qa": "问数(数据集)", "dashboard_qa": "报表(仪表板)",
             "file_qa": "问数(上传文件)", "insight": "解读", "report": "报告",
             "auto": "自动路由(按问题意图判断)"},
         "description": "决定这个工具用 Quick BI 的哪种能力。自动路由适合问题类型不固定的场景。"},
        {"key": "target", "label": "目标数据", "widget": "qbi-target-select", "required": False,
         "section": "basic", "default": {},
         "description": "这个工具固定打在哪份数据上:已登记的仪表板 / 数据集,或上传的文件。"
                        "留空时,数据集问数会自动智能选表,其余能力由模型通过参数提供。",
         "example": {"asset_id": "仪表板资产ID"}},
        {"key": "fixed_question_id", "label": "固定问题", "widget": "qbi-fixed-question",
         "required": False, "section": "basic",
         "description": "绑定一条已有的固定问题,或直接填它的 ID。绑定后模型不必自己组问题,"
                        "只需提供问题里的 {{变量}}(params)。"},
        {"key": "report_wait", "label": "等待报告生成完成", "widget": "switch", "required": False, "section": "basic",
         "default": False,
         "description": "报告通常需要数分钟。关闭时工具立即返回 run_id,可在运行记录里跟踪。"},
        {"key": "max_rows", "label": "返回行数上限", "widget": "text", "required": False, "section": "basic",
         "default": "30", "description": "每张结果表返回给模型的最大行数,避免撑爆上下文。"},
    ],
    "sections": [{"id": "basic", "label": "基础信息", "desc": "选择连接、能力与目标数据"}],
    "example": {"capability": "dashboard_qa", "target": {"asset_id": "xxx"}},
    "tutorial": [
        {"title": "推荐接入方式",
         "body": "到「工具中心 · Quick BI」新建连接 → 登记仪表板/上传文件/选择数据集 → 在操练台验证效果 →"
                 "点「生成工具」,系统会自动创建并发布 quickbi 类型工具,再绑定到 Skill 即可。"},
        {"title": "权限",
         "body": "工具以连接里的 user_token 身份访问 Quick BI,可访问的数据范围由 Quick BI 侧授权决定;"
                 "平台侧仍按工具可见范围(private/scenic/group/public)控制谁能调用。"},
    ],
}


def input_schema(cfg: dict) -> dict:
    capability = (cfg or {}).get("capability") or "dataset_qa"
    target = (cfg or {}).get("target") or {}
    if (cfg or {}).get("fixed_question_id"):
        return {"type": "object", "properties": {
            "params": {"type": "object", "description": "固定问题中的 {{变量}} 取值,如 {\"month\": \"2026-08\"};没有变量时不填"},
        }}
    props: dict[str, Any] = {
        "question": {"type": "string", "description": _question_desc(capability)},
    }
    if capability in ("dataset_qa", "auto") and not target.get("cube_id") and not target.get("asset_id"):
        props["cube_name"] = {"type": "string",
                              "description": "数据集名称(可选)。用户明确提到某个数据集时填写,否则留空由 Quick BI 自动选表"}
    if capability in ("dashboard_qa", "insight", "auto") and not (target.get("asset_id") or target.get("page_id")
                                                                   or target.get("dashboard") or target.get("file_id")):
        props["dashboard"] = {"type": "string",
                              "description": "Quick BI 仪表板链接或 pageId。用户在对话中给出仪表板链接时填写"}
    if capability in ("file_qa", "insight", "report", "auto") and not target.get("file_id"):
        props["file_id"] = {"type": "string", "description": "已上传到平台的文件 ID(可选,用户上传了数据文件时填写)"}
    return {"type": "object", "properties": props, "required": ["question"]}


def _question_desc(capability: str) -> str:
    return {
        "dataset_qa": "要查询的数据问题,用自然语言完整描述指标、维度、时间范围,如「2026年8月各景区销售额TOP5」",
        "dashboard_qa": "针对该仪表板的数据问题,如「本月客流环比变化最大的景区是哪个」",
        "file_qa": "针对上传文件的数据问题",
        "insight": "希望解读的方向,如「近期趋势、异常波动和原因」",
        "report": "报告主题与要求,如「生成8月经营分析报告,包含客流、收入和同比」",
    }.get(capability, "用户的数据问题(系统会自动判断用问数、解读还是报告)")


def schema_note(cfg: dict) -> str:
    c = (cfg or {}).get("capability") or "dataset_qa"
    return cap.CAPABILITIES.get(c, {}).get("desc", "")


async def _owner_actor(tool_id: str):
    """工具所有者的身份(角色/景区取用户表当前值)。"""
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.tool_center_service.quickbi import service
    from cogni_agent.tool_center_service.tool.models import Tool
    from cogni_agent.user_center_service.models import User

    async with get_sessionmaker()() as db:
        tool = await db.get(Tool, tool_id)
        if tool is None:
            raise ValueError("工具不存在")
        owner = await db.get(User, tool.owner_user_id)
    if owner is None:
        raise ValueError("工具所有者账号不存在")
    return service.Actor(user_id=owner.id, scenic_id=owner.scenic_id or None, role=owner.role or "",
                         source="console")


async def validate_config(user, type_config: dict) -> None:
    """创建/编辑 quickbi 工具时,按编辑者身份校验配置里引用的连接/资产/文件/固定问题。"""
    from cogni_agent.tool_center_service.quickbi import service
    from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
    from cogni_agent.common.errors import ValidationError

    actor = service.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None, role=user.role, source="console")
    try:
        await service.validate_tool_refs(actor, type_config or {})
    except QuickBIError as exc:
        raise ValidationError(str(exc)) from exc


async def invoke(tool_id: str, type_config: dict, params: dict, *, scenic_id: str | None, role: str,
                 user_id: str | None) -> dict:
    """执行引擎调用入口(由 mcp/protocol.invoke_tool 分发)。返回给模型的精简结果。"""
    from cogni_agent.tool_center_service.quickbi import service
    from cogni_agent.tool_center_service.quickbi.events import ResultAccumulator

    cfg = dict(type_config or {})
    try:
        owner = await _owner_actor(tool_id)
        conn_id, trusted = await service.validate_tool_refs(owner, cfg)
    except Exception as exc:  # noqa: BLE001 - 配置失效按工具错误返回给模型,不炸任务
        return {"ok": False, "error": f"Quick BI 工具配置不可用(按工具所有者权限校验失败):{exc}"}
    # 调用者身份:用于模型运行时传入的 file_id 等引用;工具配置里的引用已按所有者校验,显式信任
    actor = service.Actor(user_id=user_id or f"tool:{tool_id}", scenic_id=scenic_id or None, role=role or "",
                          source="agent", trusted_refs=frozenset(trusted))
    target = {**(cfg.get("target") or {})}
    for k in ("cube_name", "dashboard", "file_id"):
        if params.get(k):
            target.setdefault(k, params[k])
    try:
        max_rows = int(cfg.get("max_rows") or 30)
    except (TypeError, ValueError):
        max_rows = 30
    capability = cfg.get("capability") or "dataset_qa"
    report_async = not bool(cfg.get("report_wait"))

    acc = ResultAccumulator()
    done: dict = {}
    run_id = None
    if cfg.get("fixed_question_id"):
        gen = service.run_fixed(actor, cfg["fixed_question_id"], params=params.get("params") or {},
                                report_async=report_async)
    else:
        question = str(params.get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "缺少 question 参数"}
        # 连接固定为「所有者视角」解析出的连接(配置留空时即所有者的默认连接),不随调用者变化
        # 景区来自任务上下文(执行引擎按任务归属传入):同一个 Agent 在不同景区问,
        # 落到的是各自景区已登记的看板/数据集。
        gen = service.run_stream(actor, connection_id=conn_id,
                                 capability=None if capability == "auto" else capability,
                                 question=question, target=target, mode="tool", report_async=report_async,
                                 scenic_id=scenic_id or None)
    async for e in gen:
        if e["type"] == "run":
            run_id = e["run_id"]
        elif e["type"] == "done":
            done = e
        else:
            acc.add(e)
    ok = done.get("status") in ("success", "running")
    out: dict[str, Any] = {
        "ok": ok,
        "run_id": run_id,
        "capability": done.get("capability") or capability,
        "status": done.get("status"),
        "content": acc.as_llm_text(max_rows=max_rows),
    }
    if done.get("report_url"):
        out["report_url"] = done["report_url"]
    if done.get("status") == "running" and done.get("chat_id"):
        out["content"] += "\n\n报告已开始生成(通常需要数分钟),完成后可在运行记录中查看报告链接。"
        out["chat_id"] = done["chat_id"]
    if not ok:
        out["error"] = done.get("error")
    return out
