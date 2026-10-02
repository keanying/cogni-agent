# -*- coding: utf-8 -*-
"""主题解读的提示词、预热与资产归属。

三件事各自钉一条底线:

- 提示词是报告的成败所在(问题原文只有「【主题解读】客群」七个字),
  所以它必须**能改**,而且改完不能被「全部重绑」悄悄冲掉 ——
  冲掉的话运营下次看到报告变了味,也不会想到是点了那个按钮;
- 报告一条要 3~10 分钟,等人点了才跑等于这个功能不存在,所以要能预热、
  预热过的要能直出,而且预热本身要幂等(漏跑几轮无害,不能重复打爆额度);
- 资产归属要能改。没有这个能力,改归属只能「删了重登记」,而删掉的那一刻
  所有指着这个 asset_id 的问题全部失效,报「资产不存在」——
  运营根本不会把它和刚才那次删除联系起来。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base
from cogni_agent.common.time import now_cst

SCENIC = "PFTSCA01018088"
OTHER = "PFTSCB02000052"


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


def _actor(role="super_admin", scenic=None, user_id="u-1"):
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    return qsvc.Actor(user_id=user_id, scenic_id=scenic, role=role, source="console")


async def _seed_conn_and_asset(db, *, asset_scenic=None):
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset, QbiConnection

    db.add(QbiConnection(id="c1", name="主连接", owner_user_id="u-1",
                         visibility="group", scenic_id=None, active=True, is_default=True))
    db.add(QbiAsset(id="a1", connection_id="c1", kind="dashboard", ref_id="page-1",
                    name="九龙潭名胜风景区", owner_user_id="u-1", scenic_id=asset_scenic))
    await db.commit()


# ══════════════════════════════════════════════════════════════════════
# 一、提示词:基于看板、可编辑、重绑不被冲掉
# ══════════════════════════════════════════════════════════════════════

def test_theme_prompts_are_grounded_in_the_dashboard():
    """报告打在绑定的那块仪表板上,提示词就得让模型「按图说话」。

    不写这一句的话,模型会把看板当背景板,自己编一套通用结论出来 ——
    看着很完整,但和图上的数对不上,这种错运营一眼看不出来。
    """
    from cogni_agent.gateway_service.scenic_binding import THEMES

    for t in THEMES:
        ins = t["instruction"]
        assert "看板" in ins, t["code"]
        assert "不要推测" in ins or "看板未覆盖" in ins, f"{t['code']} 缺少「不许编」的约束"
        assert "同比" in ins or "环比" in ins, f"{t['code']} 没要求给出变化方向"
        assert len(ins) > 200, f"{t['code']} 的口径太短,模型只能靠猜"
    # 五个主题各有各的侧重,不能是同一段话复制五遍
    assert len({t["instruction"] for t in THEMES}) == len(THEMES)


@run_async
async def test_rebind_keeps_an_edited_prompt():
    """「全部重绑」重绑的是连接与数据,不该把运营改过的文案冲掉。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        await binding.ensure_theme_issues(db, SCENIC, "c1")

        row = await binding.theme_issue(db, SCENIC, "ticket")
        default_prompt = row.model_instruction
        row.model_instruction = "运营自己写的口径:只看周末,异常阈值 ±15%"
        row.title = "票务周末专题"
        await db.commit()

        await binding.ensure_theme_issues(db, SCENIC, "c1", overwrite=True)
        again = await binding.theme_issue(db, SCENIC, "ticket")
        assert again.model_instruction == "运营自己写的口径:只看周末,异常阈值 ±15%"
        assert again.title == "票务周末专题"
        assert again.model_instruction != default_prompt
        # 连接与目标数据仍然被重绑(这才是「重绑」该做的事)
        assert again.connection_id == "c1"


