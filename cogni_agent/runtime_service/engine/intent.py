"""
执行前的意图分诊(Triager)与参数槽(Slot Filling)。

**要解决什么**:用户说"帮我看看昨天的情况",Agent 绑着五六个 Skill,模型只能猜——
猜对了是运气,猜错了会真的去调工具、花 token、甚至执行写操作。更糟的是它通常不会
说"我不确定",而是自信地给出一个基于错误假设的答案。

v1.1 升级为「意图层五机制」中的:
  ① Triager        -> 一次轻量 LLM 调用完成 意图路由 + 技能裁剪 + 槽位抽取 + 问题改写
  ② Slot Filling   -> conversation 级参数槽,优先级:APP date > 问题语义 > 会话 slot > 语义默认
  ③ Query Rewriting-> 把多轮对话中的省略表述改写为自包含问题(rewritten_goal)
  ④ Plan-and-Execute -> 复杂任务由 Triager 给出分步执行计划(plan),随 state 注入

**为什么澄清不走 HITL interrupt**:interrupt 是给"高风险动作等审批"用的,语义是
*任务暂停、等第三方批准*;而澄清的语义是*这一轮正常结束、把球踢回给用户*。
用 interrupt 会让任务停在 WAITING_HUMAN,占着租约、在审批中心里堆积,而它根本不需要
审批。直接结束本轮、把问题作为回答返回,配合多轮会话记忆,用户回答后自然接着走。

**默认开启,失败降级**:Triager 调用失败/返回非法 JSON 时降级为 L2 全量执行(不裁剪、
不澄清),保证可用性优先于准确率。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import calendar

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.engine.intent")

VERDICT_CLEAR = "clear"
VERDICT_AMBIGUOUS = "ambiguous"
VERDICT_OUT_OF_SCOPE = "out_of_scope"

ACTION_EXECUTE = "execute"
ACTION_CLARIFY = "clarify"


@dataclass
class IntentResult:
    verdict: str = VERDICT_CLEAR
    reason: str = ""
    reply: str = ""          # ambiguous / out_of_scope 时直接回给用户的话
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def should_execute(self) -> bool:
        return self.verdict == VERDICT_CLEAR


@dataclass
class TriageResult:
    """Triager 的判定结果(意图层五机制 ①③④⑤ 的载体)。"""

    intent: str = ""                       # 语义意图标签(如 数据查询/报表分析,仅日志用)
    target_skills: list[str] = field(default_factory=list)   # ⑤ 按需挂载:目标技能名(空 = 不裁剪)
    params_filled: dict = field(default_factory=dict)        # ② 从问题语义中抽出的已填参数
    params_missing: list[str] = field(default_factory=list)  # ② 执行所必需但缺失的参数
    action: str = ACTION_EXECUTE           # execute / clarify
    reply: str = ""                        # clarify 时直接回给用户的话(只问一个关键参数)
    rewritten_goal: str = ""               # ③ 自包含改写后的问题(空 = 用原句)
    plan: list[str] = field(default_factory=list)            # ④ 分步执行计划(复杂任务时)
    reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    degraded: bool = False                 # Triager 调用失败 → 降级 L2 全量
    # 为什么降级,一句人话。只说「Triager 不可用」的话,运维面对的是一个没有下一步的结论 ——
    # 分诊模型没登记、超时、返回了非法 JSON,这三种的处理方式完全不同。
    degraded_reason: str = ""

    @property
    def should_execute(self) -> bool:
        return self.action != ACTION_CLARIFY


def _cfg(key: str, default):
    return get_settings().get("runtime", "intent_check", key, default=default)


def _triage_model(fallback: str | None) -> str | None:
    """Triager 独立模型:配置 runtime.intent_check.triage_model(如非推理快模型
    doubao-seed-1-6-flash)则分诊优先走它,主链路仍用主模型 —— 分诊只是轻量
    JSON 抽取/分类,不该拖着 pro 推理模型 45s+ 的慢生成(此前 Triager 稳定超时
    降级「不可用」的根因之一)。未配置/为空则跟随主模型(fallback)。

    注意:llm/client._model_cfg 的四级兜底**只在没显式指定模型时生效**。这里配的名字
    是显式指定的 —— 解析不到会直接抛「未知模型」,而不是回落。所以调用处专门接住了
    这个错并回落主模型:分诊模型只是个提速选项,不该因为它没登记就把整个意图分诊废掉。
    """
    configured = str(_cfg("triage_model", "") or "").strip()
    return configured or fallback


# ── Triager 结果缓存 ──────────────────────────────────────────────
# 同一 (conversation_id, goal, capabilities, slots, model) 组合在 TTL 内直接复用判定结果,
# 避免同景区/同类型问题反复触发 LLM 调用(多轮对话命中率高,省 token 也省延迟)。
#
# v1.1.5 框架级修复:缓存 key 必须含会话隔离维度(conversation_id,无会话时用 task_id)。
# Triager 的判定直接决定**执行链裁剪**(挂哪些技能/不挂哪些),一旦跨会话复用,上一个
# 会话的错误判定(如 conv_5a900be0 事故:漏挂「数字大脑-客流分析」只挂参数解析)会被
# "传染"给新会话——新会话明明独立调用却拿到旧判定,查客流变成一句空话。隔离粒度与
# checkpoint 的 thread_id 完全一致:同一会话多轮共享(多轮命中率高的收益保留),跨会话不共享。
#
# 只缓存成功(非 degraded)结果:Triager 调用失败的降级结果不缓存,下次仍实时调用。
# 缓存对象以 deepcopy 返回,防止调用方改动污染缓存。进程内实现,单实例够用;
# 多实例部署时可升级为 Redis(配置 runtime.intent_check.cache_backend)。
_triage_cache: dict[str, tuple[float, TriageResult]] = {}


def _triage_cache_key(*, goal: str, capabilities: list[dict], slots: dict, model: str | None,
                      conversation_id: str | None = None, history_text: str = "") -> str:
    """缓存键:会话隔离维度 + 请求原文 + 能力清单(裁剪结果依赖它) + 会话槽位 + 模型 + 日期。

    conversation_id 必须入键:Triager 判定决定执行链裁剪,缓存跨会话复用会把错误判定
    传染给新会话(见模块注释)。与 checkpoint 同粒度:同一会话多轮共享,跨会话不共享;
    conversation_id 为空(单轮/internal 任务)时由调用方传 task_id 兜底,任务间同样隔离。
    日期必须入键:Triager 的判定依赖"今天"注入的相对时间换算(如"上个月"),
    跨天后旧日期的换算结果会出错,故缓存必须在日期变化时失效。
    v1.1.7:history_text 必须入键(设计文档 14 §7)——注入历史后,同会话同 goal 但历史
    不同(如被反问后用户重述同一句)不能复用缓存,否则会把旧一轮的 clarify 判定复用给
    新一轮(缓存化的"话题漂移"事故)。history_text 已蕴含注入深度(history_rounds),
    无需再单列轮数维度;空串 = 未注入,与存量单轮缓存隔离行为一致。
    """
    payload = json.dumps(
        {
            "conversation_id": conversation_id,
            "goal": goal,
            "capabilities": capabilities,
            "slots": {str(k): str(v) for k, v in sorted((slots or {}).items())},
            "model": model,
            "history": history_text or "",
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _triage_cache_get(key: str, ttl: float) -> TriageResult | None:
    if ttl <= 0:
        return None
    hit = _triage_cache.get(key)
    if hit is None:
        return None
    ts, result = hit
    if time.monotonic() - ts > ttl:
        _triage_cache.pop(key, None)
        return None
    return deepcopy(result)


def _triage_cache_put(key: str, result: TriageResult, ttl: float, max_entries: int) -> None:
    if ttl <= 0 or result.degraded:
        return
    if max_entries > 0 and len(_triage_cache) >= max_entries:
        # 超限时清掉最旧的 1/2,防止缓存无限膨胀(简单 FIFO 淘汰即可)
        oldest = sorted(_triage_cache.items(), key=lambda kv: kv[1][0])[: max(1, len(_triage_cache) // 2)]
        for k, _ in oldest:
            _triage_cache.pop(k, None)
    _triage_cache[key] = (time.monotonic(), result)


def enabled_for(skill_names: list[str], has_side_effects: bool,
                intent_config: dict | None = None) -> bool:
    """是否对本次执行做意图判定。

    `always` = 全开;`side_effects` = 只在存在写操作/高风险工具时开;
    `off` = 关闭。
    默认值由 config/config.yaml `runtime.intent_check.mode` 控制(v1.1 默认 always)。

    v1.1.5 意图识别 agent 化:Agent 通过 intent_config.mode 覆盖框架默认 ——
    意图识别挂在 Agent 下,一个 Agent 一个意图识别;未配置时回退框架级默认。
    """
    raw = (intent_config or {}).get("mode") or _cfg("mode", "off")
    # YAML 1.1 会把裸 off/on/yes/no 解析成布尔,配置里忘了加引号时这里要兜住,
    # 否则 str(False).lower() == "false",既不等于 always 也不等于 side_effects,
    # 行为上刚好等同于关闭——虽然结果对,但那是巧合,不能依赖。
    if isinstance(raw, bool):
        mode = "always" if raw else "off"
    else:
        mode = str(raw).lower()
    if mode == "always":
        return True
    if mode == "side_effects":
        return has_side_effects
    return False


# 自定义 intent_config.prompt 只承载「本 agent 特有的路由规则/话术」;框架模板
# _SYSTEM_TRIAGE 承载通用分诊方法论 + 输出格式 + 描述驱动路由判定标准(相对时间换算、
# 缺参不臆断、完整通读能力清单、target_skills 技能名白名单等),必须整体保留。
# 若只补 JSON 格式约束(此前做法),自定义 prompt 会丢掉这些关键判定标准;且框架模板
# 末尾自带「只输出 JSON」的格式约束,可兜底模型按工具调用习惯输出数组
# (AttributeError: 'list' object has no attribute 'get')。
_SYSTEM_TRIAGE = """你是智能体的任务分诊员(Triager)。给定用户请求、当前智能体的能力清单和已收集的上下文参数,完成四件事:
1. 意图识别(intent):用简短标签概括用户想干什么(如「数据查询」「报表分析」「状态判断」「普通对话」)。意图标签仅用于日志/调试,不影响路由。
2. 技能路由(target_skills):从能力清单里选出**必须参与**的技能名列表。拿不准时宁多勿少(多挂一个只多花一点 token,漏挂会答错)。普通对话/无需任何技能时给空数组。
3. 参数槽位(slots):把已经从用户问题/会话上下文里拿到的参数放进 filled(供执行层回填会话槽);
   缺失判定只依据工具说明里**明确声明**的缺参行为(如某工具说明写明「XX 缺失时向用户确认」)做,不要凭参数名
   臆断缺失 —— 参数完整性由 L2 执行层看完整 schema 校验,路由层臆断只会误反问。**一次只追问一个最关键缺失参数**。
