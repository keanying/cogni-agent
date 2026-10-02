# -*- coding: utf-8 -*-
"""景区绑定:人 / Quick BI 连接 / 问题 / 前台可见内容,全按景区对齐。

现场报的三件事都在这:
1. 「主题解读」卡片点下去走了通用对话,回一句「请告诉我您需要查询什么」——
   因为那 5 张卡片只是一句文本,没绑任何 Quick BI 能力;
2. 关注问题点回去也一样,发的是文本,对不回问题配置;
3. 改了账号归属 / 连接归属 / 问题归属之后发起就失败 ——
   景区账号看不到集团级连接,而现场的连接全挂在集团级。

这些都不是难修的 bug,难的是它们的报错都指向最后一步(连接不存在 / 没有可用 Agent),
跟真正的原因隔着三层。所以这里把「按景区解析」的每一步都钉一遍。
"""
from __future__ import annotations

import asyncio
import functools

import pytest
import sqlalchemy as sa

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as sc  # noqa: F401  注册全部模型
from cogni_agent.common.db import Base
from cogni_agent.common.errors import ValidationError
from cogni_agent.gateway_service import scenic_binding as binding
from cogni_agent.gateway_service.deps import CurrentUser
from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
from cogni_agent.tool_center_service.quickbi.models import QbiAsset, QbiConnection


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh_db():
    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    yield


SCENIC = "PFTSCA01018088"
OTHER = "PFTSCA01018099"


async def _conn(db, cid="conn-group", scenic=None, default=True):
    db.add(QbiConnection(id=cid, name="Quick BI", owner_user_id="u-admin",
                         visibility="group", scenic_id=scenic, active=True, is_default=default))
    await db.commit()
    return cid


# ── ① 景区归属:谁能指定哪个景区 ───────────────────────────────────────

def test_scenic_account_cannot_ask_for_another_scenic():
    me = CurrentUser(user_id="u1", scenic_id=SCENIC, role="scenic_admin")
    assert binding.effective_scenic(me) == SCENIC
    assert binding.effective_scenic(me, SCENIC) == SCENIC
    with pytest.raises(ValidationError):
        binding.effective_scenic(me, OTHER)

    group = CurrentUser(user_id="u0", scenic_id=None, role="super_admin")
    assert binding.effective_scenic(group, OTHER) == OTHER, "集团账号可以指定任意景区"
    assert binding.effective_scenic(group) is None, "不指定就是集团级"


# ── ② 连接:按景区解析,改了归属也能发起 ────────────────────────────────

@run_async
async def test_connection_resolves_scenic_first_then_group_level():
    async with common_db.get_sessionmaker()() as db:
        await _conn(db, "conn-group", scenic=None)
        # 只有集团级连接时,景区也用它 —— 现场就是这种配置(一套凭证挂集团级)
        assert await binding.quickbi_connection_for(db, SCENIC) == "conn-group"

        await _conn(db, "conn-scenic", scenic=SCENIC, default=False)
        # 有了本景区专属连接就优先用它
        assert await binding.quickbi_connection_for(db, SCENIC) == "conn-scenic"
        # 别的景区不会串到这条上
        assert await binding.quickbi_connection_for(db, OTHER) == "conn-group"


@run_async
async def test_issue_connection_moved_to_another_scenic_falls_back():
    """问题上写死的连接被改绑到别的景区后,按当前景区重挑一条 —— 而不是报「连接不存在」。"""
    async with common_db.get_sessionmaker()() as db:
        await _conn(db, "conn-moved", scenic=OTHER, default=False)
        await _conn(db, "conn-group", scenic=None)
        got = await binding.connection_usable(db, "conn-moved", SCENIC)
        assert got == "conn-group"

        # 连接整条被删掉(id 已不存在)也一样兜得住
        assert await binding.connection_usable(db, "conn-deleted", SCENIC) == "conn-group"
        # 本景区自己的连接当然原样用
        assert await binding.connection_usable(db, "conn-group", None) == "conn-group"


