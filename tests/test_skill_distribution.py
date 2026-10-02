# -*- coding: utf-8 -*-
"""技能分发(0074)接口级测试:真实 PostgreSQL,自建最小 FastAPI。

覆盖的语义(全部以「分发中心按人」为基线):
- 指派表加了 resource_type 后,agent 与 skill 的指派互不干扰;
- 技能直调:有指派 → 按指派版本解析;无指派 → fail-closed(skill_not_distributed);
  超管豁免(平台方要能随时验证任意技能);
- 问题库绑技能的老路径:技能自己的指派优先于「agent 快照里的 pin」,两者都没有才降级;
- 管理端接口:/admin/skills/{id}/assignments 增删查 + /admin/skills/by-user/{uid} 三态。

不依赖 tests/conftest.py(沙箱无 asyncpg),与 tests/quickbi 同款自建 app 写法。
"""
from __future__ import annotations

import asyncio
import functools

import httpx
import pytest
from fastapi import FastAPI

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base
from cogni_agent.common.errors import ZhilvError
from cogni_agent.common.middleware import ApiResponseMiddleware
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.runtime_service.engine.resolver import ResolveError, resolve_execution_spec
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.agent.models import Agent
from cogni_agent.tool_center_service.routes import router as admin_router
from cogni_agent.tool_center_service.skill.models import Skill
from cogni_agent.user_center_service.models import User

ADMIN = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
OPERATOR = CurrentUser(user_id="u-op", scenic_id="wys", role="scenic_operator")


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
        async with common_db.get_sessionmaker()() as db:
            db.add(User(id=ADMIN.user_id, name="超管", account="admin", phone="13800000001",
                        password_hash="x", scenic_id="", role="super_admin"))
            db.add(User(id=OPERATOR.user_id, name="景区运营", account="op", phone="13800000002",
                        password_hash="x", scenic_id="wys", role="scenic_operator"))
            await db.commit()
    asyncio.run(reset())
    yield


async def _error_handler(request, exc: ZhilvError):
    from fastapi.responses import JSONResponse

    from cogni_agent.common.schemas import ApiResponse
    return JSONResponse(status_code=exc.status_code,
                        content=ApiResponse.fail(code=exc.error_code or 1000, message=str(exc)).model_dump())


def _app(user: CurrentUser = ADMIN) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ApiResponseMiddleware)
    app.add_exception_handler(ZhilvError, _error_handler)
    app.include_router(admin_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30)


def data(resp: httpx.Response):
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    return body["data"]


