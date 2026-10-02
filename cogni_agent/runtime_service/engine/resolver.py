"""
M4-02:解析层——把「能力定义」(Agent/Skill/Tool/MCP)+ 本次调用的参数覆盖,解析成
执行引擎可以直接消费的 `ExecutionSpec`。

这是文档 1.1 "系统的工作 = 解析这些定义 + 执行 + 标准化输出" 里"解析"这一半。
调用方只传 `executor_type + executor_id + overrides`,剩下的——权限过滤、参数覆盖
合并、Prompt 变量渲染、函数名规范化去重、Schema 校验、运行模式强先验——全部在这里
一次性算完;graphs.py 拿到 ExecutionSpec 后只管"怎么把这些节点连成图"。

参数覆盖优先级(文档 1.3):运行时覆盖 > Skill/Agent 预置配置 > 系统全局默认值。

**解析期诊断(diagnostics)**:解析过程中所有"降级但不中断"的决定(工具被过滤、
函数名被改写、Schema 不合法、Prompt 变量没渲染上……)都会记进 `ExecutionSpec.diagnostics`,
由 executor 推到时间线上。这类问题以前只写日志——运维在管理后台看到的是"Agent 好像
什么都没做",却完全不知道是因为它绑定的 5 个工具全被权限过滤掉了。让它可见,
比让它安静地降级重要得多。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.logging import get_logger
from cogni_agent.common.time import now_cst
from cogni_agent.config import get_settings
from cogni_agent.runtime_service.engine import naming
from cogni_agent.tool_center_service.agent.models import Agent
from cogni_agent.tool_center_service.skill.models import Skill
from cogni_agent.tool_center_service.skill.prompts import render_prompt
from cogni_agent.tool_center_service.tool.models import Tool
from cogni_agent.tool_center_service.tool.registry import GROUP_ROLES, test_permission
from cogni_agent.tool_center_service.versioning import (
    SCOPE_USER, STATUS_PUBLISHED,
    ResourceAssignment, ResourceVersion,
    latest_published_version as _latest_published_version,
)

log = get_logger("runtime_service.resolver")

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class ResolveError(Exception):
    """解析失败(能力不存在/无权限/配置不合法),executor.py 捕获后直接把任务判 FAILED。

    `code`(可选)携带更细的错误码(如 agent_not_distributed),executor 捕获时将其
    作为任务错误 kind 透出,便于 C 端/前端精确识别场景并给出行引导文案。
    """

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class Diagnostic:
    """一条解析期告警。level: warn(降级但可继续)/ info(仅供了解)。"""

    level: str
    code: str
    message: str
    ref: str | None = None  # 关联的资源 id

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message, "ref": self.ref}


@dataclass
class ToolSpec:
    id: str
    name: str                       # 展示名(可能是中文),用于时间线/审计
    description: str
    type: str                       # http / db / python
    risk_level: str
    json_schema: dict               # 发给模型的 Schema(已剔除被强制覆盖的参数)
    type_config: dict
    # 校验实参用的完整 Schema(含被 forced_params 覆盖掉的参数)。留空则回落到
    # json_schema —— 这样直接构造 ToolSpec 的调用方(测试、旧代码)不必关心这个字段。
    validate_schema: dict = field(default_factory=dict)
    allow_override: bool = True
    forced_params: dict = field(default_factory=dict)
    # 参数来源声明(结构化扩展):{参数名: {"tool": 前置工具, "key": 传给前置工具的输入,
    # "path": 从前置工具结果提取值的路径}}。来自工具 JSON Schema 属性上的
    # x-param-source 扩展字段——描述「该参数必须由前置工具按指定输入产出,而非模型
    # 臆造」。声明了来源的参数会在解析期从模型可见 Schema 剥离(模型拿不到、填不了、
    # 更不可能填错),执行期由 tools_adapter 自动调用前置工具解析并注入。
    param_sources: dict = field(default_factory=dict)
    # 系统参数引用:参数定义里 default 写成 "{{ota_account}}" 的入参 → {入参名: 参数标识}。
    # 与 forced_params 一样从模型可见 Schema 剥离,执行时按本景区取值填入(见 engine/scenic_params)。
    param_refs: dict = field(default_factory=dict)
    func_name: str = ""             # 模型侧函数名:合法且在本次调用范围内唯一
    model_description: str = ""     # 给模型看的描述(函数名被改写时会带上原始展示名)

    def to_function_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.func_name or self.name,
                "description": self.model_description or self.description or f"调用工具 {self.name}",
                "parameters": self.json_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class ResolvedSkill:
    id: str
    name: str
    system_prompt: str
    tools: list[ToolSpec]
    knowledge_sources: list[str]
    max_steps: int
    max_tool_calls: int
    # 能力一句话概述(人写,面向意图识别路由器的能力清单)。
    # 旧数据无此字段时为 None,由调用方回退 system_prompt 截断。
    description: str | None = None
    suggested_mode: str | None = None
    model: str | None = None            # Skill 级模型覆盖(constraints.model)
    # 32 号:严格取数(constraints.strict_tool_execution)——技能在本轮尚无
    # 工具调用时,首轮 generation 以 tool_choice="required" 协议级强制模型
    # 发起调用(数据查询类技能防「凭上下文直接作答」)。
    strict_tool_execution: bool = False
    # Token 预算(constraints.token_budget):单轮任务累计 prompt+completion 的上限,
    # 超了熔断。None = 该技能没配(走全局默认),0 = 显式「不限制」。出厂默认不限制 ——
    # 消耗永远照常记录,这里只决定要不要在超线时把任务掐掉。
    token_budget: int | None = None
    response_format: dict | None = None  # 结构化输出(constraints.response_format)
    # 知识使用方式(constraints.knowledge_mode):
    #   preload(默认)= 进入 Skill 前自动检索一次,结果作为背景注入系统提示
    #   tool         = 包装成 knowledge_search 工具,由模型自己决定何时检索、检索什么
    #   both         = 两者都要(先给一份背景,模型仍可按需追加检索)
    knowledge_mode: str = "preload"
    # 路由强挂关键词(constraints.route_hit_keywords):Triager 漏挂时的确定性兜底。
    # goal 原文命中任一词即强制把该技能并入 target_skills,高频技能(如客流分析)
    # 绝不因一次分诊误判被裁剪掉。关键词是技能侧业务配置(管理后台 constraints),
    # executor 只做通用合并,不参与 Triager 判定 —— 框架级意图路由保持干净。
    route_hit_keywords: list[str] = field(default_factory=list)


@dataclass
class ExecutionSpec:
    executor_type: str
    executor_id: str
    persona: dict | None = None
    skill_chain: list[ResolvedSkill] = field(default_factory=list)
    composition_hint: str | None = None  # "chain" / "dag" / "pool" / None
    skill_deps: dict[str, list[str]] = field(default_factory=dict)
    direct_tool: ToolSpec | None = None
    suggested_mode: str | None = None
    model_override: str | None = None
    # 单任务生效的 token 预算。三态:
    #   None = 没人配过 -> 用 runtime.guardrails.default_task_token_budget(出厂 0 = 不限制)
    #   0    = 显式不限制(只记账不熔断)
    #   >0   = 累计 prompt+completion 超过它就熔断
    # 来源优先级:payload.overrides.token_budget(离线预热等场景)> 技能链上的
    # constraints.token_budget 最小正值 > 全局默认。
    token_budget: int | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # 意图识别配置(agent 级,v1.1.5):意图识别挂在 Agent 下,一个 Agent 一个意图识别。
    # resolver 从 Agent(或指派版本快照)带出,executor 把它传给 Triager 覆盖框架默认。
    intent_config: dict | None = None
    # v1.1.6(P0-3 会话溯源):解析溯源,记录每个身份的「实际来源」,供排查
    # 「配置改了但执行没跟上」(版本快照漂移/pin 旧版)类问题。13 号文档 Phase 6 起
    # 覆盖到 tool 层(嵌套在所属 skill 下)。形状(来源全显式化,无「主表」兜底):
    #   agent:  {"source": "assigned"|"explicit_snapshot", "version": N, "version_id": "..."}
    #   skills: {skill_id: {"source": "pinned_snapshot"|"explicit_snapshot",
    #                       "version": N, "version_id"?: "...",
    #                       "tools": {tool_id: {"source": "pinned_snapshot", "version": N}}}}
    provenance: dict = field(default_factory=dict)

    def diagnostics_as_dicts(self) -> list[dict]:
        return [d.as_dict() for d in self.diagnostics]


class _Ctx:
    """解析过程中的可变上下文:承载调用方身份与诊断收集,避免层层透传一堆参数。"""

    def __init__(self, *, user_id: str, role: str, scenic_id: str | None, overrides: dict,
                 executor_version_id: str | None = None):
        self.user_id = user_id
        self.role = role
        self.scenic_id = scenic_id
        self.overrides = overrides or {}
        self.executor_version_id = executor_version_id  # Phase 5:调用方显式指定的执行目标版本 id(最高优先级档位 0)
        # §6.3 指派即授权穿透:agent 经 user 指派解析时置 True,下游 skill 解析
        # 不再按 visibility 拦截——管理员把用户指派到 agent 版本,等于显式授予了
        # 该版本依赖锁内全部 skill 的访问权;skill 层再按 visibility 设卡会重现
        # 「钉了版本却不可执行」(与 agent 层同一口径,agent 层已实现,skill 层补齐)。
        self.assigned = False
        # 19 号 S7:固定问题命中即授权——问题库登记了执行技能(23 号前为 route_mode=
        # workflow 的组合)是运营显式配置,等于对该技能依赖锁内的 skill/工具授予执行权。
        # skill 直达任务不走 agent 指派链(assigned=False),无此标志时工具按
        # test_permission 逐个拦截,APP 景区用户对集团工具全部失权 ->
        # skill_no_usable_tools 降级 -> 模型无工具只能文本模拟调用
        # (<|FunctionCallBegin|> 泄漏进最终回答,dev 实测事故)。
        # 穿透范围天然限定在该技能绑定的工具集合内(逐 tool_id 判断)。
        self.question_authorized = False
        self.diagnostics: list[Diagnostic] = []
        self.provenance: dict = {}

    def warn(self, code: str, message: str, ref: str | None = None) -> None:
        self.diagnostics.append(Diagnostic("warn", code, message, ref))
        log.warning(f"[resolve:{code}] {message}")

    def info(self, code: str, message: str, ref: str | None = None) -> None:
        self.diagnostics.append(Diagnostic("info", code, message, ref))


def _global_vars(scenic_id: str | None, user_id: str) -> dict:
    """HTTP 工具「全局变量」+ Prompt 模板变量共用的系统内置变量。"""
    return {
        "now": now_cst().isoformat(),
        "org_id": scenic_id or "group",
        "scenic_id": scenic_id or "",
        "user_id": user_id,
    }


def _visible(visibility: str, owner_user_id: str, res_scenic_id: str | None, *, ctx: _Ctx) -> bool:
    """G-AUTH 四级可见范围判定,与 tool.registry.test_permission 同一套算法,
    供 Skill/Agent(没有专门 registry 判定函数的资源类型)复用。"""
    if owner_user_id == ctx.user_id:
        return True
    if visibility == "public":
        return True
    if visibility == "group" and ctx.role in GROUP_ROLES:
        return True
    if visibility == "scenic" and ctx.scenic_id and res_scenic_id == ctx.scenic_id:
        return True
    return False


# ── Schema 构造与校验 ─────────────────────────────────────────────────────
def _build_json_schema(tool: Tool) -> tuple[dict, bool]:
    """按 Tool.type 从 type_config 推导 function-calling 用的 JSON Schema。"""
    cfg = tool.type_config or {}
    if tool.type == "http":
        schema = cfg.get("params_schema") or {"type": "object", "properties": {}}
        return schema, bool(cfg.get("allow_override", True))
    if tool.type == "db":
        # SQL 模板里的 {{变量}} -> 字符串参数。系统内置变量(now/scenic_id/user_id 等)
        # 由沙箱在执行时注入,不应该反过来要求模型提供,否则模型会瞎编一个 scenic_id
        # 覆盖掉真实租户上下文——这既是正确性问题也是越权风险。
        sql = cfg.get("sql_template", "")
        builtin = set(_global_vars(None, "").keys())
        var_names = sorted({v for v in _VAR_PATTERN.findall(sql) if v not in builtin})
        properties = {v: {"type": "string", "description": f"SQL 模板变量 {{{{{v}}}}}"} for v in var_names}
        return {"type": "object", "properties": properties, "required": var_names}, True
    if tool.type == "python":
        schema = cfg.get("input_schema") or {"type": "object", "properties": {}}
        return schema, True
    if tool.type == "mcp":
        # MCP 发现时把远端的 inputSchema 原样存进了 type_config.input_schema,
        # 它本身就是标准 JSON Schema,直接用。allow_override=False:MCP 工具的参数
        # 由远端定义,平台侧强行注入固定值容易与远端校验冲突。
        return cfg.get("input_schema") or {"type": "object", "properties": {}}, False
    if tool.type == "quickbi":
        # Quick BI 智能小Q:参数由能力(capability)与已固定的目标数据推导,见 quickbi/tool_type.py。
        # allow_override=False:连接/目标数据属于工具配置,不允许被运行时覆盖参数改写。
        from cogni_agent.tool_center_service.quickbi.tool_type import input_schema as _qbi_schema

        return _qbi_schema(cfg), False
    return {"type": "object", "properties": {}}, True


def _validated_schema(schema: dict, *, tool_id: str, tool_name: str, ctx: _Ctx) -> dict:
    """在解析期就校验 Schema 本身合法。

    以前这里不检查,一个手输错的 params_schema 会一路带到模型请求里,厂商回一个
    含糊的 400,排查要翻半天。现在解析期就发现,降级为宽松 Schema 并留一条诊断——
    让 Skill 还能跑(工具参数改为不校验),但运维能立刻看到是哪个工具的 Schema 有问题。
    """
    if not isinstance(schema, dict):
        ctx.warn("tool_schema_invalid", f"工具「{tool_name}」的参数 Schema 不是对象,已降级为不限制参数", tool_id)
        return {"type": "object", "properties": {}}
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        ctx.warn("tool_schema_invalid", f"工具「{tool_name}」的参数 Schema 不合法({exc.message}),已降级为不限制参数", tool_id)
        return {"type": "object", "properties": {}}
    return schema


def _strip_forced_params(schema: dict, forced: dict) -> dict:
    """把被运行时强制覆盖的参数从**模型可见**的 Schema 里摘掉。

    这些参数的值已经定死了,再让模型填一遍纯属浪费 token,还可能让模型误以为
    自己填的值会生效(实际会被 forced_params 覆盖),从而基于错误前提往下推理。
    注意只改模型可见的那份;校验实参时仍用完整 Schema(见 ToolSpec.validate_schema)。
    """
    if not forced or not isinstance(schema, dict):
        return schema
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    stripped = {k: v for k, v in props.items() if k not in forced}
    out = dict(schema)
    out["properties"] = stripped
    if isinstance(schema.get("required"), list):
        out["required"] = [r for r in schema["required"] if r not in forced]
    return out


_X_PARAM_SOURCE = "x-param-source"
# (27 号评审:原 `x-default-rule` 缺省规则收集已移除——缺省值会隐藏本该暴露的
# 缺参问题;参数一律装配期生成完整,缺=由 L2 拦截暴露。历史工具 Schema 里的
# x-default-rule 声明成为无效扩展键,jsonschema 忽略未知关键字,零影响。)


def _relax_ctx_defaults(schema: dict) -> dict:
    """声明了 `x-ctx-default` 的参数:模型**看得见、可以填,但不再是必填**。

    留着必填的话,模型每次都会被推着去填一个景区 ID —— 集团账号的对话里它手上没有,
    就会从上下文里找一个像的、甚至编一个,取回来的是别家的数据而回答看起来完全正常。
    改成可选并写明「不填则用当前景区」,它就只在用户真的点名了某个景区时才填。
    缺省值由执行层按「当前景区 → 澄清」补齐(tools_adapter._apply_ctx_defaults)。
    """
    props = (schema or {}).get("properties") or {}
    ctx_params = [n for n, p in props.items()
                  if isinstance(p, dict) and p.get("x-ctx-default")]
    if not ctx_params:
        return schema
    out = dict(schema)
    out["properties"] = {
        n: ({**p, "description": ((p.get("description") or "").rstrip("。") +
                                  "。不填则使用当前景区;只有用户明确点名了某个景区时才填").lstrip("。")}
            if n in ctx_params else p)
        for n, p in props.items()}
    req = [r for r in (schema.get("required") or []) if r not in ctx_params]
    if req:
        out["required"] = req
    else:
        out.pop("required", None)
    return out


def _collect_param_sources(schema: dict) -> dict[str, dict]:
    """从完整 Schema 收集参数来源声明(`x-param-source` 扩展字段)。

    JSON Schema 允许 `x-` 前缀的厂商扩展字段,校验器会忽略未知关键字,OpenAPI
    生态同款惯例。声明挂在参数属性上,格式:
        {"tool": "<前置工具 func_name 或 name>",
         "key": "<从当前调用参数中取哪个值传给前置工具>",
         "path": "<从前置工具结果提取值的路径,如 data.supplierId;缺省=结果整体>"}
    返回 {参数名: 声明}。声明缺 tool/key 视为配置错误,丢弃并告警(不阻断)。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    out: dict[str, dict] = {}
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        src = prop.get(_X_PARAM_SOURCE)
        if not isinstance(src, dict):
            continue
        tool = str(src.get("tool") or "").strip()
        key = str(src.get("key") or "").strip()
        if not tool or not key:
            log.warning("param_source_invalid",
                        f"Schema 参数 {name} 的 x-param-source 声明缺少 tool/key,已忽略")
            continue
        out[name] = {"tool": tool, "key": key, "path": src.get("path") or "$"}
    return out


