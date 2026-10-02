# -*- coding: utf-8 -*-
"""分发(0074 技能 / 0075 Quick BI 连接)接口级测试:真实 PostgreSQL,自建最小 FastAPI。

覆盖的语义(全部以「分发中心按人」为基线):
- 指派表加了 resource_type 后,agent 与 skill 的指派互不干扰;
- 技能直调:有指派 → 按指派版本解析;无指派 → fail-closed(skill_not_distributed);
  超管豁免(平台方要能随时验证任意技能);
- 问题库绑技能的老路径:技能自己的指派优先于「agent 快照里的 pin」,两者都没有才降级;
- 管理端接口:/admin/skills/{id}/assignments 增删查 + /admin/skills/by-user/{uid} 三态;
- Quick BI 连接分配:分配的是「能不能用这套凭证」而不是版本,按连接转白名单
  (零分配维持现状),超管豁免,解除最后一条后回到景区/可见性判定。

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
async def test_offline_releases_assignments_and_reports_who():
    """下线会**自动解除用户指派并收回权限**,并在提示里说清收回了谁。

    原先是「被指派就拒绝下线」。改掉是因为那条规则把顺序弄反了:下线的意思本来就是
    「这一版不再对外」,收回权限正是运营的本意 —— 让他先一个个去解指派再回来下线,
    中间那段时间版本还在线上跑,而他以为已经下了。

    但必须说清收回了谁:只说「已解除 1 处指派」的话,被收回的人来问
    「我为什么用不了了」时,运营翻不出是哪一次操作动的他。
    """
    from cogni_agent.tool_center_service.versioning import ResourceAssignment
    from sqlalchemy import select

    async with common_db.get_sessionmaker()() as db:
        rows = await _seed_skill(db)
        await ver.upsert_assignment(db, "sk_demo", OPERATOR.user_id, rows[0].id,
                                    resource_type="skill")
        out = await ver.offline_version(db, rows[0].id)
        assert out["ok"] is True
        assert out["released_assignments"], "要回报解除了哪些指派"
        assert OPERATOR.user_id in [a["scope_id"] for a in out["released_assignments"]]
        left = (await db.execute(select(ResourceAssignment).where(
            ResourceAssignment.version_id == rows[0].id))).scalars().all()
        assert left == [], "指派要真的删掉,否则权限并没有收回"


# ══════════════════════════════════════════════════════════════════════════
# Quick BI 连接分配(0075):特例 —— 分配的是「能不能用这套凭证」,不是版本
# ══════════════════════════════════════════════════════════════════════════

async def _seed_connection(db, conn_id="conn_a", name="羚羊QuickBI", visibility="public",
                           owner=None):
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    db.add(QbiConnection(id=conn_id, name=name, visibility=visibility,
                         owner_user_id=owner or ADMIN.user_id, scenic_id=None, active=True))
    await db.commit()


def _qbi_actor(user: CurrentUser):
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    return qsvc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None,
                      role=user.role, source="console")


@run_async
async def test_connection_without_grants_keeps_legacy_visibility():
    """零分配 = 维持现状:public 连接对景区运营依然可见(升级当天不炸存量)。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
    views = await qsvc.list_connections(_qbi_actor(OPERATOR))
    assert [v.id for v in views if not v.virtual] == ["conn_a"]


@run_async
async def test_first_grant_turns_connection_into_whitelist():
    """第一条分配把连接切成白名单:没被分配的人立刻看不到、也拿不到。"""
    from cogni_agent.common.errors import NotFound as NF
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
        db.add(User(id="u-other", name="另一个运营", account="op2", phone="13800000003",
                    password_hash="x", scenic_id="wys", role="scenic_operator"))
        await db.commit()

    r = await qsvc.grant_connection(_qbi_actor(ADMIN), "conn_a", "u-other")
    assert r["restricted"] and "第一条分配" in r["message"]
    # 通知要点名分给了谁,不能只写「该用户」
    assert "分配给用户「另一个运营」(op2)" in r["message"]

    # 被分配的人能用
    other = CurrentUser(user_id="u-other", scenic_id="wys", role="scenic_operator")
    assert [v.id for v in await qsvc.list_connections(_qbi_actor(other)) if not v.virtual] == ["conn_a"]
    # 没被分配的人:列表里没有,直接按 id 取也拿不到
    assert [v.id for v in await qsvc.list_connections(_qbi_actor(OPERATOR)) if not v.virtual] == []
    with pytest.raises(NF):
        await qsvc.get_connection(_qbi_actor(OPERATOR), "conn_a")


