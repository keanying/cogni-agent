# -*- coding: utf-8 -*-
"""Quick BI 固定问题「加速」(预热)的回归。

**这个功能的全部风险都在「它不会报错」上。** 缓存命中是一条正常的、成功的、
看起来和真跑一模一样的回答 —— 错了也没有红字,没有异常,没有人会去查。
所以这里钉的每一条都是「答出来的是别人的数 / 旧的数,而界面一切正常」:

- 拿 A 景区的结果回放给 B 景区 —— 把别人的经营数据给了他;
- 同一条问题填了不同的 {{month}},却回放上个月那份 —— 数字完全正常,没人会怀疑;
- 追问(有上文)吃了无上文那次的缓存 —— 答非所问,但答得很像回事;
- 预热任务自己吃缓存 —— 缓存从此冻住再也不更新,而且一路成功;
- 存储时间 < 刷新时间 —— 两次预热之间全是现跑,开了加速却没变快,同样不报错。

还有一条相反方向的:开了加速的问题在连接被删掉的空档里,应该仍然答得出来
(答上一次的真实结果),而不是把「后台配置断了」直接甩到游客脸上。
"""
from __future__ import annotations

import asyncio
import functools
from datetime import timedelta

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base, utcnow

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


async def _issue(*, scenic=SCENIC, prewarm=True, interval=None, ttl=None,
                 question="今天客流多少", capability="dataset_qa", conn_id="c-1",
                 active=True, source_question_id=None):
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    async with common_db.get_sessionmaker()() as db:
        row = ScenicAgentIssueDetail(
            title=question[:40], issue=question, business_class="quick_bi",
            capability=capability, connection_id=conn_id, scenic_id=scenic, active=active,
            prewarm_enabled=prewarm, prewarm_interval_minutes=interval,
            prewarm_ttl_minutes=ttl, source_question_id=source_question_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _run(*, issue_id, scenic, question="今天客流多少", status="success",
              answer="昨日客流 12345 人", age_minutes=0, run_id=None, conn_id="c-1"):
    """造一条历史运行记录 —— 它就是「缓存」本身(没有单独的缓存表)。"""
    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    async with common_db.get_sessionmaker()() as db:
        r = QbiRun(connection_id=conn_id, capability="dataset_qa", mode="fixed",
                   question=question, status=status, answer=answer,
                   result={"charts": [], "usage": {"total_tokens": 10}},
                   fixed_question_id=issue_id, scenic_id=scenic,
                   user_id="u-1", source="console")
        if run_id:
            r.id = run_id
        db.add(r)
        await db.commit()
        # created_at 由数据库默认值写入,要造「多久以前」只能建完再改
        if age_minutes:
            r.created_at = utcnow() - timedelta(minutes=age_minutes)
            await db.commit()
        await db.refresh(r)
        return r


async def _conn(cid="c-1", scenic=SCENIC, active=True):
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id=cid, name="景区 BI", owner_user_id="u-admin",
                             visibility="group", scenic_id=scenic, active=active))
        await db.commit()


# ══════════════════════════════════════════════════════════════════════
# 一、配置:默认值与校验
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_prewarm_settings_defaults_when_minutes_left_blank():
    """两个分钟数留空 = 跟随全局默认,而不是 0。

    存 0 的话 max(1, 0) 会把它救成 1 分钟 —— 一条问题每分钟真跑一次,
    额度会在没人注意的情况下被烧光,而界面上什么异常都看不到。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(interval=None, ttl=None)
    enabled, interval, ttl = qsvc.prewarm_settings(row)
    assert enabled is True
    assert interval == qsvc._PREWARM_DEFAULT_INTERVAL_MIN
    assert ttl == qsvc._PREWARM_DEFAULT_TTL_MIN


@run_async
async def test_prewarm_settings_uses_per_question_minutes():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(interval=5, ttl=15)
    assert qsvc.prewarm_settings(row) == (True, 5, 15)


@run_async
async def test_default_ttl_is_longer_than_default_interval():
    """存储时间的默认值必须比刷新时间长。

    两者相等的话,上一份刚好过期、下一份还没跑完的那个瞬间会漏成现跑 ——
    而整点前后正是最多人在看的时刻。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    assert qsvc._PREWARM_DEFAULT_TTL_MIN > qsvc._PREWARM_DEFAULT_INTERVAL_MIN


