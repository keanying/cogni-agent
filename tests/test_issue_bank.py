# -*- coding: utf-8 -*-
"""景区智能体问题体系(前后端改造 §1~§3)测试:三级标签 + 问题统一表。

覆盖:
- 标签的两道唯一约束(名称三元组 / 编码三元组)各自报清楚是哪一组撞了;
- 改标签名会同步到已挂它的问题上(否则前台按名称分组会分出一个空的旧组);
- 标签下还有问题时不让删(删了那些问题会在前台凭空消失);
- 问题:Quick BI 类必须带连接;挂标签时按编码补中文名;
- businessIssueList 的嵌套形状(主题→维度→指标→问题)与 quickbiLinkId 等字段;
- 迁移 0076 把两张旧表的数据灌进新表,并记 source_question_id。

不依赖 tests/conftest.py(沙箱无 asyncpg),与 tests/quickbi 同款自建写法。
"""
from __future__ import annotations

import asyncio
import functools

import pytest
import sqlalchemy as sa

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as sc  # noqa: F401  注册全部模型
from cogni_agent.common.db import Base
from cogni_agent.common.errors import NotFound, ValidationError
from cogni_agent.gateway_service import issue_service as issues
from cogni_agent.gateway_service.issue_models import (
    PublicProblemDimensionLabel, ScenicAgentIssueDetail,
)


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


LABEL = {"topic": "客流", "topic_id": "visitorFlow",
         "dimension": "在园人员", "dimension_id": "inParkVisitor",
         "metric": "实时人数", "metric_id": "liveVisitorCnt"}


# ── 三级标签 ────────────────────────────────────────────────────────────

@run_async
async def test_label_names_and_codes_must_be_unique():
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)

        # 名称三元组重复
        with pytest.raises(ValidationError) as e1:
            await issues.create_label(db, {**LABEL, "topic_id": "flowX",
                                           "dimension_id": "dX", "metric_id": "mX"})
        assert "名称" in str(e1.value)

        # 编码三元组重复
        with pytest.raises(ValidationError) as e2:
            await issues.create_label(db, {**LABEL, "topic": "客流量",
                                           "dimension": "园内", "metric": "实时"})
        assert "编码" in str(e2.value)


@run_async
async def test_label_rename_syncs_to_issues():
    """标签改名,挂在它下面的问题跟着改 —— 否则前台按名称分组会分出一个空的旧组。"""
    async with common_db.get_sessionmaker()() as db:
        label = await issues.create_label(db, LABEL)
        await issues.create_issue(db, {
            "title": "实时在园", "issue": "今日实时在园人数是多少?",
            "topic_id": "visitorFlow", "dimension_id": "inParkVisitor",
            "metric_id": "liveVisitorCnt",
        })
        await issues.update_label(db, label["id"], {"metric": "在园实时人数"})
        rows = await issues.list_issues(db)
        assert rows[0]["metric"] == "在园实时人数"


@run_async
async def test_label_with_issues_cannot_be_deleted():
    async with common_db.get_sessionmaker()() as db:
        label = await issues.create_label(db, LABEL)
        await issues.create_issue(db, {
            "title": "实时在园", "issue": "今日实时在园人数是多少?",
            "topic_id": "visitorFlow", "dimension_id": "inParkVisitor",
            "metric_id": "liveVisitorCnt",
        })
        with pytest.raises(ValidationError) as e:
            await issues.delete_label(db, label["id"])
        assert "还有 1 个问题" in str(e.value)


@run_async
async def test_label_tree_shape():
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
        await issues.create_label(db, {**LABEL, "metric": "承载率", "metric_id": "loadRate"})
        tree = await issues.label_tree(db)
        assert tree[0]["topic_id"] == "visitorFlow"
        assert [m["metric_id"] for m in tree[0]["dimensions"][0]["metrics"]] == [
            "liveVisitorCnt", "loadRate"]


# ── 问题 ────────────────────────────────────────────────────────────────

@run_async
async def test_quickbi_issue_requires_connection():
    """Quick BI 问题没有连接 = 发起时必然失败,保存这一步就拦下来。"""
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(ValidationError) as e:
            await issues.create_issue(db, {
                "business_class": "quick_bi", "title": "本月客流", "issue": "本月各平台销售额"})
        assert "连接" in str(e.value)


