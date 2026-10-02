# -*- coding: utf-8 -*-
"""知识中心 · 维度标签管理 + 常问问题不混入主题解读。

现场反馈两件事:
1. APP 常问问题里冒出「【主题解读】概览」—— 它是主题卡片的解读报告(由卡片的 AI总结 承载),
   不是一个可问的问题。要求「后端查询到什么问题就展示什么」:后端把它过滤掉,
   APP 也不再拿卡片自己的解读当兜底问题塞进列表。
2. public_problem_dimension_label 要在知识中心有一个菜单做 新增/删除/修改。
   后端 CRUD 早就有,缺的是页面;页面要能在删除前说清「下面还挂着 N 个问题」,
   所以列表接口补了 with_usage → issue_count。
"""
from __future__ import annotations

import asyncio
import functools
import re
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base
from cogni_agent.gateway_service.deps import CurrentUser

ROOT = Path(__file__).resolve().parent.parent
SCENIC = "PFTSCA01018088"


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


async def _seed_bank(db):
    from cogni_agent.gateway_service.models import AppQuestionBank

    for i, text in enumerate(["【主题解读】概览", "今天入园人数是多少", "近7天客流趋势"]):
        db.add(AppQuestionBank(scenic_id=None, theme_code="passenger_flow", question_text=text,
                               time_period="today", priority="P1", sort_order=i, active=True))
    await db.commit()


# ══════════════════════════════════════════════════════════════════════
# 一、常问问题不含主题解读
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_guide_questions_exclude_theme_reports():
    from cogni_agent.gateway_service.routes_app import app_guide_questions

    user = CurrentUser(user_id="u-1", scenic_id=SCENIC, role="scenic_admin")
    async with common_db.get_sessionmaker()() as db:
        await _seed_bank(db)
        res = await app_guide_questions(theme_code=None, page=1, page_size=100, user=user, db=db)
    texts = [i.question for i in res.items]
    assert texts == ["今天入园人数是多少", "近7天客流趋势"]
    assert res.total == 2, "total 也要按过滤后的数算,否则「换一批」会翻到空页"


@run_async
async def test_all_questions_exclude_theme_reports():
    from cogni_agent.gateway_service.routes_app import app_all_questions

    user = CurrentUser(user_id="u-1", scenic_id=SCENIC, role="scenic_admin")
    async with common_db.get_sessionmaker()() as db:
        await _seed_bank(db)
        res = await app_all_questions(theme_code=None, domain=None, ask_type=None,
                                      include_quickbi=False, user=user, db=db)
    text = str(res)
    assert "今天入园人数是多少" in text
    assert "【主题解读】" not in text


def test_app_no_longer_falls_back_to_the_card_report():
    """APP 以前在某主题没有常问问题时拿 card.question(=【主题解读】概览)兜底 ——
    这正是截图里那条。现在后端给什么就展示什么,没有就显示空态。"""
    import os

    # uni 工程是独立仓库:UNI_SRC 指过去;本地开发目录约定在 ../uni/<版本>/
    base = os.environ.get("UNI_SRC")
    cands = ([Path(base) / "src/pages/ai/index.vue"] if base
             else sorted((ROOT.parent / "uni").glob("*/src/pages/ai/index.vue")))
    cands = [c for c in cands if c.exists()]
    if not cands:
        pytest.skip("uni 工程不在本机")
    vue = cands[-1]
    src = vue.read_text(encoding="utf-8")
    assert not re.search(r"allQuestions\s*=\s*batches\[i\]\s*\|\|\s*\[\s*card", src), "不能再拿卡片解读兜底"
    assert "该主题还没有配置常问问题" in src


# ══════════════════════════════════════════════════════════════════════
# 二、维度标签管理
# ══════════════════════════════════════════════════════════════════════

async def _label(db, metric, metric_id, **kw):
    from cogni_agent.gateway_service import issue_service as issues

    return await issues.create_label(db, {
        "topic": "客流", "topic_id": "visitorFlow", "dimension": "在园人员",
        "dimension_id": "inParkVisitor", "metric": metric, "metric_id": metric_id, **kw})


@run_async
async def test_with_usage_counts_issues_per_label():
    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    async with common_db.get_sessionmaker()() as db:
        live = await _label(db, "实时人数", "liveVisitorCnt")
        await _label(db, "累计人数", "totalVisitorCnt")
        for n in range(2):
            db.add(ScenicAgentIssueDetail(
                issue_id=f"i-{n}", business_class="quick_bi", scenic_id=SCENIC, title="t",
                issue="现在园内多少人", capability="dataset_qa", visibility="group", active=True,
                topic="客流", topic_id="visitorFlow", dimension="在园人员",
                dimension_id="inParkVisitor", metric="实时人数", metric_id="liveVisitorCnt"))
        await db.commit()

        rows = await issues.list_labels(db, active=None, with_usage=True)
        by_metric = {r["metric_id"]: r["issue_count"] for r in rows}
        assert by_metric == {"liveVisitorCnt": 2, "totalVisitorCnt": 0}

        # 默认不带(问数前台的选择器不需要,也省一次分组查询)
        assert "issue_count" not in (await issues.list_labels(db, active=None))[0]

        # 有问题挂着就删不了;没挂的能删
        with pytest.raises(Exception):
            await issues.delete_label(db, live["id"])
        spare = next(r for r in rows if r["metric_id"] == "totalVisitorCnt")
        await issues.delete_label(db, spare["id"])
        assert [r["metric_id"] for r in await issues.list_labels(db, active=None)] == ["liveVisitorCnt"]


def test_list_route_passes_with_usage_through():
    src = (ROOT / "cogni_agent" / "gateway_service" / "routes_app_ops.py").read_text(encoding="utf-8")
    m = re.search(r'@router.get\("/dimension-labels"\).*?return \{"items".*?\)', src, re.S)
    assert m and "with_usage=with_usage" in m.group(0)


def test_knowledge_menu_and_route_are_wired():
    web = ROOT / "web" / "src"
    sidebar = (web / "components" / "Layout" / "Sidebar.tsx").read_text(encoding="utf-8")
    app = (web / "App.tsx").read_text(encoding="utf-8")
    page = (web / "pages" / "Knowledge" / "DimensionLabels.tsx").read_text(encoding="utf-8")
    api = (web / "lib" / "api.ts").read_text(encoding="utf-8")

    knowledge = sidebar[sidebar.index("path: '/knowledge'"):]
    knowledge = knowledge[:knowledge.index("]")]
    assert "/knowledge/dimension-labels" in knowledge, "菜单要挂在知识中心下"
    assert "import DimensionLabels from './pages/Knowledge/DimensionLabels'" in app
    assert 'path="knowledge/dimension-labels"' in app
    for fn in ("getDimensionLabels", "createDimensionLabel", "updateDimensionLabel", "deleteDimensionLabel"):
        assert fn in page, f"页面要用到 {fn}"
        assert f"export async function {fn}" in api or f"export const {fn}" in api
    assert "with_usage" in page, "删除前要知道挂了几个问题"