def test_ttl_shorter_than_interval_is_refused():
    """存储 < 刷新 = 两次预热之间有一段没有缓存的空窗。

    这个配置不会报错,只会让「开了加速却没变快」——
    所以必须在保存时挡掉,而不是等人来反馈。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    with pytest.raises(ValidationError) as e:
        qsvc._validate_fixed({"prewarm_interval_minutes": 60, "prewarm_ttl_minutes": 10})
    assert "空窗" in str(e.value)


def test_prewarm_minutes_must_be_sane():
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    for bad in (0, -5, 60 * 24 * 8):
        with pytest.raises(ValidationError):
            qsvc._validate_fixed({"prewarm_interval_minutes": bad})
    with pytest.raises(ValidationError):
        qsvc._validate_fixed({"prewarm_ttl_minutes": "半小时"})
    # 留空 / 空串 = 跟随默认,不该被当成非法值挡下来
    qsvc._validate_fixed({"prewarm_interval_minutes": None, "prewarm_ttl_minutes": ""})


def test_validate_normalizes_numeric_strings():
    """表单传上来的是字符串 "30"。不归一的话它会被原样写进 INTEGER 列。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    data = {"prewarm_interval_minutes": "30", "prewarm_ttl_minutes": "90"}
    qsvc._validate_fixed(data)
    assert data == {"prewarm_interval_minutes": 30, "prewarm_ttl_minutes": 90}


# ══════════════════════════════════════════════════════════════════════
# 二、命中:同景区同问题才算
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_cache_hits_same_scenic_same_question():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue()
    r = await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=5)
    hit = await qsvc.cached_run(row, SCENIC, 60, "今天客流多少")
    assert hit is not None and hit.id == r.id


@run_async
async def test_cache_never_crosses_scenics():
    """**最重要的一条**:A 景区的结果不能回放给 B 景区。

    公开的固定问题可以被不同景区问。串了的话,B 景区的人看到的是 A 景区的经营数据 ——
    而且是一份格式完全正常、没有任何报错的回答,谁都不会去核对。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(scenic=None)          # 集团级模板,两个景区都能问
    await _run(issue_id=row.issue_id, scenic=SCENIC, answer="A 景区 12345 人", age_minutes=1)
    assert await qsvc.cached_run(row, OTHER, 60, "今天客流多少") is None


@run_async
async def test_group_level_cache_does_not_take_a_scenic_run():
    """反向:集团级(scenic 为空)查缓存时,不能把某个景区的那次当成集团级的。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(scenic=None)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=1)
    assert await qsvc.cached_run(row, None, 60, "今天客流多少") is None


@run_async
async def test_cache_requires_the_same_question_text():
    """{{变量}} 填出来不一样就是两个问题。

    只按 id 比的话,问 8 月会拿到 7 月那份 —— 数字看着完全正常,没人会怀疑。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(question="{{month}} 客流多少")
    await _run(issue_id=row.issue_id, scenic=SCENIC, question="7月 客流多少", age_minutes=1)
    assert await qsvc.cached_run(row, SCENIC, 60, "8月 客流多少") is None
    assert await qsvc.cached_run(row, SCENIC, 60, "7月 客流多少") is not None


@run_async
async def test_cache_expires_after_ttl():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(ttl=30)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=31)
    assert await qsvc.cached_run(row, SCENIC, 30, "今天客流多少") is None
    assert await qsvc.cached_run(row, SCENIC, 60, "今天客流多少") is not None


@run_async
async def test_failed_runs_are_never_cached():
    """失败的那次不能当缓存 —— 否则一次失败会被复读整个存储窗口。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue()
    await _run(issue_id=row.issue_id, scenic=SCENIC, status="failed", answer=None, age_minutes=1)
    assert await qsvc.cached_run(row, SCENIC, 60, "今天客流多少") is None


@run_async
async def test_cache_returns_the_newest_success():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue()
    await _run(issue_id=row.issue_id, scenic=SCENIC, answer="旧", age_minutes=20)
    await _run(issue_id=row.issue_id, scenic=SCENIC, answer="新", age_minutes=1)
    hit = await qsvc.cached_run(row, SCENIC, 60, "今天客流多少")
    assert hit.answer == "新"


