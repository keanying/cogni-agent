"""
M4-03/04/05:LangGraph 图模板——"解析/执行引擎不是 if-else,而是运行时状态机"的落地。

架构图 ④ runtime-service 要求的编排能力是「固定DAG / 动态ReAct」两类,对应到这里:

    ┌─ build_agent_graph()  ── L0 / L1(线性链)/ L2(ReAct)共用一张 ReAct 形状的图
    ├─ build_dag_graph()    ── L1(带依赖的固定 DAG):按拓扑层推进,同层并发
    └─ build_direct_tool_graph() ── 单 Tool/MCP 直调,不经 LLM 推理层

**为什么 ReAct 那三档共用一张图**:L0/L1线性/L2 的区别只在"每一轮把哪些工具暴露给
模型、要不要在没有更多工具调用时推进到下一个 Skill",拓扑结构是同一个:

    START -> agent -> [tools | advance_skill | END]
              ^          |           |
              |__________|___________|

**为什么 DAG 要单独一张图**:DAG 的拓扑本身就不一样——多个 Skill 之间存在依赖关系、
同层可以并发、每个 Skill 是一个自包含的子任务而不是共享一条消息流水。硬塞进 ReAct
循环只会把两件事都做拧。线性链是 DAG 的退化情形(边全是 a->b->c),所以没有登记
依赖关系时会自动派生线性边,行为与原来完全一致。

"处理不确定性"四件套在这些图里落地:
- 幻觉/格式错误 -> tools 节点委托 tools_adapter,按 kind 分支"打回自纠 / 熔断"
- 目标漂移      -> 按配置步数间隔做一次轻量自检,纠偏建议作为系统提示注入
- 高风险动作    -> tools_adapter 内部 interrupt() 暂停整张图,转 WAITING_HUMAN
- 提示词注入    -> agent 节点入口做输入侧审核,出口做输出侧审核(架构图 ④ llm 要求)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import GuardrailTripped
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.knowledge_service.router import route_and_retrieve
from cogni_agent.runtime_service.engine import context as ctx
from cogni_agent.runtime_service.engine import credential_guard
from cogni_agent.runtime_service.engine import guardrails as gr
from cogni_agent.runtime_service.engine import resilience as res
from cogni_agent.runtime_service.engine import planner
from cogni_agent.runtime_service.engine import summarize as summarizer
from cogni_agent.control_plane_service import memory as ctx_memory
from cogni_agent.tool_center_service.tool import stats as tool_stats
from cogni_agent.runtime_service.engine.prompt_log import record_prompt
from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill, ToolSpec
from cogni_agent.runtime_service.engine.state import RunState
from cogni_agent.runtime_service.engine.tools_adapter import (
    bound_tools_map, execute_single_tool_call, tool_function_schemas,
)
from cogni_agent.runtime_service.llm import client as llm_client
from cogni_agent.runtime_service.llm import moderation

log = get_logger("runtime_service.graphs")


class OutputBlocked(Exception):
    """输出侧内容审核未通过——由 executor 捕获后把任务判为 FAILED 并记审计。"""


# ══════════════════════════════════════════════════════════════════════════
# 公共辅助
# ══════════════════════════════════════════════════════════════════════════
def _tl_ts(event: dict) -> dict:
    """timeline 事件打上真实产生时刻(monotonic + 墙钟)并返回副本。

    此前 timeline 事件不记录产生时刻,耗时全靠 SSE 消费端/收尾遍历时刻近似:
    实时流错位一帧、落库 timeline duration 不准。这里在事件产生时打点,
    executor 的 _with_step_durations 与实时 SSE 统一按 ts 相邻间隔计算,
    回放/落库/实时三处口径一致。无条件覆盖 ts,避免复用 dict 时沿用旧时刻。
    wall 是墙钟时间(绝对时间点),供前端「执行链分析」展示每一步发生在几点几分。
    """
    _now = datetime.now()
    return {**event, "ts": time.monotonic(),
            "wall": _now.strftime("%H:%M:%S"),
            # 毫秒级墙钟:同一秒里的几步(并行工具、紧挨着的生成)也排得出先后
            "wall_ms": _now.strftime("%H:%M:%S.") + f"{_now.microsecond // 1000:03d}"}


def _persona_preamble(persona: dict | None) -> str:
    if not persona:
        return ""
    parts = []
    if persona.get("name"):
        parts.append(f"你的名字是「{persona['name']}」。")
    if persona.get("persona_desc"):
        parts.append(f"性格设定:{persona['persona_desc']}")
    if persona.get("tone_style"):
        parts.append(f"语气风格:{persona['tone_style']}。")
    return " ".join(parts)


def _today_hint() -> str:
    """把「今天」的准确日期注入系统提示,避免模型把当前时间幻觉成训练语料里的旧日期。"""
    return f"今天是 {datetime.now().strftime('%Y-%m-%d')}。"


def _scenic_context_hint(state: RunState) -> str:
    """把会话绑定的景区 ID 无条件注入系统提示(租户上下文注入)。

    此前景区 ID 只依赖两条链路:skill prompt 模板的 {{scenic_id}} 占位符、知识检索命中。
    两者都不保证出现——模板没写占位符/检索没命中时,模型完全不知道自己在哪个景区,
    于是出现"会话明明绑定了景区,模型却反问用户景区 ID"的体验问题
    (真实会话 conv_04f6701ff0324ceda82b47bf113c6786 第一轮)。这里做无条件兜底注入。
    """
    scenic_id = (state.get("scenic_id") or "").strip()
    if not scenic_id:
        return ""
    return (
        "当前执行上下文(必须遵守):\n"
        f"- 当前会话绑定的景区 ID:{scenic_id}\n"
        "- 用户的请求默认针对该景区。调用工具需要景区 ID 且用户未另行指定时,"
        "直接使用上述景区 ID,不要向用户反问景区 ID。\n"
        "- 若用户明确要求查询其他景区的数据,请告知其需要提供相应景区的 ID,不要编造或越权。"
    )


def _tool_usage_policy() -> str:
    """全局工具调用行为规范(ask-before-act,行业通用约束):缺参先问、不编造、不越权。

    挂在每一轮 system prompt 尾部,让「先询问再调用」成为模型的硬约束而不是靠 schema
    description 隐式引导——否则模型更倾向于编造一个看似合理的参数值(如虚构的景区 ID)。
    """
    return (
        "工具调用行为规范(必须遵守):\n"
        "1. 调用工具前先检查其必填参数是否齐备。若缺少的信息必须由用户提供(如景区、日期、账号等),"
        "先向用户询问补齐,不要编造或猜测参数值。\n"
        "2. 工具调用返回参数校验失败时,先核对缺失/出错的字段:能从上下文推断的补齐后重试;"
        "必须由用户提供的,明确向用户询问,不要反复用错误参数重试。\n"
        "3. 严禁凭空捏造不存在的 ID、名称、日期或接口数据;宁可询问,不可编造。\n"
        "4. 基于工具调用结果作答时,在回答中说明数据来源(如\"根据XX系统数据/XX统计\"),"
        "不要把它伪装成自行推算的结果。\n"
        "5. 需要多个互不依赖的工具(或同一工具的不同时间段)时,**在同一轮里一次性并行发起全部调用**,"
        "不要一轮只调一个、看完结果再调下一个 —— 每多一轮就多一次完整的模型调用。"
    )


def _answer_layout_policy() -> str:
    """回答的排版规范:回答主要在 APP(手机竖屏)上看。

    用户截图(「人生阶段占比分布」):从第三节起是 ①~⑤ 每条再挂「归因」子弹、
    一张七列的表、一段 ⚠ 说明、总结、建议、「如需…」—— 手机上要划好几屏,
    而图里已经画出来的数字又在正文里逐项抄了一遍。这里给出固定的骨架和上限。
    """
    return (
        "回答排版规范(回答在手机上看,必须遵守):\n"
        "1. 第一句直接给结论(一句话,带最关键的 1~2 个数)。\n"
        "2. 正文最多 3 个小节,小节标题用「### 」;每节最多 3 条要点,每条一行、不超过 40 字。"
        "不要用 ①②③ 再嵌套「归因/原因/说明」子弹;原因写在同一行的破折号后面。\n"
        "3. 已经用 sys_chart 画出来的数,正文只点出最高/最低/异常的那几个,不要逐项再列一遍。\n"
        "4. 表格只在确实要对照多列时用:最多 4 列、8 行;超出的在正文说一句「其余见图」。\n"
        "5. 数据口径 / 局限只写一句,不要单独成段、不要 ⚠ 符号。\n"
        "6. 最后一节「建议」最多 3 条;不要「如需…可以…」这类结尾套话。\n"
        "7. 用户问的是某一个维度(如「人生阶段」)时,只讲这一个维度,其它维度的数据不要展开。"
    )

# 历史轮里助手的回答最多带多少字进上下文。
# 任务 5877db72:前两轮各是一份三千多字的完整报告(表格 + 六张图的解读),原样带进每一次
# 内容生成 —— 五次生成就是五遍预填充;模型还会照着上一份报告的结构把整份重做一遍。
# 上一轮的结论、数字、表头都在开头部分,截到这个长度足够接上话题。
PRIOR_ANSWER_CHARS = 1500


def _clip_prior_answer(text: str, limit: int | None = None) -> str:
    limit = limit or int(_runtime_cfg_int("exec_history_answer_chars", PRIOR_ANSWER_CHARS))
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(上一轮回答较长,此处只保留前 {limit} 字,原长 {len(text)} 字;需要其中数字请重新取数)"


def _runtime_cfg_int(key: str, default: int) -> int:
    try:
        from cogni_agent.config import get_settings

        v = get_settings().get("runtime", key, default=default)
        return int(v) if v else default
    except Exception:  # noqa: BLE001 - 配置读不到就用默认值
        return default


def _llm_perf(result, outgoing: list[dict]) -> dict:
    """一次模型调用的耗时 / 体量拆分,进执行日志(「内容生成」节点)。

    以前一次生成只有一个 duration:86 秒到底是排队、是在思考、还是在吐字,看不出来,
    也就不知道该砍上下文、换模型还是改提示词。
      queue_ms  = 发出 → 首个分片(排队 + 预填充;上下文越长越大)
      think_ms  = 首个分片 → 首个正文 / 工具调用(推理模型在思考)
      output_ms = 首个正文 → 结束(吐字;输出越长越大)
    """
    ttft = float(getattr(result, "ttft_ms", 0) or 0)
    first = float(getattr(result, "first_output_ms", 0) or 0) or ttft
    total = float(getattr(result, "total_ms", 0) or 0)
    sys_chars = sum(len(str(m.get("content") or "")) for m in outgoing if m.get("role") == "system")
    all_chars = sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
                    for m in outgoing)
    return {
        "model": getattr(result, "model", None),
        "prompt_tokens": getattr(result, "prompt_tokens", 0),
        "completion_tokens": getattr(result, "completion_tokens", 0),
        "queue_ms": round(ttft, 1),
        "think_ms": round(max(0.0, first - ttft), 1),
        "output_ms": round(max(0.0, total - first), 1) if total else 0.0,
        "total_ms": round(total, 1),
        "reasoning_chars": len(getattr(result, "reasoning_content", "") or ""),
        "output_chars": len(getattr(result, "content", "") or ""),
        "messages": len(outgoing),
        "system_chars": sys_chars,
        "prompt_chars": all_chars,
    }


def _gen_detail(result, outgoing: list[dict], *, tools, round_no: int, history_len: int,
                history_clipped: int, started, wall_ms: float) -> dict:
    """一次「内容生成」的完整留痕(导出执行详情时逐项列出)。

    这一步最容易「慢得莫名其妙」:86 秒里模型想了什么、上下文里塞了什么、给了它几个工具、
    最后决定调哪些工具带什么参数 —— 以前一样都看不到,只能猜。
    思考内容只留开头和结尾(中间往往是反复推演),够判断它卡在哪。
    """
    reasoning = getattr(result, "reasoning_content", "") or ""
    content = getattr(result, "content", "") or ""
    roles: dict[str, int] = {}
    tool_chars = 0
    for m in outgoing:
        r = str(m.get("role") or "?")
        roles[r] = roles.get(r, 0) + 1
        if r == "tool":
            tool_chars += len(str(m.get("content") or ""))
    return {
        "round": round_no,
        "started_at": started.strftime("%H:%M:%S.") + f"{started.microsecond // 1000:03d}",
        "wall_ms": round(wall_ms, 1),
        "finish_reason": getattr(result, "finish_reason", None),
        "tools_offered": len(tools or []),
        "context": {"messages_by_role": roles, "history_messages": history_len,
                    "history_answers_clipped": history_clipped, "tool_result_chars": tool_chars},
        "reasoning_head": reasoning[:600],
        "reasoning_tail": reasoning[-300:] if len(reasoning) > 900 else "",
        "output_preview": content[:400],
        "tool_calls": [{"name": tc.get("name"), "args": str(tc.get("arguments") or "")[:600]}
                       for tc in (getattr(result, "tool_calls", None) or [])],
    }


def _effective_model(skill: ResolvedSkill | None, model_override: str | None) -> str | None:
    """本轮实际使用的模型。优先级:运行时覆盖 > Skill 级配置 > 系统默认(None 交给 client 兜底)。

    Skill 级模型覆盖的意义:一条编排里各步骤对模型能力的要求往往差很多——意图分类/
    字段抽取用便宜的小模型就够,规划/总结才需要强模型。允许按 Skill 配置能显著降本。
    """
    if model_override:
        return model_override
    if skill is not None and skill.model:
        return skill.model
    return None


def _effective_response_format(skill: ResolvedSkill | None, has_tools: bool) -> dict | None:
    """结构化输出。带工具时不启用——json_object 模式与 function calling 在多数厂商
    上互斥(模型要么产出 JSON 要么产出 tool_calls),同时开会直接报错或让工具失效。"""
    if skill is None or not skill.response_format or has_tools:
        return None
    return skill.response_format


def _current_skill(state: RunState, skill_chain: list[ResolvedSkill]) -> ResolvedSkill | None:
    if not skill_chain:
        return None
    idx = min(state.get("skill_idx", 0), len(skill_chain) - 1)
    return skill_chain[idx]


def _current_tools(state: RunState, skill_chain: list[ResolvedSkill], composition_hint: str | None) -> list[ToolSpec]:
    if not skill_chain:
        return []
    if composition_hint == "pool" and len(skill_chain) > 1:
        merged: dict[str, ToolSpec] = {}
        for skill in skill_chain:
            for t in skill.tools:
                merged.setdefault(t.id, t)
        return list(merged.values())
    skill = _current_skill(state, skill_chain)
    return list(skill.tools) if skill else []


# 系统级伪工具:与具体业务无关,任何一轮都可能被模型需要(补知识/存凭证),始终暴露。
_SYSTEM_TOOL_TYPES = ("knowledge", "credential_save")


def _expose_subset_cfg() -> tuple[bool, int, int]:
    """按需暴露工具子集的开关与参数(runtime.tools.expose_subset / _k / _max)。"""
    settings = get_settings()
    enabled = bool(settings.get("runtime", "tools", "expose_subset", default=True))
    k = int(settings.get("runtime", "tools", "expose_subset_k", default=6))
    total_max = int(settings.get("runtime", "tools", "expose_subset_max", default=12))
    return enabled, max(1, k), max(k + 2, total_max)


def _tool_index_text(t: ToolSpec) -> str:
    """工具的检索索引文本:展示名 + 描述 + schema 描述,全部小写。"""
    parts = [t.name, getattr(t, "description", None) or ""]
    schema = getattr(t, "json_schema", None) or {}
    desc = schema.get("description") if isinstance(schema, dict) else None
    if isinstance(desc, str):
        parts.append(desc)
    return " ".join(p for p in parts if p).lower()


def _ngrams(text: str, n: int = 2) -> list[str]:
    """轻量切词:英文/数字按词、中文按 2-gram。确定性、无外部依赖。"""
    tokens: list[str] = []
    for m in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text):
        if m.isascii():
            tokens.append(m)
        else:
            tokens.append(m)
            if len(m) >= 2:
                tokens.extend(m[i:i + n] for i in range(len(m) - n + 1))
    return tokens


def _rank_candidate_tools(tools: list[ToolSpec], query: str) -> list[ToolSpec]:
    """目标检索式工具排序:查询 n-gram 在工具索引文本里的命中加权打分。

    命中工具名权重 3(工具名是语义密度最高的地方),命中描述权重 1;
    工具名整体出现在查询里是强信号,额外 +5。同分保持原顺序,保证确定性;
    查询为空或无命中时原序返回(兜底不空手)。
    """
    if not query or not query.strip():
        return tools
    q_terms = _ngrams(query.lower())
    if not q_terms:
        return tools
    name_low = query.lower()
    scored: list[tuple[int, ToolSpec]] = []
    for t in tools:
        idx = _tool_index_text(t)
        score = 0
        for term in q_terms:
            if term in t.name.lower():
                score += 3
            elif term in idx:
                score += 1
        if name_low and t.name.lower() in name_low:
            score += 5
        if score:
            scored.append((score, t))
    if not scored:
        return tools
    scored.sort(key=lambda x: x[0], reverse=True)
    # 0 分工具不丢弃,按原顺序排在队尾兜底——否则"没命中"的工具会在本轮消失
    scored_ids = {id(t) for _, t in scored}
    misses = [t for t in tools if id(t) not in scored_ids]
    return [t for _, t in scored] + misses


def _used_tool_names(state: RunState) -> set[str]:
    """从消息历史提取已真实发出的工具调用(模型侧函数名)。首轮为空。"""
    used: set[str] = set()
    for m in state.get("messages") or []:
        raw = getattr(m, "additional_kwargs", {}).get("tool_calls_raw")
        if isinstance(raw, list):
            for c in raw:
                name = c.get("name") if isinstance(c, dict) else None
                if name:
                    used.add(name)
    return used


def _exposed_tools(state: RunState, skill_chain: list[ResolvedSkill],
                   composition_hint: str | None) -> list[ToolSpec]:
    """v1.1.2:按需暴露工具子集,替代"每轮全量注入 skill 下所有工具"。

    行业做法:OpenAI 官方建议"尽量少给工具";Coze/GPTs 按插件分组渐进暴露——
    每轮给模型"够用"的工具,而不是 16 个 schema 全量 prefill(舆情 skill 正是
    16 个工具,每轮全量注入的 schema prefill 是总结轮变慢的构成之一)。

    暴露集合 = 系统级工具(knowledge_search/credential_save,常驻)
              ∪ 已调用过的工具(常驻,保证复用与修正参数重试)
              ∪ 按用户目标检索的 top-K 候选(相关性打分,确定性、无 embedding 依赖)

    检索可能漏掉"该用但没命中"的工具,故用 K + 总上限双约束,并保留
    runtime.tools.expose_subset 开关随时回退全量。
    """
    all_tools = _current_tools(state, skill_chain, composition_hint)
    if not all_tools:
        return all_tools
    enabled, k, total_max = _expose_subset_cfg()
    if not enabled:
        return all_tools
    pinned = [t for t in all_tools if getattr(t, "type", None) in _SYSTEM_TOOL_TYPES]
    pinned_ids = {t.id for t in pinned}
    used_names = _used_tool_names(state)
    used = [t for t in all_tools
            if t.id not in pinned_ids and (t.func_name in used_names or t.name in used_names)]
    used_ids = {t.id for t in used}
    rest = [t for t in all_tools if t.id not in pinned_ids and t.id not in used_ids]
    ranked = _rank_candidate_tools(rest, state.get("goal") or "")
    # 新增候选严格限制在 k 个;total_max 只做防御性兜底(已用/系统工具已占满时不再补)
    headroom = max(0, total_max - len(pinned) - len(used))
    chosen = ranked[:min(k, headroom, len(rest))]
    return [*pinned, *used, *chosen]


def _builtin_tool_specs() -> list[ToolSpec]:
    from cogni_agent.runtime_service.engine.resolver import _builtin_specs

    return _builtin_specs()


def _with_param_source_upstreams(exposed: list[ToolSpec], all_tools: list[ToolSpec]) -> list[ToolSpec]:
    """执行用的工具表 = 给模型看的那一小撮 ∪ 它们声明的前置工具(x-param-source)。

    给模型看的是按相关性挑的子集(省 schema prefill),前置工具常常不在里面 ——
    现场:第一轮只暴露了 4 个票务工具,sys_supplier_ids 没被挑中,4 个调用全报
    「前置工具不在本次可用工具中」;第二轮它碰巧被挑进来,同样的调用就走通了。
    前置工具是系统替模型调的,不该受「给模型看哪些」的影响。
    """
    from cogni_agent.tool_center_service.builtin_tools import superseded_by

    have = {t.name for t in exposed}
    need: set[str] = set()
    for t in exposed:
        for src in (getattr(t, "param_sources", None) or {}).values():
            ref = str((src or {}).get("tool") or "").strip()
            if ref and ref not in have:
                need.add(ref)
                alias = superseded_by(ref)
                if alias:
                    need.add(alias)
    extra = [t for t in all_tools if t.name in need and t.name not in have]
    return [*exposed, *extra] if extra else exposed


def _last_human_after(state) -> int:
    """本轮起点:最后一条 HumanMessage 的下标(其后 = 本轮产生的消息)。

    32 号:strict_tool_execution 的首轮判定只看本轮——非固定问题 thread 共享,
    旧轮的 assistant 工具调用不该让本轮豁免强制取数。
    """
    msgs = state.get("messages") or []
    idx = 0
    for i, m in enumerate(msgs):
        if isinstance(m, HumanMessage):
            idx = i + 1
    return idx


def _current_turn_messages(state) -> list:
    """本轮的消息:从最后一条 HumanMessage **开始**(含),到目前为止。

    与 _last_human_after 的差别是**含不含那条 Human**:那个函数找的是「本轮产生的
    消息从哪儿开始」(用来判断本轮有没有发起过工具调用),这里要的是「发给模型的
    本轮上下文」,必须把用户问的那句话本身带上 —— 不带就等于让模型盲答。

    一条 HumanMessage 都没有时返回全部(保守:宁可多发,也不要发出一个空上下文)。
    """
    msgs = state.get("messages") or []
    start = None
    for i, m in enumerate(msgs):
        if isinstance(m, HumanMessage):
            start = i
    return msgs[start:] if start is not None else list(msgs)


def _current_system_prompt(state: RunState, skill_chain: list[ResolvedSkill], composition_hint: str | None, persona: dict | None) -> str:
    preamble = _persona_preamble(persona)
    if not skill_chain:
        body = "请根据用户的请求,尽力给出准确、有帮助的回答。"
    elif composition_hint == "pool" and len(skill_chain) > 1:
        sections = [f"【能力:{s.name}】\n{s.system_prompt}" for s in skill_chain]
        body = "你可以综合运用以下几项能力(工具已在同一个可选范围内提供,自行判断调用哪个/哪些):\n\n" + "\n\n".join(sections)
    else:
        skill = _current_skill(state, skill_chain)
        body = skill.system_prompt if skill else ""
    # ── 意图层五机制落地点 ──
    # ② Slot Filling:把会话级参数槽注入提示词,让模型优先用已知参数填工具必填项,
    #    而不是再向用户重复追问已给过的信息(如 APP 端选的日期、上一轮说过的景区)。
    slots = state.get("slots") or {}
    if slots:
        slots_text = ", ".join(f"{k}={v}" for k, v in slots.items())
        body += f"\n\n已知上下文参数(来自会话,工具参数优先使用,不要再向用户追问这些):\n{slots_text}"
    # ④ Plan-and-Execute:Triager 给出分步计划时注入,要求模型按计划推进。
    plan = state.get("plan") or []
    if plan:
        plan_text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(plan))
        body += f"\n\n请按以下执行计划推进(如执行中发现计划不合理,可自行调整):\n{plan_text}"
        # 固定问题的「问题描述」接在最后:它是针对这一个问题的具体约束,
    # 应该盖过通用的技能说明(比如技能说「默认查近 7 天」,而这个问题要求「只看今天」)。
    # 上下文记忆接在最前(补充背景),问题口径接在最后(具体约束应盖过通用说明)。
    # 记忆段里已写明「若与本次请求冲突以本次为准」,避免旧偏好压过用户这轮的话。
    _mem = state.get("memory_block") or ""
    _qi = state.get("question_instruction") or ""
    _base = "\n\n".join(p for p in (preamble, body, _tool_usage_policy(), _answer_layout_policy(), _scenic_context_hint(state), _today_hint()) if p)
    _out = _base + _mem
    return (_out + f"\n\n【本次问题的口径要求】\n{_qi}") if _qi else _out


def _strip_tool_section(prompt: str) -> str:
    """剥掉 resolver 拼进 skill.system_prompt 的【工具使用说明】段。

    收尾轮模型不再调用工具,这段"什么时候用哪个工具"的说明对总结无意义,剥掉
    能省 prefill;业务指令(如舆情报告要包含哪些板块)仍保留。
    """
    idx = prompt.find("\n\n【工具使用说明】")
    return prompt[:idx] if idx != -1 else prompt


def _final_answer_system_prompt(state: RunState, skill_chain: list[ResolvedSkill],
                                composition_hint: str | None, persona: dict | None) -> str:
    """收尾轮的精简总结上下文:不再注入工具行为规范与工具使用说明。

    行业做法(OpenAI/Anthropic 的 function calling 循环):进入最终回答阶段后,
    模型的任务是"基于工具结果总结",上下文里只剩 目标锚点 + 工具结果 + 人格 +
    业务指令;工具 schema(tools 参数)、工具行为规范、工具使用说明全部剥离。
    prefill 省掉的不只是 N 个工具 schema,还有每轮重复的几百 token 工具指令。
    """
    preamble = _persona_preamble(persona)
    tail = ("请基于已获取的数据与工具执行结果,直接给出最终回答。"
            "不要编造任何不在工具结果中的数据,也不要再尝试调用任何工具。")
    if not skill_chain:
        body = tail
    elif composition_hint == "pool" and len(skill_chain) > 1:
        sections = [f"【能力:{s.name}】\n{_strip_tool_section(s.system_prompt)}" for s in skill_chain]
        body = "你可以综合运用以下几项能力(工具已全部执行完毕):\n\n" + "\n\n".join(sections) + "\n\n" + tail
    else:
        skill = _current_skill(state, skill_chain)
        prompt = _strip_tool_section(skill.system_prompt) if skill else ""
        body = (prompt.rstrip() + "\n\n" + tail) if prompt.strip() else tail
    slots = state.get("slots") or {}
    if slots:
        slots_text = ", ".join(f"{k}={v}" for k, v in slots.items())
        body += f"\n\n已知上下文参数(来自会话,不要向用户重复追问):\n{slots_text}"
    return "\n\n".join(p for p in (preamble, body, _scenic_context_hint(state), _today_hint()) if p)


async def _maybe_retrieve_knowledge(state: RunState, skill: ResolvedSkill | None) -> tuple[str | None, dict | None]:
    """Skill 进入新一轮(该 Skill 第一步)且配置了知识源时,自动做一次检索。"""
    if skill is None or not skill.knowledge_sources:
        return None, None
    # knowledge_mode=tool 时,检索完全交给模型调用 knowledge_search,这里不再前置注入
    # (否则模型会先拿到一份没头没尾的背景资料,再自己去查一遍,重复且浪费上下文)。
    if getattr(skill, "knowledge_mode", "preload") == "tool":
        return None, None
    step_count = (state.get("skill_step_counts") or {}).get(skill.id, 0)
    if step_count != 0:
        return None, None
    goal = state.get("goal", "")
    try:
        results = await route_and_retrieve(state.get("scenic_id") or "", goal, skill.knowledge_sources)
    except Exception:  # noqa: BLE001 - 知识检索失败不阻断主流程,退化为无知识上下文
        log.exception(f"skill {skill.id} 知识检索失败,跳过")
        return None, {"step": "knowledge_retrieval", "skill_id": skill.id, "ok": False}
    if not results:
        return None, {"step": "knowledge_retrieval", "skill_id": skill.id, "hits": 0}
    # 引用溯源:给每条检索结果编号 [1]..[n],注入时保持编号,并要求模型在回答里
    # 用 [n] 标注来源(未参考的片段无需标注);sources 明细随 timeline 下发,
    # 前端可渲染引用列表,供用户核验。这是行业 RAG 的标准闭环——只喂知识不标来源,
    # 用户无法判断答案可信度,幻觉也无从排查。
    numbered = list(enumerate(results[:10], start=1))
    text = "\n".join(f"- [{ref}] {json.dumps(r, ensure_ascii=False, default=str)}" for ref, r in numbered)
    text = ctx.truncate_tool_result(text, max_chars=6000)
    sources = [
        {
            "ref": ref,
            "source": r.get("source") or r.get("url") or "",
            "type": r.get("knowledge_type") or r.get("type") or "",
            "url": r.get("url") or "",
        }
        for ref, r in numbered
    ]
    hint = "\n\n回答时请用 [编号] 在句末标注引用的背景知识来源(如 [1][2]),便于用户核验;未引用的片段不要标注。"
    return f"以下是与用户请求相关的背景知识,请优先参考(如与用户请求无关可忽略):\n{text}{hint}", {
        "step": "knowledge_retrieval", "skill_id": skill.id, "hits": len(results), "sources": sources,
    }


async def _drift_check(model_override: str | None, goal: str, messages: list[BaseMessage], *, prompt_sink=None) -> tuple[bool, str, int, int, float | None, str | None]:
    """轻量目标漂移自检。返回 (是否在轨, 纠偏建议, prompt_tokens, completion_tokens, cost_usd, model)。

    token 数必须回传并由调用方计入护栏——这类"内部"调用同样花钱,漏记会让预算护栏
    和成本报表系统性偏低(步数多的任务里漂移自检可能占到总量 10% 以上)。
    cost_usd 为模型响应自带成本(USD),model 为实际模型 key,一并计入供两端计费对比。

    v1.1.6:prompt_sink 可选的异步回调,收到本次自检的最终 check_messages,由调用方
    落库为一条 drift_check 提示词日志(会话信息技能做提示词质量分析时能看到这类
    「内部监督」调用的完整输入)。
    """
    transcript = "\n".join(
        f"{'用户' if isinstance(m, HumanMessage) else '助手' if isinstance(m, AIMessage) else '工具'}: "
        f"{getattr(m, 'content', '') or '[调用工具]'}"
        for m in messages[-8:]
    )
    check_messages = [
        {"role": "system", "content": "你是任务执行监督员。给定最初目标和最近的执行记录,判断当前进展是否仍朝着完成目标推进。"
                                      "只回答一个词 ONTRACK;若已经偏离目标,以 REDIRECT: 开头给出一句话的纠偏建议。"},
        {"role": "user", "content": f"最初目标:{goal}\n\n最近执行记录:\n{transcript[:4000]}"},
    ]
    if prompt_sink is not None:
        try:
            await prompt_sink(check_messages)
        except Exception:  # noqa: BLE001 - 提示词落库失败不影响自检
            pass
    try:
        result = await llm_client.complete_collected(model_override, check_messages, max_tokens=80, temperature=0)
    except Exception:  # noqa: BLE001 - 漂移自检本身失败不应影响主流程
        log.exception("drift check 调用失败,跳过本次自检")
        return True, "", 0, 0, None, None
    text = (result.content or "").strip()
    on_track = not text.upper().startswith("REDIRECT")
    return on_track, text, result.prompt_tokens, result.completion_tokens, getattr(result, "cost_usd", None), getattr(result, "model", None)


def _coerce_tool_args(raw: Any) -> dict:
    """模型返回的工具参数可能是 dict 或 JSON 字符串,统一归一为 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