def _to_tool_spec(tool: "Tool | SimpleNamespace", *, ctx: _Ctx) -> ToolSpec:
    """把工具(主表 ORM 或 _tool_like_from_snapshot 合成的快照视图)解析为执行用 ToolSpec。

    v1.2(0051):主表无内容列 —— 运行时一律喂快照视图(SimpleNamespace,见
    _tool_like_from_snapshot),其上 description/type/type_config/risk_level 来自版本快照。
    仅剩按属性访问,两者形状一致。
    """
    raw_schema, allow_override = _build_json_schema(tool)
    validate_schema = _validated_schema(raw_schema, tool_id=tool.id, tool_name=tool.name, ctx=ctx)

    tool_param_overrides = ctx.overrides.get("tool_params") or {}
    forced = tool_param_overrides.get(tool.id) or {}
    if forced and not allow_override:
        ctx.warn("tool_override_ignored",
                 f"工具「{tool.name}」不允许外部覆盖参数(allow_override=False),已忽略覆盖 {list(forced.keys())}", tool.id)
        forced = {}

    # 参数来源声明(x-param-source):声明了前置来源的参数从模型可见 Schema 剥离,
    # 执行期由 tools_adapter 自动调用前置工具解析注入 —— 模型无权臆造派生参数。
    param_sources = _collect_param_sources(validate_schema)
    visible = _strip_forced_params(validate_schema, forced)
    visible = _relax_ctx_defaults(visible)
    if param_sources:
        visible = _strip_forced_params(visible, param_sources)
        ctx.info("tool_param_sources",
                 f"工具「{tool.name}」参数 {list(param_sources)} 声明了前置来源,"
                 f"将从模型可见 Schema 剥离并自动解析", tool.id)
    # 引用了系统参数的入参(default="{{参数标识}}"):系统填,模型看不到也不该填
    from cogni_agent.runtime_service.engine.scenic_params import schema_param_refs

    param_refs = schema_param_refs(validate_schema)
    if param_refs:
        visible = _strip_forced_params(visible, param_refs)

    return ToolSpec(
        id=tool.id, name=tool.name, description=tool.description or "", type=tool.type,
        risk_level=tool.risk_level, json_schema=visible,
        validate_schema=validate_schema, type_config=tool.type_config or {},
        allow_override=allow_override, forced_params=dict(forced),
        param_sources=param_sources, param_refs=param_refs,
    )


