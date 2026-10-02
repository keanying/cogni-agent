# -*- coding: utf-8 -*-
"""Tool / Skill / Agent / MCP 列表卡片的「最近修改时间 · 修改人 · 归属」。

现场诉求:卡片上要看得出最近谁改的、什么时候改的、属于哪个景区。
原表只有 created_by(派生草稿的人),之后谁保存、谁发布都不留名 —— 0087 加了 updated_by。
"""
from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.gateway_service.deps import CurrentUser

ROOT = Path(__file__).resolve().parent.parent
ROOT_USER = CurrentUser("u-root", None, "super_admin")


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


async def _seed(db):
    from cogni_agent.user_center_service.models import SysScenic, User

    db.add(User(id="u-root", name="平台超管", account="root", phone="13000000001",
                password_hash="x", scenic_id=None, role="super_admin", is_active=True))
    db.add(User(id="u-ed", name="尹开英", account="37853900", phone="13000000002",
                password_hash="x", scenic_id="S1", role="scenic_admin", is_active=True))
    db.add(SysScenic(scenic_id="S1", scenic_name="九龙潭景区", is_enabled=True, plan_config={}))
    await db.commit()


@run_async
async def test_tool_list_shows_last_editor_time_and_scenic():
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.routes import list_tools
    from cogni_agent.tool_center_service.tool.models import Tool

    async with common_db.get_sessionmaker()() as db:
        await _seed(db)
        t_scenic = Tool(name="票务-产品报表", owner_user_id="u-root", scenic_id="S1", visibility="public")
        t_group = Tool(name="舆情-情绪词云", owner_user_id="u-root", scenic_id=None, visibility="public")
        db.add_all([t_scenic, t_group])
        await db.commit()
        draft = await ver.derive_version(db, "tool", t_scenic.id, base_version_id=None,
                                         snapshot={"description": "a"}, created_by="u-root")
        # 超管派生,尹开英保存并发布 → 修改人是尹开英
        await ver.update_version(db, draft.id, snapshot={"description": "b"}, updated_by="u-ed")
        await ver.publish_version(db, draft.id, updated_by="u-ed")
        items = {i["name"]: i for i in (await list_tools(None, ROOT_USER, db))["items"]}

    a = items["票务-产品报表"]
    assert a["updated_by"] == "u-ed" and a["updated_by_name"] == "尹开英"
    assert a["scenic_name"] == "九龙潭景区" and a["owner_name"] == "平台超管"
    assert a["updated_at"] is not None
    # 没有任何版本:退回主表创建时间 + 创建者;集团级归属叫「集团」
    b = items["舆情-情绪词云"]
    assert b["updated_by_name"] == "平台超管" and b["scenic_name"] == "集团"
    assert b["updated_at"] is not None


@run_async
async def test_version_routes_record_who_changed_it():
    """发布 / 下线 / 保存草稿三个入口都要把操作人写进 updated_by。"""
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.tool.models import Tool

    async with common_db.get_sessionmaker()() as db:
        await _seed(db)
        t = Tool(name="x", owner_user_id="u-root", visibility="public")
        db.add(t)
        await db.commit()
        d = await ver.derive_version(db, "tool", t.id, base_version_id=None, snapshot={}, created_by="u-root")
        assert d.updated_by == "u-root"  # 派生时 = 派生人
        await ver.publish_version(db, d.id, updated_by="u-ed")
        await db.refresh(d)
        assert d.updated_by == "u-ed"
    src = (ROOT / "cogni_agent" / "tool_center_service" / "routes.py").read_text(encoding="utf-8")
    for call in ("ver.publish_version(db, version_id, updated_by=user.user_id)",
                 "ver.offline_version(db, version_id, updated_by=user.user_id)",
                 "ver.rollback_to(db, version_id, updated_by=user.user_id)",
                 "snapshot=req.snapshot, note=req.note or None,\n                                     updated_by=user.user_id"):
        assert call in src, call


def test_every_list_endpoint_attaches_meta():
    src = (ROOT / "cogni_agent" / "tool_center_service" / "routes.py").read_text(encoding="utf-8")
    for rt in ('"tool", tools', '"skill", skills', '"agent", agents', "None, rows"):
        assert f"meta = await _list_meta(db, {rt})" in src, rt