@run_async
async def test_issue_label_codes_fill_chinese_names():
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
        row = await issues.create_issue(db, {
            "title": "实时在园", "issue": "今日实时在园人数是多少?",
            "topic_id": "visitorFlow", "dimension_id": "inParkVisitor",
            "metric_id": "liveVisitorCnt",
        })
        assert (row["topic"], row["dimension"], row["metric"]) == ("客流", "在园人员", "实时人数")
        assert row["issue_id"].startswith("issue_")


@run_async
async def test_issue_with_unknown_label_is_rejected():
    """挂到不存在的三级组合上 = 前台永远看不到它,不如直接拒绝并指路。"""
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(ValidationError) as e:
            await issues.create_issue(db, {
                "title": "x", "issue": "y", "topic_id": "a",
                "dimension_id": "b", "metric_id": "c"})
        assert "维度标签" in str(e.value)


@run_async
async def test_scenic_scope_includes_group_level_issues():
    """景区看到:本景区 + 全景区通用。通用问题看不到的话,集团配的模板等于没配。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
        base = {"topic_id": "visitorFlow", "dimension_id": "inParkVisitor",
                "metric_id": "liveVisitorCnt", "issue": "今日实时在园人数是多少?"}
        await issues.create_issue(db, {**base, "title": "通用", "scenic_id": None})
        await issues.create_issue(db, {**base, "title": "本景区", "scenic_id": "wys"})
        await issues.create_issue(db, {**base, "title": "别的景区", "scenic_id": "other"})

        titles = {i["title"] for i in await issues.list_issues(db, scenic_id="wys")}
        assert titles == {"通用", "本景区"}


@run_async
async def test_business_issue_list_shape():
    """嵌套结构:主题 → 维度 → 指标 → issues,且 quickbiLinkId 就是 connection_id。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
        await issues.create_issue(db, {
            "business_class": "quick_bi", "title": "总评趋势", "issue": "按评论发布时间看总评论数",
            "connection_id": "43a56da1-662a-4d3f-827e-1c05524b3e51",
            "capability": "dashboard_qa", "ask_type": "dashboard",
            "target": {"asset_id": "a1"}, "target_data_id": "page-1", "target_data_name": "客流看板",
            "scenic_id": "PFTSCA01018088",
            "topic_id": "visitorFlow", "dimension_id": "inParkVisitor", "metric_id": "liveVisitorCnt",
        })
        tree = await issues.business_issue_list(db, "PFTSCA01018088")
        topic = tree[0]
        assert topic["topic"] == "客流" and topic["topicId"] == "visitorFlow"
        dim = topic["visitorFlow"][0]
        assert dim["dimension"] == "在园人员"
        metric = dim["inParkVisitor"][0]
        assert metric["metric"] == "实时人数"
        issue = metric["liveVisitorCnt"][0]["issues"][0]
        assert issue["businessClass"] == "quick_bi"
        assert issue["quickbiLinkId"] == "43a56da1-662a-4d3f-827e-1c05524b3e51"
        assert issue["connectionId"] == issue["quickbiLinkId"]
        assert issue["capabilityLabel"] == "报表"
        assert issue["targetDataName"] == "客流看板"


@run_async
async def test_unlabelled_issues_are_kept_but_not_shown_on_the_app_tree():
    """没挂标签的问题**留着**,但不出现在 APP 的问题树上。

    原先是兜到「未分类 / 未分类 / 未分类」一个分支里,于是前台多出一个谁也说不清
    是什么的入口;而管理端一直按「没挂标签 = 前台看不到」提示运营 —— 两边不是一回事。
    现在口径统一:挂载点没配就是没配,别替它猜一个;管理端列表照样看得到、能补标签。
    """
    async with common_db.get_sessionmaker()() as db:
        row = await issues.create_issue(db, {"title": "老问题", "issue": "昨日退票量?"})
        assert await issues.business_issue_list(db, None) == []      # 前台:不展示
        admin = await issues.list_issues(db)                          # 后台:还在,能补标签
        assert [i["issue_id"] for i in admin] == [row["issue_id"]]


@run_async
async def test_touch_run_records_last_execution():
    async with common_db.get_sessionmaker()() as db:
        row = await issues.create_issue(db, {"title": "t", "issue": "q"})
        await issues.touch_run(db, row["issue_id"], run_id="run_1", status="success")
        got = await issues.get_issue(db, row["issue_id"])
        assert got.last_run_id == "run_1" and got.last_run_status == "success"
        assert got.last_run_time is not None


@run_async
async def test_get_issue_not_found():
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(NotFound):
            await issues.get_issue(db, "issue_nope")


# ── 迁移 0076:存量两表灌进新表 ──────────────────────────────────────────

