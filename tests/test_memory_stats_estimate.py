"""
上下文记忆 / 工具统计 / 耗时预估。

**这三个模块都用 `except: log.debug` 吞异常**(写失败不该让一次本来成功的
对话或工具调用变成失败)。代价是出问题时完全静默 —— 合入时就踩了两次:

1. 唯一约束只写在迁移 SQL 里、没写进模型,全新库走 create_all 时索引丢失,
   upsert 的 ON CONFLICT 找不到目标 → **记忆写入全部静默失败**
2. 改成 `ON CONFLICT ON CONSTRAINT` 后又挂了:库里 uq_ctx_mem 是
   CREATE UNIQUE INDEX 建的**索引**,而 ON CONSTRAINT 只认**约束**,
   两者在 PG 里不等价

所以这里必须有**真的写进库再读出来**的测试,光断言函数不抛异常没有意义。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.control_plane_service import memory as mem
from cogni_agent.runtime_service.engine import estimate as est
from cogni_agent.runtime_service.engine import planner
from cogni_agent.tool_center_service.tool import stats

pytestmark = pytest.mark.asyncio

C, U, T = "t_conv", "t_user", "t_tool"


@pytest_asyncio.fixture
async def db():
    import cogni_agent.common.db as dbmod

    dbmod._engine = None
    dbmod._SessionLocal = None

    async def _clean(s):
        await s.execute(delete(mem.ContextMemory).where(mem.ContextMemory.owner_id.in_([C, U])))
        await s.execute(delete(stats.ToolDailyStat).where(stats.ToolDailyStat.tool_id == T))
        await s.commit()

    try:
        async with get_sessionmaker()() as s:
            await _clean(s)
            yield s
            await _clean(s)
    finally:
        if dbmod._engine is not None:
            await dbmod._engine.dispose()
        dbmod._engine = None
        dbmod._SessionLocal = None


# ── 记忆 ──
async def test_memory_actually_persists(db):
    """**真的写进库再读出来。** 这三个模块都吞异常,
    只断言「不抛异常」的话,写失败会完全静默地通过测试。"""
    await mem.remember(scope="user", owner_id=U, label="用户角色", value="销售总监")
    items = await mem.recall(db, conversation_id=None, user_id=U)
    assert [i["value"] for i in items] == ["销售总监"], "记忆没有真正落库"


async def test_same_label_overwrites(db):
    """「关注周期」只该有一个值 —— 用户说了 Q3 就该覆盖 Q2,
    两条并存会让模型自己猜用哪个。"""
    await mem.remember(scope="conversation", owner_id=C, label="关注周期", value="Q2")
    await mem.remember(scope="conversation", owner_id=C, label="关注周期", value="Q3")
    items = await mem.recall(db, conversation_id=C, user_id=None)
    assert len(items) == 1 and items[0]["value"] == "Q3"


async def test_user_scope_before_conversation(db):
    """对话级更具体,放后面让模型优先采信 ——
    「这次只看华南区」应该盖过「一般关注全国」。"""
    await mem.remember(scope="user", owner_id=U, label="范围", value="全国")
    await mem.remember(scope="conversation", owner_id=C, label="本次范围", value="华南")
    items = await mem.recall(db, conversation_id=C, user_id=U)
    scopes = [i["scope"] for i in items]
    assert scopes.index("user") < scopes.index("conversation")


async def test_prompt_block_warns_conflict(db):
    """必须写明「以本次为准」,否则旧偏好会压过用户这轮明确说的话。"""
    block = mem.as_prompt_block([{"label": "角色", "value": "总监"}])
    assert "已知上下文" in block and "本次为准" in block
    assert mem.as_prompt_block([]) == "", "没有记忆时不该留一个空标题"


async def test_forget_removes(db):
    """记错的信息会一直污染后续对话,必须能逐条清除。"""
    await mem.remember(scope="conversation", owner_id=C, label="错的", value="x")
    items = await mem.recall(db, conversation_id=C, user_id=None)
    await mem.forget(db, items[0]["id"])
    assert not await mem.recall(db, conversation_id=C, user_id=None)


# ── 统计 ──
async def test_stats_accumulate(db):
    """并发调同一工具时必须 upsert 累加 —— 「先查再更新」会互相覆盖,计数会丢。"""
    for ok, ms in [(True, 400), (True, 600), (False, 100)]:
        await stats.record_call(T, scenic_id="s1", ok=ok, duration_ms=ms)
    t = (await stats.totals_by_tool(db, days=7, scenic_id="s1"))[T]
    assert t["call_count"] == 3 and t["success_count"] == 2
    assert t["avg_duration_ms"] == 366           # (400+600+100)/3
    assert t["success_rate"] == pytest.approx(2 / 3, abs=1e-4)


async def test_trend_fills_gaps(db):
    """没有调用的日期必须补零 —— 直接按数据分组的话那天会整天消失,
    折线图把两个不相邻日期连起来,看着像「中间一直有量」。"""
    await stats.record_call(T, scenic_id="s1", ok=True, duration_ms=10)
    tr = await stats.daily_trend(db, days=5, scenic_id="s1")
    assert len(tr) == 5 and tr[-1]["calls"] == 1
    assert all(p["calls"] == 0 for p in tr[:-1])
    assert [p["date"] for p in tr] == sorted(p["date"] for p in tr), "日期要有序"


async def test_stats_bad_input_is_safe(db):
    """统计写失败不该让一次本来成功的工具调用变成失败。"""
    await stats.record_call("", scenic_id=None, ok=True, duration_ms=-5)


# ── 预估 ──
def _plan():
    return planner.ExecutionPlan(steps=[
        planner.PlanStep(id="1", title="查", kind=planner.KIND_TOOL, tool_name=T),
        planner.PlanStep(id="2", title="算", kind=planner.KIND_ANALYZE),
        planner.PlanStep(id="3", title="答", kind=planner.KIND_COMPOSE),
    ])


async def test_estimate_marks_no_history():
    """没有历史数据时必须说明,否则用户会把它当成靠谱的承诺 ——
    按 5 秒等、结果跑了 40 秒,下次就再也不信这个数字了。"""
    e = est.estimate_plan(_plan(), tool_stats_by_name={})
    assert e.confidence == "default" and "仅供参考" in e.text


async def test_estimate_tighter_with_history():
    """有实测数据区间可以窄一些;没有时宁可给宽的,
    也不报一个自己都不信的窄区间。"""
    m = est.estimate_plan(_plan(), tool_stats_by_name={T: {"avg_duration_ms": 366}})
    d = est.estimate_plan(_plan(), tool_stats_by_name={})
    assert m.confidence == "measured" and "仅供参考" not in m.text
    assert (m.high_seconds - m.low_seconds) < (d.high_seconds - d.low_seconds)


async def test_estimate_range_is_sane():
    """报「17-31 秒」这种精确度是假的;「5-5 秒」这种退化区间很怪。"""
    for st in ({}, {T: {"avg_duration_ms": 10}}):
        e = est.estimate_plan(_plan(), tool_stats_by_name=st)
        assert e.low_seconds % 5 == 0 and e.high_seconds % 5 == 0
        assert e.high_seconds > e.low_seconds


async def test_unique_constraint_declared_on_model():
    """唯一约束必须声明在**模型**上,不能只写在迁移 SQL 里 ——
    全新库走 create_all 建表时不执行迁移,索引会丢,
    而 upsert 的 ON CONFLICT 依赖它,结果是记忆写入全部静默失败。"""
    cols = {tuple(sorted(c.name for c in uc.columns))
            for uc in mem.ContextMemory.__table__.constraints
            if uc.__class__.__name__ == "UniqueConstraint"}
    assert ("label", "owner_id", "scope") in cols
