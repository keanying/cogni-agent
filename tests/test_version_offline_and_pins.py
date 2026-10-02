# -*- coding: utf-8 -*-
"""下线、指派与「跟随最新」绑定。

三件事,现场各自卡了人:

1. **引用提示只给 UUID**。「v4 正被 1 处引用:用户指派(user:1898973f-…)」——
   这条提示的全部作用就是告诉运营「去把谁的指派解掉」,而那串 id 他查不到是谁;
2. **技能跑得动却发不出去**。运行时早就把未指定版本的绑定解析到最新已发布版,
   而发布预检一律拦截,报「默认版本已下线(15 号)」—— 运营从没设过什么默认版本,
   括号里的日期对他更是毫无意义;
3. **下线被指派挡死**。下线的意思本来就是「这一版不再对外」,顺手收回权限正是本意;
   让人先一个个解指派再回来下线,中间那段时间版本还在线上跑,而他以为已经下了。

但第 3 件有一条必须守住:**被别的已发布版本 pin 的不能自动解**。那是另一个 Agent
正在执行的内容,悄悄改掉不报错 —— 下一次有人用那个 Agent,跑的已经不是他验收过的东西。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.tool_center_service import versioning as ver

SKILL = "sk-1111"
TOOL = "tl-2222"


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
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_res_ver_no "
                "ON numeri_core_tc_resource_versions (resource_type, resource_id, version)")
    asyncio.run(reset())
    yield


async def _seed_user(db, uid="u-9", name="尹开英", phone="13800000000"):
    from cogni_agent.user_center_service.models import User

    db.add(User(id=uid, name=name, account="yky", phone=phone,
                password_hash="x", role="scenic_admin", is_active=True))


async def _seed_tool(db, published_versions=(1,)):
    from cogni_agent.tool_center_service.versioning import ResourceVersion
    from cogni_agent.tool_center_service.tool.models import Tool

    # Tool 主表只有身份/门面列,内容(type/描述/配置)全在版本快照里(0051 起)
    db.add(Tool(id=TOOL, name="雷达图指标", owner_user_id="u-9", visibility="group"))
    for v in published_versions:
        db.add(ResourceVersion(resource_type="tool", resource_id=TOOL, version=v,
                               status="published", snapshot={"name": "雷达图指标"}))


async def _seed_skill_draft(db, tool_version=None):
    """建一个绑定了工具的技能草稿。tool_version=None 表示「跟随最新」。"""
    from cogni_agent.tool_center_service.versioning import ResourceVersion
    from cogni_agent.tool_center_service.skill.models import Skill

    db.add(Skill(id=SKILL, name="we", owner_user_id="u-9", visibility="group",
                 tool_ids=[TOOL]))
    row = ResourceVersion(
        resource_type="skill", resource_id=SKILL, version=None, status="draft",
        snapshot={"name": "we", "description": "把指标画成雷达图",
                  "constraints": {"max_steps": 20},
                  "tools": [{"tool_id": TOOL, "version": tool_version}]})
    db.add(row)
    await db.flush()
    return row.id


# ══════════════════════════════════════════════════════════════════════
# 一、「跟随最新」的绑定可以发布
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_unpinned_binding_publishes_against_the_latest_published():
    """**跑得动却发不出去** —— 运行时解析到最新已发布版,发布预检却一律拦。

    而「跟随最新」本身是合理诉求:工具修了 bug 发了新版,绑它的十几个技能
    不该为此每个都进去重新选一遍版本。
    """
    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db)
        await _seed_tool(db, published_versions=(1, 2, 3))
        draft_id = await _seed_skill_draft(db, tool_version=None)
        await db.commit()

        out = await ver.publish_version(db, draft_id)
        assert out["version"] == 1
        row = await ver.get_version(db, draft_id)
        pins = row.snapshot["tools"]
        assert pins[0]["version"] == 3, "应固化为当时最新的已发布版 v3"


@run_async
async def test_publish_precheck_reports_no_problem_for_unpinned():
    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db)
        await _seed_tool(db, published_versions=(1,))
        draft_id = await _seed_skill_draft(db, tool_version=None)
        await db.commit()
        pre = await ver.precheck_version(db, draft_id)
    assert pre["pass"] is True, pre["problems"]


@run_async
async def test_tool_without_any_published_version_is_still_blocked():
    """真正该拦的只有这一种。而且话要说清「去哪儿发」——
    只说「不可用」的话,运营会以为是绑定填错了,回去反复改绑定。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db)
        await _seed_tool(db, published_versions=())
        draft_id = await _seed_skill_draft(db, tool_version=None)
        await db.commit()
        pre = await ver.precheck_version(db, draft_id)
    assert pre["pass"] is False
    msg = " ".join(pre["problems"])
    assert "还没有任何已发布版本" in msg and "发布一版" in msg
    assert "默认版本" not in msg, "别再提那个运营从没设过的「默认版本」"