4. 问题改写(rewritten_goal):如果这是多轮对话的省略表述(如"那同比呢""接着查上个月"),改写成不依赖上下文的自包含问题;已经是完整问题则保持原样。
5. 复杂任务(plan):如果任务需要分步完成(如"先做 A 分析、再做 B 对比、最后给结论"),给出 2-5 步执行计划;单步任务给空数组。

只输出 JSON,不要有任何其它内容:
{"intent": "意图标签", "target_skills": ["技能A"], "params": {"filled": {"参数名": "值"}, "missing": ["参数名"]}, "action": "execute|clarify", "reply": "action=clarify 时对用户说的话", "rewritten_goal": "改写后的自包含问题", "plan": ["步骤1", "步骤2"], "reason": "一句话理由"}

判定标准(描述驱动路由):
- **判定前必须先完整通读能力清单**:逐一读完每个技能的 description、可用工具与全部工具说明,先在心中形成「能力全景总结」(每个技能能做什么、依赖哪些参数、哪些缺失参数可由已有参数或工具说明声明的途径补齐),**全部读完、总结完之后**再判断参数缺失与是否反问。禁止只读了部分技能或部分工具说明就提前下结论。
- **第一职责是技能路由**:依据能力清单的 description 与工具说明(tool_notes)选出必须参与的技能。
  缺失判定只依据工具说明里**明确声明**的缺参行为(如某工具说明写明「XX 缺失时确认」),不要凭参数名臆断
  缺失 —— 参数完整性由 L2 执行层看完整 schema 校验,路由层拿并集参数名臆断只会误反问(过澄清)。