@run_async
async def test_super_admin_exempt_and_revoke_restores_open_state():
    """超管不受分配限制;解除最后一条分配后连接回到「按景区/可见性」。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
    r = await qsvc.grant_connection(_qbi_actor(ADMIN), "conn_a", OPERATOR.user_id, note="试点")
    # 超管自己没被分配,依然能用
    assert [v.id for v in await qsvc.list_connections(_qbi_actor(ADMIN)) if not v.virtual] == ["conn_a"]

    back = await qsvc.revoke_grant(_qbi_actor(ADMIN), r["grant"]["id"])
    assert back["restricted"] is False and "回到按景区" in back["message"]
    assert back["message"].startswith("已解除用户「景区运营」(op)对")
    # 回到零分配:原本可见的人又能用了
    assert [v.id for v in await qsvc.list_connections(_qbi_actor(OPERATOR)) if not v.virtual] == ["conn_a"]


@run_async
async def test_grant_requires_owner_or_manager():
    """分配是管理动作:看得见 ≠ 能分配,景区运营分不了集团级连接。

    报错要说清是**为什么**不行(这是集团级的 / 不是你景区的),不能只回一句无权限 ——
    否则用户分不出是角色问题还是选错了连接。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
    with pytest.raises(Exception) as ei:
        await qsvc.grant_connection(_qbi_actor(OPERATOR), "conn_a", OPERATOR.user_id)
    assert "集团级" in str(ei.value) or "景区" in str(ei.value)


@run_async
async def test_grants_by_user_three_states():
    """人视角三态:granted / not_granted / open。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db, "conn_a", "已分配连接")
        await _seed_connection(db, "conn_b", "未启用白名单的连接")
    await qsvc.grant_connection(_qbi_actor(ADMIN), "conn_a", OPERATOR.user_id)

    res = await qsvc.grants_by_user(_qbi_actor(ADMIN), OPERATOR.user_id)
    states = {i["connection_id"]: i["state"] for i in res["items"]}
    assert states == {"conn_a": "granted", "conn_b": "open"}

    res2 = await qsvc.grants_by_user(_qbi_actor(ADMIN), "u-admin")
    states2 = {i["connection_id"]: i["state"] for i in res2["items"]}
    assert states2["conn_a"] == "not_granted"    # conn_a 已白名单,超管不在名单里
    assert states2["conn_b"] == "open"


# ══════════════════════════════════════════════════════════════════════════
# Quick BI 固定问题的可见性:集团级问题要能被景区账号看到
# (否则「后台明明配了,APP 里一条都没有」)
# ══════════════════════════════════════════════════════════════════════════

async def _seed_fixed(db, qid, *, scenic_id=None, visibility="group", conn_id="conn_a"):
    """种一条 Quick BI 固定问题。

    0079 起固定问题存在统一问题表里(business_class='quick_bi'),
    老表 tc_qbi_fixed_questions 已删除 —— 所以这里直接种统一表的行。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    db.add(ScenicAgentIssueDetail(issue_id=qid, business_class="quick_bi", connection_id=conn_id,
                                  title=f"问题{qid}", issue="本月各平台销售额",
                                  capability="dashboard_qa", ask_type="dashboard", domain="票务",
                                  target={}, owner_user_id=ADMIN.user_id, scenic_id=scenic_id,
                                  visibility=visibility, active=True))
    await db.commit()


@run_async
async def test_group_level_fixed_question_visible_to_scenic_user():
    """集团级(scenic_id 为空)+ group 可见的固定问题,景区账号也能看到。

    管理员用超管账号建的固定问题默认就是这个组合 —— 他自己看得见(集团角色直接放行),
    景区账号却看不到的话,就是「后台配了、APP 空着」。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
        await _seed_fixed(db, "fq_group", scenic_id=None, visibility="group")
        await _seed_fixed(db, "fq_mine", scenic_id="wys", visibility="scenic")
        await _seed_fixed(db, "fq_other", scenic_id="other", visibility="scenic")
        await _seed_fixed(db, "fq_private", scenic_id=None, visibility="private")

    ids = {q["id"] for q in await qsvc.list_fixed(_qbi_actor(OPERATOR), active=True)}
    assert "fq_group" in ids          # 集团级 group:能看到
    assert "fq_mine" in ids           # 本景区:能看到
    assert "fq_other" not in ids      # 别的景区:看不到
    assert "fq_private" not in ids    # 私有:看不到


@run_async
async def test_catalog_returns_questions_for_scenic_user():
    """目录接口(APP 的 /quickbi/targets 走的就是它)对景区账号返回该景区能问的问题。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed_connection(db)
        await _seed_fixed(db, "fq_group", scenic_id=None, visibility="group")

    catalog = await qsvc.question_catalog(_qbi_actor(OPERATOR), include_assets=False)
    assert [q["id"] for q in catalog["questions"]] == ["fq_group"]
    assert catalog["scenic_id"] == "wys"
