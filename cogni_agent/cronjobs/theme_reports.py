# -*- coding: utf-8 -*-
"""主题解读报告的预热。

**为什么必须预热**:主题解读是报告类的重活 —— 小Q 生成一份报告通常要 3~10 分钟。
而 APP 那五张主题卡片是用户滑到就想看的东西:让他滑过去之后干等十分钟,
等于这个功能不存在。所以报告不能等人点了才跑,要提前跑好放在那儿,点开直出。

做法:按「景区 × 主题」扫一遍,近 `_FRESH_HOURS` 小时内已经成功跑过的跳过,
其余的挨个跑。跑的就是 APP 点下去会走的那条链路(同一条问题、同一个连接、
同一份目标数据),所以跑出来的结果 APP 能原样回放 —— 见 scenic_binding.recent_success_run。

几条刻意的约束:

- **一轮只跑有限条**(`_MAX_PER_RUN`):报告很贵,一个景区五条、十个景区就是五十次
  几分钟的调用。宁可分几轮跑完,也不要一轮把额度打空、把上游打挂。
- **串行**:并发跑报告对 Quick BI 侧不友好,而且失败时分不清是谁把谁挤掉的。
- **失败不重试**:这一轮失败下一轮自然会再轮到它(近 N 小时内没有成功记录)。
  在这里写重试只会让一个坏掉的连接被连着打三次。
- **幂等**:漏跑几轮无害,下一轮兜底 —— 与 prewarm 同一套口径。
"""
from __future__ import annotations

import asyncio

from datetime import timedelta as _timedelta

import sqlalchemy as sa

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.db import utcnow as _utcnow
from cogni_agent.common.logging import get_logger

log = get_logger("cronjobs.theme_reports")

# 多久之内跑过就算新鲜,不必重跑。与前台回放窗口(recent_success_run 的 12 小时)
# 取同一个值 —— 两边不一致的话,会出现「预热判定为新鲜、前台判定为过期又现跑一遍」。
_FRESH_HOURS = 12
# 一轮最多跑几条。报告一条几分钟,这个数乘以单条耗时就是这一轮的占用时长。
_MAX_PER_RUN = 6
# 单条的上限:再慢也不能把整轮拖住
_PER_REPORT_TIMEOUT = 15 * 60
# 多久之内问过就算「这个景区在用主题解读」,值得为它单独预热一份。
# 取 7 天:一周没人点过的景区,预热了也是白跑(报告很贵)。
_ASKED_WITHIN_HOURS = 24 * 7