- **target_skills 只能返回能力清单里技能的名称,严禁返回工具名/接口名/URL。**
- 缺失参数先看能力清单里**有没有工具或技能能补齐**:只要能补齐(如某工具能由已有参数推导/解析出缺失参数,或某技能绑定的工具可完成前置准备),就**不要反问用户**,把对应技能加入 target_skills 继续 execute,补齐动作由执行层负责。
- 只有当缺失参数**没有任何工具或技能能补齐**、且工具说明明确声明它是**执行所必需**的关键参数时,才判 clarify,只反问这一个最关键缺失参数。
- target_skills 空数组 + action=execute 表示走普通对话或全量执行。
- 相对时间(如"上个月""昨天""上周""本周""近7天")必须按提示里给出的今天日期换算成具体时间段填入 filled,不要判 missing。例如今天在当月 X 日时,"上个月整月"应填上月 1 日 至 上月最后一天的起止时间(按实际月份天数,不要一律写 31 号);"上个月1日到31日"同样换算成上个月的 1 日到月末最后一天。具体的时间参数名(如开始时间/结束时间字段)以工具说明里声明的为准。
- 用户已明确时间范围时(如"上个月1日到31日"),缺的只是年份月份,直接按相对时间换算补齐,不要反问具体起止时间。
- 不要为细枝末节反复追问——被追问三次的用户会直接放弃;但关键必填缺失必须问清楚,绝不编造参数值蒙混执行。
- 时间类参数的具体参数名(如起止时间、日期字段)一律以工具说明/能力清单里声明的为准,不要在 JSON 里臆造清单外的参数名。
- **能力边界**:只能使用能力清单里声明的技能/工具。清单外的技能/工具/接口一律视为不可用,**严禁编造清单外能力**或假装调用成功;若用户请求明显超出当前能力清单覆盖范围(如要查的数据没有任何技能能提供),不要硬挂技能蒙混执行——如实判 clarify 或普通对话,向用户说明做不了什么,绝不虚构结果。
- **高风险动作不擅自执行**:若请求明显涉及删除/发布/资金/对外通知/批量修改等高风险操作,即使参数齐全也判 clarify,在 reply 里说明将执行的操作并请用户确认,不要直接 execute —— 写操作的最终放行由执行层的审批/确认机制兜底,路由层不替用户做这种决定。
- **补充参数不是新问题**:本轮只是补充一个参数(账号、商户 ID、日期、景区名等)来回应上一轮缺失或追问时,rewritten_goal 必须写成「上一轮用户的原问题 +(已补充:参数=值)」,让执行层带着新参数把上一轮问题重新答完;**不要**改写成「等待用户提出具体查询需求」「补充参数」这类没有问题的目标(执行层会拿着它从头规划一遍,既慢又答非所问)。
- **多轮延续优先**:若输入含「会话历史」段,先判断本轮请求是否在延续上轮话题、或直接回答助手上一轮的追问(如用户上轮被反问日期,本轮答「每一天」「都查」「整个月」) —— 若是,意图与技能路由应延续上轮话题,不要因本轮表述省略、缺乏话题关键词就另起炉灶、切到字面更相近的其他技能;省略表述的 rewritten_goal 优先基于会话历史改写为自包含问题(话题关键词从历史上文取,不从本轮孤立推断)。
- **历史只作上下文,不重复执行**:会话历史仅用于判断「本轮省略了什么、延续了什么」;历史中已完成的事项不要重复挂载/重复执行,用户明确切换话题时以本轮请求为准。"""


def required_param_gaps(
    *,
    skill_chain: list,
    target_skills: list[str],
    filled: dict,
    missing: list[str],
    slots: dict | None = None,
) -> list[str]:
    """L2 结构化输出 + schema 校验:Triager 输出后,对 target_skills 绑定的工具做必填覆盖检查。

    返回「工具必填、但 filled/missing/slots 均未覆盖」的参数名(去重);空列表 = 校验通过。
    拦截语义:Triager 判 execute 却漏报关键必填参数时,调用方据此并入 missing 并告警,
    不让"带病执行"静默进入执行层(conv_a5c0d4... 教训:Triager 漏报 compareStartTime/
    compareEndTime,执行层被迫反问)。

    依据 ToolSpec.json_schema.required —— resolver 已把 forced_params 与 x-param-source
    (由前置工具自动产出)的参数从模型可见 Schema 剥离,因此这里不会误报执行层能自动
    补齐的参数,与 Triager/执行层主模型看到的必填集合完全一致。
    """
    gaps: list[str] = []
    covered = {k: True for k in (missing or [])}
    covered.update(slots or {})
    for sk in skill_chain or []:
        if sk.name not in (target_skills or []):
            continue
        for t in getattr(sk, "tools", None) or []:
            # **系统内置工具不参与缺参判定**。它们是自动挂给每个技能的(resolver
            # 里 _builtin_specs() + tools),必填项是模型**调用那一刻**自己填的实参
            # —— sys_table_compare 的 key/value 来自它刚取到的那份数据,
            # sys_calc 的 expression 是它要算的式子 —— 没有一个是「用户没给的信息」。
            #
            # 算进来的后果不是报错,是**误反问**:这些参数名会并进 missing,
            # 让路由层跟用户要「key」「expression」这种用户根本看不懂的东西;
            # 内置工具越多,这份噪音越大(实测一条正常提问被判出 8 个假缺参)。
            if getattr(t, "type", None) == "builtin":
                continue
            schema = getattr(t, "json_schema", None) or {}
            for req in schema.get("required") or []:
                if req in filled or req in covered:
                    continue
                if req not in gaps:
                    gaps.append(req)
    return gaps


# (27 号评审:原「L3 x-default-rule 缺省规则补全」机制已整体移除——只要有默认,
# 最终都会是坑:本该暴露的缺参问题被缺省值隐藏。参数一律由问题行 param_template
# 在装配期生成完整,缺就是缺,由 L2 required_param_gaps 拦截暴露,
# 不做任何运行期默认补全。)


def _normalize_filled_value(value: object) -> str:
    """params_filled 值规范化:list/dict 保留 JSON 结构文本,其余转 str。

    若直接 str(list),Triager 输出的数组参数(如 supplierId=["PFTSCA01018088"])会变成
    "['PFTSCA01018088']" 这种 Python repr 字符串,合并进会话槽位后执行链要么照抄成
    带方括号引号的脏值(工具参数要求 array 却拿到字符串,接口解析失败/取错值),要么
    需再次解析。这里统一转成 JSON 文本:["PFTSCA01018088"],下游照抄即合法。
    """
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return str(value)


def _sanitize_target_skills(target: object, valid_names: set[str]) -> list[str]:
    """结构强制:target_skills 必须是「能力清单内技能名」的字符串数组(枚举校验)。

    行业做法是 JSON Schema/structured output 让模型物理上输出不了非法值;本项目走
    prompt 约束 + 解析层白名单校验的等效路线:模型可能混入工具名/接口名/URL(名称空间
    混淆)或非字符串元素,这里逐一校验 —— 非法元素剔除并告警;全部非法/非数组则置空
    (= 不裁剪,走全量执行),与 executor 的「全部未匹配回退全量」语义一致,但提前到
    路由层拦截,避免部分混入时执行层裁剪静默漏挂技能(漏挂会答错)。
    """
    if target is None:
        return []
    if not isinstance(target, list):
        log.warning(f"Triager target_skills 类型非法({type(target).__name__}),置空处理")
        return []
    cleaned: list[str] = []
    for item in target:
        if not isinstance(item, str):
            log.warning(f"Triager target_skills 元素非字符串({type(item).__name__}: {item!r}),剔除")
            continue
        if item in valid_names:
            cleaned.append(item)
        else:
            log.warning(f"Triager target_skills 含清单外名称(工具名/接口名/URL?): {item!r},剔除")
    if cleaned and len(cleaned) != len(target):
        log.warning(f"Triager target_skills 白名单过滤: {target} -> {cleaned}")
    return cleaned


# ---- v1.1.7 多轮会话历史注入:让 Triager 看到上一轮上下文,避免重复反问 ----

# 单条历史消息截断上限(与 llm_prompt_logs.history 的 300 字符惯例一致)
_HISTORY_MSG_MAX = 300
# 历史段总量兜底(约 2000-2500 token,对 Triager 轻量调用可忽略)
_HISTORY_TOTAL_MAX = 4000


def resolve_history_rounds(intent_config: dict | None) -> int:
    """解析 Agent 意图识别配置的 history_rounds(注入 Triager 的会话历史轮数)。

    规则(设计 14 §4.3):显式配置(0-5)即生效 —— 0 是有效值(强制关闭注入);
    未配置/缺失 = 0(不注入,存量单轮隔离行为);类型非法按 0 处理并 warning;
    越界:>5 clamp 到 5,<0 按 0。无框架级默认配置项(config.yaml 零改动),
    配置源唯一:intent_config.history_rounds。
    """
    raw = (intent_config or {}).get("history_rounds")
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        log.warning(f"intent_config.history_rounds 类型非法({type(raw).__name__}: {raw!r}),按 0 处理")
        return 0
    if raw < 0:
        log.warning(f"intent_config.history_rounds={raw} 小于 0,按 0 处理")
        return 0
    if raw > 5:
        log.warning(f"intent_config.history_rounds={raw} 超出上限 5,clamp 到 5")
        return 5
    return raw


def resolve_exec_history_rounds(intent_config: dict | None) -> int:
    """执行层 LLM 的会话历史轮数(32 号):agent_node 上下文按轮注入**对话文本**
    (user/assistant 原话),不再带旧轮工具结果原文——查询类多轮只需「说了什么」,
    不需要「上次查到什么」(旧工具结果是上下文毒源,会教模型不重新取数)。

    与 Triager 的 history_rounds(默认 0)区分:执行层**默认 2**(多轮追问
    「那同比呢」需要上下文延续);0 = 纯净上下文;>5 clamp 5。
    配置源唯一:intent_config.exec_history_rounds;固定问题在 executor 侧
    强制 0(工作流语义,不走本函数)。
    """
    raw = (intent_config or {}).get("exec_history_rounds")
    if raw is None:
        return 2
    if isinstance(raw, bool) or not isinstance(raw, int):
        log.warning(f"intent_config.exec_history_rounds 类型非法({type(raw).__name__}: {raw!r}),按默认 2 处理")
        return 2
    if raw < 0:
        return 0
    if raw > 5:
        return 5
    return raw


def format_history_text(turns: list[dict]) -> str:
    """把会话历史 turns 渲染为注入 Triager 的历史文本段。

    turns 由 conv_svc.get_recent_turns 产出(每轮 user+assistant 两条,时间正序,
    元素形如 {"role": "user"|"assistant", "content": str})。这里只做提示词渲染:
    单条截断到 _HISTORY_MSG_MAX、总量超 _HISTORY_TOTAL_MAX 时从最早整轮删除
    (保留最近轮,按因果阅读)。返回空串 = 无历史可注入。
    """
    if not turns:
        return ""
    chunks: list[list[str]] = []  # 每轮一个 chunk([用户行, 助手行(如有)])
    cur: list[str] = []
    for item in turns:
        role = "用户" if item.get("role") == "user" else "助手"
        content = str(item.get("content") or "")
        if len(content) > _HISTORY_MSG_MAX:
            content = content[:_HISTORY_MSG_MAX] + "…"
        if item.get("role") == "user" and cur:
            chunks.append(cur)
            cur = []
        cur.append(f"{role}: {content}")
    if cur:
        chunks.append(cur)
    # 单条已限 300 字符、一轮至多 2 条、轮数上限 5,总量必然 < 4000;
    # 仅当未来放开轮数/单条上限时此兜底才真正触发。
    while len(chunks) > 1 and sum(len(text) + 1 for ch in chunks for text in ch) > _HISTORY_TOTAL_MAX:
        chunks.pop(0)  # 从最早轮整轮删除,保最近轮
    return "\n".join(text for ch in chunks for text in ch)


async def triage(*, goal: str, capabilities: list[dict], slots: dict,
                 model: str | None, conversation_id: str | None = None,
                 intent_config: dict | None = None, history_text: str = "",
                 prompt_sink: Callable[[str, str], Awaitable[None]] | None = None) -> TriageResult:
    """执行一次 Triager 分诊(意图层五机制 ①③④⑤)。

    v1.1.6:prompt_sink 可选异步回调(system_prompt, user_prompt),在真正发出调用前
    把 Triager 的最终提示词交给调用方落库(会话信息技能做提示词质量分析)。

    capabilities: [{"name": 技能名, "description": 用途简述, "tools": ["工具名", ...],
                    "tool_notes": ["工具名:描述片段", ...]}]
    slots:        conversation 级参数槽(APP date 上下文 + 历史会话已收集参数)。
    conversation_id: 会话隔离维度(thread_id = conversation_id or task_id,与 checkpoint
                    同粒度),进缓存 key 防跨会话复用(见 _triage_cache_key 注释)。
    intent_config: v1.1.5 意图识别 agent 化 —— Agent 级意图识别配置:
        {"mode": "always|side_effects|off",   # 覆盖框架默认判定策略
         "model": str|None,                   # 覆盖分诊模型
         "prompt": str|None}                  # 覆盖 Triager system prompt(自定义意图识别)
        (27 号评审:recheck_before_clarify 二次确认已移除——性能优先,不在意图层叠加 LLM 调用)
        意图识别挂在 Agent 下,一个 Agent 一个意图识别;未配置时用框架默认模板/模型。
    任何异常都退化为 degraded=True(降级 L2 全量执行)。
    """
    from cogni_agent.runtime_service.llm import client as llm_client

    # v1.1.5 意图识别 agent 化:Agent 独立模型覆盖优先,否则框架级 Triager 独立模型覆盖
    main_model = model
    model = (intent_config or {}).get("model") or _triage_model(model)

    cache_ttl = float(_cfg("cache_ttl_seconds", 300) or 0)
    cache_max = int(_cfg("cache_max_entries", 512) or 0)
    key = _triage_cache_key(goal=goal, capabilities=capabilities, slots=slots,
                            model=model, conversation_id=conversation_id,
                            history_text=history_text)
    cached = _triage_cache_get(key, cache_ttl)
    if cached is not None:
        log.info(f"Triager 命中缓存,跳过 LLM 调用(goal={goal[:40]})")
        return cached

    if not capabilities:
        lines = "- (未绑定任何技能,只能进行普通对话)"
    else:
        # v1.1.5:技能描述全量注入(不再截断 120) —— 描述驱动路由的第一依据就是技能
        # description,截断会让「何时用/何时不用」这类关键路由信息残缺;配合工具名/工具
        # 描述全量,能力清单 = 技能名+技能描述+工具名+工具描述 四层全量,边界完整。
        lines = "\n".join(
            f"- {c.get('name', '?')}: {c.get('description', '')}"
            + (f"\n  可用工具:{', '.join(c.get('tools', []))}" if c.get("tools") else "")
            + (f"\n  工具说明:{'; '.join(c.get('tool_notes', []))}" if c.get("tool_notes") else "")
            for c in capabilities
        )
    slots_text = json.dumps(slots, ensure_ascii=False) if slots else "(空)"
    history_section = ""
    if history_text:
        history_section = (f"会话历史(最近对话;用户可能正在延续上轮话题,"
                           f"尤其是回答助手上一轮的追问):\n{history_text}\n\n")
    user = (f"今天是 {datetime.now().strftime('%Y-%m-%d')}。\n\n"
            f"{history_section}"
            f"用户请求:\n{goal}\n\n当前智能体的能力清单:\n{lines}\n\n"
            f"已收集的上下文参数(APP端日期选择/历史会话):\n{slots_text}")
    # v1.1.5 意图识别 agent 化:Agent 自定义 Triager prompt 覆盖框架模板 ——
    # 意图识别挂在 Agent 下,每个 Agent 可以定制自己的分诊规则/话术。
    # 自定义 prompt 只承载「本 agent 特有的路由规则/话术」;框架模板 _SYSTEM_TRIAGE
    # 整体保留(通用分诊方法论 + 输出格式 + 描述驱动路由判定标准)。只补格式约束会丢掉
    # 相对时间换算/缺参不臆断/完整通读能力清单等关键判定,故必须拼接完整模板。
    # 拼接顺序:自定义规则在前,框架方法论在后(靠后指令权重更高),兜底自定义规则
    # 与框架冲突时的行为;框架模板末尾自带「只输出 JSON」格式约束,防模型输出数组崩溃。
    custom_prompt = (intent_config or {}).get("prompt")
    if custom_prompt:
        triage_prompt = f"{custom_prompt}\n\n{_SYSTEM_TRIAGE}"
    else:
        triage_prompt = _SYSTEM_TRIAGE

    # v1.1.6:Triager 的最终提示词落库(step=intent_check),供提示词质量分析。
    if prompt_sink is not None:
        try:
            await prompt_sink(triage_prompt, user)
        except Exception:  # noqa: BLE001 - 落库失败不影响分诊
            log.exception("记录 Triager 提示词日志失败(goal=%s)", goal[:40])

    try:
        result = await llm_client.complete_collected(
            model, [{"role": "system", "content": triage_prompt}, {"role": "user", "content": user}],
            max_tokens=300, temperature=0,
            # 确定性优先(行业 router 共识):temperature=0 保证同一请求重复分诊结果一致;
            # 判定可缓存(上方 _triage_cache_*)进一步复用;失败不重试(max_retries=0)——
            # 路由判定错误重试只会让用户白等,超时/失败直接降级 L2 全量执行,代价可控。
            # Triager 只输出一个小 JSON:300 token 足够;max_tokens 过大会让推理模型的
            # 生成时间显著变长(实测 max_tokens=600 的大请求稳定超时,=10 只要 6.8s)。
            # 但推理模型生成完整分诊 JSON(含 plan/reason)实测需 45s+,固定 30s 会稳定
            # 超时→「Triager 不可用」持续降级(conv_fb4df0 事故),故超时走配置
            # runtime.intent_check.timeout_seconds 可调(默认 90s)。
            # 超时/失败仍降级 L2 全量执行,不重试(max_retries=0),重试只会让用户白等。
            timeout=_cfg("timeout_seconds", 90), max_retries=0,
        )
    except ValueError as exc:
        # 「未知模型」:分诊模型是一个**可选的提速配置**,配的名字在「模型配置」里没有
        # (换了供应商、接入点没开通、名字写错)时,应该回落主模型继续分诊,而不是
        # 把整个意图分诊废掉。_model_cfg 的四级兜底只在「没显式指定模型」时生效,
        # 而这里恰恰是显式指定的 —— 于是 0ms 抛错、每一轮都降级,现场看到的就是
        # 「Triager 不可用,已降级直接执行」,而主模型明明好好的。
        if "未知模型" not in str(exc) or not main_model or model == main_model:
            log.exception("Triager 调用失败,降级为 L2 全量执行")
            return TriageResult(degraded=True, degraded_reason=f"分诊模型 {model!r} 解析失败:{exc}")
        log.warning("分诊模型 %r 不可用(%s),本次回落主模型 %r 继续分诊 —— "
                    "请到「系统设置 · 模型与预算」登记它,或把 runtime.intent_check.triage_model 置空",
                    model, exc, main_model)
        try:
            result = await llm_client.complete_collected(
                main_model, [{"role": "system", "content": triage_prompt}, {"role": "user", "content": user}],
                max_tokens=300, temperature=0,
                timeout=_cfg("timeout_seconds", 90), max_retries=0)
        except Exception as exc2:  # noqa: BLE001
            log.exception("回落主模型后 Triager 仍失败,降级为 L2 全量执行")
            return TriageResult(degraded=True,
                                degraded_reason=f"分诊模型 {model!r} 不可用,回落主模型后仍失败:{exc2}")
        model = main_model
    except Exception as exc:  # noqa: BLE001
        log.exception("Triager 调用失败,降级为 L2 全量执行")
        return TriageResult(degraded=True,
                            degraded_reason=f"分诊调用失败({type(exc).__name__}):{exc}"[:300])

    raw = (result.content or "").strip()
    # 模型有时会用 ```json 包起来
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"Triager 返回的不是合法 JSON,降级为 L2 全量执行:{raw[:200]}")
        return TriageResult(degraded=True,
                            degraded_reason=f"分诊模型返回的不是合法 JSON(前 80 字:{raw[:80]})—— "
                                            f"多半是这个模型不擅长严格 JSON 输出,换一个再试",
                            prompt_tokens=result.prompt_tokens,
                            completion_tokens=result.completion_tokens)
    if not isinstance(data, dict):
        # 模型输出了非对象 JSON(如按工具调用习惯输出数组 [{"name":...,"parameters":...}]),
        # 无法按分诊字段解析。按「任何异常都降级 L2 全量执行」设计兜底,不抛未捕获异常。
        log.warning(f"Triager 返回的不是 JSON 对象(类型={type(data).__name__}),降级为 L2 全量执行:{raw[:200]}")
        return TriageResult(degraded=True,
                            degraded_reason=f"分诊模型输出了 {type(data).__name__} 而不是 JSON 对象 —— "
                                            f"常见于把分诊当成工具调用输出成数组,换一个模型再试",
                            prompt_tokens=result.prompt_tokens,
                            completion_tokens=result.completion_tokens)

    action = str(data.get("action") or ACTION_EXECUTE).lower()
    if action not in (ACTION_EXECUTE, ACTION_CLARIFY):
        action = ACTION_EXECUTE
    reply = str(data.get("reply") or "").strip()
    # 判了 clarify 却没给出 reply,等于把用户晾在那儿 —— 这种情况放行更安全
    if action == ACTION_CLARIFY and not reply:
        log.warning("Triager 判定 clarify 但没有给出 reply,降级为 execute")
        action = ACTION_EXECUTE

    filled = data.get("params") or {}
    # filled 值规范化:list/dict 保留 JSON 结构文本,防 str(list) 产生 "['a']" 这类 Python
    # repr 污染槽位(工具参数要求数组却拿到带方括号字符串,接口解析失败/取错值)。
    filled = ({str(k): _normalize_filled_value(v) for k, v in filled.get("filled", {}).items()}
              if isinstance(filled, dict) else {})
    missing = [str(m) for m in (filled.get("missing", []) if isinstance(filled, dict) else [])]
    # 结构强制(改动 4):target_skills 按「能力清单技能名」白名单枚举校验 —— 混入的
    # 工具名/接口名/URL/非字符串元素剔除告警,全部非法则置空(= 全量执行,不裁剪)。
    valid_names = {str(c.get("name")) for c in capabilities if c.get("name")}
    target = _sanitize_target_skills(data.get("target_skills"), valid_names)
    # 类型防御:intent/plan 非字符串/非数组时置空,避免 str(list)/遍历 dict key 产生垃圾值。
    intent_raw = data.get("intent")
    intent = intent_raw if isinstance(intent_raw, str) else ""
    if intent_raw is not None and not isinstance(intent_raw, str):
        log.warning(f"Triager intent 非字符串({type(intent_raw).__name__}),置空")
    plan_raw = data.get("plan")
    plan = [str(p) for p in plan_raw] if isinstance(plan_raw, list) else []

    triaged = TriageResult(
        intent=intent,
        target_skills=target,
        params_filled=filled,
        params_missing=missing,
        action=action,
        reply=reply,
        rewritten_goal=str(data.get("rewritten_goal") or "").strip(),
        plan=plan,
        reason=str(data.get("reason") or ""),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    log.debug(f"Triager 判定 action={action} intent={triaged.intent} "
              f"target_skills={target} filled={filled} missing={missing} "
              f"degraded={triaged.degraded} reason={triaged.reason[:80] if triaged.reason else ''}")

    # (27 号评审:原「反问前二次确认 recheck_before_clarify」已移除——clarify 判定
    # 后再追加一次 LLM 复核等于链路里多一跳,前端等不了更久;误反问由会话槽位/
    # 执行层自然兜底,不在意图层叠加 LLM 调用。)
    _triage_cache_put(key, triaged, cache_ttl, cache_max)
    return triaged


_SYSTEM = """你是任务分诊员。给定用户请求和当前智能体具备的能力清单,判断这次请求能否直接执行。