@run_async
async def test_explicit_pin_still_wins():
    """要钉死版本的人显式选版,两种都要支持 —— 不能为了支持「跟随最新」
    把显式指定也改成跟着最新跑,那等于没法固定版本了。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db)
        await _seed_tool(db, published_versions=(1, 2, 3))
        draft_id = await _seed_skill_draft(db, tool_version=2)
        await db.commit()
        await ver.publish_version(db, draft_id)
        row = await ver.get_version(db, draft_id)
    assert row.snapshot["tools"][0]["version"] == 2


# ══════════════════════════════════════════════════════════════════════
# 二、引用提示要说人话
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_reference_note_shows_the_person_not_a_uuid():
    """「用户指派(user:1898973f-…)」—— 这条提示的全部作用就是告诉运营
    该去解谁的指派,而那串 id 他查不到是谁。"""
    from cogni_agent.tool_center_service.versioning import ResourceAssignment, ResourceVersion

    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db, uid="u-9", name="尹开英", phone="13800000000")
        v = ResourceVersion(resource_type="skill", resource_id=SKILL, version=4,
                            status="published", snapshot={"name": "we"})
        db.add(v)
        await db.flush()
        db.add(ResourceAssignment(resource_type="skill", agent_id=SKILL, version_id=v.id,
                                  scope_type="user", scope_id="u-9", created_by="t"))
        await db.commit()
        refs = await ver.referenced_by(db, v)

    assert len(refs) == 1
    assert "尹开英" in refs[0]["note"], refs[0]["note"]
    assert "13800000000" in refs[0]["note"]
    assert refs[0]["user_label"] and refs[0]["scope_id"] == "u-9"


@run_async
async def test_unknown_user_falls_back_to_the_id():
    """查不到人时回落 id,而不是写「未知用户」—— id 至少还能拿去库里查,
    「未知用户」什么线索都不给。"""
    from cogni_agent.tool_center_service.versioning import ResourceAssignment, ResourceVersion

    async with common_db.get_sessionmaker()() as db:
        v = ResourceVersion(resource_type="skill", resource_id=SKILL, version=1,
                            status="published", snapshot={})
        db.add(v)
        await db.flush()
        db.add(ResourceAssignment(resource_type="skill", agent_id=SKILL, version_id=v.id,
                                  scope_type="user", scope_id="u-gone", created_by="t"))
        await db.commit()
        refs = await ver.referenced_by(db, v)
    assert "u-gone" in refs[0]["note"]


# ══════════════════════════════════════════════════════════════════════
# 三、下线:自动收回权限,但不改别人正在跑的东西
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_offline_releases_user_assignments_and_says_who():
    """下线的意思本来就是「这一版不再对外」,顺手收回权限正是运营的本意。

    但**必须说清收回了谁** —— 只说「已解除 3 处指派」的话,被收回的人来问
    「我为什么用不了了」时,运营翻不出是哪一次操作动的他。
    """
    from cogni_agent.tool_center_service.versioning import ResourceAssignment, ResourceVersion
    from sqlalchemy import select

    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db, uid="u-9", name="尹开英", phone="13800000000")
        v = ResourceVersion(resource_type="skill", resource_id=SKILL, version=4,
                            status="published", snapshot={"name": "we"})
        db.add(v)
        await db.flush()
        db.add(ResourceAssignment(resource_type="skill", agent_id=SKILL, version_id=v.id,
                                  scope_type="user", scope_id="u-9", created_by="t"))
        await db.commit()

        out = await ver.offline_version(db, v.id)
        assert out["ok"] is True
        assert "尹开英" in out["hint"], out["hint"]
        assert out["released_assignments"][0]["scope_id"] == "u-9"

        left = (await db.execute(select(ResourceAssignment).where(
            ResourceAssignment.version_id == v.id))).scalars().all()
        assert left == [], "指派要真的删掉,不然权限并没有收回"
        assert (await ver.get_version(db, v.id)).status == ver.STATUS_OFFLINE


@run_async
async def test_offline_refuses_when_a_published_version_pins_it():
    """**这条是自动解除的边界。**

    被别的已发布版本 pin 的,是另一个 Agent 正在执行的内容。自动解掉等于悄悄改了
    别人 Agent 的行为,而且改完不报错 —— 下一次有人用那个 Agent,
    跑的已经不是他验收过的东西了。
    """
    from cogni_agent.common.errors import Forbidden
    from cogni_agent.tool_center_service.versioning import ResourceVersion

    async with common_db.get_sessionmaker()() as db:
        tv = ResourceVersion(resource_type="tool", resource_id=TOOL, version=2,
                             status="published", snapshot={"name": "雷达图指标"})
        db.add(tv)
        db.add(ResourceVersion(resource_type="skill", resource_id=SKILL, version=1,
                               status="published",
                               snapshot={"tools": [{"tool_id": TOOL, "version": 2}]}))
        await db.commit()
        with pytest.raises(Forbidden) as e:
            await ver.offline_version(db, tv.id)
    assert "已发布" in str(e.value) and "改绑" in str(e.value)


@run_async
async def test_offline_ignores_pins_from_draft_or_offline_versions():
    """草稿/已下线版本上的 pin 本来就没在跑。拦了只会让人卡在一条早就作废的引用上,
    而那条引用他多半根本找不到在哪。"""
    from cogni_agent.tool_center_service.versioning import ResourceVersion

    async with common_db.get_sessionmaker()() as db:
        tv = ResourceVersion(resource_type="tool", resource_id=TOOL, version=2,
                             status="published", snapshot={})
        db.add(tv)
        db.add(ResourceVersion(resource_type="skill", resource_id=SKILL, version=None,
                               status="draft",
                               snapshot={"tools": [{"tool_id": TOOL, "version": 2}]}))
        db.add(ResourceVersion(resource_type="skill", resource_id=SKILL, version=1,
                               status="offline",
                               snapshot={"tools": [{"tool_id": TOOL, "version": 2}]}))
        await db.commit()
        out = await ver.offline_version(db, tv.id)
    assert out["ok"] is True


@run_async
async def test_offline_still_warns_when_nothing_is_left_online():
    from cogni_agent.tool_center_service.versioning import ResourceVersion

    async with common_db.get_sessionmaker()() as db:
        v = ResourceVersion(resource_type="skill", resource_id=SKILL, version=1,
                            status="published", snapshot={})
        db.add(v)
        await db.commit()
        out = await ver.offline_version(db, v.id)
    assert "均已下线" in out["hint"]


@run_async
async def test_multiple_versions_can_stay_online():
    """多版本在线:发布 v2 不会把 v1 顶下去 —— 这是「跟随最新」能成立的前提。"""
    from cogni_agent.tool_center_service.versioning import ResourceVersion
    from sqlalchemy import select

    async with common_db.get_sessionmaker()() as db:
        await _seed_user(db)
        await _seed_tool(db, published_versions=(1,))
        d1 = await _seed_skill_draft(db, tool_version=None)
        await db.commit()
        await ver.publish_version(db, d1)

        d2 = await ver.derive_version(db, "skill", SKILL, base_version_id=d1,
                                      snapshot={"name": "we", "description": "改一版",
                                                "constraints": {"max_steps": 20},
                                                "tools": [{"tool_id": TOOL, "version": None}]},
                                      created_by="u-9")
        await ver.publish_version(db, d2.id)

        online = (await db.execute(select(ResourceVersion.version).where(
            ResourceVersion.resource_type == "skill",
            ResourceVersion.resource_id == SKILL,
            ResourceVersion.status == "published"))).scalars().all()
    assert sorted(online) == [1, 2], "两版都该在线"


# ══════════════════════════════════════════════════════════════════════
# 四、窄屏不变形
# ══════════════════════════════════════════════════════════════════════

def test_distribution_tabs_do_not_deform_on_narrow_screens():
    """现场截图:四个 Tab 的标签被逐字拆成「按 / Agent / 管理」竖排,和标题挤在一起。

    根因是左边那段很长的说明和右边的 Tab 组并排,而 Tab 组允许被压缩。
    三条一起才治得住 —— 少一条都还会变形:
      文字块 min-w-0(允许自己收缩)、Tab 组 shrink-0(不许被压)、
      按钮 whitespace-nowrap(不许拆词);真放不下时整行横向滚动。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "web" / "src" / "pages" / "Tools"
           / "AgentDistribution.tsx").read_text(encoding="utf-8")
    head = src[src.index("分发中心"):src.index("{view === 'user'")]
    assert "min-w-0" in head, "说明文字必须允许收缩,否则它会把 Tab 挤扁"
    assert "shrink-0" in head, "Tab 组不能被压缩"
    assert "whitespace-nowrap" in head, "标签不能拆词换行"
    assert "overflow-x-auto" in head, "真的放不下时横向滚动,而不是变形"
    assert "xl:flex-row" in head, "md(768px)并排仍然太挤,断点要放到 xl"


def test_user_distribution_filter_tabs_are_protected_too():
    """同一页的另一处 Tab(按资源类型筛选)有同样的问题:
    「Quick BI」中间那个空格会被当成换行点,拆成两行。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "web" / "src" / "pages" / "Tools"
           / "UserDistribution.tsx").read_text(encoding="utf-8")
    seg = src[src.index("资源类型筛选"):src.index("资源类型筛选") + 1400]
    assert "whitespace-nowrap" in seg and "overflow-x-auto" in seg