@run_async
async def test_migration_0076_0079_backfill_and_retire_legacy_table():
    """旧表的问题都要能迁进来,并记住来自哪一行(出问题要能对回去)。

    Quick BI 老表 tc_qbi_fixed_questions 已于 0079 删除、模型也没了,所以这里用裸 SQL
    把它建出来再种一行 —— 这正是升级时的真实场景:库里有老表,迁移把它读空再删掉。
    """
    from cogni_agent.gateway_service.models import AppQuestionBank

    engine0 = common_db.get_engine()
    async with engine0.begin() as conn:
        await conn.exec_driver_sql(f"""
            CREATE TABLE IF NOT EXISTS {sc.P}tc_qbi_fixed_questions (
                id VARCHAR(36) PRIMARY KEY, connection_id VARCHAR(36) NOT NULL DEFAULT '',
                title VARCHAR(128) NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT '',
                capability VARCHAR(24) NOT NULL DEFAULT 'dataset_qa', domain VARCHAR(32),
                ask_type VARCHAR(16) NOT NULL DEFAULT 'qa', target JSONB, locale VARCHAR(8),
                category VARCHAR(64), sort_order INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT true, owner_user_id VARCHAR(36) NOT NULL DEFAULT '',
                scenic_id VARCHAR(64), visibility VARCHAR(16) NOT NULL DEFAULT 'group',
                last_run_id VARCHAR(36), last_run_status VARCHAR(16), last_run_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        await conn.exec_driver_sql(
            f"INSERT INTO {sc.P}tc_qbi_fixed_questions (id, connection_id, title, question,"
            f" capability, ask_type, target, owner_user_id, scenic_id, visibility, active)"
            f" VALUES ('fq-1','conn-1','本月客流','本月各平台销售额','dashboard_qa','dashboard',"
            f" '{{\"page_id\": \"p-1\"}}'::jsonb,'u','wys','group',true)"
            f" ON CONFLICT (id) DO NOTHING")

    async with common_db.get_sessionmaker()() as db:
        db.add(AppQuestionBank(
            id="qb-1", scenic_id="wys", theme_code="ticket", question_text="昨日退票量有多少?",
            time_period="yesterday", metric_name="退票量", dimension_name="退票预售"))
        await db.commit()

    engine = common_db.get_engine()
    async with engine.begin() as conn:
        mig = next(m for m in sc.MIGRATIONS if m.id == "0076_issue_detail_and_dimension_label")
        for stmt in sc._split_statements(mig.sql):
            await conn.exec_driver_sql(stmt)

    async with common_db.get_sessionmaker()() as db:
        rows = {r.source_question_id: r for r in (await db.execute(
            sa.select(ScenicAgentIssueDetail))).scalars()}
        assert set(rows) == {"fq-1", "qb-1"}
        assert rows["fq-1"].business_class == "quick_bi"
        assert rows["fq-1"].connection_id == "conn-1"
        assert rows["fq-1"].target_data_id == "p-1"
        assert rows["qb-1"].business_class == "local_system"
        assert rows["qb-1"].title == "退票量"
        # 三级标签初始化 40 条
        n = (await db.execute(sa.select(sa.func.count()).select_from(
            PublicProblemDimensionLabel))).scalar_one()
        assert n == 40

    # 幂等:再跑一次不会重复插
    async with engine.begin() as conn:
        for stmt in sc._split_statements(mig.sql):
            await conn.exec_driver_sql(stmt)
    async with common_db.get_sessionmaker()() as db:
        again = (await db.execute(sa.select(sa.func.count()).select_from(
            ScenicAgentIssueDetail))).scalar_one()
        assert again == 2

    # 0079:补齐归属/可见性等列,并把老表删掉(此后固定问题只有统一表这一份)
    async with engine.begin() as conn:
        mig79 = next(m for m in sc.MIGRATIONS if m.id == "0079_quickbi_fixed_questions_into_issue_detail")
        for stmt in sc._split_statements(mig79.sql):
            await conn.exec_driver_sql(stmt)
    async with common_db.get_sessionmaker()() as db:
        row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == "fq-1"))).scalars().first()
        assert row.owner_user_id == "u" and row.visibility == "group"
        gone = (await db.execute(sa.text(
            "SELECT to_regclass('" + sc.P + "tc_qbi_fixed_questions')"))).scalar_one()
        assert gone is None, "老表必须删掉 —— 留着就还会有人往里写,又变成两份数据"


# ── /app/chat 分流:Quick BI 问题不该被「没有可用的 Agent」挡下 ──────────────

@run_async
async def test_app_chat_routes_quickbi_issue_without_any_agent():
    """现场报的那个错:点一条 Quick BI 问题,回「该问题未指定执行目标,且当前没有可用的 Agent」。

    根因是判断顺序:代码先做「没有执行目标就回落默认 Agent」,再判断这条问题是不是
    Quick BI —— 于是在一个还没建 Agent 的环境里,Quick BI 问题连分流那一步都走不到。
    而它直连 Quick BI 跑,本来一个 Agent 都不需要。
    这条用例在**库里一个 Agent 都没有**的前提下,钉死它仍能发起并落成 quickbi 任务。
    """
    from cogni_agent.gateway_service import routes_app
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.gateway_service.schemas_app import AppChatRequest
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        # 连接要真实存在:0080 起发起前会按景区解析一条可用连接,
        # 解析不到会明确报「该景区还没有可用的 Quick BI 连接」(而不是继续往下跑)
        from cogni_agent.tool_center_service.quickbi.models import QbiConnection

        db.add(QbiConnection(id="conn-1", name="集团 Quick BI", owner_user_id="u-admin",
                             visibility="group", active=True, is_default=True))
        await db.commit()
        created = await issues.create_issue(db, {
            "business_class": "quick_bi", "connection_id": "conn-1",
            "capability": "dataset_qa", "ask_type": "qa",
            "title": "总评趋势", "issue": "按评论发布时间看总评论数的分布"})

    async def fake_stream(*a, **kw):
        yield {"type": "run", "run_id": "r1"}
        yield {"type": "done", "status": "success", "answer": "ok"}

    pumped: list[str] = []

    async def fake_pump(task_id, agen, issue_id=None):
        pumped.append(task_id)

    orig_stream, orig_pump = qsvc.run_stream, qbi_bridge_module().pump
    qsvc.run_stream = fake_stream
    qbi_bridge_module().pump = fake_pump
    try:
        async with common_db.get_sessionmaker()() as db:
            # 断言前提:库里确实一个 Agent 都没有(否则这条用例测不到东西)
            from cogni_agent.tool_center_service.agent.models import Agent

            assert (await db.execute(sa.select(sa.func.count()).select_from(Agent))).scalar_one() == 0
            resp = await routes_app.app_chat(
                AppChatRequest(issue_id=created["issue_id"]),
                CurrentUser(user_id="u-app", scenic_id=None, role="scenic_admin"), db)
    finally:
        qsvc.run_stream, qbi_bridge_module().pump = orig_stream, orig_pump

    assert resp.target_type == "quickbi"
    assert resp.target_id == created["issue_id"]
    assert pumped, "应当把 Quick BI 的事件流交给桥接推到同一个任务通道上"


@run_async
async def test_app_chat_routes_quickbi_when_old_entry_sends_question_id():
    """老入口(「试试问我」/关注问题)只带 question_id —— 也要能分流到 Quick BI。

    否则一条配在 Quick BI 上的问题会被本地 Agent 接走,答出来一个看着像模像样、
    其实没查过数的结果 —— 这种错比直接报错更难发现。
    """
    from cogni_agent.gateway_service import routes_app
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.gateway_service.schemas_app import AppChatRequest
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        from cogni_agent.tool_center_service.quickbi.models import QbiConnection

        db.add(QbiConnection(id="conn-1", name="集团 Quick BI", owner_user_id="u-admin",
                             visibility="group", active=True, is_default=True))
        db.add(ScenicAgentIssueDetail(
            issue_id="issue_q1", business_class="quick_bi", connection_id="conn-1",
            capability="dataset_qa", ask_type="qa", title="总评趋势",
            issue="按评论发布时间看总评论数的分布",
            source_question_id="bank-legacy-1"))
        await db.commit()

    async def fake_stream(*a, **kw):
        yield {"type": "done", "status": "success"}

    async def fake_pump(task_id, agen, issue_id=None):
        return None

    orig_stream, orig_pump = qsvc.run_stream, qbi_bridge_module().pump
    qsvc.run_stream, qbi_bridge_module().pump = fake_stream, fake_pump
    try:
        async with common_db.get_sessionmaker()() as db:
            resp = await routes_app.app_chat(
                AppChatRequest(question_id="bank-legacy-1", agent_id="agent-x",
                               message="按评论发布时间看总评论数的分布"),
                CurrentUser(user_id="u-app", scenic_id=None, role="scenic_admin"), db)
    finally:
        qsvc.run_stream, qbi_bridge_module().pump = orig_stream, orig_pump

    assert resp.target_type == "quickbi"


@run_async
async def test_app_chat_quickbi_start_failure_does_not_lock_the_conversation():
    """发起即失败(连接鉴权/入参错)时,任务行要立刻落成 FAILED。

    任务行在发起前就以 RUNNING 落库;不收尾的话要等租约过期才被巡检判死,这期间同一会话
    再问会被「上一轮尚未结束」挡住 —— 用户改完配置想马上重试却被一条没人在跑的任务锁着。
    """
    from cogni_agent.control_plane_service.models import Task
    from cogni_agent.gateway_service import routes_app
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.gateway_service.schemas_app import AppChatRequest
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.errors import QuickBIError

    async with common_db.get_sessionmaker()() as db:
        from cogni_agent.tool_center_service.quickbi.models import QbiConnection

        db.add(QbiConnection(id="conn-1", name="集团 Quick BI", owner_user_id="u-admin",
                             visibility="group", active=True, is_default=True))
        await db.commit()
        created = await issues.create_issue(db, {
            "business_class": "quick_bi", "connection_id": "conn-1",
            "capability": "dataset_qa", "ask_type": "qa",
            "title": "总评趋势", "issue": "按评论发布时间看总评论数的分布"})

    async def failing_stream(*a, **kw):
        raise QuickBIError("user_token 无效", kind="auth")
        yield  # pragma: no cover

    orig_stream = qsvc.run_stream
    qsvc.run_stream = failing_stream
    try:
        async with common_db.get_sessionmaker()() as db:
            with pytest.raises(Exception):
                await routes_app.app_chat(
                    AppChatRequest(issue_id=created["issue_id"]),
                    CurrentUser(user_id="u-app", scenic_id=None, role="scenic_admin"), db)
    finally:
        qsvc.run_stream = orig_stream

    async with common_db.get_sessionmaker()() as db:
        rows = (await db.execute(sa.select(Task.status, Task.lease_until)
                                 .where(Task.executor_type == "quickbi"))).all()
    assert rows and all(st == "FAILED" and lease is None for st, lease in rows), rows


@run_async
async def test_question_bank_save_mirrors_into_issue_table():
    """问题库新建/改名要同步到统一问题表,否则「后台建了、APP 问题树里没有」。"""
    from cogni_agent.gateway_service.models import AppQuestionBank

    async with common_db.get_sessionmaker()() as db:
        bank = AppQuestionBank(
            id="qb-sync-1", scenic_id=None, theme_code="opinion",
            question_text="昨日负面评论有多少?", time_period="yesterday",
            dimension_name="情感分析", metric_name="负面关键词", sort_order=3, active=True)
        db.add(bank)
        await db.flush()
        await issues.sync_from_question_bank(db, bank)
        await db.commit()

        row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == "qb-sync-1"))).scalars().first()
        assert row is not None and row.business_class == "local_system"
        assert row.title == "负面关键词" and row.issue == "昨日负面评论有多少?"

        # 改了再同步:更新同一行,不会多出一条
        bank.question_text = "昨日负面评论涨了多少?"
        await issues.sync_from_question_bank(db, bank)
        await db.commit()
        rows = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == "qb-sync-1"))).scalars().all()
        assert len(rows) == 1 and rows[0].issue == "昨日负面评论涨了多少?"

        # 删除:镜像行一起走
        await issues.drop_issue_of_source(db, "qb-sync-1")
        await db.commit()
        assert (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == "qb-sync-1"))).scalars().first() is None


def qbi_bridge_module():
    from cogni_agent.gateway_service import qbi_bridge

    return qbi_bridge


@run_async
async def test_get_issue_accepts_legacy_id():
    """前端缓存/历史配置里可能还是迁移前的老表 ID,不该报「问题不存在」。"""
    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(
            issue_id="issue_new01", business_class="quick_bi", connection_id="c1",
            title="老问题", issue="昨天卖了多少票",
            source_question_id="0f9a2b10-0000-4000-8000-0000000000aa"))
        await db.commit()
        row = await issues.get_issue(db, "0f9a2b10-0000-4000-8000-0000000000aa")
        assert row.issue_id == "issue_new01"


# ── Quick BI → APP 事件桥接(前端只认一个发起接口) ───────────────────────

@run_async
async def test_qbi_bridge_translates_events_and_finishes_task():
    """Quick BI 事件翻译成 APP 已有事件,并把任务收尾成 COMPLETED。

    前端只调 /app/chat 拿 task_id、订同一个流 —— 所以事件名必须落在它已经认识的
    那几个里(token/stage/structured/done),否则「统一接口」只是接口统一、渲染还得分叉。
    """
    from cogni_agent.control_plane_service.models import Task
    from cogni_agent.gateway_service import qbi_bridge
    from cogni_agent.runtime_service import streaming

    published: list[tuple[str, dict]] = []

    async def fake_publish(task_id, event, data):
        published.append((event, data))
        return "1-1"

    async def fake_stream():
        yield {"type": "run", "run_id": "r1"}
        yield {"type": "status", "message": "问数中…", "channel": "thinking"}
        yield {"type": "reasoning", "delta": "先选数据集\n再翻成 SQL\n", "channel": "thinking"}
        yield {"type": "text", "delta": "今日在园 8,523 人", "channel": "data"}
        yield {"type": "chart", "channel": "data", "chart": {
            "title": "当日趋势", "chart_type": "line",
            "fields": [{"name": "时间", "role": "dimension"}, {"name": "人数", "role": "metric"}],
            "rows": [{"时间": "08:00", "人数": 120}, {"时间": "09:00", "人数": 260}],
        }}
        yield {"type": "done", "status": "success"}

    async with common_db.get_sessionmaker()() as db:
        issue = await issues.create_issue(db, {"title": "在园", "issue": "今日实时在园人数?"})
        task = Task(scenic_id="wys", user_id="u", executor_type="quickbi", executor_id="conn-1",
                    source="app", status="RUNNING", priority=5, payload={})
        db.add(task)
        await db.commit()
        task_id = task.id

    orig = streaming.publish
    streaming.publish = fake_publish
    try:
        await qbi_bridge.pump(task_id, fake_stream(), issue_id=issue["issue_id"])
    finally:
        streaming.publish = orig

    kinds = [e for e, _ in published]
    assert "token" in kinds and "stage" in kinds and "structured" in kinds
    assert kinds[-1] == "done"
    # 推理按行聚合进「思考过程」,不进正文
    stage_texts = [d.get("text") for e, d in published if e == "stage"]
    assert "先选数据集" in stage_texts and "再翻成 SQL" in stage_texts
    # 图表转成 ECharts option(前端图表组件吃的是 option,不是字段表)
    chart_evt = next(d for e, d in published if e == "structured")
    option = chart_evt["items"][0]["charts"][0]["option"]
    assert option["xAxis"]["data"] == ["08:00", "09:00"]
    assert option["series"][0]["data"] == [120, 260]
    # 正文只有数据那一半
    done = next(d for e, d in published if e == "done")
    assert done["answer"].startswith("今日在园 8,523 人")
    assert "先选数据集" not in done["answer"]

    async with common_db.get_sessionmaker()() as db:
        row = await db.get(Task, task_id)
        assert row.status == "COMPLETED"
        assert row.result["answer"].startswith("今日在园")
        got = await issues.get_issue(db, issue["issue_id"])
        assert got.last_run_status == "success" and got.last_run_id == task_id


@run_async
async def test_qbi_bridge_marks_failed_when_upstream_errors():
    """上游报错且没产出正文 → 任务 FAILED + error 事件,前端能立刻解锁重发。"""
    from cogni_agent.control_plane_service.models import Task
    from cogni_agent.gateway_service import qbi_bridge
    from cogni_agent.runtime_service import streaming

    published: list[tuple[str, dict]] = []

    async def fake_publish(task_id, event, data):
        published.append((event, data))
        return "1-1"

    async def fake_stream():
        yield {"type": "run", "run_id": "r1"}
        yield {"type": "error", "message": "小Q额度已用尽"}
        yield {"type": "done", "status": "failed", "error": "小Q额度已用尽"}

    async with common_db.get_sessionmaker()() as db:
        task = Task(scenic_id="wys", user_id="u", executor_type="quickbi", executor_id="conn-1",
                    source="app", status="RUNNING", priority=5, payload={})
        db.add(task)
        await db.commit()
        task_id = task.id

    orig = streaming.publish
    streaming.publish = fake_publish
    try:
        await qbi_bridge.pump(task_id, fake_stream())
    finally:
        streaming.publish = orig

    assert ("error", {"message": "小Q额度已用尽"}) in published
    async with common_db.get_sessionmaker()() as db:
        row = await db.get(Task, task_id)
        assert row.status == "FAILED" and row.result["error"] == "小Q额度已用尽"
