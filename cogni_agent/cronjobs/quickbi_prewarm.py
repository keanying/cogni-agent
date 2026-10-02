# -*- coding: utf-8 -*-
"""Quick BI 固定问题的「加速」(预热)定时扫描。

**这是什么**:运营在「工具中心 · Quick BI · 固定问题」里给一条问题打开「加速」之后,
后台每隔「刷新时间」把它提前跑好;之后前台或 APP 再问**同一个景区的同一条问题**,
直接回放已经跑好的那一份,不必现跑。见 service.cached_run / _replay_events。

**和 cronjobs/prewarm.py 不是一回事**:那个预热的是 APP 问题库(app_question_bank)里
走智能体任务链的问题,按业务日一天一条;这里预热的是 Quick BI 固定问题
(scenic_agent_issue_detail),按分钟级窗口滚动刷新。两张表、两套口径,
只是都叫「预热」—— 改动时别把口径互相搬过去。

**没有缓存表**:跑出来的结果本来就整份落在 tc_qbi_runs 里,「缓存」= 这条问题在这个景区
最近一次成功的运行且还在「存储时间」内。多存一份就多一处会不同步,
而缓存与源数据不同步的表现是「看到的是旧数,还没人察觉」。

几条刻意的约束(与 theme_reports 同一套口径):

- **串行 + 每轮有上限**:问数一条几秒到几十秒,报告更久。宁可分几轮跑完,
  也不要一轮把 Quick BI 的额度打空。
- **失败不重试**:这一轮失败,下一轮自然又会轮到它(还是没有新鲜的成功记录)。
  在这里写重试,只会让一条坏掉的连接被连着打三次。
- **幂等**:漏跑几轮无害,下一轮兜底。
- **报告类不等**:report 能力创建即返回,收尾交给 quickbi_report_sweep。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import sqlalchemy as sa

from cogni_agent.common.db import get_sessionmaker, utcnow
from cogni_agent.common.logging import get_logger

log = get_logger("cronjobs.quickbi_prewarm")

# 一轮最多跑几条。乘以单条耗时就是这一轮的占用时长;
# tick 每 5 分钟一次,留足余量不让上一轮压到下一轮(registry 有防重叠锁兜底)。
_MAX_PER_RUN = 20
# 单条的上限:再慢也不能把整轮拖住
_PER_RUN_TIMEOUT = 10 * 60


async def _plan() -> list[tuple[object, str | None]]:
    """这一轮该跑哪些 (问题, 景区)。

    景区从哪来,是这里唯一需要想清楚的事:

    - 问题绑了景区 → 就给那个景区预热,只有一种组合;
    - 问题是集团级(scenic_id 为空)→ 它是个**模板**,哪个景区都能问,而每个景区问到的
      是各自的数。要是按「所有在用景区」铺开,一条集团级问题乘以几十个景区就是几十次调用,
      其中绝大多数没有人会去看。所以只给**最近真的有人问过**的那些景区预热
      —— 预热的意义是「有人会来看」,没人问过的组合预热了也是白跑。
      某个景区第一次问的时候会现跑一次(慢那一次),之后就进了这个名单。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail as D
    from cogni_agent.tool_center_service.quickbi.models import QbiRun
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    out: list[tuple[object, str | None]] = []
    async with get_sessionmaker()() as db:
        rows = (await db.execute(
            sa.select(D).where(D.prewarm_enabled.is_(True),
                               D.active.is_(True),
                               D.business_class == "quick_bi")
            .order_by(D.sort_order.asc(), D.issue_id.asc()))).scalars().all()
        for row in rows:
            enabled, interval, ttl = qsvc.prewarm_settings(row)
            if not enabled:
                continue
            ids = qsvc._fixed_ids(row)
            if not ids:
                continue
            if row.scenic_id:
                scenics: list[str | None] = [row.scenic_id]
            else:
                # 集团级:只认最近问过的景区。窗口取存储时间的 24 倍(默认 = 一天),
                # 保证「昨天有人看」的组合今天还在名单里,而一个月前问过一次的不再占额度。
                seen_since = utcnow() - timedelta(minutes=max(1, ttl) * 24)
                scenics = list((await db.execute(
                    sa.select(QbiRun.scenic_id)
                    .where(QbiRun.fixed_question_id.in_(ids), QbiRun.created_at >= seen_since)
                    .distinct())).scalars().all())
            for scenic_id in scenics:
                # 刷新时间之内已经成功跑过 → 这一轮不用动它
                fresh = await _recent_success(db, ids, scenic_id, interval)
                if fresh is None:
                    out.append((row, scenic_id))
    return out