# ── 版本解析:档位 0/1 —— 显式指定 > user 维度指派;未命中走档位 2 fail-closed ──
async def _published_version(db: AsyncSession, resource_type: str, resource_id: str,
                             version_no: int) -> ResourceVersion | None:
    """按版本号取该资源 published 的版本行;缺失 / 非 published 返回 None。"""
    return (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.version == version_no,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
    )).scalars().first()


async def _published_version_by_id(db: AsyncSession, resource_type: str, resource_id: str,
                                   version_id: str) -> ResourceVersion | None:
    """按版本行 id 取该资源 published 的版本行(Phase 5 显式指定档位用);
    归属不符 / 非 published 返回 None。"""
    return (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.id == version_id,
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
    )).scalars().first()


async def _resolve_assigned_version(db: AsyncSession, agent_id: str, *, ctx: _Ctx) -> ResourceVersion | None:
    """§6.2 档位 1:仅 user 维度的分发指派命中(15 号文档 D9-A:分发收敛为按用户,无 scenic/global)。

    命中版本必须是 published;理论上 §8 禁止下线被指派版本,这里兜底防御:
    非 published -> 记录 warning 并返回 None(由档位 2 以 agent_not_distributed fail-closed 报错)。
    """
    if not ctx.user_id:
        return None
    picked = (await db.execute(
        select(ResourceAssignment).where(
            ResourceAssignment.resource_type == "agent",
            ResourceAssignment.agent_id == agent_id,
            ResourceAssignment.scope_type == SCOPE_USER,
            ResourceAssignment.scope_id == ctx.user_id,
        )
    )).scalars().first()
    if picked is None:
        return None
    ver = await db.get(ResourceVersion, picked.version_id)
    if ver is None or ver.resource_type != "agent" or ver.resource_id != agent_id or ver.status != STATUS_PUBLISHED:
        ctx.warn("agent_assignment_stale",
                 f"Agent {agent_id} 的指派版本已下线/缺失,按未分发处理", agent_id)
        return None
    return ver


async def _resolve_distributed_skill_version(db: AsyncSession, skill_id: str, *,
                                             ctx: _Ctx,
                                             allow_latest_published: bool = False) -> str | None:
    """技能直调时按「用户分发」定版本(0074)。

    优先级:该用户对这个技能的指派版本 > (超管豁免 / 固定问题穿透)最新 published > fail-closed。

    allow_latest_published:这一轮是**运营在后台配好的固定问题**打进来的
    (question_authorized)。固定问题是运营背书过的入口,不是用户随手直调 ——
    没有指派只说明「没人指定跑哪一版」,不该因此跑不起来。

    为什么超管豁免:平台方自己要能随时验证任意技能,否则每验证一个技能都得先给
    自己指派一遍。豁免的是**调用门槛**,不是可见性 —— 可见性仍由 _resolve_skill 判。
    指派命中则置 ctx.assigned(指派即授权,同 agent 口径):被指派的人必然可用,
    否则「钉了版本却不可执行」逻辑说不过去。
    """
    from cogni_agent.tool_center_service.versioning import assigned_version, is_distribution_exempt

    assigned = await assigned_version(db, "skill", skill_id, ctx.user_id)
    if assigned is not None:
        ctx.assigned = True
        ctx.info("skill_version_assigned",
                 f"Skill 按用户指派版本 v{assigned.version} 解析", skill_id)
        return assigned.id

    skill = await db.get(Skill, skill_id)
    name = skill.name if skill else skill_id
    if allow_latest_published or is_distribution_exempt(ctx.role):
        latest = (await db.execute(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == "skill",
                ResourceVersion.resource_id == skill_id,
                ResourceVersion.status == STATUS_PUBLISHED,
            ).order_by(ResourceVersion.version.desc()).limit(1)
        )).scalars().first()
        if latest is None:
            raise ResolveError(
                f"Skill「{name}」还没有已发布的版本,跑不起来 —— "
                f"请在「工具中心 · Skill」里把它发布一个版本")
        ctx.assigned = True        # 走到这里就是被授权过的(运营配的问题 / 超管),可见性不再另判
        ctx.info("skill_version_fallback_published",
                 f"{'固定问题穿透' if allow_latest_published else '超管豁免'}分发校验,"
                 f"按最新已发布版本 v{latest.version} 解析", skill_id)
        return latest.id

    raise ResolveError(
        f"Skill「{name}」未对当前账号开通分发,请联系管理员在『分发中心-按 Skill』按用户指派版本",
        code="skill_not_distributed")


async def _include_param_source_tools(db: AsyncSession, tools: list[ToolSpec], *, ctx: "_Ctx",
                                      skill_name: str, skill_id: str | None) -> list[ToolSpec]:
    """把 x-param-source 声明的前置工具自动带上,不要求运营另外绑定。

    原先执行期只在「本技能绑定的工具」里找前置工具,找不到就报
    「该工具不在本次可用工具中」—— 于是每绑一个要 supplierId 的接口,
    都得记得把「按景区取供应商 ID」也绑上;忘了一次,这个接口就永远调不通,
    而报错指向的是一个运营压根没打算用的工具。依赖是工具自己声明的,
    就该由系统跟着它走。

    **走同一道授权**(_load_authorized_tools):对当前调用方不可见的前置工具照样带不进来,
    不会因为「是依赖」就绕过权限。版本取最新已发布(没有人为它钉过版)。
    """
    from cogni_agent.tool_center_service import builtin_tools as bi

    have = {t.name for t in tools}
    need: set[str] = set()
    for t in tools:
        for src in (getattr(t, "param_sources", None) or {}).values():
            ref = str((src or {}).get("tool") or "").strip()
            # 内置工具(sys_supplier_ids 等)和已被内置取代的老名字:由内置提供,不去库里找
            if ref and ref not in have and ref not in bi.BUILTIN and not bi.superseded_by(ref):
                need.add(ref)
    if not need:
        return tools
    rows = (await db.execute(
        select(Tool).where(Tool.name.in_(sorted(need)), Tool.active.is_(True),
                           Tool.deleted_at.is_(None)))).scalars().all()
    pins: dict[str, int | None] = {}
    for row in rows:
        latest = await _latest_published_version(db, "tool", row.id)
        if latest is not None:
            pins[row.id] = latest.version
    extra = await _load_authorized_tools(db, list(pins), ctx=ctx, skill_name=skill_name,
                                         tool_pins=pins, skill_id=skill_id) if pins else []
    # 同名工具可能不止一条(多个景区各建了一份):按名字只留第一条,避免模型看到两个同名工具
    seen: set[str] = set()
    extra = [e for e in extra if not (e.name in seen or seen.add(e.name))]
    for e in extra:
        ctx.info("param_source_tool_included",
                 f"Skill「{skill_name}」里有工具声明了前置工具「{e.name}」,已自动带上"
                 f"(不需要另外绑定)", e.id)
    for miss in sorted(need - {e.name for e in extra}):
        ctx.warn("param_source_tool_missing",
                 f"Skill「{skill_name}」里有工具需要前置工具「{miss}」自动取参,"
                 f"但它不存在、未发布或对当前账号不可见 —— 依赖它的接口本次会调用失败", skill_id)
    return tools + extra


