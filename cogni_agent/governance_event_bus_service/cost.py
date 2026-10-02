"""M7-05:成本聚合——按用户/组织/工具/Agent/api_key 多维度聚合 Token 成本。

计费后置设计(与行业主流一致):
- 交互链路(executor 收尾)只发 usage 事件,携带**原始事实**(模型/token/模型自带 USD),
  不做成本计算、不写任何计费表;
- 真实成本(单价表解析、USD→CNY 换算)统一在本模块订阅方(cost_subscriber)计算落库;
- 预算检查(check_org_daily_budget)读本模块维护的日汇总行(gov_daily_usage),O(1),
  不再实时聚合 gov_llm_usage 大表——最终一致(最多滞后几秒),符合行业「预算拦截不
  追求强实时」的做法。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class LlmUsage(Base):
    __tablename__ = table_name("gov_llm_usage")

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 可空:集团级任务没有所属景区(与 outbox / audit 保持一致)
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # 可空:同一景区可被多账号绑定,成本按账号归因;历史数据无账号信息存 NULL
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost_cny: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class DailyUsage(Base):
    """当日用量日汇总(按 景区 × 日期 一行),预算检查 O(1) 读取的基础。

    空字符串 scenic_id = 集团级(与 LlmUsage 约定一致)。由 cost_subscriber 在每次
    usage 事件落库时原子 UPSERT 累加;预算护栏(check_org_daily_budget)只读本表,
    不再 SUM(gov_llm_usage) 大表。
    """

    __tablename__ = table_name("gov_daily_usage")

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 可空:与 LlmUsage 约定一致(集团级统一存空串,列可空仅作防御,业务写入不产生 NULL)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default="")
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost_cny: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("scenic_id", "usage_date", name="uq_gov_daily_usage_scenic_date"),
    )


async def cost_subscriber(cloudevent: dict) -> None:
    """订阅 LLMUsage 类事件:写成本表 + 维护当日用量日汇总(预算检查 O(1) 读取)。

    计费后置:事件由 executor 在任务收尾发出(见 executor.py::_record_usage),只携带
    原始事实;真实成本在这里统一计算落库,交互进程不触碰任何计费表。
    """
    if cloudevent.get("type") != "com.zhilv.llm.usage":
        return
    data = cloudevent.get("data") or {}
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from cogni_agent.common.db import get_sessionmaker
    # 真实成本在订阅方计算(计费后置):单价配置表刷新/模型配置解析只发生在这里。
    # 交互收尾只读进程内单价缓存做估算展示,不再触发计费相关 DB 查询。
    from cogni_agent.runtime_service.llm.cost import compute_cost_cny

    cost_cny = await compute_cost_cny(
        data.get("model", ""), data.get("prompt_tokens", 0), data.get("completion_tokens", 0)
    )

    scenic_id = cloudevent.get("tenantid") or data.get("scenic_id") or ""
    prompt_tokens = int(data.get("prompt_tokens", 0) or 0)
    completion_tokens = int(data.get("completion_tokens", 0) or 0)
    async with get_sessionmaker()() as db:
        db.add(
            LlmUsage(
                id=f"llu_{uuid.uuid4().hex}",
                scenic_id=scenic_id,
                user_id=data.get("user_id") or None,
                task_id=data.get("task_id"),
                agent_id=data.get("agent_id"),
                model=data.get("model", ""),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_cny=cost_cny,
            )
        )
        # 当日用量日汇总:原子 UPSERT 累加(Postgres ON CONFLICT),并发收尾不丢更新
        today = datetime.now(timezone.utc).date()
        await db.execute(
            pg_insert(DailyUsage)
            .values(
                id=f"dly_{uuid.uuid4().hex}",
                scenic_id=scenic_id,
                usage_date=today,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_cny=cost_cny,
            )
            .on_conflict_do_update(
                constraint="uq_gov_daily_usage_scenic_date",
                set_={
                    "prompt_tokens": DailyUsage.prompt_tokens + prompt_tokens,
                    "completion_tokens": DailyUsage.completion_tokens + completion_tokens,
                    "cost_cny": DailyUsage.cost_cny + cost_cny,
                    "updated_at": func.now(),
                },
            )
        )
        await db.commit()
