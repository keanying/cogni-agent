"""
工具调用统计(按天汇总)。

**为什么不是在 Tool 行上放一个计数器**:

1. 计数器只能回答「一共调了多少次」,回答不了「最近 7 天趋势」「成功率变化」——
   而看板要的恰恰是后者。
2. 每次调用都 UPDATE 同一行,热门工具上会形成写热点;按天汇总把冲突面从
   「一个工具一行」降到「一个工具一天一行」。
3. 计数器没有历史,清零或回滚之后数据永久丢失。

**不落每次调用的明细** —— 工具调用量会很大,而排查单次调用要看的是任务时间线
(那里有完整参数和返回),统计表只回答「量」和「趋势」。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import Date, Integer, String, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger

log = get_logger("tool_center.stats")


class ToolDailyStat(Base):
    """工具每日调用汇总。(tool_id, stat_date) 为联合主键。"""

    __tablename__ = table_name("tc_tool_daily_stats")

    tool_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, primary_key=True)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    # 存**总和**而不是平均值:平均值没法再聚合(几天的平均值不能简单再平均),总和可以。
    # 用 INTEGER 而不是 BIGINT:结构体检按模型的类型注解判定,Mapped[int] 一律算 integer,
    # 写 BIGINT 会被判成类型漂移而拒绝启动。
    # 容量上也够用:int4 上限约 21 亿毫秒 ≈ 24 天累计执行时长,而这是**按天**的汇总行。
    total_duration_ms: Mapped[int] = mapped_column(Integer, default=0)


async def record_call(tool_id: str, *, scenic_id: str | None, ok: bool, duration_ms: int) -> None:
    """记一次调用。

    **失败不抛异常** —— 统计写失败不该让一次本来成功的工具调用变成失败。
    用 upsert 而不是「先查再更新」:并发调用同一工具时,后者会覆盖前者的读取结果,
    计数会丢。
    """
    if not tool_id:
        return
    try:
        async with get_sessionmaker()() as db:
            await db.execute(pg_insert(ToolDailyStat).values(
                tool_id=tool_id, stat_date=date.today(), scenic_id=scenic_id,
                call_count=1, success_count=1 if ok else 0,
                total_duration_ms=max(int(duration_ms or 0), 0),
            ).on_conflict_do_update(
                index_elements=["tool_id", "stat_date"],
                set_={
                    "call_count": ToolDailyStat.call_count + 1,
                    "success_count": ToolDailyStat.success_count + (1 if ok else 0),
                    "total_duration_ms": (ToolDailyStat.total_duration_ms
                                          + max(int(duration_ms or 0), 0)),
                },
            ))
            await db.commit()
    except Exception:  # noqa: BLE001
        log.debug(f"工具调用统计写入失败(不影响主流程):tool={tool_id}", exc_info=True)


async def totals_by_tool(db: AsyncSession, *, days: int = 30,
                         scenic_id: str | None = None) -> dict[str, dict]:
    """各工具近 N 天汇总。工具列表「按调用次数排序」与耗时预估都用它。"""
    since = date.today() - timedelta(days=days)
    q = (select(ToolDailyStat.tool_id,
                func.sum(ToolDailyStat.call_count),
                func.sum(ToolDailyStat.success_count),
                func.sum(ToolDailyStat.total_duration_ms))
         .where(ToolDailyStat.stat_date >= since)
         .group_by(ToolDailyStat.tool_id))
    if scenic_id:
        q = q.where(ToolDailyStat.scenic_id == scenic_id)

    out: dict[str, dict] = {}
    for tool_id, calls, oks, ms in (await db.execute(q)).all():
        calls = int(calls or 0)
        out[tool_id] = {
            "call_count": calls,
            "success_count": int(oks or 0),
            # 除零保护:没有调用记录时不该出现 NaN
            "success_rate": round((oks or 0) / calls, 4) if calls else None,
            "avg_duration_ms": int((ms or 0) / calls) if calls else None,
        }
    return out


async def daily_trend(db: AsyncSession, *, days: int = 7,
                      scenic_id: str | None = None) -> list[dict]:
    """每日调用趋势。

    **补齐没有数据的日期** —— 直接按数据分组的话,没有调用的那天会整天消失,
    折线图上表现为把两个不相邻的日期连起来,看着像「中间一直有量」。
    """
    since = date.today() - timedelta(days=days - 1)
    q = (select(ToolDailyStat.stat_date,
                func.sum(ToolDailyStat.call_count),
                func.sum(ToolDailyStat.success_count))
         .where(ToolDailyStat.stat_date >= since)
         .group_by(ToolDailyStat.stat_date))
    if scenic_id:
        q = q.where(ToolDailyStat.scenic_id == scenic_id)

    got = {d: (int(c or 0), int(s or 0)) for d, c, s in (await db.execute(q)).all()}
    out = []
    for i in range(days):
        d = since + timedelta(days=i)
        calls, oks = got.get(d, (0, 0))
        out.append({"date": d.isoformat(), "calls": calls,
                    "success": oks, "failed": calls - oks})
    return out