def _drop_superseded_builtins(tools: list[ToolSpec], *, skill_name: str,
                              skill_id: str | None, ctx: "_Ctx") -> list[ToolSpec]:
    """摘掉已被系统内置工具取代的历史工具行,并在诊断里说清楚该去下线哪一行。

    摘掉而不是改名指过去:内置的那一份下面就会注入,改名只会得到两个同名工具,
    而模型面对两个描述不同、名字相同的工具会随机挑一个 —— 那种错很难复现。
    """
    from cogni_agent.tool_center_service import builtin_tools as bi

    kept, dropped = [], []
    for t in tools:
        alias = bi.superseded_by(t.name)
        if alias:
            dropped.append((t, alias))
            continue
        kept.append(t)
    for t, alias in dropped:
        if t.type != "builtin":
            # 能调通的普通工具(如「全局-通过景区ID获取供应商ID列表」),只是有了内置的同一份:
            # 摘掉免得模型面对两个做同一件事的工具,不算故障,不告警
            ctx.info("tool_superseded_by_builtin",
                     f"Skill「{skill_name}」绑定的工具「{t.name}」已由内置 {alias} 取代,本次改用内置"
                     f"(可以从技能里解绑)", t.id)
            continue
        ctx.warn("tool_superseded_by_builtin",
                 f"Skill「{skill_name}」绑定的工具「{t.name}」已被系统内置工具 {alias} 取代 —— "
                 f"这一行本身调不通(type=builtin 是按工具名找内置函数,找不到中文名),"
                 f"本次已自动改用内置的 {alias}。可以在 Tool 中心把这一行下线了", t.id)
    return kept


def _builtin_specs() -> list[ToolSpec]:
    """内置工具的 ToolSpec。type="builtin",由 tools_adapter 直接调本地函数。"""
    from cogni_agent.tool_center_service import builtin_tools as bi

    return [ToolSpec(id=f"builtin:{n}", name=n, description=spec["description"],
                     type="builtin", risk_level="low",
                     json_schema=spec["schema"], type_config={}, allow_override=False)
            for n, spec in bi.BUILTIN.items()]


def _tool_like_from_snapshot(tool_id: str, name: str, snap: dict) -> SimpleNamespace:
    """用 tool 版本快照(description/type/type_config/risk_level)构造 _to_tool_spec 的输入。

    v1.2(0051):主表已无内容列 —— name/id 是身份字段从主表带过来,内容键只从快照取;
    快照键缺失给保守默认(防御旧格式/异常快照),正常版本的快照必有全键(创建/迁移保证)。
    """
    return SimpleNamespace(
        id=tool_id, name=name,
        description=snap.get("description"),
        type=snap.get("type") or "http",
        type_config=snap.get("type_config") or {},
        risk_level=snap.get("risk_level") or "medium",
    )


def _pin_dict(pins: list, id_key: str) -> dict[str, int | None]:
    """把快照里的 pin 数组 [{id_key, version}] 变成 {resource_id: version|None}。"""
    return {p[id_key]: p.get("version")
            for p in pins if isinstance(p, dict) and p.get(id_key)}


# ── 工具批量加载(避免 N+1) ──────────────────────────────────────────────
async def _load_authorized_tools(db: AsyncSession, tool_ids: list[str], *, ctx: _Ctx,
                                 skill_name: str, tool_pins: dict[str, int | None] | None = None,
                                 skill_id: str | None = None) -> list[ToolSpec]:
    """一次性把绑定的工具查出来再逐个判权,而不是在循环里 db.get()。

    一个绑定 10 个工具的 Skill,原来是 10 次主键查询 + 10 次判权查询;多 Skill 编排
    还要乘以 Skill 数量。批量查回来之后只剩判权那部分是逐个的(它依赖授权表,
    暂时保留)。

    `tool_pins`: skill 版本快照里的工具 pin({tool_id: version})。13 号文档 Phase 3:
    运行时 100% 走版本快照 —— 有 pin 从版本快照解析;无 pin(旧绑定未固化)一律
    视为不可用 warn + 跳过,不再回落主表。pin 版本缺失/下线 -> 同路径 warn + 跳过(§6.4)。

    `skill_id`: 传入时把每个成功解析的工具写入 `provenance.skills[skill_id].tools`
    (tool 级溯源:skill 快照 pin 到的工具实际版本,嵌套在其宿主 skill 下)。
    """
    if not tool_ids:
        return []

    allowed_ids = ctx.overrides.get("allowed_tool_ids")
    rows = (await db.execute(select(Tool).where(Tool.id.in_(tool_ids)))).scalars().all()
    by_id = {t.id: t for t in rows}

    specs: list[ToolSpec] = []
    for tool_id in tool_ids:  # 保持 Skill 登记的顺序,顺序会影响模型的选择倾向
        if allowed_ids is not None and tool_id not in allowed_ids:
            ctx.info("tool_filtered_by_override", f"工具 {tool_id} 不在本次 allowed_tool_ids 白名单内,已排除", tool_id)
            continue
        tool = by_id.get(tool_id)
        if not tool:
            ctx.warn("tool_missing", f"Skill「{skill_name}」绑定的工具 {tool_id} 已不存在,已跳过", tool_id)
            continue
        if not tool.active or getattr(tool, "deleted_at", None):
            # 被内置工具取代的老工具(时间对齐 / 数值计算…):停用/删掉正是预期做法,
            # 它的活由内置工具接着干(内置对每个技能都可用)。报成告警只会让人以为出了问题 ——
            # 现场:删了、也发了新版本,执行详情里还挂着两条「已被禁用」。
            from cogni_agent.tool_center_service.builtin_tools import superseded_by

            repl = superseded_by(tool.name)
            if repl:
                ctx.info("tool_superseded",
                         f"工具「{tool.name}」已停用,由内置 {repl} 取代(无需处理,可从技能里解绑)", tool_id)
            else:
                what = "已被删除" if getattr(tool, "deleted_at", None) else "已被禁用"
                ctx.warn("tool_disabled", f"工具「{tool.name}」{what},本次不可用", tool_id)
            continue
        # §6.3 指派即授权穿透(与 skill 层同口径):agent 经 user 指派解析时,
        # 依赖锁内的工具不再按可见性/授权表拦截——否则 group/scenic/private 工具
        # 会被静默跳过(比 skill 层报错更隐蔽:任务照常跑但模型无真实数据空谈)。
        # 19 号 S7:固定问题命中(skill 直达)同口径——问题库登记=运营显式授权。
        # active 禁用检查不受穿透影响:授予的是「可访问」,不是「绕过停用」。
        if ctx.assigned or ctx.question_authorized:
            ok, reason = True, "指派/问题命中即授权穿透"
        else:
            ok, reason = await test_permission(db, tool_id, ctx.user_id, ctx.role, ctx.scenic_id, action="use")
        if not ok:
            ctx.warn("tool_forbidden", f"工具「{tool.name}」对当前调用方不可见({reason}),已跳过", tool_id)
            continue
        pin = (tool_pins or {}).get(tool_id)
        if pin is not None:
            ver = await _published_version(db, "tool", tool_id, pin)
            if ver is None:
                ctx.warn("tool_pin_missing",
                         f"Skill「{skill_name}」pin 的工具 {tool_id} v{pin} 不存在或未发布,该绑定不可用,已跳过", tool_id)
                continue
            # P0-3 会话溯源(tool 级):工具实际来源 = 宿主 skill 快照 pin 到的版本。
            # name 一并记录(快照优先展示名),前端「执行链分析」按名字渲染,不再只露 ID。
            if skill_id is not None:
                ctx.provenance.setdefault("skills", {}).setdefault(skill_id, {}) \
                    .setdefault("tools", {})[tool_id] = {
                        "source": "pinned_snapshot", "version": pin,
                        "name": ver.snapshot.get("name") or tool.name}
            specs.append(_to_tool_spec(_tool_like_from_snapshot(tool_id, tool.name, ver.snapshot or {}), ctx=ctx))
        else:
            # 无 pin = 界面上那个「跟随最新」。
            #
            # 原先这里一律跳过,理由是「不回落主表」—— 方向没错,但把话说过头了:
            # **跟随最新 ≠ 回落主表**。解析到该工具「最新的已发布版本快照」,
            # 一样是版本化的、可追溯的,版本纪律并没有被破坏。
            #
            # 一律跳过的代价极其隐蔽:技能本身是「已发布」,界面上三个工具好好列着、
            # 还标着「跟随最新」,而运行时它们一个都不在 —— 能力清单里只剩系统内置工具,
            # 模型没有取数工具,只能凭上下文说话,答得像模像样却不是查出来的,
            # 全程不报错(现场任务 2876affe 正是如此)。
            # 界面提供的选项,运行时就该认;认不了就不该让人选。
            ver = await _latest_published_version(db, "tool", tool_id)
            if ver is None:
                ctx.warn("tool_unpublished",
                         f"工具「{tool.name}」一个已发布版本都没有(绑定为「跟随最新」),"
                         f"本次不可用,已跳过 —— 请到工具详情页发布一版", tool_id)
                continue
            if skill_id is not None:
                ctx.provenance.setdefault("skills", {}).setdefault(skill_id, {}) \
                    .setdefault("tools", {})[tool_id] = {
                        "source": "latest_published", "version": ver.version,
                        "name": ver.snapshot.get("name") or tool.name}
            ctx.info("tool_latest_published",
                     f"工具「{tool.name}」按「跟随最新」解析到 v{ver.version}", tool_id)
            specs.append(_to_tool_spec(_tool_like_from_snapshot(tool_id, tool.name, ver.snapshot or {}), ctx=ctx))
    return specs


