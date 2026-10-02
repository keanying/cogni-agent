"""M4-10:任务级 + 组织级双层 Token 预算校验。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import GuardrailTripped
from cogni_agent.common.logging import get_logger
from cogni_agent.governance_event_bus_service.cost import DailyUsage
from cogni_agent.governance_event_bus_service.settings import BudgetThreshold

log = get_logger("runtime_service.llm.budget")


async def check_org_daily_budget(scenic_id: str | None, tokens_about_to_use: int) -> None:
    """查当日用量日汇总(gov_daily_usage)按 scenic_id 取当日消耗,与「系统设置·模型与
    预算」里登记的预算护栏比较。超过硬预算(daily_token_budget)直接熔断(GuardrailTripped,
    任务判 FAILED);超过告警阈值(alert_threshold_pct)只记录告警日志,不阻断——对应功能
    清单"预算护栏(集团级/景区级预算上限,阈值告警)"里"上限"与"告警"两种不同的严重程度。
    没有为该 scope 登记预算配置时(BudgetThreshold 表为空)不做任何限制,不阻断已有部署。

    计费后置:日汇总由 cost_subscriber 在 usage 事件落库时原子累加,这里只 O(1) 读一行,
    不再实时聚合 gov_llm_usage 大表(表越大聚合越慢,且挡在每次对话的第一环)。允许
    最终一致(最多滞后几秒),符合行业「预算拦截不追求强实时」的做法。
    """
    async with get_sessionmaker()() as db:
        stmt = select(BudgetThreshold).where(
            (BudgetThreshold.scope == "group") if scenic_id is None else
            ((BudgetThreshold.scope == "scenic") & (BudgetThreshold.scenic_id == scenic_id))
        )
        threshold = (await db.execute(stmt)).scalars().first()
        if threshold is None:
            return

        today = datetime.now(timezone.utc).date()
        daily_stmt = select(DailyUsage).where(
            DailyUsage.scenic_id == (scenic_id or ""), DailyUsage.usage_date == today
        )
        daily = (await db.execute(daily_stmt)).scalars().first()
        used_today = (daily.prompt_tokens + daily.completion_tokens) if daily else 0

    projected = used_today + tokens_about_to_use
    pct = projected / threshold.daily_token_budget if threshold.daily_token_budget else 0
    if projected > threshold.daily_token_budget:
        raise GuardrailTripped(
            f"{'集团' if scenic_id is None else f'景区 {scenic_id}'} 当日 Token 用量({projected})已超过预算上限"
            f"({threshold.daily_token_budget}),任务熔断"
        )
    if pct * 100 >= threshold.alert_threshold_pct:
        log.warning(
            f"{'集团' if scenic_id is None else f'景区 {scenic_id}'} 当日 Token 用量已达预算的 {pct:.0%}"
            f"(阈值 {threshold.alert_threshold_pct}%),接近上限 {threshold.daily_token_budget}"
        )