只输出 JSON,不要有任何其它内容:
{"verdict": "clear|ambiguous|out_of_scope", "reason": "一句话理由", "reply": "当 verdict 不是 clear 时,直接对用户说的话"}

判定标准:
- clear:请求明确,且用现有能力可以完成。**倾向于判 clear** —— 只要能合理推断出用户想要什么就直接执行,不要因为细节不全就打断用户。
- ambiguous:缺少**执行所必需**的关键信息(比如要查某个时间段的数据却没说时间段、要操作某个对象却没说是哪个),且无法从上下文合理推断。reply 里提出**一个**最关键的问题,不要一次问一堆。
- out_of_scope:现有能力明显完成不了。reply 里说明做不了什么、建议用户找谁或换哪个智能体。

注意:宁可判 clear 让它执行,也不要为了细枝末节反复追问——被追问三次的用户会直接放弃。"""


async def classify(*, goal: str, skill_summaries: list[str], tool_names: list[str],
                   model: str | None) -> IntentResult:
    """做一次意图判定(旧版三分类,兼容保留)。任何异常都退化为 clear。"""
    from cogni_agent.runtime_service.llm import client as llm_client

    capability = "\n".join(f"- {s}" for s in skill_summaries) or "- (未绑定任何技能,只能进行普通对话)"
    tools = "、".join(tool_names[:20]) or "(无可用工具)"
    user = f"用户请求:\n{goal}\n\n当前智能体的能力:\n{capability}\n\n可用工具:{tools}"

    try:
        result = await llm_client.complete_collected(
            model, [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            max_tokens=300, temperature=0,
        )
    except Exception:  # noqa: BLE001
        log.exception("意图判定调用失败,按 clear 放行")
        return IntentResult()

    raw = (result.content or "").strip()
    # 模型有时会用 ```json 包起来
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"意图判定返回的不是合法 JSON,按 clear 放行:{raw[:200]}")
        return IntentResult(prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)
    if not isinstance(data, dict):
        log.warning(f"意图判定返回的不是 JSON 对象(类型={type(data).__name__}),按 clear 放行:{raw[:200]}")
        return IntentResult(prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)

    verdict = str(data.get("verdict") or VERDICT_CLEAR).lower()
    if verdict not in (VERDICT_CLEAR, VERDICT_AMBIGUOUS, VERDICT_OUT_OF_SCOPE):
        verdict = VERDICT_CLEAR
    reply = str(data.get("reply") or "").strip()
    # 判了不执行却没给出要对用户说的话,等于把用户晾在那儿 —— 这种情况放行更安全
    if verdict != VERDICT_CLEAR and not reply:
        log.warning(f"意图判定为 {verdict} 但没有给出 reply,按 clear 放行")
        verdict = VERDICT_CLEAR

    return IntentResult(
        verdict=verdict, reason=str(data.get("reason") or ""), reply=reply,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
    )


# ── L3 确定性参数补全 ──────────────────────────────────────────────────────
#
# **解决的是一类真实事故**:Agent 提示词里声明「对比期默认环比」,
# 但 Triager 输出时漏填 compareStartTime/compareEndTime,执行层只能反问用户
# ——而用户明明什么都没少说,只是模型没遵守提示词里的约定。
#
# 对策不是"把提示词写得更凶",而是**把这类能算出来的参数交给代码算**:
# 工具在 param_defaults 里声明规则(参照哪个参数、按什么粒度平移多少),
# Triager 输出后由 L3 确定性补全。不反问、不依赖模型遵守率。
#
# 三条纪律:
# 1. **只补真正缺失的**,已填的一律不覆盖 —— 用户显式说了「对比 6 月」就得听他的;
# 2. **宁缺勿臆造**:参照参数缺失或解析失败时不补,留给 L2 去拦截并反问。
#    补一个猜出来的时间比反问更糟 —— 用户会拿到一份口径不对却看不出问题的数据;
# 3. **幂等**:补全结果并入后再调用不再补,避免多轮里反复平移越走越远。

_PERIODS = ("day", "week", "month", "year")


def _shift_time(value: str, period: str, offset: int) -> str | None:
    """把时间字符串按粒度平移。解析失败或粒度非法返回 None(由调用方决定不补)。

    **月末对齐**是这里唯一有技巧的地方:3月31日减一个月,2月没有31号,
    按「该月最后一天」对齐到 2月28日(闰年 2月29日)。
    直接减 30 天会得到 3月1日,那在「上月同期」的语义下是错的。
    """
    if not isinstance(value, str) or period not in _PERIODS or not offset:
        return None

    text = value.strip()
    # 输入什么格式就输出什么格式 —— 带时分秒的参数换成纯日期会让下游解析失败
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        return None

    if period == "day":
        shifted = dt + timedelta(days=offset)
    elif period == "week":
        shifted = dt + timedelta(weeks=offset)
    else:
        months = offset * (12 if period == "year" else 1)
        total = dt.year * 12 + (dt.month - 1) + months
        year, month = divmod(total, 12)
        month += 1
        # 月末对齐:目标月没有这一天就取该月最后一天
        last_day = calendar.monthrange(year, month)[1]
        shifted = dt.replace(year=year, month=month, day=min(dt.day, last_day))

    return shifted.strftime(fmt)


def _valid_rule(rule: object) -> bool:
    """规则合法性。非法规则**静默忽略**而不是报错 ——
    工具配置由业务方维护,一条写错的规则不该让整次执行失败。"""
    if not isinstance(rule, dict):
        return False
    if rule.get("mode") != "shift":
        return False
    ref = rule.get("ref")
    if not isinstance(ref, list) or not ref:
        return False
    if rule.get("period") not in _PERIODS:
        return False
    return bool(rule.get("offset"))     # offset=0 等于没平移,视为无效


def apply_param_defaults(*, skill_chain: list, target_skills: list[str],
                         filled: dict) -> dict:
    """按工具声明的 param_defaults 补全缺失参数,返回**新增的那些**(不含已填的)。

    只处理 target_skills 命中的技能 —— Triager 已经判定这次用不到的技能,
    它的工具规则不该影响本次参数。
    """
    if not target_skills or not skill_chain:
        return {}

    targets = set(target_skills)
    out: dict = {}
    for skill in skill_chain:
        if getattr(skill, "name", None) not in targets:
            continue
        for tool in getattr(skill, "tools", None) or []:
            for param, rule in (getattr(tool, "param_defaults", None) or {}).items():
                # 已填的不覆盖:用户显式给了值就得听他的
                if param in filled and str(filled.get(param) or "").strip():
                    continue
                if param in out or not _valid_rule(rule):
                    continue
                ref_value = filled.get(rule["ref"][0])
                if not ref_value:
                    continue          # 参照参数缺失 -> 算不出来,留给 L2 拦截
                shifted = _shift_time(str(ref_value), rule["period"], int(rule["offset"]))
                if shifted:
                    out[param] = shifted
    return out


import re as _re  # noqa: E402 - 只这一处用

_EMPTY_GOAL = _re.compile(r"等待用户.{0,8}(提出|给出|说明)|等待.{0,6}(具体|查询)?(需求|问题)|无具体(查询|问题|需求)")


def is_empty_goal(goal: str) -> bool:
    """分诊改写出来的「没有问题的目标」(如「补充OTA商户ID,等待用户提出具体查询需求」)。

    任务 5877db72:用户只是补了一个商户 ID,分诊把目标改写成「……等待用户提出具体查询需求」,
    执行层拿着这句话从头规划,第二次内容生成花了 86 秒,又把整份四维报告重做了一遍。
    这种改写不如不改:用用户原话,执行层能从会话历史里接上上一轮的问题。
    """
    return bool(_EMPTY_GOAL.search(goal or ""))