async def _seed_skill(db, skill_id="sk_demo", name="客流分析", visibility="public",
                      versions=(1, 2)) -> list[ver.ResourceVersion]:
    """建技能主表行 + 若干 published 版本快照(直接插,等价「历史上已发布」)。"""
    db.add(Skill(id=skill_id, name=name, tool_ids=[], owner_user_id=ADMIN.user_id,
                 visibility=visibility))
    await db.commit()
    rows = []
    for v in versions:
        row = ver.ResourceVersion(
            resource_type="skill", resource_id=skill_id, version=v,
            status=ver.STATUS_PUBLISHED,
            snapshot={"name": name, "visibility": visibility, "description": f"v{v} 描述",
                      "system_prompt": f"你是客流分析技能 v{v}", "tools": []},
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


# ══════════════════════════════════════════════════════════════════════════
# 数据层:agent / skill 指派互不干扰
# ══════════════════════════════════════════════════════════════════════════

@run_async
async def test_agent_and_skill_assignments_are_independent():
    """同一个人对 agent 与 skill 各有一条指派,互不覆盖(唯一键含 resource_type)。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_skill(db)
        db.add(Agent(id="ag_demo", name="景区大脑", persona_desc="p", tone_style="t",
                     opening_line="hi", rate_limit_qps=5, rate_limit_daily=100,
                     visibility="public", owner_user_id=ADMIN.user_id))
        await db.commit()
        agent_ver = ver.ResourceVersion(resource_type="agent", resource_id="ag_demo", version=1,
                                        status=ver.STATUS_PUBLISHED,
                                        snapshot={"name": "景区大脑", "visibility": "public"})
        db.add(agent_ver)
        await db.commit()

        await ver.upsert_assignment(db, "ag_demo", OPERATOR.user_id, agent_ver.id)
        skill_v1 = (await ver.list_versions(db, "skill", "sk_demo"))[-1]
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, skill_v1["id"],
                                    resource_type="skill")

        agents = await ver.list_assignments(db, "ag_demo")
        skills = await ver.list_assignments(db, "sk_demo", resource_type="skill")
        assert len(agents["items"]) == 1 and agents["items"][0]["resource_type"] == "agent"
        assert len(skills["items"]) == 1 and skills["items"][0]["resource_type"] == "skill"
        assert skills["items"][0]["resource_id"] == "sk_demo"


@run_async
async def test_assigned_version_returns_published_only():
    """指派版本被下线 → assigned_version 返回 None(等价未分发,不静默用下线版本)。"""
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, rows[0].id,
                                    resource_type="skill")
        got = await ver.assigned_version(db, "skill", "sk_demo", OPERATOR.user_id)
        assert got is not None and got.version == 1

        row = await db.get(ver.ResourceVersion, rows[0].id)
        row.status = ver.STATUS_OFFLINE
        await db.commit()
        assert await ver.assigned_version(db, "skill", "sk_demo", OPERATOR.user_id) is None


# ══════════════════════════════════════════════════════════════════════════
# 运行时:技能直调的分发闸门
# ══════════════════════════════════════════════════════════════════════════

@run_async
async def test_skill_direct_call_without_assignment_is_fail_closed():
    """未分发的技能直调 → skill_not_distributed(不回落「最新 published」)。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_skill(db)
        with pytest.raises(ResolveError) as ei:
            await resolve_execution_spec(
                db, executor_type="skill", executor_id="sk_demo",
                scenic_id="wys", role=OPERATOR.role, user_id=OPERATOR.user_id)
        assert ei.value.code == "skill_not_distributed"
        assert "分发中心" in str(ei.value)


@run_async
async def test_skill_direct_call_uses_assigned_version():
    """指派了 v1 → 即便 v2 也已发布,也按 v1 解析(版本体系的意义就是可指定)。"""
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, rows[0].id,
                                    resource_type="skill")
        spec = await resolve_execution_spec(
            db, executor_type="skill", executor_id="sk_demo",
            scenic_id="wys", role=OPERATOR.role, user_id=OPERATOR.user_id)
        assert spec.executor_type == "skill"
        assert spec.provenance["skills"]["sk_demo"]["version"] == 1


