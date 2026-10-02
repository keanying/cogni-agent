# -*- coding: utf-8 -*-
"""Quick BI 没答上来时,转交给自研智能体。

现场最常见的三种「没答上来」:小Q 判「该问题与数据集无关」、凭证/额度出问题、
以及「跑完了但一个字都没有」。这三种对用户是同一件事 —— 他问了个问题,什么也没拿到。
而我们自己的智能体手里有雷达图指标这类业务工具,很多时候答得出来。

这一组守的是转交本身的几条边界,每一条错了都不报错:

- **必须接在同一个 task_id 上**。新建任务的话,APP 那边开着的 SSE 流收不到后续事件,
  用户对着转圈等到超时,而后台其实已经答完了 —— 答在另一个他看不到的任务里;
- **用户主动取消的不转交**。他要的是停下来,不是换个人再答一遍(还照样计费);
- **转交失败要回到原来的失败路径**,不能把「Quick BI 失败」变成「什么都没有」;
- **智能体也没跑起来时要自己收尾**,否则任务一直挂在 RUNNING 上,
  等巡检判死的时候用户早走了。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.gateway_service import qbi_bridge


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



@pytest.fixture
def fake_executor(monkeypatch):
    """替身 executor 模块。

    真 executor 顶层 import langchain_core(本沙箱装不了),而 _handoff_to_agent 是在
    函数体里 import 它的 —— 所以往 sys.modules 里放一个替身就够,不必装整套图框架。
    替身只记「被谁调过」,验的正是我们关心的那件事:有没有接在同一个 task_id 上。
    """
    import sys
    import types

    mod = types.ModuleType("cogni_agent.runtime_service.engine.executor")
    calls: list[str] = []

    async def execute_task(task_id):
        calls.append(task_id)
        if getattr(mod, "boom", False):
            raise RuntimeError("智能体也挂了")

    mod.execute_task = execute_task
    mod.calls = calls
    monkeypatch.setitem(sys.modules, "cogni_agent.runtime_service.engine.executor", mod)

    # 事件发布也一并接住:沙箱没有 redis,而转交本身就会发一条 stage 事件 ——
    # 不接的话每个用例都死在「沙箱无 redis」上,和被测逻辑毫无关系
    from cogni_agent.runtime_service import streaming

    mod.published = []

    async def fake_publish(task_id, event, data):
        mod.published.append((event, data))

    monkeypatch.setattr(streaming, "publish", fake_publish)
    return mod


async def _seed_task(task_id="t-qbi-1", message="近一日的雷达图数据"):
    from cogni_agent.control_plane_service.models import Task
    from cogni_agent.user_center_service.models import User

    async with common_db.get_sessionmaker()() as db:
        db.add(User(id="u-1", name="测试", account="u1", phone="13000000000",
                    password_hash="x", role="super_admin", is_active=True))
        db.add(Task(id=task_id, executor_type="quickbi", executor_id="conn-1",
                    status="RUNNING", source="app", scenic_id=None, user_id="u-1",
                    payload={"message": message, "user_id": "u-1", "role": "super_admin",
                             "connection_id": "conn-1", "capability": "dashboard_qa",
                             "target_type": "quickbi", "target_id": "issue_1"}))
        await db.commit()
    return task_id


# ══════════════════════════════════════════════════════════════════════
# 一、什么时候该转交
# ══════════════════════════════════════════════════════════════════════

def test_cancellation_is_never_handed_off():
    """用户点了停止,就是不想要了 —— 再转交等于无视他,而且照样计费。"""
    assert qbi_bridge._is_cancelled("已取消") is True
    assert qbi_bridge._is_cancelled("Cancelled by user") is True
    assert qbi_bridge._is_cancelled("Quick BI 执行失败") is False
    assert qbi_bridge._is_cancelled(None) is False


def test_handoff_rewrites_the_same_task_not_a_new_one(monkeypatch, fake_executor):
    """**必须接在同一行任务上。**

    新建任务的话,APP 那边开着的 SSE 流收不到后续事件 —— 用户对着一个转圈等到超时,
    而后台其实已经答完了,答在另一个他看不到的任务里。
    """
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))
    monkeypatch.setattr(qbi_bridge, "_latest_published_id", lambda db, t, i: _coro("ver-latest"))

    async def body():
        tid = await _seed_task()
        ok = await qbi_bridge._handoff_to_agent(tid, reason="小Q 判定与数据集无关")
        assert ok is True
        assert fake_executor.calls == [tid], "要在原任务上继续跑,不能另起炉灶"
        async with common_db.get_sessionmaker()() as db:
            row = await db.get(Task, tid)
            assert row.executor_type == "agent" and row.executor_id == "agent-9"
            assert row.payload["handoff_from"] == "quickbi"
            assert "数据集无关" in row.payload["handoff_reason"], "要记下为什么转交"
            assert row.payload["message"] == "近一日的雷达图数据", "用户原话必须留着"
    asyncio.run(body())


def test_quickbi_context_is_stripped_before_handoff(monkeypatch, fake_executor):
    """连接/能力/目标数据是 Quick BI 的上下文,智能体用不上 —— 留着只会干扰它的解析。"""
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))
    monkeypatch.setattr(qbi_bridge, "_latest_published_id", lambda db, t, i: _coro("ver-latest"))

    async def body():
        tid = await _seed_task()
        await qbi_bridge._handoff_to_agent(tid, reason="空结果")
        async with common_db.get_sessionmaker()() as db:
            p = (await db.get(Task, tid)).payload
        for k in ("connection_id", "capability", "target_type", "target_id"):
            assert k not in p, f"{k} 是 Quick BI 的上下文,不该带给智能体"
    asyncio.run(body())


async def _coro(v):
    return v


# ══════════════════════════════════════════════════════════════════════
# 二、转不成的时候不能把事情搞得更糟
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_no_available_agent_falls_back_to_the_original_failure(monkeypatch):
    """这个账号没有可用智能体时,老老实实走原来的失败路径 ——
    转交是增强,不能让它把「Quick BI 失败」变成「什么都没有」。"""
    tid = await _seed_task()
    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro(None))
    assert await qbi_bridge._handoff_to_agent(tid, reason="空") is False


@run_async
async def test_missing_task_or_message_does_not_hand_off():
    assert await qbi_bridge._handoff_to_agent("no-such-task", reason="空") is False
    tid = await _seed_task("t-empty", message="   ")
    assert await qbi_bridge._handoff_to_agent(tid, reason="空") is False


def test_agent_failure_finalizes_instead_of_hanging(monkeypatch, fake_executor):
    """**智能体也没跑起来时必须自己收尾。**

    不收的话任务一直挂在 RUNNING 上,前端转圈转到超时,
    等巡检把它判死的时候用户早就走了 —— 而这一整段他什么提示都没有。
    """
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))
    monkeypatch.setattr(qbi_bridge, "_latest_published_id", lambda db, t, i: _coro("ver-latest"))
    fake_executor.boom = True
    published = fake_executor.published

    async def body():
        tid = await _seed_task()
        assert await qbi_bridge._handoff_to_agent(tid, reason="空") is True, \
            "已经自己收过尾了,调用方不该再走一遍原路径"
        async with common_db.get_sessionmaker()() as db:
            row = await db.get(Task, tid)
        assert row.status == "FAILED", "不能把任务留在 RUNNING 上"
        events = [e for e, _ in published]
        assert "done" in events, "必须发 done,否则前端一直转圈"
        assert "error" in events
    asyncio.run(body())


def test_user_is_told_about_the_handoff(monkeypatch, fake_executor):
    """不说一声的话,用户会看到思考面板突然从 Quick BI 跳到另一套步骤、
    而正文迟迟不出来 —— 那比直接报错更让人摸不着头脑。"""
    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))
    monkeypatch.setattr(qbi_bridge, "_latest_published_id", lambda db, t, i: _coro("ver-latest"))

    async def body():
        tid = await _seed_task()
        await qbi_bridge._handoff_to_agent(tid, reason="空")
        stages = [d.get("text", "") for e, d in fake_executor.published if e == "stage"]
        assert any("转由智能体" in t for t in stages), stages
    asyncio.run(body())


def test_pump_consults_the_handoff_before_finalizing_as_failed():
    """钉住接线点:转交要发生在「判失败并发 done」**之前**。

    放在之后的话,用户会先收到一条失败的 done、前端收尾,再收到后续事件 —— 全丢。
    """
    import inspect

    src = inspect.getsource(qbi_bridge.pump)
    assert "_handoff_to_agent" in src
    assert src.index("_handoff_to_agent") < src.index('status = "FAILED" if failed_msg')
    assert "_is_cancelled" in src, "取消的那一路不能被转交"


# ══════════════════════════════════════════════════════════════════════
# 三、接手的跑「最新已发布版本」;问题上绑了技能就交给它;「未查询到数据」也算没答上来
# ══════════════════════════════════════════════════════════════════════

async def _versions(rtype, rid, specs):
    """specs: [(version, status)] —— 造版本行,返回 {version: id}"""
    from cogni_agent.tool_center_service.versioning import ResourceVersion

    ids = {}
    async with common_db.get_sessionmaker()() as db:
        for v, st in specs:
            row = ResourceVersion(resource_type=rtype, resource_id=rid, version=v, status=st,
                                  snapshot={}, created_by="u-1")
            db.add(row)
            await db.flush()
            ids[v] = row.id
        await db.commit()
    return ids


async def _issue(issue_id="issue-1", *, skill_id=None, agent_id=None):
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(issue_id=issue_id, business_class="quick_bi", scenic_id=None,
                                      title="t", issue="近一日的雷达图数据", capability="dataset_qa",
                                      visibility="group", active=True, skill_id=skill_id, agent_id=agent_id))
        await db.commit()
    return issue_id


def test_default_agent_runs_its_latest_published_version(monkeypatch, fake_executor):
    """不钉版本就走「按用户指派」—— 降级路上的人多半没被指派过,Quick BI 没答上来、
    转过去又卡在「未分发」,两头落空。要跑的是**最新已发布**,不是最新的草稿或已下线版本。"""
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))

    async def body():
        ids = await _versions("agent", "agent-9", [(1, "published"), (2, "published"), (3, "offline")])
        await _versions("agent", "agent-9", [(None, "draft")])
        tid = await _seed_task()
        assert await qbi_bridge._handoff_to_agent(tid, reason="空") is True
        async with common_db.get_sessionmaker()() as db:
            row = await db.get(Task, tid)
        assert row.payload["executor_version_id"] == ids[2]
        assert not row.payload.get("handoff_authorized"), "默认智能体不是问题上配的,不给授权穿透"
    asyncio.run(body())


def test_issue_bound_skill_takes_over_with_its_latest_version(monkeypatch, fake_executor):
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))

    async def body():
        ids = await _versions("skill", "skill-7", [(4, "published"), (5, "published")])
        await _versions("agent", "agent-9", [(1, "published")])
        iid = await _issue(skill_id="skill-7", agent_id="agent-9")
        tid = await _seed_task()
        assert await qbi_bridge._handoff_to_agent(tid, reason="空", issue_id=iid) is True
        async with common_db.get_sessionmaker()() as db:
            row = await db.get(Task, tid)
        assert (row.executor_type, row.executor_id) == ("skill", "skill-7"), "问题上绑的技能优先"
        assert row.payload["executor_version_id"] == ids[5]
        assert row.payload["handoff_authorized"] is True
    asyncio.run(body())


def test_unpublished_issue_skill_falls_through_to_agent(monkeypatch, fake_executor):
    """问题上绑的技能从没发布过:跳过它,用问题上的智能体;都没有才用默认智能体。"""
    from cogni_agent.control_plane_service.models import Task

    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-default"))

    async def body():
        await _versions("skill", "skill-7", [(None, "draft")])
        ids = await _versions("agent", "agent-issue", [(2, "published")])
        iid = await _issue(skill_id="skill-7", agent_id="agent-issue")
        tid = await _seed_task()
        assert await qbi_bridge._handoff_to_agent(tid, reason="空", issue_id=iid) is True
        async with common_db.get_sessionmaker()() as db:
            row = await db.get(Task, tid)
        assert (row.executor_type, row.executor_id) == ("agent", "agent-issue")
        assert row.payload["executor_version_id"] == ids[2]
    asyncio.run(body())


@run_async
async def test_default_agent_without_published_version_does_not_hand_off(monkeypatch):
    import cogni_agent.gateway_service.routes_app as ra
    monkeypatch.setattr(ra, "_default_agent_for", lambda db, u: _coro("agent-9"))
    await _versions("agent", "agent-9", [(None, "draft")])
    tid = await _seed_task()
    assert await qbi_bridge._handoff_to_agent(tid, reason="空") is False


def test_no_data_answers_are_detected_but_real_answers_are_not():
    ok_chart = {"fields": [{"name": "渠道"}, {"name": "销量", "role": "metric"}],
                "rows": [{"渠道": "携程", "销量": 12}]}
    empty_chart = {"fields": [{"name": "渠道"}, {"name": "销量", "role": "metric"}], "rows": []}
    assert qbi_bridge._looks_like_no_data("未查询到相关数据,请换个问法", [])
    assert qbi_bridge._looks_like_no_data("该问题与数据集无关", [])
    assert not qbi_bridge._looks_like_no_data("", []), "空回答走原来那条判断"
    long = "近7日客流 12,345 人,环比上升 8%。" * 10 + "其中抖音渠道暂无数据。"
    assert not qbi_bridge._looks_like_no_data(long, []), "正经分析里顺带一句「暂无数据」不能转走"
    assert not qbi_bridge._looks_like_no_data("暂无数据", [ok_chart]), "有带数据的图就不算没数据"
    assert qbi_bridge._looks_like_no_data("暂无数据", [empty_chart]), "空图等于没图"


def test_pump_hands_off_no_data_answers_and_passes_issue():
    import inspect

    src = inspect.getsource(qbi_bridge.pump)
    assert "_looks_like_no_data(answer, charts)" in src
    assert "issue_id=issue_id" in src


def test_executor_honours_handoff_authorization():
    from pathlib import Path

    src = (Path(qbi_bridge.__file__).resolve().parents[1] / "runtime_service/engine/executor.py"
           ).read_text(encoding="utf-8")
    assert 'question_authorized=bool(payload.get("question_id") or payload.get("handoff_authorized"))' in src
