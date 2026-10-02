"""
执行耗时预估:「预计需要 15-30 秒…」。

**核心问题是没有历史数据时怎么办。** 直接拍一个数字会很离谱,而
**离谱的预估比不给预估更糟** —— 用户按 5 秒等、结果跑了 40 秒,
下次就再也不信这个数字了。

处理办法是分级降级 + 明说置信度:

| 情况 | 文案 |
|---|---|
| 计划里的工具都有历史数据 | 「预计需要 20-35 秒」 |
| 部分有 | 「预计需要 15-50 秒」 |
| 都没有 | 「预计需要 15-50 秒**(首次运行,仅供参考)**」 |
"""
from __future__ import annotations

from dataclasses import dataclass

from cogni_agent.runtime_service.engine import planner

# 没有历史数据时各类步骤的默认耗时(毫秒)。量级判断,不是精确值 ——
# 只用于兜底,有实测一律优先用实测。
_DEFAULTS = {
    planner.KIND_TOOL: 2000,
    planner.KIND_SCRIPT: 1200,
    planner.KIND_KNOWLEDGE: 1500,
    planner.KIND_ANALYZE: 6000,     # 一轮模型推理,最不确定的一项
    planner.KIND_COMPOSE: 5000,
}
# 每步的模型往返开销:即使纯工具步骤,模型也要先决定调它、再读结果
_PER_STEP_LLM_MS = 2500


@dataclass
class Estimate:
    low_seconds: int
    high_seconds: int
    confidence: str          # measured / partial / default
    covered: int
    total_steps: int

    @property
    def text(self) -> str:
        base = f"预计需要 {self.low_seconds}-{self.high_seconds} 秒"
        # 没有任何历史数据时必须说明,否则用户会把它当成靠谱的承诺
        return base + "(首次运行,仅供参考)" if self.confidence == "default" else base

    def as_dict(self) -> dict:
        return {"low_seconds": self.low_seconds, "high_seconds": self.high_seconds,
                "confidence": self.confidence, "covered_steps": self.covered,
                "total_steps": self.total_steps, "text": self.text}


def _round_up_to_5(x: float) -> int:
    """向上取整到 5 秒。报「17-31 秒」这种精确度是假的,反而显得不可信。"""
    return max(5, int(-(-x // 5) * 5))


def estimate_plan(plan: planner.ExecutionPlan, *,
                  tool_stats_by_name: dict[str, dict]) -> Estimate | None:
    """按计划估算总耗时。

    不碰数据库 —— 统计由调用方取好传进来,便于测试也便于复用。
    """
    steps = plan.steps or []
    if not steps:
        return None

    total_ms = 0.0
    covered = 0
    for s in steps:
        measured = (tool_stats_by_name.get(s.tool_name) or {}).get("avg_duration_ms") \
            if s.tool_name else None
        if measured:
            total_ms += float(measured)
            covered += 1
        else:
            total_ms += _DEFAULTS.get(s.kind, 3000)
        total_ms += _PER_STEP_LLM_MS

    tool_steps = sum(1 for s in steps if s.tool_name)
    if tool_steps == 0 or covered == 0:
        confidence = "default"
    elif covered == tool_steps:
        confidence = "measured"
    else:
        confidence = "partial"

    # 区间按不确定性放宽。报一个自己都不信的窄区间,不如老实给个宽的。
    lo_f, hi_f = (0.75, 1.4) if confidence == "measured" else (0.6, 2.0)
    sec = total_ms / 1000.0
    est = Estimate(low_seconds=_round_up_to_5(sec * lo_f),
                   high_seconds=_round_up_to_5(sec * hi_f),
                   confidence=confidence, covered=covered, total_steps=len(steps))
    # 区间退化成一个点时抬一档 —— 「预计 5-5 秒」很怪
    if est.high_seconds <= est.low_seconds:
        est.high_seconds = est.low_seconds + 5
    return est