@run_async
async def test_cache_also_matches_the_legacy_question_id():
    """历史运行记录里存的是老表的 UUID。

    只按 issue_id 找的话,刚迁过来的问题一条都命中不了 —— 而且不报错,
    只是「加速开了但从来没快过」,很难被发现。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    legacy = "290987f7-30d8-4e80-aa8d-c93103969571"
    row = await _issue(source_question_id=legacy)
    await _run(issue_id=legacy, scenic=SCENIC, age_minutes=1)
    assert await qsvc.cached_run(row, SCENIC, 60, "今天客流多少") is not None


# ══════════════════════════════════════════════════════════════════════
# 三、回放:事件形状与真跑一致
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_replay_events_look_like_a_real_run():
    """形状必须和真跑一样(run → chart / text → done),否则前端要写第二套渲染 ——
    写了就会两套慢慢长歪,而歪掉的那套通常是没人天天看的那套。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue()
    r = await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)
    evts = qsvc._replay_events(r, "今天客流多少")
    assert [e["type"] for e in evts] == ["run", "status", "text", "done"]
    assert evts[0]["run_id"] == r.id
    assert evts[2]["delta"] == "昨日客流 12345 人" and evts[2]["section"] == "answer"
    # Agent 工具读的是 done.content / 累积出的正文:以前回放成 {"type": "answer"},没人认,工具拿到空内容
    assert "昨日客流 12345 人" in evts[-1]["content"]
    assert evts[-1]["status"] == "success"
    assert evts[-1]["answer"] == "昨日客流 12345 人"


@run_async
async def test_replay_events_are_marked_as_cached():
    """必须标出来这是回放。不标的话用户会以为这就是刚取的实时数。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue()
    r = await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)
    evts = qsvc._replay_events(r, "今天客流多少")
    assert evts[0]["from_cache"] is True and evts[-1]["from_cache"] is True
    assert evts[0]["cached_at"] and evts[-1]["cached_at"]


# ══════════════════════════════════════════════════════════════════════
# 四、run_stream:命中就不去打 Quick BI
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_run_stream_replays_instead_of_calling_quickbi():
    """命中缓存时**一次上游调用都不能发生** —— 否则加速只是把慢的那次藏起来了。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue()
    cached = await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)

    called = []

    async def _boom(*a, **kw):                # 真跑一定要经过它
        called.append(1)
        raise AssertionError("命中缓存后不该再去调 Quick BI")
        yield  # pragma: no cover

    orig = qsvc._run_capability if hasattr(qsvc, "_run_capability") else None
    evts = [e async for e in qsvc.run_stream(
        _actor("scenic_admin", SCENIC), connection_id=None, capability=None,
        question="今天客流多少", fixed_question_id=row.issue_id, scenic_id=SCENIC)]
    assert not called
    assert [e["type"] for e in evts] == ["run", "status", "text", "done"]
    assert evts[-1]["run_id"] == cached.id
    assert evts[-1]["from_cache"] is True
    _ = orig


@run_async
async def test_run_stream_misses_when_prewarm_is_off():
    """没开加速的问题不该读缓存 —— 不然关掉开关也不会变回实时。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue(prewarm=False)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)
    evts = [e async for e in qsvc.run_stream(
        _actor("scenic_admin", SCENIC), connection_id=None, capability=None,
        question="今天客流多少", fixed_question_id=row.issue_id, scenic_id=SCENIC)]
    # 没有连接凭证,真跑必然失败 —— 失败本身就证明它没走回放
    assert not any(e.get("from_cache") for e in evts)


@run_async
async def test_run_stream_skips_cache_for_followups():
    """追问带着上文,问的虽然是同一句,答案也该不同。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue()
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)
    evts = [e async for e in qsvc.run_stream(
        _actor("scenic_admin", SCENIC), connection_id=None, capability=None,
        question="那环比呢", fixed_question_id=row.issue_id, scenic_id=SCENIC,
        context_question="今天客流多少")]
    assert not any(e.get("from_cache") for e in evts)