# v1.1.2:工具入参快照的敏感键。凭证类工具整体不记录(_snapshot_tool_args 直接跳过),
# 这里再兜底剔除普通工具参数里可能出现的敏感字段,双保险防泄漏。
_SENSITIVE_ARG_KEYS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_key", "access_token", "private_key", "authorization",
)


def _response_note(spec) -> str | None:
    """工具响应字段说明(29 号):type_config.response_schema 渲染成几行说明。

    配置于工具编辑页「响应字段说明」——让 LLM 正确解读响应的语义
    (空值含义/单位/枚举),如「orderCount: 0=当日无销售(有效值)」
    「totalAmount: null=无销售时为空(≠错误)」。无配置返回 None。
    """
    rs = ((getattr(spec, "type_config", None) or {}).get("response_schema") or {})
    lines: list[str] = []
    for f in rs.get("fields") or []:
        # path 是现行结构;早先按接口文档导入的工具存的是 name,一并认
        fpath = isinstance(f, dict) and (f.get("path") or f.get("name"))
        if not fpath:
            continue
        t = str(f.get("type") or "").strip()
        u = str(f.get("unit") or "").strip()
        head = str(fpath) + (f"({t}" + (f",{u}" if u else "") + ")" if t else "")
        d = str(f.get("desc") or "").strip()
        lines.append(f"- {head}: {d}" if d else f"- {head}")
    return "[响应字段说明]\n" + "\n".join(lines) if lines else None


