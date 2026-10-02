"""
M4-09:工具调用适配层——图里的"tools"节点只调这一个模块,不直接碰
tool_center_service.mcp.protocol.invoke_tool()。这一层专门负责"处理不确定性":

1. 幻觉/随机路由:模型编出了一个不在 bound_tools 里的工具名 -> 不执行,构造纠正性
   ToolMessage 打回给模型,由 guardrails 计数,超限熔断(而不是无限重试)。
2. 格式错误:模型给的参数不是合法 JSON,或不满足该工具注册时登记的 JSON Schema ->
   同样不执行,构造纠正性 ToolMessage,由 guardrails 计数。
3. 高风险动作:命中 risk_level=high -> 调用 langgraph.types.interrupt() 让整张图在
   这个工具调用上暂停(状态由 checkpointer 持久化),外层 executor.py 捕获暂停信号
   转 WAITING_HUMAN;驳回时这里直接抛 ApprovalRejected,由 executor.py 收尾为 CANCELLED。
4. 正常路径:三元鉴权(scenic_id×role×tool_id,tool_center_service.mcp.authz)+
   凭证注入 + 沙箱执行,全部委托给已有的 mcp/protocol.py::invoke_tool(),这里不重复实现。

"知识检索"默认不建模成可被模型调用的工具,而是在 Skill 进入新一轮对话前自动触发
(见 graphs.py 的 agent 节点),更贴近标准 RAG 用法,也避免模型"选不选检索"这件事
本身成为新的不确定性来源。
"""
from __future__ import annotations

import inspect

import json
import re
from typing import Any

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import ApprovalRejected, ClarificationNeeded
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.governance_event_bus_service.audit import write_audit
from cogni_agent.knowledge_service.router import route_and_retrieve
from cogni_agent.runtime_service.engine import credential_guard
from cogni_agent.runtime_service.engine.resolver import ToolSpec
from cogni_agent.tool_center_service.mcp.protocol import invoke_tool
from cogni_agent.tool_center_service.mcp.vault import save_credential

log = get_logger("runtime_service.tools_adapter")

# 高风险动作的人工审批要用图框架的 interrupt。这里用「模块级 try 导入 + 兜底」
# 而不是在函数里 import,原因有两条,缺一不可:
#
# ① 没装 langgraph 时本模块仍要能导入 —— 它的本职是**分发工具调用**,
#    只有审批这一条分支用得上 interrupt,为它在顶层挂硬依赖的话,
#    没装 langgraph 就连工具分发的单测都无从写起;
# ② 同时 `interrupt` 必须是**模块属性**。写成函数内 import 的话,
#    `patch("...tools_adapter.interrupt")` 会直接 AttributeError ——
#    于是人工审批那几条用例根本跑不起来,而「跑不起来」和「跑挂了」在
#    一堆红色里长得一模一样,等于审批这条最该有人盯的链路悄悄没了测试。
try:                                        # pragma: no cover - 取决于环境装没装
    from langgraph.types import interrupt
except ImportError:                         # pragma: no cover
    def interrupt(payload):                 # type: ignore[misc]
        raise RuntimeError(
            "高风险工具需要人工确认,但当前环境没有安装 langgraph,无法发起审批中断。"
            "请安装 langgraph,或把该工具的 risk_level 调低")

# 集团级角色(可跨景区操作)。其余角色(scenic_admin/scenic_operator 等)视为景区级:
# 工具参数里出现的景区 ID 必须与会话绑定的景区一致(租户隔离,防横向越权)。
_GROUP_ROLES = {"super_admin", "group_admin", "group_developer", "group_viewer"}
# 工具参数里代表"景区"的常见字段名,可用 runtime.tools.scenic_param_fields 覆盖。
_SCENIC_PARAM_FIELDS = ("scenic_id", "scene_id", "spot_id", "scenicId", "景区id", "景区ID")


def bound_tools_map(tool_specs: list[ToolSpec]) -> dict[str, ToolSpec]:
    """按**模型侧函数名**建索引。

    这里必须用 func_name 而不是展示名 name:name 可能是中文、也可能在多个 Skill 之间
    重复(集团级和景区级各有一个「查询订单」是很正常的事)。用 name 做 key 的后果是
    重名工具互相覆盖——模型调用该名字时执行到的是后写入的那一个,静默执行错误的工具。
    func_name 由 resolver 在整个 ExecutionSpec 范围内统一分配,保证合法且唯一。
    """
    return {t.func_name or t.name: t for t in tool_specs}


def tool_function_schemas(tool_specs: list[ToolSpec]) -> list[dict]:
    return [t.to_function_schema() for t in tool_specs]


