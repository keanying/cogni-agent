# -*- coding: utf-8 -*-
"""快捷问题(自研智能体的固定问题)接在统一问题表上。

以前这一页读的是 app_question_bank —— 早期的 APP 问题库。而 0076/0079 之后真正生效的是
scenic_agent_issue_detail:前台的问题树、Quick BI 的固定问题都在那张表上。

于是这一页永远是空的:运营在这儿建了问题,APP 上一条也看不到,而页面不报任何错,
只写着「暂无问题」—— 他只会以为是自己没建对,反复建几遍。
"""
from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base

WEB = Path(__file__).resolve().parent.parent / "web" / "src"


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


# ══════════════════════════════════════════════════════════════════════
# 一、页面确实接到了统一表
# ══════════════════════════════════════════════════════════════════════

def test_page_uses_the_unified_issue_api_not_the_dead_one():
    """**这条是这一页空了很久的直接原因。**

    读错表不会报错,只会让列表永远是空的 —— 而空列表看起来就像「还没建过」。
    """
    src = (WEB / "pages" / "AppOps" / "QuestionBank.tsx").read_text(encoding="utf-8")
    assert "listScenicIssues" in src, "快捷问题必须读统一问题表"
    assert "business_class: 'local_system'" in src, "要按自研智能体那一类筛,不然会混进 Quick BI 的"
    for dead in ("listAppQuestions", "createAppQuestion", "updateAppQuestion", "deleteAppQuestion"):
        assert dead not in src, f"{dead} 读的是早已不生效的 app_question_bank"


def test_page_warns_about_missing_labels():
    """三级标签是 APP 问题树的唯一挂载点:没挂的问题前台一条都看不到,
    而后台列表看起来一切正常 —— 这是「配了但前台没有」最常见的一种,必须在页面上说破。"""
    src = (WEB / "pages" / "AppOps" / "QuestionBank.tsx").read_text(encoding="utf-8")
    assert "未挂标签" in src and "APP 不展示" in src


def test_api_client_targets_the_right_endpoints():
    src = (WEB / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "/admin/app-ops/issues" in src
    assert "'PATCH'" in src, "改问题走 PATCH(后端就是 PATCH,写成 PUT 会 405)"


# ══════════════════════════════════════════════════════════════════════
# 二、后端契约:这一页依赖的那几件事
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_local_system_issues_round_trip():
    from cogni_agent.gateway_service import issue_service as issues

    async with common_db.get_sessionmaker()() as db:
        created = await issues.create_issue(db, {
            "business_class": "local_system", "title": "今日在园人数",
            "issue": "今天景区在园人数多少", "scenic_id": "PFTSCA01018088",
            "model_instruction": "只看今天 00:00 至此刻", "active": True})
        iid = created["issue_id"]

        got = await issues.list_issues(db, scenic_id="PFTSCA01018088",
                                       business_class="local_system")
        assert [i["issue_id"] for i in got] == [iid]

        await issues.update_issue(db, iid, {"title": "改过的标题"})
        again = await issues.list_issues(db, business_class="local_system")
        assert again[0]["title"] == "改过的标题"


@run_async
async def test_quickbi_and_local_questions_do_not_mix():
    """两类问题同表,但各自的维护页只该看到自己那一类 ——
    混在一起的话,运营会在快捷问题页上看到一堆自己动不了的 Quick BI 问题。"""
    from cogni_agent.gateway_service import issue_service as issues

    async with common_db.get_sessionmaker()() as db:
        await issues.create_issue(db, {"business_class": "local_system",
                                       "title": "本地", "issue": "本地问题"})
        await issues.create_issue(db, {"business_class": "quick_bi",
                                       "title": "QBI", "issue": "QBI 问题",
                                       "connection_id": "c-1"})
        local = await issues.list_issues(db, business_class="local_system")
        qbi = await issues.list_issues(db, business_class="quick_bi")
    assert [i["title"] for i in local] == ["本地"]
    assert [i["title"] for i in qbi] == ["QBI"]