def _snapshot_tool_args(call: dict, tools_by_name: dict, max_len: int = 1500) -> str | None:
    """工具入参快照(脱敏 + 截断),供执行分析事后确认「为什么这么调、参数是什么」。

    - 凭证保存类(credential_save)不记录任何参数:agent_node 已剥除明文,但
      _execute_tool_calls 为做 schema 校验恢复了明文(exec_calls),这里必须整体跳过,
      绝不能把 username/password 落库;
    - 其余工具:归一为 dict,剔除常见敏感键,超长 JSON 截断,防止时间线被大参数撑爆。
    """
    spec = tools_by_name.get(call.get("name"))
    if spec is not None and getattr(spec, "type", "") == "credential_save":
        return None
    args = _coerce_tool_args(call.get("arguments"))
    if not args:
        return None
    redacted = {k: "***" for k in _SENSITIVE_ARG_KEYS if k in args}
    if redacted:
        args = {**args, **redacted}
    text = json.dumps(args, ensure_ascii=False)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _sanitize_credential_tool_calls(
    task_id: str,
    tool_calls: list[dict] | None,
    tools_by_name: dict,
) -> list[dict]:
    """§10.3 动作1:剥离「保存凭证」调用的明文参数。

    - 模型发出保存凭证调用 → **立即**把 username/password 从 arguments 中剥出暂存
      credential_guard(纯内存,不入 checkpoint),本轮消息只剩脱敏后的参数;
    - v1.1.1 起不再在此擦除本轮 HumanMessage:擦除时机后移到「保存成功 / 用户忽略」
      (credential_guard.mark_handled,由 tools_node 消费 pop_msg_erase)。失败
      (format_error/error)时保留原文,模型下一轮才能看到账号密码自行纠错,
      避免"蒙在鼓里"死循环(框架能力测试 01 文档);
    - v1.1.2(B 方案):剥离只影响 checkpoint/上下文。tools_node 执行前会从
      credential_guard **取回明文恢复完整参数**再做 schema 校验与重复判定
      (见 _restore_credential_args_for_validation),不再因剥离导致 `{}` 必然
      校验失败、指纹相同被误判重复调用;
    - key 用 task_id + call_id 唯一化,多任务并发互不干扰;
    - 返回剥离后的 tool_calls(可直接写入 AIMessage.additional_kwargs)。
    """
    out: list[dict] = []
    for call in tool_calls or []:
        c = dict(call)
        spec = tools_by_name.get(c.get("name"))
        if spec is not None and spec.type == "credential_save":
            args = _coerce_tool_args(c.get("arguments"))
            credential_guard.stash_credential_args(
                credential_guard.stash_key(task_id, c.get("id", "")),
                {
                    "username": args.get("username"),
                    "password": args.get("password"),
                    "scope": args.get("scope"),
                    "target_tool_id": (spec.type_config or {}).get("target_tool_id"),
                },
            )
            safe_args = {k: v for k, v in args.items() if k not in ("username", "password")}
            c["arguments"] = json.dumps(safe_args, ensure_ascii=False)
        out.append(c)
    return out