class ToolCallOutcome:
    """执行单次工具调用的结果——kind 用于驱动图里的重试/熔断/正常延续分支。"""

    def __init__(self, kind: str, content: Any, raw_result: Any = None):
        self.kind = kind  # ok / format_error / hallucination / error
        self.content = content
        # 工具返回的**原始结构**(dict/list),未经序列化和截断。
        # 结构化摘要要基于它来算 —— 对着已经序列化成字符串的 content,
        # 既数不出记录条数,也看不出有哪些维度。
        self.raw_result = raw_result if raw_result is not None else content
        # 景区级接口参数注入记录([{arg, param, mode, overrode_model_value}],不含值)。
        # 进执行日志:「这个 account 是模型填的还是系统按景区配置填的」一眼能看出来。
        self.injected: list[dict] = []

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _parse_arguments(raw_args: Any) -> tuple[dict | None, str | None]:
    if isinstance(raw_args, dict):
        return raw_args, None
    if raw_args in (None, ""):
        return {}, None
    try:
        parsed = json.loads(raw_args)
        if not isinstance(parsed, dict):
            return None, f"参数应为 JSON 对象,实际得到:{type(parsed).__name__}"
        return parsed, None
    except (json.JSONDecodeError, TypeError) as exc:
        return None, f"参数不是合法 JSON:{exc}"


def _coerce_scalar(target_type: str, value: Any) -> Any:
    """把单个标量值转成目标 Schema 声明的类型;转换不了就原样返回(交给 Schema 校验报错)。"""
    try:
        if target_type == "string":
            if isinstance(value, str):
                return value
            # 只转标量。dict/list 走 str() 会变成 Python repr —— "{'a': 1}" 这种东西
            # 是合法字符串,于是 Schema 校验放行,然后原样发给接口。
            # 接口收到一串花括号,回一个业务错误,看起来像「接口有问题」。
            # 转不了就原样留着,交给下一步的 Schema 校验报错 —— 报错比静默发错好。
            if isinstance(value, (dict, list, tuple, set)):
                return value
            return str(value)
        if target_type == "integer":
            # bool 是 int 的子类,先排除:True 不该被静默转成 1
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return int(value)
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return value
        if target_type == "number":
            return float(value) if isinstance(value, str) else value
        if target_type == "boolean":
            return value if isinstance(value, bool) else bool(value)
    except (TypeError, ValueError):
        return value
    return value