@run_async
async def test_group_level_connection_visible_to_scenic_account():
    """景区账号要能看到集团级连接 —— 这正是「改完账号归属就发起失败」的根因。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _conn(db, "conn-group", scenic=None)

    scenic_actor = qsvc.Actor(user_id="u1", scenic_id=SCENIC, role="scenic_admin")
    assert [c.id for c in await qsvc.list_connections(scenic_actor)] == ["conn-group"]

    # 绑到别的景区的连接仍然看不到(放宽不等于不隔离)
    async with common_db.get_sessionmaker()() as db:
        row = await db.get(QbiConnection, "conn-group")
        row.scenic_id = OTHER
        await db.commit()
    assert await qsvc.list_connections(scenic_actor) == []


# ── ③ 主题解读:绑 Quick BI、按景区生成 ───────────────────────────────

@run_async
async def test_theme_message_is_recognised():
    """老前端发的就是这句原文,后端得认得出来。"""
    assert binding.theme_of_message("【主题解读】客群")["code"] == "passenger_flow"
    assert binding.theme_of_message("【主题解读】概览")["code"] == "overview"
    assert binding.theme_of_message("本月各平台销售额") is None


@run_async
async def test_ensure_theme_issues_binds_quickbi_and_picks_dashboard():
    async with common_db.get_sessionmaker()() as db:
        await _conn(db, "conn-group", scenic=None)
        db.add(QbiAsset(id="a-ticket", connection_id="conn-group", kind="dashboard",
                        name="票务大盘", ref_id="page-ticket", owner_user_id="u-admin",
                        scenic_id=SCENIC))
        await db.commit()

        out = await binding.ensure_theme_issues(db, SCENIC)
        assert len(out["created"]) == 5 and out["connection_id"] == "conn-group"

        rows = {r.metric_id: r for r in (await db.execute(
            sa.select(ScenicAgentIssueDetail))).scalars()}
        ticket = rows["themeReport_ticket"]
        assert ticket.business_class == "quick_bi", "主题解读必须绑 Quick BI,否则点下去走通用对话"
        assert ticket.capability == "report" and ticket.connection_id == "conn-group"
        assert ticket.scenic_id == SCENIC
        assert ticket.target_data_name == "票务大盘", "该主题的仪表板要挑对"
        assert ticket.topic == "主题解读" and ticket.dimension == "票务"

        # 幂等:再跑一次不会重复建
        again = await binding.ensure_theme_issues(db, SCENIC)
        assert again["created"] == [] and len(again["skipped"]) == 5


@run_async
async def test_ensure_theme_issues_without_connection_says_why():
    """没有连接时要说清楚去哪儿配,而不是抛一个看不懂的错。"""
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(ValidationError) as e:
            await binding.ensure_theme_issues(db, SCENIC)
        assert "Quick BI" in str(e.value) and "连接" in str(e.value)


@run_async
async def test_theme_issue_prefers_own_scenic_over_group_level():
    async with common_db.get_sessionmaker()() as db:
        await _conn(db, "conn-group", scenic=None)
        await binding.ensure_theme_issues(db, None)      # 集团级通用
        await binding.ensure_theme_issues(db, SCENIC)    # 本景区专属

        hit = await binding.theme_issue(db, SCENIC, "ticket")
        assert hit.scenic_id == SCENIC, "本景区配过就用自己那条(连接/数据都不一样)"
        assert (await binding.theme_issue(db, OTHER, "ticket")).scenic_id is None


# ── ④ 关注问题:按文本对回问题配置 ────────────────────────────────────

@run_async
async def test_followed_question_text_maps_back_to_issue():
    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(
            issue_id="issue_x1", business_class="quick_bi", connection_id="conn-group",
            scenic_id=SCENIC, title="总评趋势", issue="按评论发布时间看总评论数的分布",
            capability="dataset_qa", active=True))
        await db.commit()

        hit = await binding.match_issue_by_text(db, SCENIC, "按评论发布时间看总评论数的分布")
        assert hit is not None and hit.issue_id == "issue_x1"
        # 标题也认(关注的时候存的可能是标题)
        assert (await binding.match_issue_by_text(db, SCENIC, "总评趋势")).issue_id == "issue_x1"
        # 别的景区对不上(问题是别人景区的)
        assert await binding.match_issue_by_text(db, OTHER, "总评趋势") is None
        # 对不上就返回 None,照常走自由提问
        assert await binding.match_issue_by_text(db, SCENIC, "随便问一句") is None


# ── ⑤ 参数展开:日期让代码算,别让模型推 ──────────────────────────────

def test_expand_params_fills_dates():
    from datetime import date

    row = ScenicAgentIssueDetail(issue_id="i", title="t", issue="{{month}} 各平台销售额",
                                 param_template={"month": "{{month}}"})
    out = binding.expand_params(row, "")
    assert date.today().strftime("%Y-%m") in out and "{{" not in out
