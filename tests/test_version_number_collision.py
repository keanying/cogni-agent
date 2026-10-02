# -*- coding: utf-8 -*-
"""版本号分配:下线过的版本号不能被再发一次。

现场报的是一句数据库原话:

    duplicate key value violates unique constraint "uq_res_ver_no"
    DETAIL: Key (resource_type, resource_id, version)=(skill, bc3a972a-…, 1) already exists.

页面上的表现是「发布」点下去转个圈,然后 500 —— 技能改好了却发不出去,
而报错里全是 SQLAlchemy 的调用栈,运营完全看不出该做什么。

根因是两套口径对不上:
  - 唯一索引 `uq_res_ver_no` 管的是**全部行**(resource_type, resource_id, version);
  - 而 `_next_version_no` 只数 `status='published'` 的行。
把 v1 下线之后,published 一条都不剩,下一次发布又算出 1 —— 而那个 1 还占在
那条已下线的行上。于是「发布过、下线过、再发布」这条完全正常的运维路径必炸。

草稿不占号这件事不受影响:草稿的 version 是 NULL,而 Postgres 的唯一索引不约束 NULL,
`max(version)` 也自然跳过它们 —— 所以改成数全部行,既补上下线这一档,
又保留「派生草稿再丢弃不留版本空洞」。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base

SKILL_ID = "bc3a972a-8534-4185-8d86-23f70f2eebd9"      # 现场那条


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
            # 唯一索引由迁移建,建表时不会带上 —— 这条用例的全部意义就是它,得手动补
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_res_ver_no "
                "ON numeri_core_tc_resource_versions (resource_type, resource_id, version)")
    asyncio.run(reset())
    yield


_SNAP = {"name": "雷达图技能", "description": "把指标画成雷达图并给出解读",
         "constraints": {"max_steps": 20, "缺参时": "反问用户"},
         "tools": [], "tool_ids": []}


async def _seed_skill():
    from cogni_agent.tool_center_service.skill.models import Skill

    async with common_db.get_sessionmaker()() as db:
        db.add(Skill(id=SKILL_ID, name="雷达图技能", owner_user_id="u-1",
                     visibility="group", tool_ids=[]))
        await db.commit()


async def _publish_once(db, note=""):
    """派生一个草稿并发布,返回版本行。"""
    from cogni_agent.tool_center_service import versioning as ver

    draft = await ver.derive_version(db, "skill", SKILL_ID, base_version_id=None,
                                     snapshot=dict(_SNAP), created_by="u-1", note=note)
    await ver.publish_version(db, draft.id)
    return await ver.get_version(db, draft.id)


@run_async
async def test_publish_after_offline_does_not_reuse_the_version_number():
    """发布 → 下线 → 再发布。这是完全正常的运维路径,以前必炸。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _seed_skill()
    async with common_db.get_sessionmaker()() as db:
        v1 = await _publish_once(db, "第一版")
        assert v1.version == 1

        await ver.offline_version(db, v1.id)

        v2 = await _publish_once(db, "改完再发")
        assert v2.version == 2, "下线过的 1 号不能再发一次 —— 那个号还占在已下线的行上"


@run_async
async def test_version_numbers_keep_climbing_across_several_offlines():
    """连续下线也不该让号退回去。号退回去 = 历史记录里出现两个 v2,对账时谁也说不清。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _seed_skill()
    async with common_db.get_sessionmaker()() as db:
        seen = []
        for i in range(3):
            row = await _publish_once(db, f"第 {i + 1} 次")
            seen.append(row.version)
            await ver.offline_version(db, row.id)
        assert seen == [1, 2, 3], seen


@run_async
async def test_discarded_drafts_still_leave_no_version_holes():
    """改成数全部行之后,草稿不占号这条要保持不变。

    草稿的 version 是 NULL,不进唯一索引也不进 max() —— 派生几个再丢弃,
    下一个正式号仍然是紧挨着的那个。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _seed_skill()
    async with common_db.get_sessionmaker()() as db:
        v1 = await _publish_once(db, "第一版")
        assert v1.version == 1

        # 派生一个草稿又丢弃
        draft = await ver.derive_version(db, "skill", SKILL_ID, base_version_id=v1.id,
                                         snapshot=dict(_SNAP), created_by="u-1")
        assert draft.version is None, "草稿不该预占版本号"
        await ver.discard_draft(db, draft.id)

        v2 = await _publish_once(db, "第二版")
        assert v2.version == 2, "丢弃的草稿不该在版本号上留下空洞"


@run_async
async def test_next_version_no_counts_offlined_rows():
    """把口径本身钉住:版本号的依据必须与唯一索引的口径一致(全部行,不只 published)。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _seed_skill()
    async with common_db.get_sessionmaker()() as db:
        v1 = await _publish_once(db)
        await ver.offline_version(db, v1.id)
        assert await ver._next_version_no(db, "skill", SKILL_ID) == 2