def _assign_function_names(spec: ExecutionSpec, ctx: _Ctx) -> None:
    """在**整个 ExecutionSpec 范围内**统一分配函数名。

    为什么是全局而不是按 Skill:L2 动态可选池会把多个 Skill 的工具合并到同一个
    tools 数组里暴露给模型,只保证"每个 Skill 内部不重名"是不够的。全局唯一是
    每 Skill 唯一的超集,统一在这里做一次,下游(含 pool 合并)就不需要再操心。
    """
    # 注意:同一个工具被多个 Skill 绑定时,每个 Skill 各自持有一个 ToolSpec **对象**
    # (它们 tool.id 相同但不是同一个对象)。所以这里按对象收集全部实例,由
    # naming.assign_function_names 按 tool_id 去重命名,最后再回填到每一个实例上——
    # 只按对象身份去重会漏掉后面的实例,导致同一工具在不同 Skill 里名字不一致。
    all_tools: list[ToolSpec] = []
    if spec.direct_tool is not None:
        all_tools.append(spec.direct_tool)
    for skill in spec.skill_chain:
        all_tools.extend(skill.tools)
    if not all_tools:
        return

    # v1.1.1:内建「保存凭证」工具是**系统级**能力(不归属某个 Agent/Skill,只与目标工具
    # 关联),函数名必须稳定且与业务工具天然隔离——直接固定为 credential_save_<tool_id8>,
    # 不走展示名 mangle。否则「保存PFT12301 景区下单账号凭证」会被 sanitize 成
    # PFT12301,与查询工具同名被改成 PFT12301_2,模型极易混淆(框架能力测试 01 文档 P1-1)。
    fixed: dict[str, str] = {}
    naming_items: list[tuple[str, str]] = []
    for t in all_tools:
        if t.type == "credential_save":
            target = (t.type_config or {}).get("target_tool_id", "")
            fixed[t.id] = f"credential_save_{target.replace('-', '')[:8]}"
        else:
            naming_items.append((t.id, t.name))
    assigned = naming.assign_function_names(naming_items)
    # 固定名也参与全局去重:业务工具若恰好同名,固定名兜底追加序号(仍稳定且唯一)。
    used = {name for _, (name, _) in assigned.items()}
    for tid, fname in fixed.items():
        base, n, final = fname, 2, fname
        while final in used:
            final = f"{base}_{n}"
            n += 1
        used.add(final)
        assigned[tid] = (final, True)
    for t in all_tools:
        func_name, renamed = assigned[t.id]
        t.func_name = func_name
        t.model_description = naming.describe_for_model(t.name, t.description, func_name, renamed)
        if renamed:
            ctx.info("tool_renamed", f"工具「{t.name}」的模型侧函数名规范化为 {func_name}(原名已写入工具描述)", t.id)


_KNOWLEDGE_MODES = ("preload", "tool", "both")
KNOWLEDGE_TOOL_NAME = "knowledge_search"

# v1.1.1:草稿资源(Agent/Skill)运行期策略——只允许非生产环境执行,生产环境直接阻断。
# 生产误消费草稿版本可能把未验证的定义/工具暴露给真实用户。
_PROD_ENVS = {"prod", "production", "prd"}


def _runtime_env() -> str:
    """运行时环境(local/dev/prod),来自 config.yaml app.env,默认 dev。

    注意:Settings.get 的 default 是 keyword-only 参数——写成
    get("app","env","dev") 会把 "dev" 当成 path 的一部分,永远返回 None,
    导致 prod 分支(_PROD_ENVS)从未生效。必须用 default="dev"。
    """
    return str(get_settings().get("app", "env", default="dev")).lower()


def _build_knowledge_tool(skill_id: str, skill_name: str, sources: list[str]) -> ToolSpec:
    """把知识检索包装成一个可被模型调用的伪工具。

    相对"进入 Skill 前自动检索一次"的好处:
    - 模型可以**自己决定检索词**。前置注入只能拿用户原话去查,而用户原话往往不是好的
      检索词("上次说的那个多少钱")。交给模型等于免费获得了一次查询改写。
    - 检索结果不理想时模型可以换个说法**再查一次**,前置注入没有第二次机会。
    - 明显不需要知识的轮次可以完全不检索,省一次向量查询和一大段上下文。

    代价是"检不检索"变成了模型的判断,多了一处不确定性——所以默认仍是 preload,
    需要的 Skill 显式配置 knowledge_mode=tool/both 才启用。
    """
    src_desc = "、".join(sources) if sources else "向量知识库"
    return ToolSpec(
        id=f"__knowledge__{skill_id}",
        name=KNOWLEDGE_TOOL_NAME,
        description=(f"检索与问题相关的背景资料(可用知识源:{src_desc})。"
                     f"当你需要「{skill_name}」相关的事实、数据或政策依据,而当前上下文里没有时,调用它。"
                     f"query 请填写便于检索的关键信息,而不是照抄用户原话。"),
        type="knowledge",
        risk_level="low",
        json_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索关键词或问题"}},
            "required": ["query"],
        },
        type_config={"sources": list(sources), "skill_id": skill_id},
        allow_override=False,
    )


_CREDENTIAL_PLACEHOLDER = "{{credential}}"


def _uses_credential_placeholder(value) -> bool:
    """工具 type_config 里是否使用了 `{{credential}}` 占位符(递归判定)。

    这是「该工具需要登录凭证」的显式信号——以此决定是否为该工具注入
    内建「保存凭证」工具(§10.2),而不是靠工具名/描述去猜。
    """
    if isinstance(value, str):
        return _CREDENTIAL_PLACEHOLDER in value
    if isinstance(value, dict):
        return any(_uses_credential_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_uses_credential_placeholder(v) for v in value)
    return False


def _build_credential_tool(tool_id: str, tool_name: str) -> ToolSpec:
    """§10.2 内建「保存<工具>账号凭证」工具。

    仿 knowledge 伪工具:type 为引擎内建类型,不走 tool_center 注册表/沙箱,
    由 tools_adapter 拦截直接写 tc_credentials。risk_level=high 强制 HITL,
    明文不落审批 payload(见 tools_adapter.credential_save 分支)。
    v0.1.4:凭证一律归属当前对话用户本人,不存在共享凭证。
    """
    return ToolSpec(
        id=f"__credential_save__{tool_id}",
        name=f"保存{tool_name}账号凭证",
        description=(
            f"当用户向你提供「{tool_name}」的登录账号和密码,或调用该工具返回"
            f"未授权/401/403 等鉴权错误时,调用本工具把账号密码安全保存到平台凭证库,"
            f"归属当前对话用户本人(不存在共享凭证)。保存前会在对话中向用户发起确认"
            f"(确认卡片,不显示账号密码),用户确认后才真正保存;若用户忽略则不要保存,"
            f"继续引导用户完成该工具的使用。确认后后续调用「{tool_name}」将自动使用该"
            f"凭证,无需用户再次提供。如果当前用户没有该工具的凭证,不要猜测或使用他人"
            f"的凭证,应先向用户索取账号密码。"
        ),
        type="credential_save",
        risk_level="high",
        json_schema={
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "登录账号"},
                "password": {"type": "string", "description": "登录密码"},
                "scope": {"type": "string", "description": "凭证使用场景标签,可选;不填表示默认场景"},
            },
            "required": ["username", "password"],
        },
        type_config={"target_tool_id": tool_id},
        allow_override=False,
    )


# ── Skill 解析 ───────────────────────────────────────────────────────────
def _resolve_knowledge_sources(base_sources: list[str], overrides: dict) -> list[str]:
    ks_override = overrides.get("knowledge_sources")
    if not ks_override:
        return list(base_sources or [])
    mode = ks_override.get("mode", "replace")
    sources = ks_override.get("sources") or []
    if mode == "extend":
        return list(dict.fromkeys([*(base_sources or []), *sources]))
    return list(sources)