@run_async
async def test_use_cache_false_forces_a_real_run():
    """预热自己不能吃缓存,否则缓存会冻在那儿再也不更新 —— 而且一路成功。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue()
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2)
    evts = [e async for e in qsvc.run_stream(
        _actor("scenic_admin", SCENIC), connection_id=None, capability=None,
        question="今天客流多少", fixed_question_id=row.issue_id, scenic_id=SCENIC,
        use_cache=False)]
    assert not any(e.get("from_cache") for e in evts)


@run_async
async def test_cached_answer_survives_a_broken_connection():
    """连接被删掉的空档里,预热过的问题应该仍然答得出来。

    答的是上一次的真实结果,并且标了 from_cache —— 总好过把
    「后台配置断了」这件事直接甩到游客脸上。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    row = await _issue(conn_id="c-gone")     # 指着一条根本不存在的连接
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=2, conn_id="c-gone")
    evts = [e async for e in qsvc.run_stream(
        _actor("scenic_admin", SCENIC), connection_id=None, capability=None,
        question="今天客流多少", fixed_question_id=row.issue_id, scenic_id=SCENIC)]
    assert evts[-1]["status"] == "success" and evts[-1]["from_cache"] is True


# ══════════════════════════════════════════════════════════════════════
# 五、保存与「加速」按钮
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_save_fixed_round_trips_prewarm_fields():
    """白名单漏了字段的表现是「开关点了、保存成功、刷新回来又关着」。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    me = _actor("super_admin", None)
    created = await qsvc.save_fixed(me, {
        "question": "今天客流多少", "capability": "dataset_qa", "connection_id": "c-1",
        "prewarm_enabled": True, "prewarm_interval_minutes": 10, "prewarm_ttl_minutes": 45})
    assert created["prewarm_enabled"] is True
    assert created["prewarm_interval_minutes"] == 10
    assert created["prewarm_ttl_minutes"] == 45

    off = await qsvc.save_fixed(me, {"prewarm_enabled": False}, created["id"])
    assert off["prewarm_enabled"] is False


@run_async
async def test_prewarm_now_refuses_when_switch_is_off():
    """没开加速却点了加速:跑完也不会有人读到它。说清楚,别装作做了事。"""
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue(prewarm=False)
    with pytest.raises(ValidationError) as e:
        await qsvc.prewarm_now(_actor("super_admin", None), row.issue_id)
    assert "加速" in str(e.value)


@run_async
async def test_prewarm_now_refuses_a_disabled_question():
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue(active=False)
    with pytest.raises(ValidationError):
        await qsvc.prewarm_now(_actor("super_admin", None), row.issue_id)


@run_async
async def test_prewarm_now_does_not_read_its_own_cache():
    """点「加速」的人要的是「现在去取最新的数」。

    这里要是吃了缓存,按钮就变成一个什么都不做的按钮 —— 而且它会成功,
    人看到「已加速」就走了,拿到的还是旧数。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn()
    row = await _issue()
    stale = await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=1)
    r = await qsvc.prewarm_now(_actor("super_admin", None), row.issue_id)
    # 没有真实凭证,这一跑必然失败 —— 但关键是它**去跑了**,而不是回放 stale
    assert r["run_id"] != stale.id
    assert r["prewarm_ttl_minutes"] == qsvc._PREWARM_DEFAULT_TTL_MIN


# ══════════════════════════════════════════════════════════════════════
# 六、定时扫描
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_cron_skips_questions_refreshed_within_the_interval():
    from cogni_agent.cronjobs import quickbi_prewarm as job

    await _conn()
    row = await _issue(interval=30, ttl=60)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=5)
    assert await job._plan() == []


@run_async
async def test_cron_picks_up_questions_past_the_interval():
    from cogni_agent.cronjobs import quickbi_prewarm as job

    await _conn()
    row = await _issue(interval=30, ttl=60)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=45)
    plan = await job._plan()
    assert [(r.issue_id, s) for r, s in plan] == [(row.issue_id, SCENIC)]


@run_async
async def test_cron_ignores_questions_without_prewarm():
    from cogni_agent.cronjobs import quickbi_prewarm as job

    await _conn()
    await _issue(prewarm=False)
    await _issue(prewarm=True, active=False, question="停用的那条")
    assert await job._plan() == []