@run_async
async def test_first_generation_writes_the_default_prompt():
    """第一次生成时默认口径要写进去 —— 留空的话报告只有七个字可依。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        await binding.ensure_theme_issues(db, SCENIC, "c1")
        for t in binding.THEMES:
            row = await binding.theme_issue(db, SCENIC, t["code"])
            assert (row.model_instruction or "").strip(), t["code"]


@run_async
async def test_theme_labels_are_selectable_in_the_picker():
    """三级选择器要挑得到「主题解读」——问题行上的三级是写死的,
    标签表里没有对应行的话,运营想把自己写的解读挂到主题板块下也挂不上。"""
    from sqlalchemy import select

    from cogni_agent.common import schema as sc
    from cogni_agent.gateway_service.issue_models import PublicProblemDimensionLabel
    from cogni_agent.gateway_service.scenic_binding import THEMES

    # 只跑这一条迁移的 SQL:整条链路要建 pgvector 的表,而沙箱里没装那个扩展
    mig = next(m for m in sc.MIGRATIONS if m.id == "0081_theme_report_dimension_labels")
    async with common_db.get_engine().begin() as conn:
        for stmt in sc._split_statements(mig.sql):
            await conn.exec_driver_sql(stmt)
    async with common_db.get_sessionmaker()() as db:
        rows = (await db.execute(select(PublicProblemDimensionLabel).where(
            PublicProblemDimensionLabel.topic_id == "themeReport"))).scalars().all()
        got = {r.metric_id for r in rows}
        assert got == {f"themeReport_{t['code']}" for t in THEMES}, got
        assert all(r.topic == "主题解读" and r.metric == "解读报告" for r in rows)


def test_every_theme_domain_is_offered_in_the_domain_dropdown():
    """下面配了「OTA直销」主题,上面的数据域下拉里却只有「OTA」,筛选时就对不上。"""
    from cogni_agent.gateway_service.scenic_binding import THEMES
    from cogni_agent.tool_center_service.quickbi.service import PRESET_DOMAINS

    assert "全域" in PRESET_DOMAINS, "概览类问题不属于任何单一域,要有「全域」可选"
    for t in THEMES:
        assert t["domain"] in PRESET_DOMAINS, f"主题 {t['code']} 的域 {t['domain']} 不在下拉里"


# ══════════════════════════════════════════════════════════════════════
# 二、预热
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_prewarm_plans_only_stale_themes():
    """近 12 小时跑过的跳过 —— 否则每轮都把五条重跑一遍,额度很快就见底。"""
    from cogni_agent.cronjobs import theme_reports
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        await binding.ensure_theme_issues(db, SCENIC, "c1")
        fresh = await binding.theme_issue(db, SCENIC, "overview")
        # scenic_id 必须跟着问题走:预热与回放都是按 (问题, 景区) 找的,
        # 少了它这条记录属于「集团级」,景区那一档照样会被判成没跑过。
        # target 同样必须跟着问题走:「跑过了」现在的完整含义是
        # 「**按当前绑定的这块看板**跑过了」—— 换了看板,旧报告答的是另一块,
        # 不能算数(见 test_stale_when_the_board_changed)。
        db.add(QbiRun(connection_id="c1", capability="report", mode="fixed",
                      question="x", status="success", source="console", user_id="u-1",
                      scenic_id=SCENIC, target={"page_id": fresh.target_data_id},
                      fixed_question_id=fresh.issue_id, answer="上一次的报告"))
        await db.commit()

    out = await theme_reports.run(dry_run=True)
    assert out["planned"] == len(binding.THEMES) - 1, out
    assert out["ran"] == 0 and out["dry_run"] is True


@run_async
async def test_stale_when_the_board_changed():
    """换了绑定的看板之后,改绑**之前**跑的那份不算「跑过了」。

    不认这一条的话:主题解读绑错看板修完,预热会认为五条都已经有结果,于是永远不重跑,
    前台也就永远看不到按新看板写的那份 —— 代码改对了、现象一点没变,
    而没人会想到去怀疑缓存。
    """
    from cogni_agent.cronjobs import theme_reports
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        await binding.ensure_theme_issues(db, SCENIC, "c1")
        row = await binding.theme_issue(db, SCENIC, "overview")
        db.add(QbiRun(connection_id="c1", capability="report", mode="fixed",
                      question="x", status="success", source="console", user_id="u-1",
                      scenic_id=SCENIC,
                      target={"page_id": "另一块看板的 pageId"},   # 改绑前跑的
                      fixed_question_id=row.issue_id, answer="按另一块看板写的旧报告"))
        await db.commit()

    out = await theme_reports.run(dry_run=True)
    assert out["planned"] == len(binding.THEMES), out


@run_async
async def test_prewarmed_report_is_replayed_to_the_app_card():
    """预热跑好之后,APP 滑到主题卡片要直出 —— 这正是预热要消灭的那十分钟。"""
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        await binding.ensure_theme_issues(db, SCENIC, "c1")
        row = await binding.theme_issue(db, SCENIC, "passenger_flow")
        db.add(QbiRun(connection_id="c1", capability="report", mode="fixed",
                      question="【主题解读】客群", status="success", source="console",
                      user_id="u-1", scenic_id=SCENIC, fixed_question_id=row.issue_id,
                      answer="## 客群解读\n本期以 25-34 岁为主(38%),环比上升 3 个百分点。"))
        await db.commit()

        cached = await binding.recent_success_run(db, row.issue_id, SCENIC)
        assert cached is not None and "25-34" in cached["answer"]


@run_async
async def test_group_level_theme_is_prewarmed_per_asking_scenic():
    """回放按景区找之后,集团级主题**必须按景区各预热一份**。

    集团级的主题问题是模板:哪个景区都用它,但每个景区读到的是各自的数。
    只跑集团级那一份的话,景区用户永远命中不了,每次点都要等十分钟 ——
    而这正是预热要消灭的那十分钟。(以前不会命中不了,是因为回放没比景区,
    那是把 A 景区的报告给了 B 景区。)

    只给**最近真的问过**的景区预热:报告很贵,按全量景区铺开就是几十次没人会看的调用。
    """
    from cogni_agent.cronjobs import theme_reports
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db)
        row = ScenicAgentIssueDetail(
            title="【主题解读】客群", issue="【主题解读】客群", business_class="quick_bi",
            capability="report", connection_id="c1", scenic_id=None, active=True,
            topic="主题解读", topic_id="themeReport", dimension="客群",
            dimension_id="theme_passenger_flow", metric="解读报告",
            metric_id="themeReport_passenger_flow")
        db.add(row)
        await db.commit()
        # OTHER 景区问过一次(所以它在用主题解读),SCENIC 没问过
        db.add(QbiRun(connection_id="c1", capability="report", mode="fixed",
                      question="【主题解读】客群", status="failed", source="console",
                      user_id="u-b", scenic_id=OTHER, fixed_question_id=row.issue_id))
        await db.commit()
        scenics = await theme_reports._scenics_to_warm(db)

    assert OTHER in scenics, "问过的景区要单独预热一份,否则它永远命中不了"
    assert None in scenics, "集团自己看的那一份还要跑"
    assert SCENIC not in scenics, "没人问过的景区不该占额度"


@run_async
async def test_theme_replay_never_crosses_scenics():
    """**集团级主题解读是模板,不是共享结果。**

    一条集团级的【主题解读】客群对所有景区生效(theme_issue 找不到本景区专属的就回退到它),
    于是「这条问题最近一次成功的运行」这句话在没有景区限定时,取到的可能是**别的景区**
    那一次 —— A 景区的客群报告会原样出现在 B 景区的卡片上。

    而这件事不会报错:卡片正常渲染、正文结构完整、数字看着都对,只是那是别人家的数。
    没人会去核对,所以只能靠这条钉住。
    """
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(
            title="【主题解读】客群", issue="【主题解读】客群", business_class="quick_bi",
            capability="report", connection_id="c1", scenic_id=None, active=True,
            topic="主题解读", topic_id="themeReport", dimension="客群",
            dimension_id="theme_passenger_flow", metric="解读报告",
            metric_id="themeReport_passenger_flow"))
        await db.commit()
        row = await binding.theme_issue(db, OTHER, "passenger_flow")
        assert row is not None and row.scenic_id is None, "应当回退到集团级那条"

        # A 景区跑过一次
        db.add(QbiRun(connection_id="c1", capability="report", mode="fixed",
                      question="【主题解读】客群", status="success", source="console",
                      user_id="u-a", scenic_id=SCENIC, fixed_question_id=row.issue_id,
                      answer="A 景区:以 25-34 岁为主(38%)"))
        await db.commit()

        assert await binding.recent_success_run(db, row.issue_id, OTHER) is None, \
            "B 景区不该看到 A 景区的报告"
        assert await binding.recent_success_run(db, row.issue_id, SCENIC) is not None


def test_prewarm_window_matches_the_replay_window():
    """两个窗口必须是同一个值。不一致的话会出现「预热判定为新鲜、
    前台判定为过期又现跑一遍」—— 预热等于白做。"""
    import inspect

    from cogni_agent.cronjobs import theme_reports
    from cogni_agent.gateway_service import scenic_binding as binding

    sig = inspect.signature(binding.recent_success_run)
    assert sig.parameters["within_hours"].default == theme_reports._FRESH_HOURS


# ══════════════════════════════════════════════════════════════════════
# 三、资产归属可改,且不打断指着它的问题
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_changing_asset_scenic_keeps_existing_questions_working():
    """改归属改的是同一行(asset_id 不变),所以指着它的问题一条都不用动。"""
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db, asset_scenic=None)
        await binding.ensure_theme_issues(db, SCENIC, "c1")
        row = await binding.theme_issue(db, SCENIC, "ticket")
        assert row.target.get("asset_id") == "a1"

    out = await qsvc.update_asset(_actor(), "a1", name="九龙潭·经营看板", scenic_id=SCENIC)
    assert out["scenic_id"] == SCENIC and out["name"] == "九龙潭·经营看板"

    async with common_db.get_sessionmaker()() as db:
        again = await binding.theme_issue(db, SCENIC, "ticket")
        assert again.target.get("asset_id") == "a1", "问题指向的资产不该被改动"
        # 列表上显示的目标数据名跟着刷新(不刷新的话运营改完回列表一看没变)
        assert again.target_data_name == "九龙潭·经营看板"
    # 改完之后景区账号仍然用得了它
    assert (await qsvc.get_asset(_actor("scenic_operator", SCENIC), "a1")).scenic_id == SCENIC


@run_async
async def test_unbinding_asset_back_to_group_level():
    """空串 = 解绑回集团级(所有景区共用)。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db, asset_scenic=SCENIC)
    out = await qsvc.update_asset(_actor(), "a1", scenic_id="")
    assert out["scenic_id"] is None
    # 解绑之后别的景区也用得了
    assert (await qsvc.get_asset(_actor("scenic_admin", OTHER), "a1")).id == "a1"


@run_async
async def test_scenic_role_cannot_push_an_asset_into_another_scenic():
    """景区角色只能动本景区的资产,也不能把资产推到别人景区名下。"""
    from cogni_agent.common.errors import Forbidden
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_conn_and_asset(db, asset_scenic=SCENIC)
    with pytest.raises(Forbidden):
        await qsvc.update_asset(_actor("scenic_admin", SCENIC), "a1", scenic_id=OTHER)
    # 本景区内改名字是可以的
    out = await qsvc.update_asset(_actor("scenic_admin", SCENIC), "a1", name="本景区看板")
    assert out["name"] == "本景区看板"
