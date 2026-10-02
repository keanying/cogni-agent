"""
M4-06/M4-09:护栏——最大步数/工具调用次数/Token 预算超限即熔断(GuardrailTripped);
同时承载"处理不确定性"三件套里另外两件的计数与限流:

- 幻觉/随机路由(模型选了不存在的工具、编造了工具名) -> hallucination_retries
- 格式错误(工具调用参数不是合法 JSON、不满足 JSON Schema) -> format_retries

两类重试都是"有限次数的自愈"——允许模型在图内自我纠正,但超过阈值同样熔断,
避免死循环里空耗 Token。阈值均可被 Skill.constraints 覆盖,全局默认见 config.yaml。
"""
from __future__ import annotations

from dataclasses import dataclass

from cogni_agent.common.errors import GuardrailTripped
from cogni_agent.config import get_settings


@dataclass
class GuardrailState:
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    format_retries: int = 0
    hallucination_retries: int = 0
    drift_checks: int = 0
    hop_count: int = 0  # L3 预留:跨任务 Agent 消息防成环计数
    # 模型/网关在响应里返回的成本累计(USD)。None 之外大多数模型不返回,通常为 0。
    # 用于与本地按配置单价算出的 cost_cny 对比,看两端计费是否有差异。
    model_cost_usd: float = 0.0
    # 按模型归集的计费返回情况:{"<model key>": {"calls": 调用次数,
    #   "returned_cost": 其中响应里带了 cost 的次数, "cost_usd": 返回成本累计(USD)}}。
    # 用于最终提示"某模型 N 次调用未返回对方计费字段",与本地估算区分开。
    model_cost_status: dict[str, dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.model_cost_status is None:
            self.model_cost_status = {}

    def as_dict(self) -> dict:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "format_retries": self.format_retries,
            "hallucination_retries": self.hallucination_retries,
            "drift_checks": self.drift_checks,
            "hop_count": self.hop_count,
            "model_cost_usd": self.model_cost_usd,
            "model_cost_status": self.model_cost_status,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "GuardrailState":
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def new_guardrail_state() -> GuardrailState:
    return GuardrailState()


def _limits() -> dict:
    return get_settings().get("runtime", "guardrails", default={}) or {}


def check_and_increment_step(state: GuardrailState, *, max_steps: int | None = None) -> None:
    limit = max_steps if max_steps is not None else _limits().get("max_steps", 25)
    state.steps += 1
    if state.steps > limit:
        raise GuardrailTripped(f"超过最大步数 {limit}")


def check_and_increment_tool_call(state: GuardrailState, *, max_tool_calls: int | None = None) -> None:
    limit = max_tool_calls if max_tool_calls is not None else _limits().get("max_tool_calls", 10)
    state.tool_calls += 1
    if state.tool_calls > limit:
        raise GuardrailTripped(f"超过最大工具调用次数 {limit}")


def remaining_tool_calls(state: GuardrailState, *, max_tool_calls: int | None = None) -> int:
    """本任务还能真正执行几次工具调用(0 = 已用完)。

    工具节点据此**截断**一轮里超额的并行调用,而不是让 check_and_increment_tool_call 抛出:
    模型一轮常并发请求五六个工具,在 98 次时来一轮 5 个 —— 以前直接熔断成 FAILED,
    前面九分钟取回来的数据、已经花掉的 token 全部作废,用户什么都拿不到。
    截断后下一轮 agent 节点看到额度用完,会走「收尾轮」基于已有结果作答。
    """
    limit = max_tool_calls if max_tool_calls is not None else _limits().get("max_tool_calls", 10)
    return max(0, int(limit) - int(state.tool_calls))


def resolve_token_budget(task_budget: int | None = None) -> int:
    """算出本任务真正生效的 Token 预算。返回 0 表示**不限制**(只记账,不熔断)。

    三态,别把「没配」和「配了 0」混为一谈 —— 这是本函数存在的全部理由:
      - task_budget is None  -> 没人配过,回退全局 runtime.guardrails.default_task_token_budget
      - task_budget <= 0     -> 显式配成「不限制」,**不再回退全局默认**
      - task_budget > 0      -> 就按它
    全局默认本身同样支持 0/留空 = 不限制(config.yaml 的出厂值就是不限制:
    消耗照常记录并计费,是否要卡额度由业务自己在 Skill 护栏或系统设置里定)。
    """
    if task_budget is None:
        raw = _limits().get("default_task_token_budget", 0)
    else:
        raw = task_budget
    try:
        budget = int(raw or 0)
    except (TypeError, ValueError):         # 配置写成了非数字,按不限制处理,不因配置错误杀任务
        return 0
    return budget if budget > 0 else 0


def check_token_budget(state: GuardrailState, task_budget: int | None = None) -> None:
    """超出预算即熔断;预算为「不限制」时直接返回(token 仍在 state 里累计,不影响记账)。"""
    budget = resolve_token_budget(task_budget)
    if budget <= 0:
        return
    used = state.prompt_tokens + state.completion_tokens
    if used > budget:
        raise GuardrailTripped(f"超过 Token 预算 {budget}")


def check_and_increment_format_retry(state: GuardrailState) -> None:
    """M4-09:工具调用参数格式错误(非法 JSON / 不满足 Schema)时调用——允许有限次自我纠正。"""
    limit = _limits().get("max_format_retries", 3)
    state.format_retries += 1
    if state.format_retries > limit:
        raise GuardrailTripped(f"工具调用参数格式连续错误超过 {limit} 次,判定模型无法自我纠正")


def check_and_increment_hallucination_retry(state: GuardrailState) -> None:
    """M4-09:模型调用了不在绑定范围内的工具名(幻觉/试图越权路由)时调用。"""
    limit = _limits().get("max_hallucination_retries", 3)
    state.hallucination_retries += 1
    if state.hallucination_retries > limit:
        raise GuardrailTripped(f"模型连续 {limit} 次调用不存在/未授权的工具,判定为持续幻觉,熔断")


def record_tokens(
    state: GuardrailState,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None = None,
    model_key: str | None = None,
) -> None:
    state.prompt_tokens += prompt_tokens
    state.completion_tokens += completion_tokens
    if cost_usd:
        state.model_cost_usd += cost_usd
    if model_key:
        item = state.model_cost_status.setdefault(
            model_key, {"calls": 0, "returned_cost": 0, "cost_usd": 0.0}
        )
        item["calls"] += 1
        if cost_usd is not None:
            item["returned_cost"] += 1
            item["cost_usd"] += cost_usd