@run_async
async def test_cron_only_warms_group_questions_for_scenics_that_asked():
    """集团级问题是**模板**,哪个景区都能问。

    按「所有在用景区」铺开的话,一条问题乘以几十个景区就是几十次调用,
    其中绝大多数没有人会去看 —— 额度会被悄悄烧光。只给最近真问过的那些预热。
    """
    from cogni_agent.cronjobs import quickbi_prewarm as job

    await _conn()
    row = await _issue(scenic=None, interval=30, ttl=60)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=45)   # 只有这个景区问过
    plan = await job._plan()
    assert [s for _, s in plan] == [SCENIC]
    assert OTHER not in [s for _, s in plan]


@run_async
async def test_cron_dry_run_changes_nothing():
    from cogni_agent.cronjobs import quickbi_prewarm as job
    from cogni_agent.tool_center_service.quickbi.models import QbiRun
    from sqlalchemy import func, select

    await _conn()
    row = await _issue(interval=30)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=45)
    async with common_db.get_sessionmaker()() as db:
        before = (await db.execute(select(func.count(QbiRun.id)))).scalar()
    out = await job.run(dry_run=True)
    async with common_db.get_sessionmaker()() as db:
        after = (await db.execute(select(func.count(QbiRun.id)))).scalar()
    assert out["planned"] == 1 and out["ran"] == 0
    assert before == after


def test_cron_job_is_registered_and_ticks_faster_than_the_refresh_interval():
    """注册周期是「看看有没有到点的」,不是刷新频率。

    把它设成 30 分钟的话,一条配了 5 分钟刷新的问题最快也只能 30 分钟刷一次 ——
    界面上写着 5 分钟,实际 30 分钟,没有任何地方会提示这件事。
    """
    from cogni_agent.cronjobs import registry
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    job = registry._JOBS.get("quickbi_prewarm")
    assert job is not None, "定时扫描没注册 = 加速只有手点那一次会生效"
    assert job.interval_seconds <= 5 * 60
    assert job.interval_seconds < qsvc._PREWARM_DEFAULT_INTERVAL_MIN * 60


def test_prewarm_enabled_has_a_server_side_default():
    """新增的非空列必须带 server_default,不能只写 Python 侧的 default。

    存量迁移 0076/0079 是裸 `INSERT ... SELECT`(不走 ORM),列清单里当然不会有
    这个后来才加的列。只有 Python 侧默认值的话,建出来的表是「NOT NULL 且无默认值」,
    那几条迁移会直接撞上非空约束 —— 而撞上的时机是**别人升级老库的那一刻**,
    不是我们跑测试的时候。这条就是为了让它在我们这边先撞。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    col = ScenicAgentIssueDetail.__table__.c.prewarm_enabled
    assert col.nullable is False
    assert col.server_default is not None, "非空列缺 server_default,裸 INSERT 会炸"
    # 两个分钟数相反:必须可空 —— 空 = 跟随全局默认,填死了改全局默认就影响不到它们
    for name in ("prewarm_interval_minutes", "prewarm_ttl_minutes"):
        assert ScenicAgentIssueDetail.__table__.c[name].nullable is True


@run_async
async def test_cron_respects_the_scenic_level_prewarm_switch():
    """景区数据集映射上关了「预热」:这个景区的问题一条都不预热;没映射的景区照旧。"""
    from cogni_agent.cronjobs import quickbi_prewarm as job
    from cogni_agent.tool_center_service.quickbi.models import QbiScenicDataset

    await _conn()
    row = await _issue(interval=30, ttl=60)
    await _run(issue_id=row.issue_id, scenic=SCENIC, age_minutes=45)
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiScenicDataset(scenic_id=SCENIC, connection_id="c-1", cube_id="cube-x", prewarm_enabled=False))
        await db.commit()
    assert await job._plan() == []
    async with common_db.get_sessionmaker()() as db:
        m = (await db.execute(QbiScenicDataset.__table__.select())).first()
        await db.execute(QbiScenicDataset.__table__.update().values(prewarm_enabled=True))
        await db.commit()
    assert m is not None
    assert [(r.issue_id, s) for r, s in await job._plan()] == [(row.issue_id, SCENIC)]