async def _resolve_skill(db: AsyncSession, skill_id: str, *, ctx: _Ctx,
                         pinned_version: int | None = None,
                         explicit_version_id: str | None = None) -> ResolvedSkill:
    """解析单个 Skill。§6.2(15 号文档 Q1/§7):`pinned_version`/`explicit_version_id` 任一命中
    -> 用该版本的 published 快照构造;无 pin 且未显式指定(直连未带版本) -> 不可用报错,
    无任何隐式「默认/最新 published」兜底。13 号文档 Phase 3:运行时不再读主表字段
    /tc_prompts,来源(显式/pin)输出结构完全一致,下游零改动,溯源均带具体版本号。

    - 快照含 prompt 全文(发布时冻结),prompt 一律从快照取;
    - pin 版本缺失/下线 -> 该绑定视为不可用,按「绑定资源不存在」抛 ResolveError(§6.4);
    - `explicit_version_id`(Phase 5 Console 显式指定,版本行 id)优先于内部 pin 档位,
      只用于 Skill 直连场景;由调用方显式钉到某个 published 版本。
    """
    skill = await db.get(Skill, skill_id)
    if not skill:
        raise ResolveError(f"Skill {skill_id} 不存在")

    explicit = False
    if explicit_version_id is not None:
        # 显式指定档位:先按版本行 id 预取并校验归属/published,复用下方 pinned 快照构造
        ver = await _published_version_by_id(db, "skill", skill_id, explicit_version_id)
        if ver is None:
            raise ResolveError(f"Skill {skill_id} 不存在指定版本 {explicit_version_id}(或未发布),不可执行")
        pinned_version = ver.version
        explicit = True

    if pinned_version is not None:
        # ── 数据来源:pin 版本快照(pin 绑定,或显式指定时已预取 ver) ──
        ver = ver if explicit else await _published_version(db, "skill", skill_id, pinned_version)
        if ver is None:
            ctx.warn("skill_pin_missing",
                     f"Skill「{skill.name}」pin 的版本 v{pinned_version} 不存在或未发布,该绑定不可用", skill_id)
            raise ResolveError(f"Skill {skill_id} 的 pin 版本 v{pinned_version} 不存在或未发布")
        snap = ver.snapshot or {}
        # §6.3 指派即授权穿透:agent 指派命中(非 skill 直连/显式指定)时,skill
        # 可见性校验跳过——指派动作本身就是授权;直连与显式指定仍按 visibility 校验。
        # 19 号 S7:固定问题命中直跳同口径(问题库登记=运营显式授权该编排依赖锁)。
        if (not ctx.assigned and not ctx.question_authorized
                and not _visible(snap.get("visibility") or skill.visibility,
                                 skill.owner_user_id, skill.scenic_id, ctx=ctx)):
            raise ResolveError(f"Skill {skill_id} 对当前调用方不可见")
        name = snap.get("name") or skill.name
        # v1.1.10:内容键纯快照(主表列已删 0050)。skill 解析必有 published 快照
        # (无 pin/版本即不可用,上方已拦截);老快照缺键(字段加入版本管理之前发布)
        # 回退空值/默认,不再触碰主表(无列可读)。
        description = snap.get("description")  # None -> 调用方回退 system_prompt 截断
        tool_usage = dict(snap.get("tool_usage") or {})
        constraints = snap.get("constraints") or {}
        knowledge_sources = _resolve_knowledge_sources(snap.get("knowledge_sources") or [], ctx.overrides)
        # **不要在这里兜底成 "L2"。** 兜底之后 suggested_mode 永远不为空,
        # decide_mode 会认为「这是管理后台显式声明的运行模式」而直接采用,
        # 自动模式判定(按工具规模在 ReAct / Plan-and-Execute 之间选)就形同虚设 ——
        # 而且判定理由还会写成「登记的运行模式建议值为 L2,直接采用」,
        # 让人以为真的有人配过。没配就是没配,交给 decide_mode 去判断。
        suggested_mode = snap.get("suggested_mode") or None
        tool_pins = _pin_dict(snap.get("tools") or [], "tool_id")
        tool_ids = list(tool_pins.keys())
        if explicit:
            ctx.info("skill_version_explicit", f"Skill「{name}」按调用方指定版本 v{pinned_version} 解析", skill_id)
            ctx.provenance.setdefault("skills", {})[skill_id] = {
                "source": "explicit_snapshot", "version": ver.version, "version_id": ver.id, "name": name}
        else:
            ctx.info("skill_version_pinned", f"Skill「{name}」按 pin v{pinned_version} 解析", skill_id)
            # P0-3 会话溯源:skill 来源 = pin 版本快照
            ctx.provenance.setdefault("skills", {})[skill_id] = {
                "source": "pinned_snapshot", "version": pinned_version, "name": name}
    else:
        # ── 无 pin 且未显式指定:不可用(15 号文档 Q1/§7:删默认版本后无任何隐式兜底) ──
        ctx.warn("skill_no_pin_or_version",
                 f"Skill「{skill.name}」未指定版本(无 pin 绑定/未显式选择),不可用", skill_id)
        raise ResolveError(
            f"Skill「{skill.name}」未指定版本,不可执行;请通过 Agent 绑定(pin)或显式选择已发布版本")

    tools = await _load_authorized_tools(db, tool_ids, ctx=ctx, skill_name=name, tool_pins=tool_pins,
                                         skill_id=skill_id)
    tools = await _include_param_source_tools(db, tools, ctx=ctx, skill_name=name, skill_id=skill_id)
    # 系统内置工具(时间换算、表格统计、BI 看板兜底)对**每个** Skill 都可用,不必逐个绑。
    # 这几样每个景区都要、配法完全一样,靠运营去绑的结果就是没人绑 ——
    # 而少了时间工具,模型会按训练时的日期推算「今天」,错得悄无声息。
    # 历史上有人在 Tool 中心手建过「时间对齐」「数值计算」这类 type=builtin 的行,
    # 而 type=builtin 是按**工具名**去内置注册表找函数的,中文名一个都找不到 ——
    # 这些行从建出来那天起就调不通。这里把它们摘掉(能力由下面注入的内置工具提供),
    # 而不是让模型对着一个必定报「内置工具不存在」的工具白白浪费一次调用和一轮重试。
    tools = _drop_superseded_builtins(tools, skill_name=name, skill_id=skill_id, ctx=ctx)
    tools = _builtin_specs() + tools
    bound_count = len(tool_ids)
    if bound_count and not tools:
        # 这是最值得报警的一种降级:Skill 设计上是要用工具的,但一个都没剩下。
        # 模型此时只能空谈,产出看起来"像模像样"却完全没有真实数据支撑。
        ctx.warn("skill_no_usable_tools",
                 f"Skill「{name}」绑定的 {bound_count} 个工具全部不可用(不存在/已禁用/无权限),"
                 f"本次将在无工具的情况下执行", skill_id)

    # 13 号文档 Phase 3:运行时一律从快照取 prompt(发布时冻结),不再读主表 tc_prompts。
    template = (snap or {}).get("prompt")
    if template is None:
        template = f"你是一个专注于「{name}」的智能助手,请根据用户请求完成任务。"
        ctx.info("prompt_default", f"Skill「{name}」尚未发布 Prompt,使用默认提示词", skill_id)
    # 系统参数({{ota_account}} 这类,系统设置 · 参数配置):按本景区取值渲染;敏感参数只给「已配置」
    from cogni_agent.runtime_service.engine import scenic_params as _sp

    variables = {**_sp.prompt_vars(await _sp.load(db, ctx.scenic_id)),
                 **_global_vars(ctx.scenic_id, ctx.user_id), **(ctx.overrides.get("prompt_vars") or {})}
    system_prompt = render_prompt(template, variables)
    # 把「什么时候用哪个工具」拼进系统提示。
    #
    # 为什么由系统拼而不是让人写进 Prompt:工具是会变的(增删、改名、被权限过滤掉)。
    # 手写的说明和实际可用工具很容易对不上——描述了一个已经没有的工具,模型照着去调,
    # 只能得到「工具不存在」;或者加了工具却忘了写说明,模型不知道什么时候该用它。
    # 由系统按**本次实际解析出来的工具列表**生成,两者不可能脱节。
    usage_lines = [f"- `{t.name}`:{tool_usage[t.id]}" for t in tools if tool_usage.get(t.id)]
    if usage_lines:
        system_prompt += ("\n\n【工具使用说明】\n" + "\n".join(usage_lines) +
                          "\n没有列出使用场景的工具,按其自身描述判断是否调用。")
    # render_prompt 对未知变量是原样保留 {{var}} —— 模型会把这段占位符当字面量读到,
    # 轻则困惑,重则把 "{{order_id}}" 当成真实值去调工具。至少要让运维看见。
    leftover = sorted(set(_VAR_PATTERN.findall(system_prompt)))
    if leftover:
        ctx.warn("prompt_unresolved_vars",
                 f"Skill「{name}」的 Prompt 中有未赋值的变量 {leftover},将以原始占位符形式出现在提示词里", skill_id)

    settings = get_settings()
    default_steps = settings.get("runtime", "guardrails", "max_steps", default=25)
    default_calls = settings.get("runtime", "guardrails", "max_tool_calls", default=10)

    response_format = constraints.get("response_format")
    if response_format is not None and not isinstance(response_format, dict):
        ctx.warn("skill_response_format_invalid",
                 f"Skill「{name}」的 response_format 配置不是对象,已忽略", skill_id)
        response_format = None

    knowledge_mode = str(constraints.get("knowledge_mode") or "preload").lower()
    if knowledge_mode not in _KNOWLEDGE_MODES:
        ctx.warn("skill_knowledge_mode_invalid",
                 f"Skill「{name}」的 knowledge_mode={knowledge_mode!r} 不是 {_KNOWLEDGE_MODES} 之一,按 preload 处理", skill_id)
        knowledge_mode = "preload"
    if knowledge_mode in ("tool", "both"):
        if knowledge_sources:
            tools = [*tools, _build_knowledge_tool(skill.id, name, knowledge_sources)]
        else:
            # 配了工具化检索却没有任何知识源,等于给模型一个永远返回空的工具——
            # 白白浪费一次调用,还会让它以为"查过了、确实没有",从而给出错误结论。
            ctx.warn("skill_knowledge_tool_no_source",
                     f"Skill「{name}」配置了 knowledge_mode={knowledge_mode} 但未启用任何知识源,已退回 preload", skill_id)
            knowledge_mode = "preload"

    # §10.2 内建「保存凭证」工具:为 type_config 中使用 {{credential}} 占位符的工具
    # 各注入一个(一个目标工具一个)。knowledge 伪工具不含占位符,自然被排除。
    credential_tools = [
        _build_credential_tool(t.id, t.name)
        for t in tools
        if t.type not in ("knowledge", "credential_save") and _uses_credential_placeholder(t.type_config)
    ]
    if credential_tools:
        tools = [*tools, *credential_tools]

    return ResolvedSkill(
        id=skill.id, name=name, system_prompt=system_prompt, tools=tools,
        knowledge_sources=knowledge_sources, knowledge_mode=knowledge_mode,
        description=description,
        max_steps=_positive_int(constraints.get("max_steps"), default_steps),
        max_tool_calls=_positive_int(constraints.get("max_tool_calls"), default_calls),
        suggested_mode=suggested_mode,
        # Skill 级模型覆盖:分类/抽取这类步骤用便宜模型,规划/总结用强模型,
        # 同一个编排里混用不同模型是很常见的成本优化手段。
        model=constraints.get("model") or None,
        response_format=response_format,
        route_hit_keywords=_route_hit_keywords(constraints),
        strict_tool_execution=bool(constraints.get("strict_tool_execution")),
        token_budget=_optional_budget(constraints.get("token_budget")),
    )