def _restore_credential_args_for_validation(
    task_id: str, call: dict, tools_by_name: dict,
) -> dict:
    """v1.1.2(B 方案):校验与重复判定需要**完整**参数,执行前从 credential_guard 取回明文。

    背景:agent_node 剥离明文写入 checkpoint 是防泄漏闸门,但 tools_node 拿到的
    是剥离后的参数——jsonschema 校验用 `{}` 必然报 required 错误(format_error),
    且重复判定指纹基于 `{}` → 模型修正参数后指纹仍相同 → 误判为「参数相同重复调用」,
    3 次后工具被停用,模型从未真正执行保存(框架能力测试 01 文档 P3 案例)。

    这里只做**内存内**恢复(peek 不消费缓存),用于本轮:
    - jsonschema 校验(validate_schema 的 required:[username,password] 能通过);
    - 重复判定指纹(模型真正改了参数时指纹变化,不再误判)。
    不写回 AIMessage,checkpoint / ToolMessage / timeline 依旧不含明文。
    取不到明文(进程重启)→ 保持剥离后参数,由 schema 校验自然失败并引导模型
    重新提供(安全降级,与 _execute_credential_save 的兜底一致)。
    """
    spec = tools_by_name.get(call.get("name"))
    if spec is None or spec.type != "credential_save":
        return call
    args = _coerce_tool_args(call.get("arguments"))
    pending = credential_guard.peek_credential_args(
        credential_guard.stash_key(task_id, call.get("id", ""))
    )
    if not pending:
        return call
    restored = dict(args)
    if pending.get("username") is not None:
        restored["username"] = pending["username"]
    if pending.get("password") is not None:
        restored["password"] = pending["password"]
    if pending.get("scope") is not None:
        restored["scope"] = pending["scope"]
    c = dict(call)
    c["arguments"] = json.dumps(restored, ensure_ascii=False)
    return c


def _maybe_erase_credential_message(task_id: str, messages: list[BaseMessage]) -> None:
    """v1.1.1:保存凭证**成功 / 用户忽略**后,擦除本轮 HumanMessage 原文。

    由 tools_node / 子循环在 `_execute_tool_calls` 之后调用(消费
    credential_guard.pop_msg_erase)。失败(format_error/error)时不置位、不擦除,
    模型下一轮仍能看到账号密码自行纠错——脱敏锚点从「模型发出调用」后移到「保存成功」。
    """
    if not credential_guard.pop_msg_erase(task_id):
        return
    # reversed 找最后一个 HumanMessage = 本轮用户消息;原地改对象,
    # checkpoint 保存 state 时读到的已是脱敏版。
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            m.content = credential_guard.PLACEHOLDER_MESSAGE
            break


async def _execute_tool_calls(
    raw_calls: list[dict], *, db: AsyncSession, state: RunState, tools_by_name: dict,
    guardrail: gr.GuardrailState, max_tool_calls: int | None,
    tracker: res.ToolCallTracker | None = None,
) -> tuple[list[BaseMessage], list[dict]]:
    """执行一批工具调用。

    **并发策略**:模型一次返回多个工具调用时,低风险工具用 asyncio.gather 并发执行
    (串行会把延迟直接叠加);high 风险工具必须串行——它们内部会调用 LangGraph 的
    interrupt() 暂停整张图,把 interrupt 放进 gather 里会让中断语义变得无法推理
    (谁先中断、其它并发分支的副作用怎么算),这是刻意的保守选择。

    护栏计数在派发前一次性完成,保证并发不会绕过次数上限。
    """
    tracker = tracker if tracker is not None else res.ToolCallTracker()

    def _display_name(name: str) -> str:
        """工具展示名(中文),没有则回落函数名,便于操练台时间线可读。"""
        spec = tools_by_name.get(name)
        return spec.name if spec else name

    # v1.1.2(B 方案):agent_node 剥离明文是为 checkpoint 安全,但校验/重复判定需要
    # 完整参数。这里从 credential_guard 取回明文做内存内恢复(不写回 AIMessage):
    # - jsonschema 校验不再用 `{}` → 不再必然 format_error;
    # - 重复指纹基于完整参数 → 模型修正参数后指纹变化,不再误判「参数相同重复调用」。
    # 真正消费明文仍在 _execute_credential_save 的 pop(一处消费、一次消费)。
    exec_calls = [
        _restore_credential_args_for_validation(state["task_id"], call, tools_by_name)
        for call in raw_calls
    ]

    # 重复调用拦截:参数完全相同的调用不再真的执行,直接把上次结果 + 提示回给模型。
    # 不静默拦截而是明确告知,模型才知道该换参数还是换思路。
    repeats: dict[str, res.RepeatVerdict] = {}
    to_run: list[dict] = []
    for call in exec_calls:
        v = res.check_repeat(tracker, call["name"], call.get("arguments"))
        if v.is_repeat:
            repeats[call["id"]] = v
        else:
            to_run.append(call)

    # 额度只剩 k 次时,一轮并发请求超出的部分**不执行**,而不是整任务熔断(见 remaining_tool_calls)
    _left = gr.remaining_tool_calls(guardrail, max_tool_calls=max_tool_calls)
    over_budget = to_run[_left:]
    to_run = to_run[:_left]
    for _ in to_run:
        gr.check_and_increment_tool_call(guardrail, max_tool_calls=max_tool_calls)
    # 空转计数:这一轮有请求、却一次都没真执行(全是重复)→ +1;有真执行就清零
    if exec_calls:
        tracker.stalls = 0 if to_run else tracker.stalls + (0 if over_budget else 1)

    # 每个调用的发出时刻(执行日志用)。放旁表,不写进 call 本身 —— call 会回传给模型厂商,多一个字段就 400
    _starts: dict[int, str] = {}

    async def _run(call: dict, session: AsyncSession, *, parallel: bool = False):
        t0 = time.monotonic()
        _starts[id(call)] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        outcome = await execute_single_tool_call(
            session, task_id=state["task_id"], tool_name=call["name"], raw_arguments=call.get("arguments"),
            bound_tools=tools_by_name, scenic_id=state.get("scenic_id"),
            role=state.get("role"), user_id=state.get("user_id"),
            call_id=call.get("id", ""),  # §10.3:定位暂存的凭证明文
        )
        # v1.1.2:记录每个工具的真实墙钟耗时(不受相邻事件间隔影响),供前端展示
        # "并行执行"下的独立耗时 —— 否则并行调用的后一个工具会显示成 ~0ms 假象。
        return call, outcome, (time.monotonic() - t0) * 1000, parallel

    def _is_high_risk(call: dict) -> bool:
        spec = tools_by_name.get(call.get("name"))
        return bool(spec and getattr(spec, "risk_level", "low") == "high")

    high_risk = [c for c in to_run if _is_high_risk(c)]
    low_risk = [c for c in to_run if not _is_high_risk(c)]

    pairs: list[tuple[dict, object, float, bool]] = []  # (call, outcome, real_ms, parallel)
    # 先串行处理高风险(可能 interrupt 整张图),再并发处理其余
    for call in high_risk:
        pairs.append(await _run(call, db, parallel=False))
    if low_risk:
        parallel_enabled = get_settings().get("runtime", "tools", "parallel", default=True)
        if parallel_enabled and len(low_risk) > 1:
            # 并发分支每个调用用独立 session:同一个 AsyncSession 不允许并发操作
            # (SQLAlchemy「concurrent operations are not permitted」,会随机打挂并行工具)。
            sessions = [get_sessionmaker()() for _ in low_risk]
            try:
                pairs.extend(await asyncio.gather(
                    *(_run(c, s, parallel=True) for c, s in zip(low_risk, sessions))
                ))
            finally:
                for s in sessions:
                    await s.close()
        else:
            for call in low_risk:
                pairs.append(await _run(call, db, parallel=False))

    # 恢复成模型给出的原始顺序,避免 tool 消息顺序与 tool_calls 顺序不一致
    order = {id(c): i for i, c in enumerate(exec_calls)}
    pairs.sort(key=lambda p: order.get(id(p[0]), 0))

    out_messages: list[BaseMessage] = []
    timeline: list[dict] = []
    notices: list[str] = []

    # 格式错误**按轮计、连续才累加**:一轮里并发 4 个调用各错一次,只算模型「写错了一轮」;
    # 有任何一个调用成功,说明模型已经会写了,计数清零。以前按调用逐个累加、从不清零 ——
    # 一轮并发就能把「连续错误超过 3 次」顶满,模型连改一次的机会都没有。
    _kinds = [o.kind for _c, o, _ms, _p in pairs]
    if "ok" in _kinds:
        guardrail.format_retries = 0
    elif "format_error" in _kinds:
        gr.check_and_increment_format_retry(guardrail)

    for call, outcome, real_ms, parallel in pairs:
        if outcome.kind == "hallucination":
            gr.check_and_increment_hallucination_retry(guardrail)
        # 先压缩再截断:上千行的返回原样塞进对话,要么超上下文被截断(模型基于半截
        # 数据作答且不自知),要么占满上下文把后面的追问挤掉 —— 两种都不报错。
        # 压缩靠的是工具的 response_schema(Markdown 接口文档里那张返回参数表)。
        _spec = tools_by_name.get(call.get("name"))
        content = ctx.compress_or_truncate(
            outcome.content,
            response_schema=((getattr(_spec, "type_config", None) or {}).get("response_schema") or None),
            # 带上任务与工具名:压缩掉的明细会留在服务端,摘要末尾给模型一个句柄,
            # 它就能用内置数据工具把这上千行真算一遍,而不是照着合计估
            task_id=state.get("task_id"), tool_name=call.get("name") or "")
        res.remember_result(tracker, call["name"], call.get("arguments"), content)

        # 连续失败到阈值就把工具摘掉(下一轮不再暴露给模型)
        notice = res.record_outcome(tracker, call["name"], ok=(outcome.kind == "ok"))
        if notice:
            notices.append(notice)
            timeline.append(_tl_ts({"step": "tool_disabled", "tool_name": call["name"],
                             "tool_display_name": _display_name(call["name"]),
                             "reason": f"连续失败 {tracker.failures.get(call['name'])} 次"}))

        # 响应字段说明注入(29 号):工具首次成功返回时附上字段表,让 LLM 正确
        # 解读响应语义(空值/单位/枚举)——同任务重复调用不重复注入(token 可控)。
        if outcome.kind == "ok" and not any(
                isinstance(m, ToolMessage) and getattr(m, "name", "") == call["name"]
                for m in [*state.get("messages", []), *out_messages]):
            _note = _response_note(tools_by_name.get(call.get("name")))
            if _note:
                content = f"{content}\n\n{_note}"
        out_messages.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        _raw_chars = len(outcome.content if isinstance(outcome.content, str) else json.dumps(
            outcome.content, ensure_ascii=False, default=str))
        # v1.1.1:timeline 记录失败详情,操练台回放可定位 format_error/error 的具体原因
        # (框架能力测试 01 文档 P3-2:此前只记 kind,线上问题全靠猜)。
        _tl_entry: dict = {"step": "tool_call", "tool_name": call["name"],
                           "tool_display_name": _display_name(call["name"]), "kind": outcome.kind,
                           "source_type": "tool_call",
                           # v1.1.2:真实耗时 + 并行标记。并行执行的工具真实耗时相同,
                           # 时间线的相邻间隔 duration 只对"串行链"有意义。
                           "real_duration_ms": round(real_ms, 1), "parallel": parallel,
                           # 细节留痕:几点几分发出、返回多大、塞给模型多大(被压缩 / 截断了多少)
                           "io": {"started_at": _starts.get(id(call)),
                                  "result_chars": _raw_chars, "sent_to_model_chars": len(content),
                                  "compressed": len(content) < _raw_chars,
                                  "http_status": ((getattr(outcome, "raw_result", None) or {}).get("status_code")
                                                  if isinstance(getattr(outcome, "raw_result", None), dict) else None),
                                  "fanout": ((getattr(outcome, "raw_result", None) or {}).get("fanout")
                                             if isinstance(getattr(outcome, "raw_result", None), dict) else None)}}
        # v1.1.2:入参快照(脱敏 + 截断),执行分析可事后确认「为什么这么调、参数是什么」
        # (如 conv_2133b5d0 两次客流比对汇总的参数差异)。credential_save 整体不记录。
        _args_snapshot = _snapshot_tool_args(call, tools_by_name)
        if _args_snapshot:
            _tl_entry["args"] = _args_snapshot
        if getattr(outcome, "injected", None):
            # 哪些入参是系统按景区配置填的(不含值)
            _tl_entry["scenic_params"] = outcome.injected
        if outcome.kind in ("error", "format_error") and outcome.content:
            _tl_entry["error"] = outcome.content[:200]
        # 结构化摘要在这里就算好并落进 timeline。
        # **不把原始结果塞进 timeline** —— 一次查询几千条记录,落库体积会失控;
        # 而摘要只有一行,回放时也能看出「这步取到了什么」。
        if outcome.kind == "ok":
            try:
                _sum, _find = summarizer.summarize(
                    getattr(outcome, "raw_result", None) or outcome.content)
                _tl_entry["summary"] = _sum
                if _find:
                    _tl_entry["findings"] = _find
            except Exception:  # noqa: BLE001 - 摘要失败不该影响工具调用本身
                log.debug("结果摘要失败", exc_info=True)
        timeline.append(_tl_ts(_tl_entry))
        # 工具声明的记忆(返回 _remember)落库。同样不 await ——
        # 记忆写失败不该让一次本来成功的工具调用变成失败。
        if outcome.kind == "ok":
            _raw = getattr(outcome, "raw_result", None)
            if isinstance(_raw, dict) and _raw.get("_remember"):
                asyncio.create_task(ctx_memory.capture_from_result(
                    _raw, conversation_id=state.get("conversation_id"),
                    user_id=state.get("user_id"), scenic_id=state.get("scenic_id")))

        # 调用统计(工具列表按调用次数排序、L4 耗时预估都用它)。
        # **不 await** —— 统计写慢了不该拖慢工具调用本身;写失败也不影响主流程。
        _spec = tools_by_name.get(call["name"])
        if _spec is not None and getattr(_spec, "id", None):
            asyncio.create_task(tool_stats.record_call(
                _spec.id, scenic_id=state.get("scenic_id"),
                ok=(outcome.kind == "ok"), duration_ms=int(real_ms)))

    # 重复调用:不执行,但必须回一条 ToolMessage —— 协议要求每个 tool_call_id 都有响应,
    # 少一条下一轮请求就是非法的(厂商直接 400)。
    for call in exec_calls:
        v = repeats.get(call["id"])
        if v is None:
            continue
        content = res.repeat_notice(call["name"], v)
        out_messages.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        timeline.append(_tl_ts({"step": "tool_call_skipped", "tool_name": call["name"],
                         "tool_display_name": _display_name(call["name"]),
                         "reason": f"参数相同的第 {v.times} 次重复调用,已跳过实际执行"}))
        # 重复本身也计入摘除判定:否则被拦截的调用不再产生失败计数,
        # 工具永远达不到阈值,模型会抱着它原地打转直到步数耗尽
        pressure = res.record_repeat_pressure(tracker, call["name"], v.times)
        if pressure:
            notices.append(pressure)
            timeline.append(_tl_ts({"step": "tool_disabled", "tool_name": call["name"],
                             "reason": f"相同参数重复调用 {v.times} 次仍无进展"}))

    # 超出额度没执行的调用同样要逐个回 ToolMessage(协议要求),并告诉模型为什么
    for call in over_budget:
        out_messages.append(ToolMessage(
            content="本任务的工具调用次数已用完,这次调用未执行。请直接基于已经取到的结果作答。",
            tool_call_id=call["id"], name=call["name"]))
        timeline.append(_tl_ts({"step": "tool_call_skipped", "tool_name": call["name"],
                         "tool_display_name": _display_name(call["name"]),
                         "reason": "工具调用次数已达上限,未执行"}))

    for n in notices:
        out_messages.append(SystemMessage(content=n))
    return out_messages, timeline