def coerce_args_to_schema(schema: dict | None, args: dict) -> dict:
    """按目标工具的 JSON Schema 声明强制转换实参类型。

    注入类参数(slots 模板值 / x-param-source)来自 JSON 装配,类型未必与目标
    Schema 一致——如 supplierIds 由前置工具取到数字 `[1757026]`,而工具契约是
    `array<string>`。此前这类不匹配直接撞 Schema 校验报错,导致
    工具明明有数据却取不到(任务 5f906283:同一参数模型自己传 ["1757026"] 是成功的,
    说明契约没错,是注入值没转换)。

    这里按**目标 Schema 声明的类型**做转换,而不是硬编码"一律转字符串":
    日后某工具声明 integer 契约时同样自动兼容,不必再改一遍。方向上也不去改工具
    Schema —— 外部接口契约不接受我们就改契约,方向是反的。
    """
    props = (schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return args
    out = dict(args)
    for key, sub in props.items():
        if key not in out or not isinstance(sub, dict):
            continue
        value = out[key]
        if value is None:
            continue
        target = sub.get("type")
        if target == "array" and isinstance(sub.get("items"), dict):
            item_type = sub["items"].get("type")
            if isinstance(value, (list, tuple)):
                out[key] = [_coerce_scalar(item_type, v) for v in value]
            else:
                out[key] = [_coerce_scalar(item_type, value)]
        elif isinstance(target, str):
            out[key] = _coerce_scalar(target, value)
    return out


# ── 参数来源解析(x-param-source) ─────────────────────────────────────────────
# 工具 Schema 属性上的 x-param-source 声明「该参数必须由前置工具按指定输入产出,
# 而非模型臆造」(如 supplierId ← getSupplierIdByScenicId(scenicId))。解析期
# (resolver._to_tool_spec)已把这些参数从模型可见 Schema 剥离,模型拿不到也填不了;
# 执行期在这里自动调用前置工具取真实值注入。
_MAX_PARAM_SOURCE_DEPTH = 3            # 前置工具链递归深度上限,防声明成环
_TASK_PARAM_CACHE_LIMIT = 256          # 模块级 task 缓存上限(超限整体清空,靠幂等重查兜底)
_TASK_PARAM_CACHE: dict[str, dict] = {}  # task_id -> {(前置工具, 入参json): 提取值}
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


# 缺景区时问用户的那句话。景区 ID 和景区名都认(scenic_binding.scenic_from_text),
# 回答完下一轮会记到会话上 —— 不认简称的话,集团账号会被同一个问题反复问。
SCENIC_QUESTION = "请问要查哪个景区?直接告诉我景区名称或景区 ID 就行(比如「九龙潭」或「PFTSCA01018088」)。"

_X_CTX_DEFAULT = "x-ctx-default"


def _apply_ctx_defaults(spec: ToolSpec, args: dict, *, scenic_id: str | None) -> dict:
    """按 Schema 上的 `x-ctx-default` 给模型没填的参数补上下文值。

    口径:**请求里指定了就用指定的 → 没指定用当前景区 → 都没有就问用户**。
    - 模型填了(用户在话里点名了某个景区)就尊重它;景区账号填了别的景区,
      后面的 _check_scenic_scope 会拦下来,这里不重复判;
    - 模型没填 → 用本轮景区(景区账号是当前归属,集团账号是会话里点名过的);
    - 本轮也没有 → 发起澄清。**绝不编一个**:编出来的景区 ID 取回来的是别家的数据,
      而回答看起来完全正常。
    """
    schema = spec.validate_schema or spec.json_schema or {}
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    out = dict(args or {})
    for name, prop in props.items():
        src = (prop or {}).get(_X_CTX_DEFAULT) if isinstance(prop, dict) else None
        if src != "$ctx.scenicId":
            continue
        if out.get(name) not in (None, ""):
            continue
        if scenic_id:
            out[name] = scenic_id
            continue
        raise ClarificationNeeded(
            SCENIC_QUESTION, slot="scenic_id",
            detail=f"工具「{spec.name}」需要参数 {name}(景区),模型没有指定,本轮也没有可用景区")
    return out


def clear_param_cache(task_id: str | None = None) -> None:
    """清空前置参数缓存(指定 task 或全部)。测试与任务收尾用;运行期靠容量上限兜底。"""
    if task_id is None:
        _TASK_PARAM_CACHE.clear()
    else:
        _TASK_PARAM_CACHE.pop(task_id, None)


def _extract_path(result: Any, path: str) -> Any:
    """按点路径+下标从工具结果提取值:data.supplierId / items[0].id / $=结果整体。

    result 可能是 dict/list,也可能是 JSON 文本(HTTP 工具常见)——文本先尝试解析。
    """
    if not path or path == "$":
        return result
    cur = result
    if isinstance(cur, str):
        try:
            cur = json.loads(cur)
        except (json.JSONDecodeError, TypeError):
            pass
    for seg, idx in _PATH_TOKEN.findall(path):
        if idx:
            if not isinstance(cur, (list, tuple)) or int(idx) >= len(cur):
                raise KeyError(f"路径 {path} 的第 {idx} 项不存在")
            cur = cur[int(idx)]
        else:
            if not isinstance(cur, dict) or seg not in cur:
                raise KeyError(f"路径 {path} 的字段 {seg} 不存在")
            cur = cur[seg]
    return cur


class _UpstreamFailure:
    """缓存里的「这个前置调用已经失败过」。消息里的 {target} 换成当前目标工具名。"""
    __slots__ = ("message",)

    def __init__(self, message: str):
        self.message = message


async def _resolve_param_sources(
    db: AsyncSession,
    *,
    task_id: str,
    spec: ToolSpec,
    merged_args: dict,
    bound_tools: dict[str, ToolSpec],
    scenic_id: str | None,
    role: str,
    user_id: str,
    depth: int,
) -> tuple[dict, ToolCallOutcome | None]:
    """解析 spec.param_sources:对每个声明了前置来源的参数,自动调用前置工具取真实值。

    返回 (补齐后的 merged_args, 错误或 None)。错误非 None 时调用方直接返回该 outcome:
    前置工具缺失/高风险/调用失败/提取失败,都不执行目标工具,把问题明确打回给模型。
    前置工具结果按 (task_id, 工具, 入参) 缓存:同一会话多个工具引用同一来源只查一次。

    **这几种失败都记成 error,不是 format_error。** format_error 的意思是「模型参数写错了,
    让它自己改」,并且有次数上限;而这里出问题的是配置或上游接口(供应商接口 401、
    取值路径不对、前置工具没绑),模型怎么改参数都没用。以前记成 format_error:
    一轮并发 4 个票务工具,供应商接口一挂就是 4 次「格式错误」,当轮就超过 3 次上限,
    任务以「模型无法自我纠正」失败(现场任务 ababbb8c)—— 说的完全不是真正的原因。
    失败结果同样按任务缓存:同一轮并发的几个工具不会把一个已经挂了的接口再打几遍。
    """
    if depth > _MAX_PARAM_SOURCE_DEPTH:
        return {}, ToolCallOutcome(
            "format_error",
            f"调用工具「{spec.name}」失败:前置参数解析嵌套过深(超过 {_MAX_PARAM_SOURCE_DEPTH} 层)。"
            f"请检查相关工具的 x-param-source 声明是否存在循环引用。",
        )

    for param, src in spec.param_sources.items():
        tool_ref = str(src.get("tool") or "").strip()
        key = str(src.get("key") or "").strip()
        # 取值源:key 为 "$ctx.<字段名>" 时从会话上下文取(目前仅支持 scenicId,
        # 即 execute_single_tool_call 的 scenic_id 参数);否则从当前调用参数取。
        if key.startswith("$ctx."):
            upstream_key = key[len("$ctx."):]
            key_val = {"scenicId": scenic_id}.get(upstream_key)
        else:
            upstream_key = key
            key_val = merged_args.get(key)
        if key_val in (None, ""):
            if key.startswith("$ctx."):
                # 原先这里是 `continue`,注释写着「由路由层向用户确认」—— 但没有任何代码
                # 去确认。于是集团账号发起的请求:supplierId 被悄悄跳过,要么撞上一句看不懂的
                # 「参数不满足规范」白白消耗一次重试,要么直接不带参数去调接口拿回空数据,
                # 然后模型一本正经地告诉用户「没有数据」。缺的是只有用户能给的东西,就问用户。
                raise ClarificationNeeded(
                    SCENIC_QUESTION, slot="scenic_id",
                    detail=f"工具「{spec.name}」的参数 {param} 需要按景区自动获取,但本轮没有景区")
            # 前置工具的输入来自模型实参且缺失:交给后面的完整校验报「缺必填参数」
            continue

        cache_key = (tool_ref, json.dumps(key_val, ensure_ascii=False, sort_keys=True))
        task_cache = _TASK_PARAM_CACHE.setdefault(task_id, {})
        if task_id not in _TASK_PARAM_CACHE and len(_TASK_PARAM_CACHE) > _TASK_PARAM_CACHE_LIMIT:
            _TASK_PARAM_CACHE.clear()
            task_cache = _TASK_PARAM_CACHE.setdefault(task_id, {})
        if cache_key in task_cache:
            cached = task_cache[cache_key]
            if isinstance(cached, _UpstreamFailure):
                return {}, ToolCallOutcome("error", cached.message.replace("{target}", spec.name))
            merged_args[param] = cached
            continue

        upstream = bound_tools.get(tool_ref) or next(
            (t for t in bound_tools.values() if t.name == tool_ref), None)
        if upstream is None:
            # 声明里写的还是被内置取代的老工具名(人改过、没被启动自动修正的工具):
            # 改由内置那一份提供,不因为老工具没绑 / 已下线就整条调不通
            from cogni_agent.tool_center_service.builtin_tools import superseded_by

            alias = superseded_by(tool_ref)
            if alias:
                upstream = bound_tools.get(alias)
                if upstream is not None and alias == "sys_supplier_ids":
                    src = {**src, "path": "supplierId"}   # 内置返回 {scenicId, supplierId}
        if upstream is None:
            return {}, ToolCallOutcome(
                "error",
                f"调用工具「{spec.name}」失败:参数 {param} 需由系统通过前置工具「{tool_ref}」自动获取,"
                f"但该工具不在本次可用工具中。请检查该工具是否已绑定到当前技能。",
            )
        if getattr(upstream, "risk_level", "low") == "high":
            return {}, ToolCallOutcome(
                "error",
                f"调用工具「{spec.name}」失败:参数 {param} 的前置工具「{tool_ref}」为高风险操作,"
                f"系统不会自动调用。请联系配置方调整其风险等级,或改为显式引导用户处理。",
            )

        outcome = await execute_single_tool_call(
            db, task_id=task_id,
            tool_name=upstream.func_name or upstream.name,
            raw_arguments={upstream_key: key_val},
            bound_tools=bound_tools, scenic_id=scenic_id, role=role, user_id=user_id,
            call_id="", _param_source_depth=depth + 1,
        )
        # 内置工具的失败是「成功返回一个 {"error": …}」(给模型看的约定),不是 outcome 失败。
        # 不单独认的话会去这份报错里提取 supplierId,报出一句「路径不存在」,真正的原因
        # (认证无效 / 没配密钥 / 景区没开通)反而被盖掉了。
        upstream_err = None
        if outcome.ok:
            try:
                _body = json.loads(outcome.content) if isinstance(outcome.content, str) else outcome.content
            except (TypeError, ValueError):
                _body = None
            if isinstance(_body, dict) and _body.get("error") and len(_body) <= 2:
                upstream_err = str(_body["error"])
        if not outcome.ok or upstream_err:
            msg = (f"调用工具「{{target}}」失败:自动获取参数 {param} 失败"
                   f"(前置工具「{tool_ref}」:{upstream_err or outcome.content})。这是配置或上游接口的问题,改参数无法解决,"
                   f"请不要重试这类工具,直接告诉用户暂时取不到这部分数据。")
            task_cache[cache_key] = _UpstreamFailure(msg)
            return {}, ToolCallOutcome("error", msg.replace("{target}", spec.name))
        try:
            value = _extract_path(outcome.content, src.get("path") or "$")
        except KeyError as exc:
            msg = (f"调用工具「{{target}}」失败:自动获取参数 {param} 时,无法从前置工具"
                   f"「{tool_ref}」的结果中提取字段({exc})。请检查该参数的 x-param-source.path 声明。")
            task_cache[cache_key] = _UpstreamFailure(msg)
            return {}, ToolCallOutcome("error", msg.replace("{target}", spec.name))
        task_cache[cache_key] = value
        merged_args[param] = value

    return merged_args, None


def _check_scenic_scope(spec: ToolSpec, args: dict, scenic_id: str | None,
                        role: str) -> ToolCallOutcome | None:
    """参数级景区越权校验(租户隔离):景区级角色只能操作自己绑定的景区。

    集团级角色(super_admin / group_*)不受限。校验对象是 args(模型可控参数);
    forced_params 是系统按会话注入的可信值,不参与校验。
    防的是"会话绑定景区 A,参数里却填景区 B"的横向越权(真实会话
    conv_04f6701ff0324ceda82b47bf113c6786:绑定 PFTSCX00012325,却用
    PFTSCA01018088 成功调用舆情工具)。打回后模型会按 ask-before-act 向用户确认。

    抽成函数是因为「改参数后批准」也要走这一道 —— 审批人改出来的参数如果不复验,
    这个功能就成了绕过租户隔离的口子(任务仍以原用户身份执行)。
    """
    if not scenic_id or role in _GROUP_ROLES:
        return None
    fields = get_settings().get("runtime", "tools", "scenic_param_fields", default=_SCENIC_PARAM_FIELDS)
    for f in fields:
        v = args.get(f)
        if v in (None, ""):
            continue
        if str(v) != str(scenic_id):
            return ToolCallOutcome(
                "format_error",
                f"调用工具「{spec.name}」被拒绝:参数 {f} 指定的景区 ID({v})与当前账号绑定的"
                f"景区({scenic_id})不一致。当前账号仅能操作本景区的数据。"
                f"若确需查询其他景区,请切换到对应景区的账号,或联系管理员开通跨景区权限。",
            )
    return None


def _apply_approval_patch(spec: ToolSpec, merged_args: dict, resume: Any,
                          scenic_id: str | None, role: str) -> tuple[bool, dict, ToolCallOutcome | None]:
    """解析审批返回值,支持第三种动作:**改参数后批准**。

    审批人看到「要取消订单 A」时,常见需求是「改成取消订单 B 再执行」。
    原先只有批准/驳回两个动作,这种情况只能驳回、让用户从头再问一遍。

    resume 的两种形态(旧的照常认,不破坏既有审批):
      - bool:批准 / 驳回
      - dict:{"approved": bool, "params": {...}} —— params 是审批人改过的参数

    **改过的参数要重新过一遍校验**,而且是按**任务的**景区/角色,不是审批人的 ——
    任务恢复后仍以原用户身份执行,拿审批人的权限去放行,等于给了一条提权路径。
    """
    if not isinstance(resume, dict):
        return bool(resume), merged_args, None

    approved = bool(resume.get("approved", False))
    patch = resume.get("params")
    if not approved or not isinstance(patch, dict) or not patch:
        return approved, merged_args, None

    # 只允许改「模型可控参数」:forced_params 是系统按会话注入的可信值(如景区),
    # 让审批人改它等于把注入机制架空
    forced = set(spec.forced_params or {})
    dropped = [k for k in patch if k in forced]
    patched = {**merged_args, **{k: v for k, v in patch.items() if k not in forced}}
    if dropped:
        log.warning("审批改参忽略了系统注入参数 %s(工具 %s)", dropped, spec.name)

    patched = coerce_args_to_schema(spec.validate_schema or spec.json_schema, patched)
    try:
        jsonschema.validate(patched, spec.validate_schema or spec.json_schema
                            or {"type": "object", "properties": {}})
    except jsonschema.ValidationError as exc:
        return approved, merged_args, ToolCallOutcome(
            "format_error",
            f"审批人修改后的参数不满足「{spec.name}」的参数规范:{exc.message}。已取消本次执行,"
            "请在审批页改成合法值后重新提交。")
    except jsonschema.SchemaError:
        pass

    tenant_err = _check_scenic_scope(spec, patched, scenic_id, role)
    if tenant_err is not None:
        return approved, merged_args, tenant_err
    log.info("工具 %s 按审批人修改后的参数执行(改动字段:%s)", spec.name, sorted(patch))
    return approved, patched, None


async def execute_single_tool_call(
    db: AsyncSession,
    *,
    task_id: str,
    tool_name: str,
    raw_arguments: Any,
    bound_tools: dict[str, ToolSpec],
    scenic_id: str | None,
    role: str,
    user_id: str,
    call_id: str = "",  # 模型侧 tool_call id,用于定位暂存的凭证明文(§10.3)
    _param_source_depth: int = 0,  # 内部递归用:x-param-source 前置工具解析的嵌套深度
) -> ToolCallOutcome:
    spec = bound_tools.get(tool_name)
    if spec is None:
        available = "、".join(bound_tools.keys()) or "(当前无可用工具)"
        return ToolCallOutcome(
            "hallucination",
            f"工具「{tool_name}」不存在或不在本次调用的授权范围内。可用工具:{available}。请只从可用工具中选择,或直接给出最终回答。",
        )

    args, err = _parse_arguments(raw_arguments)
    if err:
        return ToolCallOutcome("format_error", f"调用工具「{tool_name}」失败:{err}。请重新以合法 JSON 输出该工具的参数。")

    # 上下文缺省必须在第一次校验**之前**补:模型没填 scenicId 时先补上,
    # 否则会先撞上「缺少必填参数」,反过来引导模型去编一个景区 ID。
    args = _apply_ctx_defaults(spec, args, scenic_id=scenic_id)
    # 系统参数(系统设置 · 参数配置 + 景区覆盖):同样在校验之前补 —— OTA 的 account 这类必填项
    # 配好了,就不该先撞上「缺少必填参数」、再引导模型去问用户。
    from cogni_agent.runtime_service.engine import scenic_params as _sp

    _sparams = await _sp.load(db, scenic_id)
    _pmap = _sp.by_key(_sparams)
    _inj = _sp.apply_to_args(
        spec.validate_schema or spec.json_schema, args, _sparams,
        param_refs=getattr(spec, "param_refs", None) or {},
        config_refs=_sp.refs_in(spec.type_config) | _sp.refs_in(spec.forced_params))
    if _inj.missing:
        # 工具里写了 {{参数}},本景区没值、系统也没默认值:这是配置问题,模型改参数没用
        return ToolCallOutcome(
            "error",
            f"调用工具「{spec.name}」需要系统参数 {'、'.join('{{' + k + '}}' for k in _inj.missing)},"
            f"但本景区没有配置、系统也没有默认值。请告诉用户:需要管理员在「APP 运营 · 景区配置 · 接口参数」"
            f"(或「系统设置 · 参数配置」)里填上。")
    args = _inj.args
    _injected = _inj.notes
    _ref_args = set((getattr(spec, "param_refs", None) or {}).keys())

    try:
        # 校验模型可控部分用 json_schema(可见 Schema):被 forced_params / param_sources
        # 剥离的参数模型看不到也不会提供,若按完整 validate_schema 校验必报 required。
        # 补齐后的完整实参在下方 merged_args 复验,系统注入的值不会绕过校验。
        # 引用了系统参数的入参不在模型可见 Schema 里(已剥离),不拿它去校验可见 Schema
        jsonschema.validate({k: v for k, v in args.items() if k not in _ref_args},
                            spec.json_schema or {"type": "object", "properties": {}})
    except jsonschema.ValidationError as exc:
        # 缺必填参数是「可恢复」错误里最常见的一类:优先引导模型向用户询问补齐,
        # 而不是反复用编造的参数值重试(ask-before-act)。其余 schema 错误走通用文案。
        if getattr(exc, "validator", None) == "required":
            missing = exc.validator_value
            missing_txt = "、".join(str(m) for m in missing) if isinstance(missing, list) else exc.message
            return ToolCallOutcome(
                "format_error",
                f"调用工具「{spec.name}」缺少必填参数:{missing_txt}。若这些参数必须由用户提供,请先向用户询问补齐,不要编造或猜测参数值。",
            )
        return ToolCallOutcome(
            "format_error",
            f"调用工具「{spec.name}」的参数不满足其参数规范:{exc.message}。请修正后按 JSON Schema 重新给出参数。",
        )
    except jsonschema.SchemaError:
        log.warning(f"tool {spec.id} 的 json_schema 本身不合法,跳过 Schema 校验")

    merged_args = {**args, **(spec.forced_params or {})}
    if spec.forced_params and _pmap:
        # 运行时覆盖的固定参数里也可以写 {{参数}};逐个调用的那个参数留到执行时再换
        _fan_key = _inj.fanout["key"] if _inj.fanout else None
        merged_args = _sp.render_obj(merged_args, {k: v for k, v in _pmap.items() if k != _fan_key})

    # 前置参数解析(x-param-source):声明了前置来源的参数由系统自动调用前置工具
    # 获取真实值注入,模型无权也不应提供(解析期已从模型可见 Schema 剥离)。即使
    # 模型/旧调用路径填了该参数,这里也会被真实值覆盖,杜绝臆造。
    if spec.param_sources:
        merged_args, src_err = await _resolve_param_sources(
            db, task_id=task_id, spec=spec, merged_args=merged_args,
            bound_tools=bound_tools, scenic_id=scenic_id, role=role, user_id=user_id,
            depth=_param_source_depth,
        )
        if src_err is not None:
            return src_err

    # 系统补齐(forced/前置解析)后的完整实参类型转换 + 复验:强制值与派生值同样要
    # 满足参数规范。转换放在所有注入完成之后、校验之前,一次覆盖全部来源
    # (模型实参 / forced_params / x-param-source)。
    if spec.param_sources or spec.forced_params:
        merged_args = coerce_args_to_schema(spec.validate_schema or spec.json_schema, merged_args)
        try:
            jsonschema.validate(merged_args, spec.validate_schema or {"type": "object", "properties": {}})
        except jsonschema.ValidationError as exc:
            return ToolCallOutcome(
                "format_error",
                f"调用工具「{spec.name}」失败:系统补齐后的参数不满足其参数规范:{exc.message}。",
            )
        except jsonschema.SchemaError:
            pass

    # 参数级景区越权校验(租户隔离):景区级角色只能操作自己绑定的景区。
    # 集团级角色(super_admin / group_*)不受限。校验对象是 args(模型可控参数);
    # forced_params 是系统按会话注入的可信值,不参与校验。
    # 防的是"会话绑定景区 A,参数里却填景区 B"的横向越权(真实会话
    # conv_04f6701ff0324ceda82b47bf113c6786:绑定 PFTSCX00012325,却用
    # PFTSCA01018088 成功调用舆情工具)。打回后模型会按 ask-before-act 向用户确认。
    tenant_err = _check_scenic_scope(spec, args, scenic_id, role)
    if tenant_err is not None:
        return tenant_err

    # §10.3 动作1:保存凭证工具的 username/password 绝不随 HITL 审批卡/审批表/checkpoint
    # 持久化——interrupt payload 用剥离后的参数。明文由 agent_node 剥离时暂存
    # credential_guard,审批通过后由 _execute_credential_save 取回使用。
    # v1.1.2(B 方案):这里的 args 已由 graphs._restore_credential_args_for_validation
    # 从 credential_guard 恢复为完整参数(校验用),但 interrupt payload 依旧剥离,
    # 审批卡/审批表不含明文。
    interrupt_params = merged_args
    if spec.type == "credential_save":
        interrupt_params = {k: v for k, v in merged_args.items() if k not in ("username", "password")}
        # E 方案(v0.1.5):确认卡片要展示"给哪个工具保存凭证",target_tool_id 在工具
        # 配置里而不在模型参数里,这里补上(非机密,可随 interrupt payload 传递)。
        interrupt_params.setdefault("target_tool_id", (spec.type_config or {}).get("target_tool_id"))

    if spec.risk_level == "high":
        # interrupt 在模块顶层(见文件开头):既允许没装 langgraph 时导入本模块,
        # 又保住它是个可被 patch 的模块属性 —— 审批链路的用例靠这一点才跑得起来
        approved = interrupt(
            {
                "task_id": task_id,
                "tool_id": spec.id,
                "tool_name": spec.name,
                "tool_type": spec.type,
                "params": interrupt_params,
                "risk_level": spec.risk_level,
            }
        )
        approved, merged_args, patch_err = _apply_approval_patch(
            spec, merged_args, approved, scenic_id, role)
        if patch_err is not None:
            return patch_err
        if not approved:
            if spec.type == "credential_save":
                # E 方案(v0.1.5):用户点「忽略」只是不保存凭证,不是取消整个任务——
                # 清理明文缓存(防泄漏),跳过保存,继续对话。
                credential_guard.pop_credential_args(credential_guard.stash_key(task_id, call_id))
                # v1.1.1:忽略后模型不再需要明文 → 标记「已处理」,由 tools_node 擦除
                # 本轮 HumanMessage、executor 收尾脱敏 payload.message(与"保存成功"同处理)。
                credential_guard.mark_handled(task_id)
                return ToolCallOutcome(
                    "ok",
                    f"用户未确认保存该工具的账号凭证,已跳过保存。"
                    f"后续调用「{spec.name}」若需要凭证,请引导用户重新提供账号密码。",
                )
            raise ApprovalRejected(f"高风险操作「{spec.name}」未获批准,任务已取消")

    try:
        if spec.type == "builtin":
            # 系统内置工具:不落库、不走沙箱、没有凭证,就是一段本地函数。
            # 时间这类能力每个景区都要、配法完全一样,却因为「没人配」而缺席 ——
            # 而模型自己推日期推错了很难发现:它照样给一份结构完整、数字却对不上的回答。
            from cogni_agent.tool_center_service import builtin_tools as _bi

            fn = _bi.get(spec.name)
            if fn is None:
                return ToolCallOutcome("error", f"内置工具「{spec.name}」不存在")
            call_kwargs = dict(merged_args)
            # 景区由执行上下文给,不让模型自己填 —— 填了就是跨租户
            if "scenic_id" in ((spec.json_schema or {}).get("properties") or {}):
                call_kwargs["scenic_id"] = scenic_id or call_kwargs.get("scenic_id") or ""
            # 任务号:数据工具按它去取本任务留存的数据集(databuf)。
            # **不进 schema**,所以模型看不见也填不了 —— 能填的话就是跨任务读别人的数据。
            # 所有内置函数签名都收 **_,多给一个参数不会炸。
            call_kwargs["_task_id"] = task_id
            result = (await fn(**call_kwargs) if inspect.iscoroutinefunction(fn)
                      else fn(**call_kwargs))
        elif spec.type == "credential_save":
            # 返回 (kind, content),避免被下方统一包装成 ("ok", ...) 二次套壳
            _kind, _content = await _execute_credential_save(db, task_id, call_id, spec, merged_args, user_id, scenic_id)
            return ToolCallOutcome(_kind, _content)
        elif spec.type == "knowledge":
            # 知识检索伪工具:不走 tool_center 的注册表/沙箱,直接调知识中心的多源路由。
            # 可用来源在 resolver 解析 Skill 时就定死了(type_config["sources"]),模型只能
            # 在这个范围内选;允许模型自由指定 sources 等于让它绕过 Skill 的知识源配置。
            allowed = list(spec.type_config.get("sources") or ["vector"])
            requested = merged_args.get("sources")
            sources = [s for s in requested if s in allowed] if requested else allowed
            if not sources:
                sources = allowed
            result = await route_and_retrieve(scenic_id or "", merged_args.get("query", ""), sources)
        else:
            # §10.1 M0(v0.1.4):凭证必属具体用户,按 (user_id, scope) 走两级降级链;
            # 无凭证时不注入(由模型引导用户交互提供)。credential_scope 是保留参数,
            # 只用于选凭证,不随调用发给沙箱(避免多出一个业务无关的请求字段)。
            inject_scope = merged_args.get("credential_scope")
            call_args = (
                {k: v for k, v in merged_args.items() if k != "credential_scope"}
                if "credential_scope" in merged_args else merged_args
            )
            # v1.1.10:把解析层已解析的配置(type/type_config)传下去 —— spec 来源 =
            # pin 版本快照(已发布)或直连主表;修复「执行重读主表」导致已发布新版工具
            # 仍按创建初值配置执行的漂移(主表内容列不随版本化更新)。
            if _inj.fanout:
                result = await _invoke_fanout(db, spec, scenic_id, role, call_args, user_id=user_id,
                                              scope=inject_scope, params=_sparams, fanout=_inj.fanout)
            else:
                result = await invoke_tool(db, spec.id, scenic_id or "", role, call_args, user_id=user_id,
                                           scope=inject_scope, type_config=spec.type_config or {},
                                           tool_type=spec.type, extra_vars=_sp.template_vars(_sparams))
    except ApprovalRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - 工具执行失败按"错误"返回给模型,不直接炸掉整个任务
        log.exception(f"tool {spec.id} 执行失败")
        return ToolCallOutcome("error", f"工具「{tool_name}」执行出错:{exc}")

    _out = ToolCallOutcome("ok", result, raw_result=result)
    _out.injected = _injected
    return _out


async def _invoke_fanout(db, spec, scenic_id, role, call_args: dict, *, user_id, scope,
                         params, fanout: dict) -> dict:
    """多值参数逐个调用(multi_mode=fanout):每个值各调一次工具,结果合并成一份交给模型。

    典型场景:一个景区有两个 OTA 商户账号,接口一次只收一个 account。
    单个值调用失败不影响其它值 —— 失败的那一项在结果里写明原因,模型据此说「某账号没取到」。
    敏感参数的值不写进结果(结果会进上下文和日志),只写备注 / 序号。
    """
    from cogni_agent.runtime_service.engine import scenic_params as _sp

    pmap = _sp.by_key(params)
    p = pmap.get(fanout["key"])
    items = []
    for i, pv in enumerate(fanout["values"], 1):
        args_i = dict(call_args)
        for a in fanout.get("args") or []:
            args_i[a] = pv.value
        args_i = _sp.render_obj(args_i, pmap, pick={fanout["key"]: pv.value})
        shown = (pv.label or f"第 {i} 个") if (p and p.is_secret) else pv.value
        try:
            r = await invoke_tool(db, spec.id, scenic_id or "", role, args_i, user_id=user_id,
                                  scope=scope, type_config=spec.type_config or {}, tool_type=spec.type,
                                  extra_vars=_sp.template_vars(params, pick={fanout["key"]: pv.value}))
            items.append({"value": shown, "label": pv.label, "ok": True, "result": r})
        except Exception as exc:  # noqa: BLE001 - 单个值失败只记在这一项上
            log.warning(f"工具 {spec.id} 按 {fanout['key']} 第 {i} 个值调用失败:{exc}")
            items.append({"value": shown, "label": pv.label, "ok": False, "error": str(exc)[:300]})
    return {
        "fanout": {"param": fanout["key"], "label": fanout.get("label"), "count": len(items),
                   "note": f"按「{fanout.get('label')}」的 {len(items)} 个值各调用了一次,结果逐项列出;"
                           f"汇总时注意区分,不要把某一项当成全部"},
        "results": items,
    }


async def _execute_credential_save(
    db: AsyncSession,
    task_id: str,
    call_id: str,
    spec: ToolSpec,
    args: dict,
    user_id: str,
    scenic_id: str | None = None,
) -> tuple[str, str]:
    """§10.2 内建「保存凭证」工具:审批通过后从内存缓存取回明文并加密入库。

    明文在 agent_node 剥离 tool_calls 时已暂存 credential_guard(不入 checkpoint),
    这里 pop 出来使用。进程重启导致缓存丢失时安全降级——返回错误引导用户重新提供,
    绝不以任何形式回显明文。返回 (kind, content),由调用方包装成 ToolCallOutcome。
    """
    key = credential_guard.stash_key(task_id, call_id)
    pending = credential_guard.pop_credential_args(key)
    if pending is None:
        return (
            "error",
            "凭证保存请求已过期(审批期间服务可能重启),无法安全保存。请引导用户重新提供账号密码后再试。",
        )
    username = str(pending.get("username") or "").strip()
    password = str(pending.get("password") or "")
    if not username or not password:
        return ("format_error", "缺少账号或密码,无法保存。请让用户提供完整的账号密码。")
    # v0.1.4:凭证必属具体用户。无调用者身份(如未登录)时拒绝保存,引导交互提供身份,
    # 绝不落成共享凭证。
    uid = user_id.strip() if isinstance(user_id, str) else ""
    if not uid:
        return (
            "format_error",
            "当前会话没有用户身份,无法保存凭证。请先登录,再向用户索取账号密码后重试。",
        )
    target_tool_id = pending.get("target_tool_id") or (spec.type_config or {}).get("target_tool_id")
    if not target_tool_id:
        return ("error", "保存凭证失败:未关联目标工具。")
    scope = pending.get("scope") or args.get("scope") or None
    # 复用调用方会话(与 §10.3 动作 3 的 Task.payload.message 脱敏同事务),此处 commit。
    await save_credential(
        target_tool_id, f"{username}:{password}",
        user_id=uid, scope=scope or None,
        created_by=uid, db=db,
    )
    await db.commit()
    # §10.2 审计留痕:谁在何时为哪个工具的哪个场景保存了凭证——detail 不含明文。
    await write_audit(
        "credential.saved",
        scenic_id or "",
        uid,
        {
            "task_id": task_id,
            "tool_id": target_tool_id,
            "scope": scope or None,
        },
    )
    # v1.1.1:保存**成功**后才标记「已处理」——由 tools_node 擦除本轮 HumanMessage、
    # executor 收尾脱敏 payload.message。失败(format_error/error)不置位,
    # 模型保留原文可自行纠错(框架能力测试 01 文档)。
    credential_guard.mark_handled(task_id)
    return (
        "ok",
        "账号凭证已安全保存。后续调用该工具时将自动使用,无需用户再次提供账号密码。",
    )