@run_async
async def test_super_admin_is_exempt_from_distribution():
    """超管不受分发限制:没有任何指派也能直调,按最新已发布版本解析。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_skill(db)
        spec = await resolve_execution_spec(
            db, executor_type="skill", executor_id="sk_demo",
            scenic_id=None, role="super_admin", user_id=ADMIN.user_id)
        assert spec.provenance["skills"]["sk_demo"]["version"] == 2


@run_async
async def test_private_skill_assigned_user_passes_visibility():
    """指派即授权:private 技能指派给本人以外的人本就被拦在指派这一步。

    这里验证的是另一半 —— 已经指派成功的(public)技能,执行时不再因可见性被拦。
    """
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db, skill_id="sk_priv", name="私有技能", visibility="private")
        # private 只能指派给创建者本人
        with pytest.raises(Exception):
            await ver.upsert_assignment(db, "sk_priv", OPERATOR.user_id, rows[0].id,
                                        resource_type="skill")


# ══════════════════════════════════════════════════════════════════════════
# 固定问题链路:技能自己的指派优先于 agent 快照里的 pin
# ══════════════════════════════════════════════════════════════════════════

@run_async
async def test_direct_skill_assignment_wins_over_agent_pin():
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        db.add(Agent(id="ag_demo", name="景区大脑", persona_desc="p", tone_style="t",
                     opening_line="hi", rate_limit_qps=5, rate_limit_daily=100,
                     visibility="public", owner_user_id=ADMIN.user_id))
        await db.commit()
        agent_ver = ver.ResourceVersion(
            resource_type="agent", resource_id="ag_demo", version=1, status=ver.STATUS_PUBLISHED,
            snapshot={"name": "景区大脑", "visibility": "public",
                      "bound_skills": [{"skill_id": "sk_demo", "version": 2}]})
        db.add(agent_ver)
        await db.commit()
        await ver.upsert_assignment(db, "ag_demo", OPERATOR.user_id, agent_ver.id)

        # 只有 agent 指派时:走快照 pin 的 v2
        assert await ver.assigned_skill_version(db, "ag_demo", OPERATOR.user_id, "sk_demo") == 2
        # 技能自己也指派了 v1:以技能指派为准
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, rows[0].id,
                                    resource_type="skill")
        assert await ver.assigned_skill_version(db, "ag_demo", OPERATOR.user_id, "sk_demo") == 1


@run_async
async def test_skill_version_anchor_none_when_nothing_assigned():
    """两处都没有 → None,调用方(网关)据此降级 agent 执行,而不是猜一个版本。"""
    async with common_db.get_sessionmaker()() as db:
        await _seed_skill(db)
        assert await ver.assigned_skill_version(db, "", OPERATOR.user_id, "sk_demo") is None


# ══════════════════════════════════════════════════════════════════════════
# 管理端接口
# ══════════════════════════════════════════════════════════════════════════

@run_async
async def test_skill_assignment_api_crud():
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        v1_id, v2_id = rows[0].id, rows[1].id

    async with _client(_app()) as c:
        empty = data(await c.get("/api/admin/skills/sk_demo/assignments"))
        assert empty["items"] == []

        r = data(await c.post("/api/admin/skills/sk_demo/assignments",
                              json={"user_id": OPERATOR.user_id, "version_id": v1_id,
                                    "note": "试点用户"}))
        assert r["ok"] and r["assignment"]["version"] == 1
        assert r["assignment"]["resource_type"] == "skill"

        # 重复指派 = 切版本,不新增行
        r2 = data(await c.post("/api/admin/skills/sk_demo/assignments",
                               json={"user_id": OPERATOR.user_id, "version_id": v2_id}))
        assert r2["assignment"]["version"] == 2
        items = data(await c.get("/api/admin/skills/sk_demo/assignments"))["items"]
        assert len(items) == 1 and items[0]["note"] == "试点用户"

        # 列表徽标:已指派 1 人
        skills = data(await c.get("/api/admin/skills"))["items"]
        mine = [s for s in skills if s["id"] == "sk_demo"][0]
        assert mine["assigned_count"] == 1 and mine["published_count"] == 2

        gone = data(await c.delete(f"/api/admin/skills/sk_demo/assignments/{items[0]['id']}"))
        assert gone["ok"]
        assert data(await c.get("/api/admin/skills/sk_demo/assignments"))["items"] == []


@run_async
async def test_skills_by_user_three_states():
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        await _seed_skill(db, skill_id="sk_draftonly", name="未发布技能", versions=())
        v1_id = rows[0].id

    async with _client(_app()) as c:
        res = data(await c.get(f"/api/admin/skills/by-user/{OPERATOR.user_id}"))
        states = {i["skill_id"]: i["state"] for i in res["items"]}
        assert states["sk_demo"] == "not_distributed"       # 已发布未分发 → 红标
        assert states["sk_draftonly"] == "no_published"
        assert res["user"]["id"] == OPERATOR.user_id

        data(await c.post("/api/admin/skills/sk_demo/assignments",
                          json={"user_id": OPERATOR.user_id, "version_id": v1_id}))
        res2 = data(await c.get(f"/api/admin/skills/by-user/{OPERATOR.user_id}"))
        hit = [i for i in res2["items"] if i["skill_id"] == "sk_demo"][0]
        assert hit["state"] == "assigned" and hit["assigned_version"] == 1


@run_async
async def test_offline_blocked_while_assigned():
    """被指派的技能版本不能直接下线(否则那位用户瞬间不可用还查不出原因)。"""
    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, rows[0].id,
                                    resource_type="skill")
        with pytest.raises(Exception) as ei:
            await ver.offline_version(db, rows[0].id)
        assert "引用" in str(ei.value)