# ══════════════════════════════════════════════════════════════════════════
# L0 / L1(线性)/ L2 —— ReAct 形状统一图
# ══════════════════════════════════════════════════════════════════════════
def build_agent_graph(spec: ExecutionSpec, mode: str, db: AsyncSession, checkpointer, *, conversation_id: str | None = None):
    skill_chain = spec.skill_chain
    composition_hint = spec.composition_hint
    persona = spec.persona
    model_override = spec.model_override
    settings = get_settings()
    drift_every_n = settings.get("runtime", "guardrails", "drift_check_every_n_steps", default=4)

    async def agent_node(state: RunState) -> dict:
        guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
        skill = _current_skill(state, skill_chain)
        gr.check_and_increment_step(guardrail, max_steps=skill.max_steps if skill else None)

        timeline: list[dict] = []
        extra_systems: list[dict] = []

        # ── 输入侧内容审核(架构图 ④ llm「输入/输出双侧内容审核」)──
        # 只在第一步做:后续轮次的输入是工具结果,不是用户可控内容。
        if guardrail.steps == 1:
            mod = await moderation.check_input(state.get("goal", ""), model_override=_effective_model(skill, model_override))
            gr.record_tokens(guardrail, mod.prompt_tokens, mod.completion_tokens, getattr(mod, "cost_usd", None), getattr(mod, "model", None))
            if mod.flagged:
                timeline.append(_tl_ts({"step": "moderation", "side": "input", "passed": mod.passed, "reason": mod.reason}))
            if not mod.passed:
                raise GuardrailTripped(f"输入内容审核未通过:{mod.reason}")
            if mod.defensive_note:
                extra_systems.append({"role": "system", "content": mod.defensive_note})

        knowledge_text, kt_event = await _maybe_retrieve_knowledge(state, skill)
        if kt_event:
            timeline.append(_tl_ts(kt_event))

        tools = _exposed_tools(state, skill_chain, composition_hint)
        # 已被判定为不可用的工具从这一轮的可选列表里拿掉。
        # 只在提示里说"别用它"是不够的——只要它还在 tools 数组里,模型就可能再选。
        tracker = res.ToolCallTracker.from_dict(state.get("tool_tracker"))
        tools = res.usable_tools(tools, tracker)
        # v1.1.2:工具预算/步数用尽即收尾(纯流程自动,不要求按 skill 配置调用次数):
        # 触发后本轮 tools 置空——既强制模型基于已有结果直接总结,又省掉 N 个工具
        # schema 的 prefill(舆情 skill 有 16 个工具,收尾轮不再全量注入)。触发条件:
        #   1) guardrail.tool_calls 达到全局护栏上限(skill 未配 max_tool_calls 时
        #      回落全局默认 10,配置只是覆盖,不要求每个 skill 都去配);
        #   2) 步数将尽(只剩最后一步),防失控任务拖到被护栏熔断。
        # 提示必须在本轮 extra_systems 里(outgoing 在此函数稍后统一 extend
        # extra_systems),模型才知道为什么没工具了。
        max_calls = skill.max_tool_calls if skill else 10
        steps_left = skill.max_steps - guardrail.steps if skill else 0
        # 3) 连续空转(整轮全是被拦下的重复调用):数据已经都在上下文里了,
        #    再给工具只会继续原地打转,每轮白付一次完整生成。
        _stalled = bool(tools) and res.stalled(tracker)
        finalize = (bool(tools) and guardrail.tool_calls >= max_calls) or steps_left <= 1 or _stalled
        if finalize:
            tools = []
            extra_systems.append({
                "role": "system",
                "content": ("最近几次请求的都是已经取过的数据(已被拦截),说明需要的数据已经齐了。"
                            if _stalled else "") +
                           "所有必要的工具调用已完成,请直接基于已有数据与工具结果给出最终回答,不要再尝试调用任何工具。",
            })
            if _stalled:
                timeline.append(_tl_ts({"step": "stall_finalize",
                                        "reason": f"连续 {tracker.stalls} 轮只有重复调用,直接收尾作答"}))

        if finalize:
            # 收尾轮用精简总结上下文:工具行为规范与【工具使用说明】都不再注入,
            # 模型此刻的任务是总结,不是再调用工具(见 _final_answer_system_prompt)。
            system_prompt = _final_answer_system_prompt(state, skill_chain, composition_hint, persona)
        else:
            system_prompt = _current_system_prompt(state, skill_chain, composition_hint, persona)
        # 32 号:执行层历史策略——按轮注入对话文本,不再取 checkpoint 全量 messages
        # (旧轮工具结果原文/重复拦截提示是上下文毒源,会教模型不重新取数,轮 16
        # 实证)。exec_history_rounds:固定问题 0(纯净,executor 强制)、非固定
        # Agent 配置(默认 2);0=纯净上下文。取数失败静默降级为纯净(不阻断)。
        _hrounds = state.get("exec_history_rounds")
        history = []
        _hist_clipped = 0          # 历史里被截短的上一轮长回答条数(进执行日志)
        if _hrounds and conversation_id:
            try:
                from cogni_agent.control_plane_service import conversations as conv_svc

                _turns = await conv_svc.get_recent_turns(
                    conversation_id, exclude_task_id=state.get("task_id"), limit=_hrounds)
                history = [
                    HumanMessage(t["content"]) if t.get("role") == "user"
                    else AIMessage(_clip_prior_answer(t["content"] or ""))
                    for t in _turns if (t.get("content") or "").strip()
                ]
                _hist_clipped = sum(1 for t in _turns if t.get("role") != "user"
                                    and len(t.get("content") or "") > _runtime_cfg_int(
                                        "exec_history_answer_chars", PRIOR_ANSWER_CHARS))
            except Exception:  # noqa: BLE001 - 历史注入失败不阻断执行,降级纯净上下文
                history = []
        outgoing: list[dict] = [{"role": "system", "content": system_prompt}]
        outgoing.extend(extra_systems)
        if knowledge_text:
            outgoing.append({"role": "system", "content": knowledge_text})
        outgoing.extend(ctx.to_litellm_message(m) for m in history)
        # **本轮消息必须发出去**:用户这次问的话、模型发起的工具调用、以及工具返回。
        #
        # 「按轮注入对话文本、不取 checkpoint 全量 messages」的本意是不让**旧轮**的
        # 工具结果原文污染上下文(它会教模型不重新取数)。但上面只 extend 了 history,
        # 而 history 是**别的轮次**的纯文本(get_recent_turns 明确 exclude 了本任务) ——
        # 于是本轮的一切都没发出去:模型既看不到用户问什么,也看不到工具返回了什么。
        #
        # 表现极具迷惑性:工具明明调用成功(执行链里有一条 ok、几千毫秒、有返回),
        # 下一轮模型却拿不到那份结果,只好照着系统提示词里的「已知上下文参数」
        # 回一句「我看到带了景区和日期,但还没有收到您的具体问题」——
        # 任务状态还是 COMPLETED。数据取回来了,然后被丢掉了。
        #
        # 所以这里只取**本轮**(最后一条 HumanMessage 起):既把问题和工具结果带上,
        # 又不把旧轮的工具原文翻出来,两个目的都守住。
        outgoing.extend(ctx.to_litellm_message(m)
                        for m in ctx.trim_messages(_current_turn_messages(state)))

        writer = get_stream_writer()
        # v1.1.2:generation 步骤开始时即发一个 timeline 事件。此前 timeline 事件在
        # LLM 完整返回后才 append(见下 tool_calls/return 分支):长生成(如工具规划
        # 134s)期间前端执行链一片空白,worker 心跳也看不到进展——模型若长时间只
        # 输出 reasoning、不产生 content 增量(token 事件缺失),90s 无进展心跳会
        # 停止续租,任务被巡检误回收。这里先发「内容生成中…」节点(实时可见),
        # executor 收到后 _publish → mark_progress 即完成活性登记,后续逐 token 的
        # custom 事件会持续刷新活性。打点只走实时流、不进 checkpoint:完成态以
        # 流结束后的 generation 事件为准,回放/落库只出现完成态节点,不会重复。
        writer({"type": "generation_start", **_tl_ts({
            "step": "generation", "status": "streaming",
            "skill_id": skill.id if skill else None,
        })})
        _gen_t0 = time.monotonic()
        _gen_started = datetime.now()
        result = await llm_client.stream_and_collect(
            _effective_model(skill, model_override), outgoing,
            tools=tool_function_schemas(tools) if tools else None,
            response_format=_effective_response_format(skill, bool(tools)),
            writer=writer,
            # 32 号:strict_tool_execution(技能 constraints 配置)——协议级强制
            # 取数:**本轮**(最后一条 Human 之后)尚无任何工具调用时,
            # tool_choice="required" 让模型必须发起工具调用(连「要不要调」的
            # 决策都不做),杜绝模型凭上下文直接作答;有调用后回 auto(后续
            # ReAct 自主)。只看本轮:非固定问题 thread 共享,旧轮工具调用
            # 不该让本轮豁免。
            tool_choice=("required" if (
                tools and bool(getattr(skill, "strict_tool_execution", False))
                and not any(
                    getattr(m, "tool_calls", None)
                    or (getattr(m, "additional_kwargs", {}) or {}).get("tool_calls_raw")
                    for m in state["messages"][_last_human_after(state):]
                )
            ) else None),
        )
        gr.record_tokens(guardrail, result.prompt_tokens, result.completion_tokens, getattr(result, "cost_usd", None), getattr(result, "model", None))
        # v1.1.6:记录本次 LLM 调用真正收到的最终提示词(系统提示词完整、历史截断),
        # 供会话信息技能做提示词质量分析(业务噪音/内部信息泄漏排查)。失败不影响主流程。
        await record_prompt(
            db, task_id=state["task_id"], conversation_id=conversation_id,
            scenic_id=state.get("scenic_id"), user_id=state.get("user_id"),
            step="generation",
            model=_effective_model(skill, model_override),
            system_prompt="\n\n".join(m["content"] for m in outgoing if m["role"] == "system"),
            history=[m for m in outgoing if m["role"] != "system"],
            prompt_tokens=result.prompt_tokens,
        )
        gr.check_token_budget(guardrail, spec.token_budget)

        if result.tool_calls:
            # 有意不用 langchain 原生 AIMessage.tool_calls 字段(它要求 args 已是合法 dict)——
            # 模型返回的参数本身可能就是格式错误的来源,必须原样保留字符串一路带到
            # tools_adapter 做 JSON 解析/Schema 校验。
            # 注意:context.to_litellm_message() 会把这里的 tool_calls_raw 还原成协议要求的
            # assistant.tool_calls 字段,不能丢(丢了下一轮 tool 消息就成孤儿,厂商直接 400)。
            # §10.3 动作1:保存凭证调用的明文参数在写入 state(→checkpoint)前剥除并
            # 暂存 credential_guard;擦除本轮 HumanMessage 的时机在 tools_node
            # (保存成功/忽略后,见 _maybe_erase_credential_message),失败时保留原文供模型自纠。
            tools_by_name = bound_tools_map(tools)
            sanitized_calls = _sanitize_credential_tool_calls(
                state["task_id"], result.tool_calls, tools_by_name,
            )
            ai_message = AIMessage(content=result.content or "", additional_kwargs={"tool_calls_raw": sanitized_calls})
            timeline.append(_tl_ts({"step": "generation", "skill_id": skill.id if skill else None,
                             "will_call_tools": [tc["name"] for tc in result.tool_calls],
                             "llm": _llm_perf(result, outgoing),
                             "detail": _gen_detail(result, outgoing, tools=tools, round_no=guardrail.steps,
                                                   history_len=len(history), history_clipped=_hist_clipped,
                                                   started=_gen_started, wall_ms=(time.monotonic() - _gen_t0) * 1000)}))
        else:
            # ── 输出侧内容审核:只审最终自然语言输出 ──
            mod_out = await moderation.check_output(result.content or "", model_override=_effective_model(skill, model_override))
            gr.record_tokens(guardrail, mod_out.prompt_tokens, mod_out.completion_tokens, getattr(mod_out, "cost_usd", None), getattr(mod_out, "model", None))
            if mod_out.flagged:
                timeline.append(_tl_ts({"step": "moderation", "side": "output", "passed": mod_out.passed, "reason": mod_out.reason}))
            if not mod_out.passed:
                raise OutputBlocked(mod_out.reason or "输出内容审核未通过")
            ai_message = AIMessage(content=result.content or "")
            timeline.append(_tl_ts({"step": "generation", "skill_id": skill.id if skill else None,
                             "preview": (result.content or "")[:120],
                             "llm": _llm_perf(result, outgoing),
                             "detail": _gen_detail(result, outgoing, tools=tools, round_no=guardrail.steps,
                                                   history_len=len(history), history_clipped=_hist_clipped,
                                                   started=_gen_started, wall_ms=(time.monotonic() - _gen_t0) * 1000)}))

        step_counts = dict(state.get("skill_step_counts") or {})
        if skill:
            step_counts[skill.id] = step_counts.get(skill.id, 0) + 1

        out = {
            "messages": [ai_message],
            "guardrail": guardrail.as_dict(),
            "timeline": timeline,
            "usage": {"prompt_tokens": guardrail.prompt_tokens, "completion_tokens": guardrail.completion_tokens},
            "skill_step_counts": step_counts,
        }
        if not result.tool_calls and result.content:
            out["final_answer"] = result.content
        return out

    async def tools_node(state: RunState) -> dict:
        guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
        skill = _current_skill(state, skill_chain)
        tools = _with_param_source_upstreams(_exposed_tools(state, skill_chain, composition_hint),
                                             _current_tools(state, skill_chain, composition_hint))
        tools_by_name = bound_tools_map(tools)

        tracker = res.ToolCallTracker.from_dict(state.get("tool_tracker"))
        raw_calls = state["messages"][-1].additional_kwargs.get("tool_calls_raw") or []
        out_messages, timeline = await _execute_tool_calls(
            raw_calls, db=db, state=state, tools_by_name=tools_by_name,
            guardrail=guardrail, max_tool_calls=skill.max_tool_calls if skill else None,
            tracker=tracker,
        )

        # ── L4:回写计划步骤状态 ──
        # 从本轮时间线里取实际发生的工具调用,映射回计划里对应的步骤。
        # 不改 _execute_tool_calls 的签名 —— 它被 ReAct 和 DAG 两条路径共用,
        # 加参数会让「有没有计划」这件事渗透到所有调用点。
        exec_plan = planner.ExecutionPlan.from_dict(state.get("exec_plan"))
        if exec_plan.steps:
            for ev in timeline:
                if ev.get("step") != "tool_call":
                    continue
                st = exec_plan.step_by_tool(ev.get("tool_name") or "")
                if st is None:
                    # 映射不上 = 模型偏离了计划。计划是主线不是枷锁,
                    # 记一条让人看得见,但不阻断
                    timeline.append({"step": "plan_deviation",
                                     "tool_name": ev.get("tool_name"),
                                     "note": "该调用不在计划内"})
                    continue
                st.status = (planner.STATUS_DONE if ev.get("kind") == "ok"
                             else planner.STATUS_FAILED)
                # 摘要在构造 timeline 时已算好,这里直接取 —— 不重复计算
                st.result_summary = ev.get("summary") or (ev.get("error") or "")[:150]
                st.findings = list(ev.get("findings") or [])
                if ev.get("duration_ms") is not None:
                    st.duration_ms = ev["duration_ms"]
            timeline.append({"step": "plan_update", "plan": exec_plan.as_dict()})

        # 漂移自检的触发条件:工具调用数**跨过**下一个 N 的整数倍时才做一次。
        #
        # 原来写的是 `tool_calls % drift_every_n == 0` —— 看着一样,实际差很多:
        # 计数停在 4 的倍数上不动的那些轮次会**每轮都再自检一次**。而计数恰恰经常不动:
        # 重复调用会被拦截、不计入 tool_calls(见上面的 `for _ in to_run`),
        # 于是模型一旦开始原地打转(反复请求同一批参数),每一轮都要多付一次
        # 目标校验的整轮模型调用 —— 现场日志里就是这样:4 次「跳过重复调用」全是 0ms,
        # 紧接着一条 3141ms 的目标校验。打转本身不烧时间,替它做的自检才烧。
        #
        # 按「已经做过几次自检」和「当前该做到第几次」比较,跨过去才做,停着不动就不做。
        _drift_bucket = guardrail.tool_calls // drift_every_n if drift_every_n else 0
        if drift_every_n and _drift_bucket > guardrail.drift_checks:
            # v1.1.6:漂移自检的最终提示词同样落库(step=drift_check),供提示词质量分析。
            async def _sink_drift_prompt(msgs: list[dict]) -> None:
                await record_prompt(
                    db, task_id=state["task_id"], conversation_id=conversation_id,
                    scenic_id=state.get("scenic_id"), user_id=state.get("user_id"),
                    step="drift_check", model=model_override,
                    system_prompt=msgs[0]["content"] if msgs else "",
                    history=msgs[1:] or None,
                )
            _drift_t0 = time.monotonic()
            on_track, note, pt, ct, cost_usd, drift_model = await _drift_check(model_override, state.get("goal", ""), state["messages"] + out_messages, prompt_sink=_sink_drift_prompt)
            _drift_ms = (time.monotonic() - _drift_t0) * 1000
            gr.record_tokens(guardrail, pt, ct, cost_usd, drift_model)   # 漂移自检的 token 同样计入护栏与成本
            guardrail.drift_checks += 1
            # v1.1.6:漂移检查结果只进 timeline(drift_check 事件),不再以
            # 「[执行监督员提醒]…」SystemMessage 注入对话消息流——那是给运维看的
            # 内部提醒,下一轮 LLM 会读到,属于内部信息泄漏,反而干扰模型按业务作答。
            timeline.append(_tl_ts({"step": "drift_check", "on_track": on_track, "note": note,
                                    "llm": {"total_ms": round(_drift_ms, 1), "prompt_tokens": pt,
                                            "completion_tokens": ct, "model": drift_model},
                                    # 自检结论目前只留痕、不改变执行路径 —— 判了「偏离」也照常往下跑,
                                    # 这里写明,免得看日志的人以为它拦过
                                    "acted": False}))

        # v1.1.1:保存凭证成功/忽略后,擦除本轮 HumanMessage 原文(失败保留供模型自纠)。
        _maybe_erase_credential_message(state["task_id"], state["messages"])

        return {
            "messages": out_messages, "guardrail": guardrail.as_dict(), "timeline": timeline,
            "exec_plan": exec_plan.as_dict(),
            "tool_tracker": tracker.as_dict(),
            "usage": {"prompt_tokens": guardrail.prompt_tokens, "completion_tokens": guardrail.completion_tokens},
        }

    def advance_skill_node(state: RunState) -> dict:
        cur_idx = state.get("skill_idx", 0)
        cur_skill = skill_chain[cur_idx] if cur_idx < len(skill_chain) else None
        next_idx = cur_idx + 1
        next_skill = skill_chain[next_idx] if next_idx < len(skill_chain) else None
        return {"skill_idx": next_idx,
                "timeline": [{"step": "skill_advance",
                              "from_skill_id": cur_skill.id if cur_skill else None,
                              "from_skill_name": cur_skill.name if cur_skill else None,
                              "to_skill_id": next_skill.id if next_skill else None,
                              "to_skill_name": next_skill.name if next_skill else None}]}

    def route_after_agent(state: RunState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.additional_kwargs.get("tool_calls_raw"):
            return "tools"
        if mode == "L1" and state.get("skill_idx", 0) + 1 < len(skill_chain):
            return "advance_skill"
        return END

    graph = StateGraph(RunState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("advance_skill", advance_skill_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "advance_skill": "advance_skill", END: END})
    graph.add_edge("tools", "agent")
    graph.add_edge("advance_skill", "agent")
    return graph.compile(checkpointer=checkpointer)


# ══════════════════════════════════════════════════════════════════════════
# L1(固定 DAG)—— 架构图 ④「多 Skill 编排:固定DAG」
# ══════════════════════════════════════════════════════════════════════════
def topological_layers(skill_ids: list[str], deps: dict[str, list[str]]) -> list[list[str]]:
    """把依赖图切成拓扑层:同一层内的 Skill 互不依赖,可以并发执行。

    检测到环时不抛异常,而是把剩余节点按原顺序压成最后一层——编排配置错误不应该
    让整个任务直接失败,降级成串行执行仍能产出结果,同时打 warning 便于排查。
    """
    remaining = list(skill_ids)
    resolved: set[str] = set()
    layers: list[list[str]] = []

    while remaining:
        ready = [sid for sid in remaining if all(d in resolved or d not in skill_ids for d in deps.get(sid, []))]
        if not ready:
            log.warning(f"Skill 编排依赖图存在环或悬空依赖,剩余 {remaining} 降级为串行执行")
            layers.extend([sid] for sid in remaining)
            break
        layers.append(ready)
        resolved.update(ready)
        remaining = [sid for sid in remaining if sid not in resolved]
    return layers


async def _synthesize_final_answer(
    state: RunState,
    *,
    parts: dict[str, str],
    db: AsyncSession,
    model_override: str | None,
    persona: dict | None,
    conversation_id: str | None = None,
    extra_system: str = "",
    force_llm: bool = False,
) -> dict:
    """把若干「执行产出」汇总成一份面向用户的最终答复(最多一次 LLM)。

    build_dag_graph 的「多 Skill 产物汇总」与 build_direct_dispatch_graph 的
    「多工具返回汇总」共用此处。这件事必须在**图内**完成——若在网关另起一次 LLM 调用,
    会绕过 SSE 流式 / 提示词落库 / token 统计 / 输出审核 / 步数护栏 / checkpoint
    六项基础设施,排障与合规两头留洞。

    parts: {片段标题: 文本},标题作为【xxx】块名出现在给模型的 user 消息里。
    extra_system: 追加在默认汇总指令之后的额外约束(如编排直出的「如实」硬约束)。
    force_llm: True 时哪怕只有一个片段也走 LLM(工具原始返回不适合直接给用户看)。
    """
    guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
    if len(parts) == 1 and not force_llm:
        answer = next(iter(parts.values()))
    else:
        joined = "\n\n".join(f"【{k}】\n{v}" for k, v in parts.items())
        gr.check_and_increment_step(guardrail)
        outgoing = [
            {"role": "system", "content": "\n\n".join(p for p in (
                _persona_preamble(persona), _today_hint(),
                "以下是各个步骤的执行产出,请整合成一份完整、连贯、直接面向用户的最终答复。",
                extra_system,
            ) if p)},
            {"role": "user", "content": f"用户最初的请求:{state.get('goal', '')}\n\n各步骤产出:\n{joined}"},
        ]
        result = await llm_client.stream_and_collect(model_override, outgoing, writer=get_stream_writer())
        gr.record_tokens(guardrail, result.prompt_tokens, result.completion_tokens,
                         getattr(result, "cost_usd", None), getattr(result, "model", None))
        # 汇总同样是一次 LLM 调用,提示词日志也必须落库,否则排障时 timeline 缺 generation 记录。
        await record_prompt(
            db, task_id=state["task_id"], conversation_id=conversation_id,
            scenic_id=state.get("scenic_id"), user_id=state.get("user_id"),
            step="generation", model=model_override,
            system_prompt=outgoing[0]["content"], history=outgoing[1:],
            prompt_tokens=result.prompt_tokens,
        )
        answer = result.content or ""

    mod_out = await moderation.check_output(answer, model_override=model_override)
    gr.record_tokens(guardrail, mod_out.prompt_tokens, mod_out.completion_tokens,
                     getattr(mod_out, "cost_usd", None), getattr(mod_out, "model", None))
    if not mod_out.passed:
        raise OutputBlocked(mod_out.reason or "输出内容审核未通过")

    return {
        "messages": [AIMessage(content=answer)], "final_answer": answer, "guardrail": guardrail.as_dict(),
        "timeline": [{"step": "generation", "preview": answer[:120]}],
        "usage": {"prompt_tokens": guardrail.prompt_tokens, "completion_tokens": guardrail.completion_tokens},
    }


def build_dag_graph(spec: ExecutionSpec, db: AsyncSession, checkpointer, *, conversation_id: str | None = None):
    """固定 DAG 编排:按拓扑层推进,同层并发,层间传递产物。

    与 ReAct 图的本质区别:每个 Skill 是一个**自包含子任务**,拿到"原始目标 + 上游
    Skill 的产物"后自己跑一个受限的 agent↔tools 小循环,产出一段文本。所有层跑完后
    做一次汇总生成最终答案。

    高风险工具的 interrupt() 在这里同样有效——它会中断整张图,恢复后 LangGraph 从
    检查点重放,已完成层的产物都在 state 里,不会重跑。
    """
    skill_chain = spec.skill_chain
    by_id = {s.id: s for s in skill_chain}
    layers = topological_layers([s.id for s in skill_chain], spec.skill_deps or {})
    persona = spec.persona
    model_override = spec.model_override
    max_inner_steps = get_settings().get("runtime", "dag", "max_steps_per_skill", default=6)

    async def _run_skill_subloop(skill: ResolvedSkill, state: RunState, upstream: dict[str, str],
                                 guardrail: gr.GuardrailState, db: AsyncSession) -> tuple[str, list[dict]]:
        """单个 Skill 的受限 ReAct 子循环。返回 (产物文本, 时间线)。"""
        timeline: list[dict] = []
        goal = state.get("goal", "")
        sys_parts = [_persona_preamble(persona), skill.system_prompt, _tool_usage_policy(), _answer_layout_policy(), _scenic_context_hint(state), _today_hint()]
        if upstream:
            joined = "\n\n".join(f"【上游能力「{by_id[k].name if k in by_id else k}」的产出】\n{v}" for k, v in upstream.items())
            sys_parts.append(f"以下是本流程中前置步骤的产出,请在此基础上继续:\n{joined}")
        system_prompt = "\n\n".join(p for p in sys_parts if p)

        knowledge_text, kt_event = await _maybe_retrieve_knowledge({**state, "skill_step_counts": {}}, skill)
        if kt_event:
            timeline.append(_tl_ts({**kt_event, "skill_id": skill.id}))

        messages: list[BaseMessage] = [HumanMessage(content=goal)]
        tools_by_name = bound_tools_map(skill.tools)
        # 每个 Skill 子任务有自己的轨迹:A 的工具坏了不该影响 B 的可选范围
        sub_tracker = res.ToolCallTracker()

        for _ in range(max_inner_steps):
            gr.check_and_increment_step(guardrail)
            outgoing: list[dict] = [{"role": "system", "content": system_prompt}]
            if knowledge_text:
                outgoing.append({"role": "system", "content": knowledge_text})
            outgoing.extend(ctx.to_litellm_message(m) for m in ctx.trim_messages(messages))

            usable = res.usable_tools(skill.tools, sub_tracker)
            # 连续空转 / 额度用完:不再给工具,让它就地基于已有结果作答(同主循环的收尾轮)
            if usable and (res.stalled(sub_tracker)
                           or gr.remaining_tool_calls(guardrail, max_tool_calls=skill.max_tool_calls) == 0):
                usable = []
                outgoing.append({"role": "system", "content":
                                 "需要的数据已经取到了,请直接基于已有结果作答,不要再调用任何工具。"})
            result = await llm_client.stream_and_collect(
                _effective_model(skill, model_override), outgoing,
                tools=tool_function_schemas(usable) if usable else None,
                response_format=_effective_response_format(skill, bool(skill.tools)),
                writer=None,  # 子任务并发执行,逐 token 混流没有意义,只推时间线
            )
            gr.record_tokens(guardrail, result.prompt_tokens, result.completion_tokens, getattr(result, "cost_usd", None), getattr(result, "model", None))
            # v1.1.6:DAG 子任务的每次 LLM 调用同样落库提示词日志(用子循环独立 session)。
            await record_prompt(
                db, task_id=state["task_id"], conversation_id=conversation_id,
                scenic_id=state.get("scenic_id"), user_id=state.get("user_id"),
                step="generation",
                model=_effective_model(skill, model_override),
                system_prompt="\n\n".join(m["content"] for m in outgoing if m["role"] == "system"),
                history=[m for m in outgoing if m["role"] != "system"],
                prompt_tokens=result.prompt_tokens,
            )
            gr.check_token_budget(guardrail, spec.token_budget)

            if not result.tool_calls:
                timeline.append(_tl_ts({"step": "generation", "skill_id": skill.id, "preview": (result.content or "")[:120]}))
                return result.content or "", timeline

            # §10.3 动作1:剥离保存凭证调用的明文参数;擦除时机在保存成功/忽略后,
            # 失败保留原文让模型自纠(见 _maybe_erase_credential_message)。
            sanitized_calls = _sanitize_credential_tool_calls(
                state["task_id"], result.tool_calls, tools_by_name,
            )
            messages.append(AIMessage(content=result.content or "", additional_kwargs={"tool_calls_raw": sanitized_calls}))
            tool_msgs, tl = await _execute_tool_calls(
                sanitized_calls, db=db, state=state, tools_by_name=tools_by_name,
                guardrail=guardrail, max_tool_calls=skill.max_tool_calls,
                tracker=sub_tracker,
            )
            messages.extend(tool_msgs)
            # v1.1.1:保存凭证成功/忽略后,擦除本轮 HumanMessage 原文。
            # messages 是子循环局部列表不落 checkpoint,擦除保证下一轮发送给模型的内容脱敏。
            _maybe_erase_credential_message(state["task_id"], messages)
            timeline.extend({**t, "skill_id": skill.id} for t in tl)

        timeline.append(_tl_ts({"step": "skill_truncated", "skill_id": skill.id, "reason": f"子循环达到上限 {max_inner_steps} 步"}))
        last_text = next((m.content for m in reversed(messages) if isinstance(m, AIMessage) and m.content), "")
        return last_text or "(该步骤未在限定步数内收敛)", timeline

    async def dag_layer_node(state: RunState) -> dict:
        guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
        layer_idx = state.get("skill_idx", 0)
        outputs = dict(state.get("skill_outputs") or {})
        deps = spec.skill_deps or {}

        if layer_idx == 0:
            # 注意:这里在整批 Skill 之前,没有"当前 Skill"可言,用编排级模型覆盖;
            # 各 Skill 自己的模型覆盖在子循环 _run_skill_subloop 里生效。
            mod = await moderation.check_input(state.get("goal", ""), model_override=model_override)
            gr.record_tokens(guardrail, mod.prompt_tokens, mod.completion_tokens, getattr(mod, "cost_usd", None), getattr(mod, "model", None))
            if not mod.passed:
                raise GuardrailTripped(f"输入内容审核未通过:{mod.reason}")

        layer = layers[layer_idx]
        timeline: list[dict] = [{"step": "dag_layer", "layer": layer_idx, "skills": layer, "parallel": len(layer) > 1}]

        async def _one(sid: str):
            skill = by_id[sid]
            upstream = {d: outputs[d] for d in deps.get(sid, []) if d in outputs}
            # 同层 Skill 并发执行,每个子循环用独立 session,避免共享 AsyncSession 并发冲突;
            # 单 Skill 层同样走独立 session,保证行为一致且与主 db 解耦。
            async with get_sessionmaker()() as sub_db:
                return sid, await _run_skill_subloop(skill, state, upstream, guardrail, db=sub_db)

        # 同层内并发:这正是 DAG 相对线性链的核心价值
        results = await asyncio.gather(*(_one(sid) for sid in layer)) if len(layer) > 1 else [await _one(layer[0])]
        for sid, (text, tl) in results:
            outputs[sid] = text
            timeline.extend(tl)

        return {
            "skill_idx": layer_idx + 1, "skill_outputs": outputs, "guardrail": guardrail.as_dict(), "timeline": timeline,
            "usage": {"prompt_tokens": guardrail.prompt_tokens, "completion_tokens": guardrail.completion_tokens},
        }

    async def synthesize_node(state: RunState) -> dict:
        """所有层跑完后汇总。只有一个 Skill 时直接用它的产物,不做多余的一次 LLM 调用。"""
        outputs = state.get("skill_outputs") or {}
        parts = {by_id[k].name if k in by_id else k: v for k, v in outputs.items()}
        return await _synthesize_final_answer(
            state, parts=parts, db=db, model_override=model_override,
            persona=persona, conversation_id=conversation_id)

    def route_after_layer(state: RunState) -> str:
        return "dag_layer" if state.get("skill_idx", 0) < len(layers) else "synthesize"

    graph = StateGraph(RunState)
    graph.add_node("dag_layer", dag_layer_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_edge(START, "dag_layer")
    graph.add_conditional_edges("dag_layer", route_after_layer, {"dag_layer": "dag_layer", "synthesize": "synthesize"})
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)


# ══════════════════════════════════════════════════════════════════════════
# 单 Tool/MCP 直调
# ══════════════════════════════════════════════════════════════════════════
def build_direct_tool_graph(spec: ExecutionSpec, db: AsyncSession, checkpointer):
    """文档 3.2「直接调试单个工具」——不经过 LLM 推理层,单节点鉴权+执行,
    但复用同一套 interrupt()/HITL 机制,使审批恢复逻辑在两条路径上保持一致。"""
    tool_spec = spec.direct_tool

    async def invoke_node(state: RunState) -> dict:
        guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
        gr.check_and_increment_step(guardrail)
        gr.check_and_increment_tool_call(guardrail)
        last_human = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        raw_args = last_human.content if last_human else "{}"
        outcome = await execute_single_tool_call(
            db, task_id=state["task_id"], tool_name=tool_spec.func_name or tool_spec.name, raw_arguments=raw_args,
            # 直连单个工具(含工具中心「在线调试」)同样要能拿到它声明的前置工具:
            # 票务工具的 supplierId 来自内置 sys_supplier_ids
            bound_tools=bound_tools_map(_with_param_source_upstreams(
                [tool_spec], _builtin_tool_specs())), scenic_id=state.get("scenic_id"),
            role=state.get("role"), user_id=state.get("user_id"),
        )
        content = ctx.compress_or_truncate(
            outcome.content,
            response_schema=((getattr(tool_spec, "type_config", None) or {}).get("response_schema") or None),
            task_id=state.get("task_id"), tool_name=tool_spec.name or "")
        return {
            "messages": [AIMessage(content=content)], "guardrail": guardrail.as_dict(),
            "timeline": [{"step": "tool_call", "tool_name": tool_spec.name, "kind": outcome.kind}],
            "final_answer": content,
        }

    graph = StateGraph(RunState)
    graph.add_node("invoke", invoke_node)
    graph.add_edge(START, "invoke")
    graph.add_edge("invoke", END)
    return graph.compile(checkpointer=checkpointer)


# ══════════════════════════════════════════════════════════════════════════
# 固定问题编排直出
# ══════════════════════════════════════════════════════════════════════════

# 成文「如实」硬约束——这不是锦上添花,而是编排直出能不能用开关的前提:
# 默认汇总指令只要求「完整、连贯」,而喂进去的是 ①成功数据 ②缺参/报错文本 ③空结果
# 的混合体;模型的惯性恰恰是用合理的数据把缺口补上,补完就没人看得出缺过参数。
_DIRECT_DISPATCH_CONSTRAINTS = (
    "以下是各个工具本次调用的真实返回,其中可能混杂失败信息(如缺少必填参数、接口报错)"
    "或空结果。整合时必须如实呈现:\n"
    "1. 缺失必报:哪个工具没取到数据,必须在答案里明确指出是哪一块没取到、原因是什么"
    "(可引用工具返回的原话);\n"
    "2. 禁止填补:不得用推断值、历史值、行业的合理值去填补没取到的部分,也不得含糊带过;\n"
    "3. 允许不完整:宁可答案缺一块,也不允许看起来完整但数据是编的。"
)


def _dispatch_args(tool: ToolSpec, slots: dict) -> dict:
    """从本轮参数槽里挑该工具声明的那些参数。

    slots 是 executor 装配好的完整参数槽(会话历史 ∪ param_template 展开值 ∪ APP date
    ∪ 景区槽 ∪ Triager 抽取)——与 ReAct 路径下**模型看到的参数完全同源**,所以编排直出
    不存在第二套参数口径。挑不出值就让它缺着:由工具执行环节回答缺不缺,不在这里预判。
    """
    props = ((tool.json_schema or {}).get("properties") or {})
    if not props:
        return {}
    return {k: v for k, v in slots.items() if k in props and v not in (None, "")}


def _predicted_gaps(tool: ToolSpec, args: dict) -> list[str]:
    """预判该工具缺哪些必填参数。**纯诊断,不参与是否调用的决策。**

    会不会误判?会。它只是 json_schema.required 与已装配参数的差集,猜的是「如果不存在
    任何补齐机制会发生什么」;而真实的补齐发生在 execute_single_tool_call 内部
    (forced_params / x-param-source)。

    好在模型可见 Schema 已经把这些系统补齐的参数剥离掉了(json_schema ≠ validate_schema),
    所以这里**不需要**维护一张「当前有哪些补齐方式」的清单——那种清单是执行层逻辑的影子
    复制,每新增一种补齐方式都要跟着改、漏改还不报错。这正是当年否决「本地缺参判定决定
    调不调」的理由;此处只做记录,误判的代价仅限于一条时间线日志。
    """
    required = (tool.json_schema or {}).get("required") or []
    return [k for k in required if k not in args]


def build_direct_dispatch_graph(spec: ExecutionSpec, db: AsyncSession, checkpointer,
                                *, conversation_id: str | None = None):
    """固定问题「编排直出」图:技能绑定工具全集并行取数 → 一次成文(LLM 只出现 1 次)。

    适用前提:固定问题 + 已按指派 pin 解析出技能版本 + 该技能绑了工具。此时
    「问什么、调哪个接口、传什么参数」在配置期就已确定,没有让 LLM 去猜的必要。

    与 ReAct 图的本质区别:
    - 调不调某个工具:不由模型决定,**也不由本地缺参预判决定** —— 全集全调
    - 会不会缺参:不预判,由**真实调用结果**回答(既相通 ①数据 ②报错 ③空)
    - LLM 只负责最后一步:把 N 份真实返回整理成答案

    已知风险(链路内无法根治):缺参时接口未必报错,可能返回默认值/全量聚合 —— 那不是
    「缺参告知」而是静默的错口径数据。根治手段在工具契约侧:必填缺失必须报错。
    """
    skill = spec.skill_chain[0]
    tools = list(skill.tools)
    tools_map = bound_tools_map(tools)
    # Skill 级模型覆盖优先于编排级(与 DAG 图口径一致:constraints.model)
    model_override = skill.model or spec.model_override
    persona = spec.persona

    async def plan_node(state: RunState) -> dict:
        slots = state.get("slots") or {}
        gaps = {t.name: _predicted_gaps(t, _dispatch_args(t, slots)) for t in tools}
        return {
            "timeline": [_tl_ts({
                "step": "orchestration_plan",
                "skill": skill.name,
                "tools": [t.name for t in tools],
                # 诊断信息,仅供参考;与实际调用结果冲突时以实际结果为准
                "predicted_gaps": {k: v for k, v in gaps.items() if v},
            })],
        }

    async def invoke_all_node(state: RunState) -> dict:
        guardrail = gr.GuardrailState.from_dict(state.get("guardrail"))
        slots = state.get("slots") or {}

        async def _one(tool: ToolSpec) -> dict:
            # 每次真实调用都要计数:resilience 的重复调用检测依赖它,不能因为
            # 「不是模型调用的」就跳过。
            gr.check_and_increment_tool_call(guardrail)
            # 并发下每个工具用独立 AsyncSession(与 DAG 子循环同一做法):
            # 共享主 session 会并发冲突。
            async with get_sessionmaker()() as sub_db:
                outcome = await execute_single_tool_call(
                    sub_db, task_id=state["task_id"],
                    tool_name=tool.func_name or tool.name,
                    raw_arguments=_dispatch_args(tool, slots),
                    bound_tools=tools_map, scenic_id=state.get("scenic_id"),
                    role=state.get("role"), user_id=state.get("user_id"),
                )
            # 与 ReAct 主循环同一口径:**先压缩再截断**。
            # 这条是编排/并行路径(orchestration),原先只做原样截断 —— 而它恰恰是
            # 一次跑好几个工具、返回量最大的那条路:几个上千行的结果拼在一起,
            # 截断之后模型看到的是几段半截 JSON,然后一本正经地基于半截数据作答。
            # 压缩靠工具的 response_schema(Markdown 接口文档里那张返回参数表)。
            return {"tool": tool, "ok": outcome.ok, "kind": outcome.kind,
                    "content": ctx.compress_or_truncate(
                        outcome.content,
                        response_schema=((getattr(tool, "type_config", None) or {})
                                         .get("response_schema") or None),
                        task_id=state.get("task_id"), tool_name=tool.name or "")}

        results = (await asyncio.gather(*(_one(t) for t in tools))
                   if len(tools) > 1 else [await _one(tools[0])])

        outputs: dict[str, str] = {}
        timeline: list[dict] = []
        failed: list[str] = []
        for r in results:
            tool: ToolSpec = r["tool"]
            outputs[tool.name] = r["content"]
            if not r["ok"]:
                failed.append(tool.name)
            event = {
                "step": "tool_call", "tool_name": tool.func_name or tool.name,
                "tool_display_name": tool.name, "kind": r["kind"],
                "preview": r["content"][:120],
            }
            if not r["ok"]:
                # 缺参排查的主入口:失败原因原文留在时间线里(executor 的 _fallback_answer
                # 也读这里的 error 生成归因文案)。
                event["error"] = f"{r['kind']}:{r['content'][:300]}"
            timeline.append(_tl_ts(event))

        gr.check_and_increment_step(guardrail)
        return {
            "skill_outputs": outputs,
            # 全流程失败的信号要能随 checkpoint 恢复,故走 state 而非闭包变量
            "tool_tracker": {"dispatch_failed": failed,
                             "dispatch_all_failed": len(failed) == len(tools)},
            "guardrail": guardrail.as_dict(), "timeline": timeline,
            "usage": {"prompt_tokens": guardrail.prompt_tokens,
                      "completion_tokens": guardrail.completion_tokens},
        }

    async def synthesize_node(state: RunState) -> dict:
        # D3:全部工具都没拿到数据时不再消耗一次 LLM(零 token)。留空 final_answer,
        # 由 executor 收尾的 _fallback_answer 按时间线里的失败原因给出可读归因。
        if (state.get("tool_tracker") or {}).get("dispatch_all_failed"):
            log.warning(f"task {state['task_id']} 编排直出:全部工具失败,跳过成文 LLM")
            return {
                "messages": [], "final_answer": "",
                "timeline": [_tl_ts({"step": "generation", "preview": "",
                                     "skipped": "all_tools_failed"})],
            }
        return await _synthesize_final_answer(
            state, parts=dict(state.get("skill_outputs") or {}), db=db,
            model_override=model_override, persona=persona,
            conversation_id=conversation_id,
            extra_system=_DIRECT_DISPATCH_CONSTRAINTS, force_llm=True)

    graph = StateGraph(RunState)
    graph.add_node("plan", plan_node)
    graph.add_node("invoke_all", invoke_all_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "invoke_all")
    graph.add_edge("invoke_all", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)