def _route_hit_keywords(constraints: dict) -> list[str]:
    """从 constraints 提取路由强挂关键词;非法配置静默降级为空列表(不拦截执行)。

    类型防御:误配成字符串/数字时不能直接 for 遍历单个字符或抛异常,静默当没有。
    """
    raw = constraints.get("route_hit_keywords") or []
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(k).strip() for k in raw if str(k).strip()]


def _optional_budget(value) -> int | None:
    """解析 constraints.token_budget:None/空 = 没配(回退全局),其余按非负整数,负数当 0。

    和 _positive_int 的区别在于 **0 是有意义的取值**(显式不限制),不能被当成误填
    回落成默认值 —— 「默认不限制」这件事就是靠这个 0 表达的。
    """
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None                          # 配置写歪了当没配,不因此改变熔断行为
    return n if n > 0 else 0


def _positive_int(value, default: int) -> int:
    """约束值必须是正整数,否则回落默认值。
    max_steps=0 会让任务一步都跑不了(第一次 check 就熔断),这种配置多半是误填
    而不是"故意禁用",静默接受只会变成一个很难查的"任务莫名其妙失败"。"""
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


async def _resolve_direct_tool(db: AsyncSession, tool_id: str, *, ctx: _Ctx) -> ToolSpec:
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise ResolveError(f"Tool {tool_id} 不存在")
    if not tool.active:
        raise ResolveError(f"Tool {tool_id} 已被禁用")
    ok, reason = await test_permission(db, tool_id, ctx.user_id, ctx.role, ctx.scenic_id, action="use")
    if not ok:
        raise ResolveError(f"无权限调用 Tool {tool_id}: {reason}")
    # v1.2(0051):Tool 与 Skill/Agent 同构 —— 主表无内容列,内容一律来自版本快照。
    # 取数:最新已发布 > 最新草稿(未发布/调试态) > fail-closed。
    # direct(tool/mcp)是「调试面板/后台直调/评估」路径(executor_type=tool 没有
    # skill/agent 编排的 pin 上下文),草稿优先是「编辑保存即所见即所得」的刻意例外
    # (与 create_tool 自动发 v1 配合,工具始终有内容可用);skill/agent 编排内的工具
    # 仍走 pin→published 快照纪律,草稿永不进入编排运行时。
    from cogni_agent.tool_center_service import versioning as ver

    pub = await ver.latest_published_version(db, "tool", tool_id)
    snap = (pub.snapshot or {}) if pub is not None else None
    if snap is None:
        draft = await ver.latest_draft(db, "tool", tool_id)
        snap = (draft.snapshot or {}) if draft is not None else None
    if snap is None:
        raise ResolveError(f"Tool {tool_id} 从未创建过版本,不可直接调用(请先发布)")
    return _to_tool_spec(_tool_like_from_snapshot(tool_id, tool.name, snap), ctx=ctx)


def _build_skill_deps(composition_hint: str, ordered_ids: list[str], dag_edges: dict | None) -> dict[str, list[str]]:
    """构造 Skill 之间的依赖邻接表。

    - mode=dag 且登记了 dag_edges -> 直接采用(只保留指向本链路内 Skill 的边,
      悬空依赖会被过滤,避免拓扑排序时永远等一个不存在的前置)
    - 其余情况(chain / 未登记)-> 按 ordered_ids 派生线性依赖 a->b->c
      线性链本质上是 DAG 的退化情形,统一表示后执行器只需实现一套逻辑。
    """
    known = set(ordered_ids)
    if composition_hint == "dag" and dag_edges:
        return {sid: [d for d in (dag_edges.get(sid) or []) if d in known] for sid in ordered_ids}
    return {sid: ([ordered_ids[i - 1]] if i > 0 else []) for i, sid in enumerate(ordered_ids)}


async def _resolve_chain(db: AsyncSession, ordered_ids: list[str], *, ctx: _Ctx,
                         skill_pins: dict[str, int | None] | None = None) -> list[ResolvedSkill]:
    """按顺序解析一组 Skill。

    `skill_pins`: 上游 agent 版本快照的绑定 pin({skill_id: version|None});无 pin 的
    Skill 走主表(默认版本)。

    刻意保持串行而不用 asyncio.gather:这些解析共用同一个 AsyncSession,
    SQLAlchemy 的 AsyncSession 不是并发安全的,并发跑会踩到
    "another operation is in progress"。要并行得先给每个分支开独立 session,
    而解析本身是毫秒级的,不值得为此增加连接占用。
    """
    return [await _resolve_skill(db, sid, ctx=ctx, pinned_version=(skill_pins or {}).get(sid))
            for sid in ordered_ids]


async def _resolve_effective_model_name() -> str | None:
    """取回「本次实际会用到」的模型标识,用于回填 model_override。

    复用 llm_client._model_cfg(None) 的完整解析链(显式 > 页面默认 > 页面首个可用
    > config.yaml runtime.default_model),保证 usage 落库的模型与真实执行一致。
    返回的是**供应商模型 ID(model_id)**而非展示名:业务记录(spec.model_override /
    gov_llm_usage.model)只持久化 model_id,展示名改名不影响历史归因;解析链
    _model_cfg 内部以不可变的模型 id 为清单 key,并支持按 id / 展示名 / model_id
    三种形态匹配,后续从任务恢复执行时传 model_id 同样能解析。
    解析失败保持 None,不阻断 resolve——执行阶段该报错仍会报,行为与修复前一致。
    """
    try:
        from cogni_agent.runtime_service.llm.client import _model_cfg

        name, cfg = await _model_cfg(None)
        return (cfg or {}).get("model_id") or name
    except Exception:  # noqa: BLE001 - 没有可用模型时保持 None,由执行阶段处理
        return None


async def resolve_execution_spec(
    db: AsyncSession, *, executor_type: str, executor_id: str, scenic_id: str | None,
    role: str, user_id: str, overrides: dict | None = None,
    executor_version_id: str | None = None,
    question_authorized: bool = False,
) -> ExecutionSpec:
    """解析执行规格。executor_version_id(Phase 5 Console 两层选择):调用方显式钉到某个
    published 版本 —— 对 agent/skill 直连生效,优先于指派/默认档位。
    question_authorized(19 号 S7):固定问题命中直跳时由网关注入,等价 agent 指派的
    授权穿透(见 _Ctx.question_authorized 注释)。"""
    ctx = _Ctx(user_id=user_id, role=role, scenic_id=scenic_id, overrides=overrides or {},
               executor_version_id=executor_version_id)
    ctx.question_authorized = question_authorized
    executor_type = (executor_type or "agent").lower()
    spec = await _resolve_by_type(db, executor_type, executor_id, ctx)
    _assign_function_names(spec, ctx)
    # 模型名回填:调用方没显式指定模型时,把「实际会用的默认模型名」写进 model_override。
    # 以前这里不回填,执行时 llm_client 内部照常解析出默认模型,但 usage 事件里
    # model 恒为空串,导致 gov_llm_usage.model 为空、成本账无法按模型维度归因。
    if spec.model_override is None:
        spec.model_override = await _resolve_effective_model_name()
    # 单任务预算:优先级 payload.overrides > 技能链上配置的最小正值 > 全局默认。
    # 「最小正值」= 链上任一技能设了上限就按最严的那个算 —— guardrail 计的是
    # **整个任务**累计的 token,一个链共用一份计数,取最严格的口径才不会让某个
    # 配了 2 万上限的技能因为同链里另一个没配而白配。配成 0(不限制)的技能不参与
    # 取最小值,它表达的是「我不提要求」,不是「把别人也放开」。
    _override = (overrides or {}).get("token_budget")
    if _override is not None:
        spec.token_budget = _override
    else:
        _caps = [sk.token_budget for sk in spec.skill_chain
                 if sk.token_budget is not None and sk.token_budget > 0]
        # 全链都显式配了 0 = 大家都要求不限制,那就别再回退全局默认
        _all_unlimited = bool(spec.skill_chain) and all(
            sk.token_budget == 0 for sk in spec.skill_chain)
        spec.token_budget = min(_caps) if _caps else (0 if _all_unlimited else None)
    spec.diagnostics = ctx.diagnostics
    # P0-3 会话溯源:把解析期收集的来源(agent→skill→tool 各层实际解析到的版本)挂到 spec,
    # executor 据此推 resolve_provenance 时间线事件,执行链分析可直接看到「实际用的版本」。
    spec.provenance = ctx.provenance
    return spec