async def _recent_success(db, ids: list[str], scenic_id: str | None, within_minutes: int):
    """这组 (问题, 景区) 在 within_minutes 分钟内有没有成功记录。"""
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    since = utcnow() - timedelta(minutes=max(1, within_minutes))
    stmt = sa.select(QbiRun.id).where(QbiRun.fixed_question_id.in_(ids),
                                      QbiRun.status == "success",
                                      QbiRun.created_at >= since)
    stmt = stmt.where(QbiRun.scenic_id == scenic_id) if scenic_id \
        else stmt.where(QbiRun.scenic_id.is_(None))
    return (await db.execute(stmt.limit(1))).scalars().first()


async def _run_one(row, scenic_id: str | None) -> bool:
    """跑一条,把结果刷成新缓存。返回是否成功。"""
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with get_sessionmaker()() as db:
        conn_id = await binding.connection_usable(db, row.connection_id, scenic_id)
    if not conn_id:
        # 连接被删/停用/改绑了别的景区。这里只记一条日志就走 —— 预热不是报警渠道,
        # 真正该看见这件事的是运营打开固定问题列表时的「连接不可用」标记。
        log.info(f"加速跳过 {row.issue_id}:景区 {scenic_id or '(集团级)'} 没有可用连接")
        return False

    # 用系统身份跑:预热是系统行为,不该因为某个具体账号的可见范围而跑不动
    actor = qsvc.Actor(user_id=row.owner_user_id or "cron", scenic_id=None,
                       role="super_admin", source="console")
    status = "failed"
    try:
        async def _consume():
            nonlocal status
            async for e in qsvc.run_stream(
                actor, connection_id=conn_id, capability=row.capability,
                question=row.issue or row.title or "", target=dict(row.target or {}),
                mode="fixed", locale=row.locale, fixed_question_id=row.issue_id,
                scenic_id=scenic_id,
                # 报告类创建即返回,收尾交给 quickbi_report_sweep
                report_async=True,
                # 关键:预热自己不能吃缓存,否则它会读到上一次的结果就当跑完了,
                # 缓存从此冻在那儿再也不更新 —— 而且一路成功,不会有任何报错
                use_cache=False,
            ):
                if e.get("type") == "done":
                    status = e.get("status") or "failed"
        await asyncio.wait_for(_consume(), timeout=_PER_RUN_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning(f"加速超时 {row.issue_id} / 景区 {scenic_id or '(集团级)'}(下一轮再试)")
        return False
    except Exception as exc:                      # noqa: BLE001 - 一条失败不该带倒整轮
        log.warning(f"加速失败 {row.issue_id} / 景区 {scenic_id or '(集团级)'}:"
                    f"{type(exc).__name__}: {exc}")
        return False
    return status in ("success", "running")


async def run(dry_run: bool = False) -> dict:
    """扫一遍开了加速的固定问题,把不新鲜的跑出来。"""
    planned = await _plan()
    total = len(planned)
    batch = planned[:_MAX_PER_RUN]
    if dry_run:
        return {"planned": total, "would_run": len(batch), "ran": 0, "ok": 0}
    ok = 0
    for row, scenic_id in batch:
        if await _run_one(row, scenic_id):
            ok += 1
    if total:
        log.info(f"Quick BI 加速一轮:待跑 {total},本轮 {len(batch)},成功 {ok}")
    return {"planned": total, "would_run": len(batch), "ran": len(batch), "ok": ok}


if __name__ == "__main__":                        # pragma: no cover - 手工触发用
    import argparse

    ap = argparse.ArgumentParser(description="Quick BI 固定问题加速(预热)扫描")
    ap.add_argument("--dry-run", action="store_true", help="只算这一轮会跑哪些,不真跑")
    args = ap.parse_args()
    print(asyncio.run(run(dry_run=args.dry_run)))
