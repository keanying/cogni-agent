"""
工具调用的韧性策略:重复检测 + 失败兜底。

**要解决的两个真实失败模式**(都不会报错,只会安静地烧掉预算然后给出一个烂答案):

1. **原地打转**:模型拿完全相同的参数反复调用同一个工具。常见于工具返回了空结果或
   模型没看懂返回值——它会"再试一次",然后再试一次。护栏只限制总次数,所以表现是
   10 次工具调用全花在同一个无效调用上,最后因为达到上限而失败。

2. **坏工具拖垮整个任务**:某个工具的上游挂了,每次调用都报错。模型不知道"这个工具
   现在不可用",会继续尝试,同样把预算烧光。而它本可以换个工具或直接告诉用户
   "某某数据暂时取不到"。

处理原则是**给模型可行动的信息,而不是简单拦截**:
- 重复调用:把上一次的结果原样回给它,并明确告诉它"参数完全相同,结果不会变,
  请换参数或换思路" —— 比静默拦截好,模型知道发生了什么才能自我纠正
- 连续失败:达到阈值后**把这个工具从可选列表里摘掉**,并在提示里说明原因。
  摘掉比继续放着更有效——只要它还在工具列表里,模型就有可能再选它
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.engine.resilience")


def _cfg(key: str, default):
    return get_settings().get("runtime", "resilience", key, default=default)


def call_signature(tool_name: str, raw_arguments: str | None) -> str:
    """把「工具 + 参数」归一化成一个指纹。

    参数先尝试按 JSON 解析再用排序键序列化 —— 模型两次生成的 JSON 可能键序不同、
    空格不同,但语义完全一样。只比字符串会漏掉这种"实质重复"。
    解析失败(参数本身就是坏 JSON)时退回原始串比较。
    """
    raw = raw_arguments or ""
    try:
        normalized = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        normalized = raw.strip()
    return hashlib.sha1(f"{tool_name}\x00{normalized}".encode()).hexdigest()[:16]


@dataclass
class ToolCallTracker:
    """一次任务执行内的工具调用轨迹。随 RunState 一起走 checkpoint,因此必须可序列化。"""

    # 调用指纹 -> 出现次数
    seen: dict[str, int] = field(default_factory=dict)
    # 调用指纹 -> 上次的结果摘要(重复时原样回给模型)
    last_result: dict[str, str] = field(default_factory=dict)
    # 工具名 -> 连续失败次数(成功一次即清零)
    failures: dict[str, int] = field(default_factory=dict)
    # 已被摘掉的工具名
    disabled: list[str] = field(default_factory=list)
    # 连续「空转」轮数:一轮工具调用**全部**是被拦下的重复调用(一次真执行都没有)。
    # 空转不烧工具,但每轮都要付一次完整的模型生成(上下文大时一次 40 秒),
    # 而工具调用计数不动,「额度用尽即收尾」永远触发不了 —— 现场任务就这样转了 9 分钟。
    stalls: int = 0

    def as_dict(self) -> dict:
        return {"seen": self.seen, "last_result": self.last_result,
                "failures": self.failures, "disabled": self.disabled, "stalls": self.stalls}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ToolCallTracker":
        d = d or {}
        return cls(seen=dict(d.get("seen") or {}), last_result=dict(d.get("last_result") or {}),
                   failures=dict(d.get("failures") or {}), disabled=list(d.get("disabled") or []),
                   stalls=int(d.get("stalls") or 0))


@dataclass
class RepeatVerdict:
    is_repeat: bool
    times: int
    previous_result: str | None


def check_repeat(tracker: ToolCallTracker, tool_name: str, raw_arguments: str | None) -> RepeatVerdict:
    """登记本次调用并判断是否为重复。返回 times 是**含本次**的出现次数。"""
    sig = call_signature(tool_name, raw_arguments)
    times = tracker.seen.get(sig, 0) + 1
    tracker.seen[sig] = times
    threshold = int(_cfg("repeat_threshold", 2))
    return RepeatVerdict(is_repeat=times >= threshold, times=times,
                         previous_result=tracker.last_result.get(sig))


def remember_result(tracker: ToolCallTracker, tool_name: str, raw_arguments: str | None, content: str) -> None:
    """记住结果摘要,重复调用时回给模型。只留一小段——完整结果已经在上一条 ToolMessage 里,
    这里的作用是提醒,不是再塞一份数据进上下文。"""
    sig = call_signature(tool_name, raw_arguments)
    tracker.last_result[sig] = (content or "")[:300]


def repeat_notice(tool_name: str, verdict: RepeatVerdict) -> str:
    """给模型的重复调用提示。写得具体一些,让它知道该怎么改。

    v1.1.6:「调用过 N 次」这类内部计数是系统诊断细节,对模型决策无增量信息,反而是
    提示词噪音;只留「参数相同→结果不会变→换思路」这一事实供模型自我纠正。
    计数仍保留在 log 与 timeline 的 tool_call_skipped / tool_disabled 事件里。
    """
    prev = f"\n上次的返回是:{verdict.previous_result}" if verdict.previous_result else ""
    return (
        f"【系统提示】你已经用**完全相同的参数**调用过 `{tool_name}`,"
        f"参数没变结果就不会变,不要再重复调用。{prev}\n"
        f"请从以下几种方式中选一种继续:换一组参数再试、改用其它工具、"
        f"或者基于已有信息直接作答并说明哪部分数据没能取到。"
    )


def record_repeat_pressure(tracker: ToolCallTracker, tool_name: str, times: int) -> str | None:
    """重复调用本身也是"卡住了"的信号,必须计入摘除判定。

    **这是实测发现的坑**:最初只在"真实执行失败"时累计失败次数。但重复调用会被拦截、
    不再真的执行,于是失败计数停在 1,工具永远达不到摘除阈值 —— 模型就抱着这个工具
    原地打转,直到步数耗尽任务失败。单元测试全过,端到端才暴露出来。

    现在:同一调用重复到阈值,直接判定这个工具在本次任务里没用,摘掉。
    """
    limit = int(_cfg("repeat_disable_threshold", 3))
    if times < limit:
        return None
    if tool_name in tracker.disabled:
        return None
    tracker.disabled.append(tool_name)
    log.warning(f"工具 {tool_name} 被以相同参数重复调用 {times} 次仍无进展,本次任务内不再提供")
    # v1.1.6:给 LLM 只留「该工具不可用」事实;次数等内部细节进 log 与 timeline 的 tool_disabled 事件。
    return (
        f"【系统提示】工具 `{tool_name}` 已多次重复调用仍无新进展,这个工具**不会再提供**给你。"
        f"请基于现有信息作答,并明确告诉用户哪部分数据没能取到。"
    )


def record_outcome(tracker: ToolCallTracker, tool_name: str, ok: bool) -> str | None:
    """记录一次调用的成败,必要时摘掉工具。返回需要注入给模型的提示(没有则 None)。

    只统计**连续**失败:中间成功过一次就清零。偶发抖动不应该让工具被摘掉。
    """
    if ok:
        tracker.failures.pop(tool_name, None)
        return None

    n = tracker.failures.get(tool_name, 0) + 1
    tracker.failures[tool_name] = n
    limit = int(_cfg("max_consecutive_tool_failures", 3))
    if n < limit:
        return None
    if tool_name not in tracker.disabled:
        tracker.disabled.append(tool_name)
        log.warning(f"工具 {tool_name} 连续失败 {n} 次,本次任务内不再提供给模型")
    # v1.1.6:给 LLM 只留「该工具不可用」事实;失败次数等内部细节进 log 与 timeline 的 tool_disabled 事件。
    return (
        f"【系统提示】工具 `{tool_name}` 已判定为当前不可用,本次任务剩余步骤中**不会再提供**这个工具。"
        f"请改用其它工具完成,或基于现有信息作答,并明确告诉用户哪部分数据暂时取不到。"
    )


def usable_tools(tools: list, tracker: ToolCallTracker) -> list:
    """过滤掉已被摘掉的工具。

    摘掉比"继续放着但提示不要用"更有效:只要工具还在 tools 数组里,模型就有可能再选它;
    从列表里拿走则是物理上不可选。
    """
    if not tracker.disabled:
        return tools
    disabled = set(tracker.disabled)
    return [t for t in tools if (getattr(t, "func_name", None) or getattr(t, "name", "")) not in disabled]


def stalled(tracker: ToolCallTracker) -> bool:
    """连续空转到阈值(默认 2 轮):该收尾了。runtime.resilience.stall_rounds 可调,0 = 关闭。"""
    n = int(_cfg("stall_rounds", 2))
    return n > 0 and tracker.stalls >= n
