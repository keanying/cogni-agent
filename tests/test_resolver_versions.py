"""#4 resolver 版本解析测试(15 号文档重写版)。

验证运行时解析链路(15 号文档 §6.2 档位收敛):
- 档位 0:调用方显式指定版本(explicit);
- 档位 1:user 维度分发指派命中即用(指派即授权,不按 visibility 拦截);
- 档位 2:未分发 -> fail-closed,raise ResolveError(code=agent_not_distributed),
  绝不回落任何「默认/最新 published」;库中残留的 scenic 指派行被运行时忽略。
- pin 逐层下钻(agent -> skill -> tool),输出结构不变,下游零改动。

测试规则(与 test_versioning.py 一致):
- 用真实 DB(独立引擎 fixture),monkeypatch 掉 llm _model_cfg;
- 每个用例自行造主表资源 + ResourceVersion + ResourceAssignment。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from cogni_agent.runtime_service.engine.resolver import ResolveError, resolve_execution_spec
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.agent.models import Agent
from cogni_agent.tool_center_service.skill.models import Skill
from cogni_agent.tool_center_service.tool.models import Tool

pytestmark = pytest.mark.asyncio

AGENT_ID = "res_agent"
SKILL_ID = "res_skill"
TOOL_ID = "res_tool"


@pytest_asyncio.fixture
async def db(test_sessionmaker):
    """使用 conftest 的测试库 sessionmaker。"""
    async with test_sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def world(db):
    """主表世界:agent(绑 res_skill) -> skill(绑 res_tool) -> tool,全部 public 可见。

    版本记录(均为已发布快照,15 号文档:无 is_default 概念):
    - agent v2: persona 快照 + bound_skills=[{skill_id, version:2}]
    - skill v2: tools=[{tool_id, version:2}]
    - tool  v2: description/type_config/risk_level 快照
    """
    await db.execute(delete(ver.ResourceAssignment).where(ver.ResourceAssignment.agent_id == AGENT_ID))
    await db.execute(delete(ver.ResourceVersion).where(ver.ResourceVersion.resource_id.in_([AGENT_ID, SKILL_ID, TOOL_ID])))
    await db.execute(delete(Agent).where(Agent.id == AGENT_ID))
    await db.execute(delete(Skill).where(Skill.id == SKILL_ID))
    await db.execute(delete(Tool).where(Tool.id == TOOL_ID))
    await db.commit()

    # v1.2(0051):主表无内容列(同 skill 0050),tool 内容在 v2 快照(见下)
    db.add(Tool(id=TOOL_ID, name="查询订单",
                active=True, owner_user_id="owner",
                scenic_id=None, visibility="public"))
    # v1.1.10:主表无内容列(0050 删列),skill 内容在 v2 快照(tools pin 已显式化)
    db.add(Skill(id=SKILL_ID, name="主表技能", tool_ids=[TOOL_ID],
                 owner_user_id="owner", scenic_id=None, visibility="public"))
    db.add(Agent(id=AGENT_ID, name="主表人格", bound_skill_ids=[SKILL_ID],
                 active=True, owner_user_id="owner", scenic_id=None, visibility="public",
                 persona_desc="主表人格描述", tone_style="", opening_line="",
                 rate_limit_qps=10, rate_limit_daily=100))
    # 版本记录(§2 pin 格式)
    db.add(ver.ResourceVersion(resource_type="tool", resource_id=TOOL_ID, version=2,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"description": "版本工具描述", "type": "http",
                                         "type_config": {"method": "POST", "url": "http://t/v2",
                                                         "params_schema": {"type": "object", "properties": {}}},
                                         "risk_level": "high", "visibility": "group"}))
    db.add(ver.ResourceVersion(resource_type="skill", resource_id=SKILL_ID, version=2,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"name": "版本技能", "tool_usage": {},
                                         "knowledge_sources": [], "constraints": {},
                                         "suggested_mode": "L1", "visibility": "public",
                                         "tools": [{"tool_id": TOOL_ID, "version": 2}]}))
    db.add(ver.ResourceVersion(resource_type="agent", resource_id=AGENT_ID, version=2,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"name": "版本人格", "persona_desc": "版本人格描述",
                                         "tone_style": "", "opening_line": "",
                                         "visibility": "public",
                                         "bound_skills": [{"skill_id": SKILL_ID, "version": 2}]}))
    await db.commit()
    yield
    await db.execute(delete(ver.ResourceAssignment).where(ver.ResourceAssignment.agent_id == AGENT_ID))
    await db.execute(delete(ver.ResourceVersion).where(ver.ResourceVersion.resource_id.in_([AGENT_ID, SKILL_ID, TOOL_ID])))
    await db.execute(delete(Agent).where(Agent.id == AGENT_ID))
    await db.execute(delete(Skill).where(Skill.id == SKILL_ID))
    await db.execute(delete(Tool).where(Tool.id == TOOL_ID))
    await db.commit()


async def _version(db, resource_type: str, resource_id: str, version: int) -> ver.ResourceVersion:
    row = (await db.execute(select(ver.ResourceVersion).where(
        ver.ResourceVersion.resource_type == resource_type,
        ver.ResourceVersion.resource_id == resource_id,
        ver.ResourceVersion.version == version))).scalars().first()
    assert row is not None, f"{resource_type} v{version} 不存在"
    return row


async def _assign(db, agent_id: str, user_id: str, version_id: str):
    db.add(ver.ResourceAssignment(agent_id=agent_id, scope_type=ver.SCOPE_USER,
                                  scope_id=user_id, version_id=version_id))
    await db.commit()


async def _resolve(db, *, scenic_id=None, user_id="u1", role="agent_user", executor_version_id=None):
    """走真实解析链路,只 mock 掉 llm 模型配置。"""
    async def fake_model_cfg(model_name):
        assert model_name is None
        return ("test-model", {"model_id": "test-model"})

    import cogni_agent.runtime_service.engine.resolver as resolver_mod

    resolver_mod._model_cfg = fake_model_cfg  # type: ignore[attr-defined]
    return await resolve_execution_spec(
        db, executor_type="agent", executor_id=AGENT_ID,
        scenic_id=scenic_id, role=role, user_id=user_id,
        executor_version_id=executor_version_id)


async def test_undistributed_agent_fails_closed(db, world):
    """档位 2 fail-closed(15 号文档核心):无 user 指派、未显式指定 -> agent_not_distributed,
    不再回落默认/最新 published。"""
    with pytest.raises(ResolveError) as ei:
        await _resolve(db)
    assert ei.value.code == "agent_not_distributed"
    assert "分发" in str(ei.value)


async def test_user_assignment_resolves_pinned_snapshot(db, world):
    """档位 1:user 指派命中 -> agent/skill/tool 全部按 pin 版本快照解析,主表不生效。"""
    v2 = await _version(db, "agent", AGENT_ID, 2)
    await _assign(db, AGENT_ID, "u1", v2.id)

    spec = await _resolve(db)
    assert spec.persona["name"] == "版本人格"          # 快照名,非主表
    assert spec.persona["persona_desc"] == "版本人格描述"
    assert len(spec.skill_chain) == 1
    s = spec.skill_chain[0]
    assert s.name == "版本技能" and s.suggested_mode == "L1"   # skill pin v2
    assert s.tools[0].description == "版本工具描述"            # tool pin v2
    assert s.tools[0].risk_level == "high"
    assert any(d.code == "agent_version_assigned" for d in spec.diagnostics)


async def test_non_user_assignment_rows_are_ignored(db, world):
    """运行时只认 user 维度(15 号 D9-A):scenic 残留行不影响解析;
    仅 scenic 行存在时 -> fail-closed(不命中,不回落)。"""
    v2 = await _version(db, "agent", AGENT_ID, 2)
    # 仅 scenic 行:user u1 无指派 -> 报 agent_not_distributed(旧语义会命中 scenic,现已收敛)
    db.add(ver.ResourceAssignment(agent_id=AGENT_ID, scope_type="scenic", scope_id="sc_1",
                                  version_id=v2.id))
    await db.commit()
    with pytest.raises(ResolveError, match="分发"):
        await _resolve(db, scenic_id="sc_1", user_id="u1")

    # 补 user 指派:即便 scenic 行仍在,user 命中即用
    await _assign(db, AGENT_ID, "u1", v2.id)
    spec = await _resolve(db, scenic_id="sc_1", user_id="u1")
    assert spec.persona["name"] == "版本人格"
    assert any(d.code == "agent_version_assigned" for d in spec.diagnostics)


async def test_stale_assignment_fails_closed(db, world):
    """指派版本异常下线 -> 不再降级任何版本,档位 2 fail-closed(agent_not_distributed)。

    正常流程 §8 禁止下线被指派版本,这里直接改库模拟极端异常(如迁移/误操作),
    验证 resolver 的兜底防御:记录 agent_assignment_stale 后按未分发报错。
    """
    v2 = await _version(db, "agent", AGENT_ID, 2)
    await _assign(db, AGENT_ID, "u1", v2.id)
    v2.status = ver.STATUS_OFFLINE  # 模拟异常:被指派版本被手工下线
    await db.commit()

    with pytest.raises(ResolveError) as ei:
        await _resolve(db)
    assert ei.value.code == "agent_not_distributed"


async def test_skill_pin_missing_raises_resolve_error(db, world):
    """pin 引用的 skill 版本缺失 -> 该绑定视为不可用,抛 ResolveError(§6.4)。"""
    db.add(ver.ResourceVersion(resource_type="agent", resource_id=AGENT_ID, version=3,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"name": "v3", "visibility": "public",
                                         "bound_skills": [{"skill_id": SKILL_ID, "version": 99}]}))
    await db.commit()
    v3 = await _version(db, "agent", AGENT_ID, 3)
    await _assign(db, AGENT_ID, "u1", v3.id)

    with pytest.raises(ResolveError, match="v99"):
        await _resolve(db)


async def test_tool_pin_missing_skips_tool_with_warning(db, world):
    """pin 引用的 tool 版本缺失 -> 该绑定视为不可用,warn + 跳过(§6.4)。"""
    db.add(ver.ResourceVersion(resource_type="skill", resource_id=SKILL_ID, version=3,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"name": "版本技能", "tool_usage": {}, "knowledge_sources": [],
                                         "constraints": {}, "suggested_mode": "L1", "visibility": "public",
                                         "tools": [{"tool_id": TOOL_ID, "version": 99}]}))
    db.add(ver.ResourceVersion(resource_type="agent", resource_id=AGENT_ID, version=3,
                               status=ver.STATUS_PUBLISHED,
                               snapshot={"name": "版本人格", "visibility": "public",
                                         "bound_skills": [{"skill_id": SKILL_ID, "version": 3}]}))
    await db.commit()
    v3 = await _version(db, "agent", AGENT_ID, 3)
    await _assign(db, AGENT_ID, "u1", v3.id)

    spec = await _resolve(db)
    assert spec.skill_chain[0].tools == []  # 工具被跳过
    assert any(d.code == "tool_pin_missing" for d in spec.diagnostics)


async def test_group_visibility_version_assignment_grants_access(db, world):
    """§6.3 指派即授权:v2(visibility=group)对非组用户本不可见,但 user 指派命中后
    直接执行,不再按版本 visibility 拦截;未指派 -> 一律 agent_not_distributed。"""
    v2 = await _version(db, "agent", AGENT_ID, 2)
    v2.snapshot = {**v2.snapshot, "visibility": "group"}
    await db.commit()

    # 未指派:group 版不可见与未分发同结果(fail-closed,消息为分发引导)
    with pytest.raises(ResolveError, match="分发"):
        await _resolve(db, scenic_id="sc_1", user_id="scenic_user", role="scenic_admin")

    # P0-3 指派即授权:user 指派命中 group 版 -> 直接执行(不再拦截)
    await _assign(db, AGENT_ID, "u1", v2.id)
    spec = await _resolve(db, scenic_id="sc_1", user_id="u1", role="agent_user")
    assert spec.persona["name"] == "版本人格"
    assert any(d.code == "agent_version_assigned" for d in spec.diagnostics)


# ── Phase 5(13 号文档):调用方显式指定版本(档位 0,Console 两层版本选择) ──


async def test_explicit_agent_version_overrides_assignment(db, world):
    """档位 0 > 档位 1:显式 version_id 直接解析该版本并溯源 explicit(与指派无关)。"""
    # 显式指定 v2(id):无指派也能跑(管理端操练台)
    v2 = await _version(db, "agent", AGENT_ID, 2)
    spec = await _resolve(db, executor_version_id=v2.id)
    assert spec.persona["name"] == "版本人格"
    assert spec.persona["persona_desc"] == "版本人格描述"
    assert len(spec.skill_chain) == 1
    assert spec.skill_chain[0].name == "版本技能"
    assert any(d.code == "agent_version_explicit" for d in spec.diagnostics)


async def test_explicit_skill_version_for_direct_skill(db, world):
    """Skill 直连 + 显式 version_id -> 该版本快照(含工具 pin),溯源 skill_version_explicit。"""
    skill_v2 = await _version(db, "skill", SKILL_ID, 2)
    spec = await resolve_execution_spec(
        db, executor_type="skill", executor_id=SKILL_ID,
        scenic_id=None, role="agent_user", user_id="u1",
        executor_version_id=skill_v2.id)
    assert len(spec.skill_chain) == 1
    s = spec.skill_chain[0]
    assert s.name == "版本技能" and s.suggested_mode == "L1"
    assert s.tools[0].description == "版本工具描述"
    assert any(d.code == "skill_version_explicit" for d in spec.diagnostics)


async def test_explicit_version_rejected_foreign_or_unpublished(db, world):
    """显式版本归属不符 / 未发布 -> 拒绝执行(不降级)。"""
    # tool 的版本 id 用于 agent -> 归属不符
    tool_v2 = await _version(db, "tool", TOOL_ID, 2)
    with pytest.raises(ResolveError, match="不存在指定版本"):
        await _resolve(db, executor_version_id=tool_v2.id)
    # draft 版本 -> 未发布拒绝
    db.add(ver.ResourceVersion(resource_type="agent", resource_id=AGENT_ID, version=9,
                               status=ver.STATUS_DRAFT,
                               snapshot={"name": "草稿人格", "visibility": "public", "bound_skills": []}))
    await db.commit()
    v9 = await _version(db, "agent", AGENT_ID, 9)
    with pytest.raises(ResolveError, match="不存在指定版本"):
        await _resolve(db, executor_version_id=v9.id)
    # 组合 Skill 任务 + 显式版本 -> 明确报错(单版本语义不适用)
    skill_v2 = await _version(db, "skill", SKILL_ID, 2)
    with pytest.raises(ResolveError, match="不支持指定单个版本"):
        await resolve_execution_spec(
            db, executor_type="skill", executor_id=SKILL_ID,
            scenic_id=None, role="agent_user", user_id="u1",
            overrides={"skill_ids": [SKILL_ID, "other_skill"]},
            executor_version_id=skill_v2.id)


async def test_direct_skill_without_version_fails_closed(db, world):
    """Skill 直连未指定版本 -> 不可用报错(15 号文档 Q1/§7:无默认/最新 published 兜底)。"""
    with pytest.raises(ResolveError, match="未指定版本"):
        await resolve_execution_spec(
            db, executor_type="skill", executor_id=SKILL_ID,
            scenic_id=None, role="agent_user", user_id="u1")


async def test_provenance_includes_tool_level_sources(db, world):
    """Phase 6:溯源到 tool 级 —— provenance.skills[skill].tools 记录实际 pin 到的工具版本。

    agent -> skill -> tool 每层都写实际解析来源;15 号文档后来源枚举收敛为
    assigned / pinned_snapshot / explicit_snapshot,无 default_snapshot。
    """
    v2 = await _version(db, "agent", AGENT_ID, 2)
    await _assign(db, AGENT_ID, "u1", v2.id)
    spec = await _resolve(db)
    p = spec.provenance
    a = p["agent"]
    assert a["source"] == "assigned"
    assert a["version"] == 2
    sk = p["skills"][SKILL_ID]
    assert sk["source"] == "pinned_snapshot"
    assert sk["version"] == 2
    assert sk["tools"][TOOL_ID] == {"source": "pinned_snapshot", "version": 2, "name": "查询订单"}, \
        "工具层溯源:宿主 skill 快照 pin 到的工具实际版本(含补全的 name)"