async def _resolve_by_type(db: AsyncSession, executor_type: str, executor_id: str, ctx: _Ctx) -> ExecutionSpec:
    overrides = ctx.overrides

    if executor_type in ("tool", "mcp"):
        tool_spec = await _resolve_direct_tool(db, executor_id, ctx=ctx)
        return ExecutionSpec(executor_type=executor_type, executor_id=executor_id, direct_tool=tool_spec,
                             suggested_mode="L0", model_override=overrides.get("model"))

    if executor_type == "skill":
        skill_ids = overrides.get("skill_ids") or [executor_id]
        # Phase 5:显式版本只对单个 Skill 直连有意义,多 Skill 链路没有「一个版本」语义
        if ctx.executor_version_id and len(skill_ids) > 1:
            raise ResolveError("多 Skill 任务不支持指定单个版本,请逐个 Skill 直接执行或去掉版本选择")
        if len(skill_ids) == 1:
            # 0074:技能直调也走「按用户分发」——没显式选版本、也不是固定问题穿透时,
            # 用该用户对这个技能的指派版本;没有指派就 fail-closed(超管豁免)。
            version_id = ctx.executor_version_id
            if version_id is None and not ctx.question_authorized:
                version_id = await _resolve_distributed_skill_version(db, skill_ids[0], ctx=ctx)
            elif version_id is None:
                # question_authorized = 这一轮是运营在后台配好的固定问题打进来的。
                # 它的含义是「**不必再过按用户分发那道闸**」,而不是「不需要版本」——
                # 原先这里什么都不做,于是带着一个没有版本的技能走到 _resolve_skill,
                # 被一句「Skill 未指定版本,不可执行;请通过 Agent 绑定(pin)」挡下。
                # 对运营来说这句话完全无从下手:他配的就是这条问题、指的就是这个技能。
                # 所以退到「该用户的指派版本,没有就用最新已发布」——
                # 跑哪一版可以没人指定,但不该因此跑不起来。
                version_id = await _resolve_distributed_skill_version(
                    db, skill_ids[0], ctx=ctx, allow_latest_published=True)
            resolved = await _resolve_skill(db, skill_ids[0], ctx=ctx, explicit_version_id=version_id)
            return ExecutionSpec(executor_type=executor_type, executor_id=executor_id, skill_chain=[resolved],
                                 composition_hint=None, suggested_mode=resolved.suggested_mode,
                                 model_override=overrides.get("model"))
        # 23 号:tc_skill_compositions 已删除,不再按 skill 集合反查编排定义。
        # 多 Skill 仅保留「按 overrides 给定顺序的线性链」——APP 固定问题路径恒单技能,
        # 该分支已无已知调用方(技术债登记,见 23 号 §7 D2)。
        ordered_ids = list(skill_ids)
        return ExecutionSpec(
            executor_type=executor_type, executor_id=executor_id,
            skill_chain=await _resolve_chain(db, ordered_ids, ctx=ctx),
            composition_hint="chain",
            skill_deps=_build_skill_deps("chain", ordered_ids, None),
            model_override=overrides.get("model"),
        )

    if executor_type == "agent":
        agent = await db.get(Agent, executor_id)
        if not agent:
            raise ResolveError(f"Agent {executor_id} 不存在")
        if not agent.active:
            raise ResolveError(f"Agent {executor_id} 已被禁用")

        # ── 档位 0:调用方显式指定版本(13 号文档 Phase 5 Console 两层选择,最高优先级) ──
        # 优先级:显式指定 > user 指派(15 号 D9-A 收敛后唯一生效途径) > 无兜底(fail-closed)。
        # 显式版本先按可见性校验(区别于指派即授权),通过后并入档位 1 的快照构造。
        if ctx.executor_version_id:
            explicit_ver = await _published_version_by_id(db, "agent", executor_id, ctx.executor_version_id)
            if explicit_ver is None:
                raise ResolveError(f"Agent「{agent.name}」不存在指定版本 {ctx.executor_version_id}(或未发布),不可执行")
            _snap = explicit_ver.snapshot or {}
            if not _visible(_snap.get("visibility") or agent.visibility,
                            agent.owner_user_id, agent.scenic_id, ctx=ctx):
                raise ResolveError(f"Agent {executor_id} 对当前调用方不可见")
        else:
            explicit_ver = None

        # ── 档位 1:显式指定 / user 指派命中即用;未命中 -> 档位 2 fail-closed(§6.1/§6.2) ──
        assigned = explicit_ver or await _resolve_assigned_version(db, executor_id, ctx=ctx)
        if assigned is not None:
            snap = assigned.snapshot or {}
            # §6.3 user 指派命中当前调用方 → 指派即授权,
            # 不再按资源 visibility 拦截:被指派者必然可见,否则「钉了版本却不可执行」逻辑说不过去。
            # 指派穿透标志置位:下游 skill 解析同样跳过 visibility 校验(同一口径)。
            if explicit_ver is None:
                ctx.assigned = True
            persona = {"name": snap.get("name") or agent.name,
                       "persona_desc": snap.get("persona_desc") or agent.persona_desc,
                       "tone_style": snap.get("tone_style") or agent.tone_style,
                       "opening_line": snap.get("opening_line") or agent.opening_line}
            # v1.1.5 意图识别 agent 化:指派版本快照优先,回退主表
            intent_config = snap.get("intent_config") or agent.intent_config
            binds = snap.get("bound_skills")
            if isinstance(binds, list):
                skill_pins = _pin_dict(binds, "skill_id")
                bound_ids = list(skill_pins.keys())
            else:
                # 兼容旧格式快照(bound_skill_ids 纯 id 数组):无 pin,按默认版本解析
                bound_ids = list(snap.get("bound_skill_ids") or [])
                skill_pins = {}
            if explicit_ver is not None:
                ctx.info("agent_version_explicit",
                         f"Agent「{agent.name}」按调用方指定版本 v{assigned.version} 解析({len(bound_ids)} 个 Skill)", executor_id)
                ctx.provenance["agent"] = {"source": "explicit_snapshot", "version": assigned.version,
                                           "version_id": assigned.id, "name": agent.name}
            else:
                ctx.info("agent_version_assigned",
                         f"Agent「{agent.name}」按指派版本 v{assigned.version} 解析({len(bound_ids)} 个 Skill)", executor_id)
                # P0-3 会话溯源:记录 agent 实际来源 = 指派版本
                ctx.provenance["agent"] = {"source": "assigned", "version": assigned.version,
                                           "version_id": assigned.id, "name": agent.name}
        else:
            # ── 档位 2:无显式指定且未分发 -> fail-closed(15 号文档 §4.1/§6.2) ──
            # 默认版本概念已删除(15 号文档),绝不静默回落「最新 published」;
            # 直接报错并引导用户在『分发中心』开通用户指派。
            raise ResolveError(
                f"Agent「{agent.name}」未对当前账号开通分发,请联系管理员在『分发中心』按用户指派版本",
                code="agent_not_distributed")

        if not bound_ids:
            ctx.info("agent_no_skill", f"Agent「{agent.name}」未绑定任何 Skill,退化为纯人格对话", executor_id)
            return ExecutionSpec(executor_type=executor_type, executor_id=executor_id, persona=persona,
                                 skill_chain=[], suggested_mode="L0", model_override=overrides.get("model"),
                                 intent_config=intent_config)

        if len(bound_ids) == 1:
            resolved = await _resolve_skill(db, bound_ids[0], ctx=ctx,
                                            pinned_version=skill_pins.get(bound_ids[0]))
            return ExecutionSpec(executor_type=executor_type, executor_id=executor_id, persona=persona,
                                 skill_chain=[resolved], composition_hint=None,
                                 suggested_mode=resolved.suggested_mode, model_override=overrides.get("model"),
                                 intent_config=intent_config)

        # v1.2(19 号文档 D4-①):Agent 内部协作模式收进 agent 自身(composition_mode),
        # 不再查 tc_skill_compositions——组合表只服务 skill 直达路径(固定问题工作流)。
        # 优先级:版本快照(发布冻结,编辑改的是草稿)> 主表列(老 agent 兜底)> 默认 chain。
        # 消灭「技能集合精确相等」静默失配:改绑技能不再让编排定义意外失效。
        composition_hint = snap.get("composition_mode") or agent.composition_mode or "chain"
        ordered_ids = bound_ids
        return ExecutionSpec(
            executor_type=executor_type, executor_id=executor_id, persona=persona,
            skill_chain=await _resolve_chain(db, ordered_ids, ctx=ctx, skill_pins=skill_pins),
            composition_hint=composition_hint,
            # agent 级 dag 无自定义边登记:按绑定顺序派生线性依赖(DAG 的退化情形)
            skill_deps=_build_skill_deps(composition_hint, ordered_ids, None),
            model_override=overrides.get("model"),
            intent_config=intent_config,
        )

    raise ResolveError(f"未知 executor_type: {executor_type}")