async def _scenics_to_warm(db) -> list[str | None]:
    """给哪些景区预热主题解读。

    三个来源并起来:

    - **自己配了主题问题的景区** —— 它显然要用;
    - **集团级那一档(None)** —— 集团自己看的那一份;
    - **最近真的问过主题解读的景区** —— 关键的一档。集团级的主题问题是**模板**,
      哪个景区都能用(theme_issue 找不到本景区专属的就回退到它),而每个景区读到的
      是各自的数。报告是按景区跑、按景区回放的,所以必须**按景区各预热一份** ——
      只跑集团级那一份的话,景区用户永远命中不了,每次点都现跑十分钟。
      (以前不会命中不了,是因为回放没比景区 —— 那是把 A 景区的报告给了 B 景区。)

      只取「最近问过的」而不是「所有在用景区」:报告很贵,按全量景区铺开就是几十次
      没人会看的调用,额度会在没人注意的情况下被烧光。某个景区第一次点的时候现跑一次,
      之后就进了这个名单。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    own = set((await db.execute(
        sa.select(ScenicAgentIssueDetail.scenic_id)
        .where(ScenicAgentIssueDetail.topic_id == "themeReport",
               ScenicAgentIssueDetail.active.is_(True))
        .distinct())).scalars().all())
    theme_ids = set((await db.execute(
        sa.select(ScenicAgentIssueDetail.issue_id)
        .where(ScenicAgentIssueDetail.topic_id == "themeReport"))).scalars().all())
    asked: set = set()
    if theme_ids:
        since = _utcnow() - _timedelta(hours=_ASKED_WITHIN_HOURS)
        asked = set((await db.execute(
            sa.select(QbiRun.scenic_id)
            .where(QbiRun.fixed_question_id.in_(theme_ids), QbiRun.created_at >= since)
            .distinct())).scalars().all())
    return list(own | asked | {None})


async def _run_one(issue_row, scenic_id: str | None = None) -> bool:
    """给某个景区跑一条主题报告。返回是否成功。

    scenic_id 不传时沿用问题行自己的景区(集团级那份就是 None)。
    传了就按那个景区跑 —— 集团级的问题行是模板,景区用的是同一行、不同的数。
    """
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    scenic_id = scenic_id if scenic_id is not None else issue_row.scenic_id
    async with get_sessionmaker()() as db:
        conn_id = await binding.connection_usable(db, issue_row.connection_id, scenic_id)
    if not conn_id:
        log.info(f"主题解读预热跳过 {issue_row.issue_id}:景区 {scenic_id} 没有可用连接")
        return False

    # 用问题所有者的身份跑(集团角色,不受景区限制)——预热是系统行为,
    # 不该因为某个具体账号的可见范围而跑不动
    actor = qsvc.Actor(user_id=issue_row.owner_user_id or "cron",
                       scenic_id=None, role="super_admin", source="console")
    status = "failed"
    try:
        async def _consume():
            nonlocal status
            async for e in qsvc.run_stream(
                actor, connection_id=conn_id, capability=issue_row.capability or "report",
                question=issue_row.issue or issue_row.title or "",
                target=dict(issue_row.target or {}), mode="fixed",
                fixed_question_id=issue_row.issue_id,
                scenic_id=scenic_id, session_id=None,
                # 预热自己不能吃缓存,否则它会读到上一次的结果就当跑完了
                use_cache=False,
            ):
                if e.get("type") == "done":
                    status = e.get("status") or "failed"
        await asyncio.wait_for(_consume(), timeout=_PER_REPORT_TIMEOUT)
    except asyncio.TimeoutError:
        # 报告可能还在远端生成,报告收尾扫描(quickbi_report_sweep)会把它收回来
        log.warning(f"主题解读预热超时 {issue_row.issue_id}(报告可能仍在生成,由收尾扫描接手)")
        return False
    except Exception as exc:                      # noqa: BLE001 - 一条失败不该带倒整轮
        log.warning(f"主题解读预热失败 {issue_row.issue_id} / 景区 {scenic_id}:"
                    f"{type(exc).__name__}: {exc}")
        return False
    return status in ("success", "running")


async def run(dry_run: bool = False) -> dict:
    """扫一遍「景区 × 主题」,把不新鲜的报告跑出来。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    planned: list[tuple] = []
    async with get_sessionmaker()() as db:
        for scenic_id in await _scenics_to_warm(db):
            for theme in binding.THEMES:
                # 用**这个景区实际会读到的那一行**(本景区专属的,没有就是集团级模板),
                # 并按这个景区跑 —— 回放也是按景区找的,两边必须是同一个口径
                row = await binding.theme_issue(db, scenic_id, theme["code"])
                if row is None:
                    continue
                # strict_target:绑的看板换过之后,旧报告答的是另一块看板,不算「跑过了」——
                # 不加这一条,重绑之后预热会认为已经有结果,于是永远不重跑,
                # 前台也就永远看不到按新看板写的那份。
                fresh = await binding.recent_success_run(
                    db, row.issue_id, scenic_id, within_hours=_FRESH_HOURS,
                    expect_target_data_id=row.target_data_id, strict_target=True)
                if fresh is None:
                    planned.append((row, scenic_id))

    total = len(planned)
    batch = planned[:_MAX_PER_RUN]
    if dry_run:
        log.info(f"主题解读预热(dry-run):待跑 {total} 条,本轮会跑 {len(batch)} 条")
        return {"planned": total, "ran": 0, "ok": 0, "dry_run": True}

    ok = 0
    for row, scenic_id in batch:
        if await _run_one(row, scenic_id):
            ok += 1
    log.info(f"主题解读预热:待跑 {total} 条,本轮跑了 {len(batch)} 条,成功 {ok} 条")
    return {"planned": total, "ran": len(batch), "ok": ok, "dry_run": False}
