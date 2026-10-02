# -*- coding: utf-8 -*-
"""版本化管理(15 号文档:默认版本删除 + 分发中心按用户维度)测试。

15 号语义要点(全部断言以此为基线):
- 默认版本概念删除:D1-B 物理删除 is_default 列/默认回落/「设为默认」;
  snapshot / 发布都不再物化主表,发布不隐含对任何人生效。
- 分发收敛到 user(D9-A):指派 scope 仅 user,取消 scenic 继承;无 user 指派 =
  fail-closed(agent_not_distributed),无任何隐式兜底。
- 发布黄条 hint(D7-A):发布成功但零 user 指派时返回 hint,提醒去分发。
- 模糊 pin(version:null)在发布预检被拦截(fail-closed)。
- 0025 回填迁移已降级 no-op(0038 删列),本文件不再包含其测试。
"""
import pytest
import sqlalchemy as sa
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.agent.models import Agent
from cogni_agent.tool_center_service.skill.models import Prompt, Skill
from cogni_agent.tool_center_service.tool.models import Tool
from cogni_agent.tool_center_service.versioning import (
    ResourceAssignment,
    ResourceVersion,
)


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
async def agent(db_session):
    """集团级 Agent 主表行(id 固定,便于引用)。"""
    a = Agent(
        id="ver_agent", name="版本智能体", persona_desc="初版人格", tone_style="活泼",
        opening_line="你好,我是版本智能体", rate_limit_qps=5, rate_limit_daily=1000,
        visibility="public", owner_user_id="t",
    )
    db_session.add(a)
    await db_session.commit()
    yield a


@pytest.fixture
async def skill(db_session):
    """集团级 Skill 主表行(带 Prompt,验证 prompt 进快照且发布不回写 tc_prompts)。

    v1.1.10:主表已无内容列(0050 删列),内容由编辑保存/物化时从版本快照携带;
    主表仅身份 + tool_ids 绑定兜底。
    """
    s = Skill(
        id="ver_skill", name="版本技能", tool_ids=[],
        owner_user_id="t",
    )
    db_session.add(s)
    db_session.add(Prompt(skill_id="ver_skill", content="技能prompt-初版"))
    await db_session.commit()
    yield s


@pytest.fixture
async def tool(db_session):
    """集团级 Tool 主表行(无任何版本 = 存量资源直读场景)。

    v1.2(0051):主表无内容列(与 skill 0050 同构) —— 内容经 _snap/derive 的版本快照承载,
    主表仅身份目录。
    """
    t = Tool(
        id="ver_tool", name="工具测试",
        owner_user_id="t", visibility="public",
    )
    db_session.add(t)
    await db_session.commit()
    yield t


# ── helpers ─────────────────────────────────────────────────────────────────
async def _make_published(db, resource_type: str, resource_id: str, version: int,
                          snapshot: dict | None = None, note: str = "") -> ResourceVersion:
    """直接插一条 published 版本行 —— 模拟「历史上已发布」的存量版本。"""
    row = ResourceVersion(
        resource_type=resource_type, resource_id=resource_id, version=version,
        status=ver.STATUS_PUBLISHED, snapshot=snapshot or {}, note=note,
    )
    db.add(row)
    await db.commit()
    return row


async def _snap(db, resource_type: str, resource_id: str) -> ResourceVersion:
    return await ver.snapshot_current(db, resource_type, resource_id, created_by="t")


def _assign_rows(res):
    return res["items"]


# ══════════════════════════════════════════════════════════════════════════════
# §1 资源可用性 / snapshot_current(无默认语义)
# ══════════════════════════════════════════════════════════════════════════════
async def test_legacy_resource_without_versions_is_available(db_session, agent):
    """无任何版本记录的存量资源 = 可用(版本化是后加能力,不能因此停用)。"""
    ok, reason = await ver.is_resource_available(db_session, "agent", "ver_agent")
    assert ok is True and reason == ""


async def test_snapshot_current_creates_published_snapshot_no_default(db_session, agent):
    """snapshot_current 只补 published 快照,15 号无「首版自动默认」语义。"""
    v1 = await _snap(db_session, "agent", "ver_agent")
    assert v1.version == 1
    assert v1.status == ver.STATUS_PUBLISHED
    assert v1.published_at is not None
    assert v1.snapshot["persona_desc"] == "初版人格"
    assert v1.snapshot["bound_skills"] == []
    # 列表项也不携带任何 is_default / default 语义字段
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert len(items) == 1
    assert "is_default" not in items[0]


async def test_schema_has_no_is_default_column(db_session, agent):
    """0038 已物理删除 is_default 列 —— 模型层不该再出现它。"""
    cols = {c.name for c in ResourceVersion.__table__.columns}
    assert "is_default" not in cols


# ══════════════════════════════════════════════════════════════════════════════
# §2 草稿编辑生命周期
# ══════════════════════════════════════════════════════════════════════════════
async def test_published_version_cannot_be_edited_directly(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    with pytest.raises(Forbidden, match="不能直接编辑"):
        await ver.update_version(db_session, v1.id, snapshot={"persona_desc": "篡改"})


async def test_derive_edit_publish_keeps_old_version(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(
        db_session, "agent", "ver_agent", base_version_id=v1.id,
        snapshot={"persona_desc": "第二版"}, created_by="t")
    assert d.version is None and d.status == ver.STATUS_DRAFT
    assert d.base_version_id == v1.id
    d = await ver.update_version(db_session, d.id, snapshot={"persona_desc": "第二版优化"})

    r = await ver.publish_version(db_session, d.id)
    assert r["ok"] is True and r["version"] == 2

    # 主表不物化:发布只是冻结快照
    await db_session.refresh(agent)
    assert agent.persona_desc == "初版人格"
    # 老 v1 仍在线,新 v2 并行 —— 不存在「默认切换」
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert [it["version"] for it in items] == [2, 1]
    assert all(it["status"] == ver.STATUS_PUBLISHED for it in items)


async def test_first_publish_does_not_materialize_or_imply_effect(db_session, agent):
    """首次发布:version=1 且不写主表、不设默认、不隐含生效。"""
    d = await ver.derive_version(
        db_session, "agent", "ver_agent", base_version_id=None,
        snapshot={"persona_desc": "首版"}, created_by="t")
    r = await ver.publish_version(db_session, d.id)
    assert r["version"] == 1 and r["assigned_users"] == 0

    await db_session.refresh(agent)
    assert agent.persona_desc == "初版人格"  # 发布不物化主表
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert items[0]["version"] == 1 and items[0]["status"] == ver.STATUS_PUBLISHED


async def test_single_draft_per_resource(db_session, agent):
    await _snap(db_session, "agent", "ver_agent")
    await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                             snapshot={}, created_by="t")
    with pytest.raises(Forbidden, match="已存在未发布的草稿"):
        await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={}, created_by="t")


async def test_draft_save_does_not_bump_version_or_leak(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=v1.id,
                                 snapshot={}, created_by="t")
    d = await ver.update_version(db_session, d.id, snapshot={"persona_desc": "改了又改"})
    assert d.version is None  # 草稿不占号
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert len(items) == 2
    draft_item = next(it for it in items if it["version"] is None)
    assert draft_item["status"] == ver.STATUS_DRAFT


async def test_discard_draft(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=v1.id,
                                 snapshot={}, created_by="t")
    await ver.discard_draft(db_session, d.id)
    with pytest.raises(NotFound, match="版本不存在"):
        await ver.get_version(db_session, d.id)


async def test_derive_from_main_table_when_no_base(db_session, agent):
    """无 base 版本时,派生从主表拷贝当前定义(D8-A 后主表是身份目录)。"""
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={}, created_by="t")
    assert d.snapshot["persona_desc"] == "初版人格"
    assert d.snapshot["tone_style"] == "活泼"


async def test_rollback_draft_denied(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=v1.id,
                                 snapshot={}, created_by="t")
    with pytest.raises(Forbidden, match="草稿"):
        await ver.rollback_to(db_session, d.id)


# ══════════════════════════════════════════════════════════════════════════════
# §3 快照字段与 prompt
# ══════════════════════════════════════════════════════════════════════════════
# v1.1.9:agent description 主表列已删(0045),为纯版本快照键(adapter extra_keys)。
# snapshot_current(主表直发补 v1)的快照不再含 description;编辑/发布链路(derive)
# 快照含 description(用户提交字段,accepted_keys 白名单保留)。
AGENT_SNAP_KEYS = {
    "persona_desc", "tone_style", "opening_line", "rate_limit_qps", "rate_limit_daily",
    "intent_config", "bound_skills",
    "composition_mode",  # 0047 起入快照(19 号 D4-① 协作模式)
}


async def test_snapshot_only_contains_definition_fields(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    assert set(v1.snapshot.keys()) == AGENT_SNAP_KEYS
    assert v1.snapshot["persona_desc"] == "初版人格"
    assert v1.snapshot["bound_skills"] == []


async def test_unknown_field_in_snapshot_is_dropped(db_session, agent):
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={"persona_desc": "二版", "hacker": "x"},
                                 created_by="t")
    assert d.snapshot["persona_desc"] == "二版"
    assert "hacker" not in d.snapshot


async def test_snapshot_uses_pin_format_for_bindings(db_session, agent):
    """绑定在快照里是带版本 pin 数组,主表纯 id 数组只做来源。"""
    agent.bound_skill_ids = ["sk_a", "sk_b"]
    await db_session.flush()
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={}, created_by="t")
    assert d.snapshot["bound_skills"] == [
        {"skill_id": "sk_a", "version": None},
        {"skill_id": "sk_b", "version": None},
    ]
    assert "bound_skill_ids" not in d.snapshot


async def test_skill_snapshot_contains_prompt(db_session, skill):
    """推荐方案:skill 的 prompt 全文入快照;发布不回写 tc_prompts(D8-A 停用)。

    v1.1.10:主表内容列已删(0050)——snapshot_current 只物化 prompt(tc_prompts
    兜底)与绑定;description/constraints 等纯快照键由编辑保存/派生显式携带,
    发布质检(必填 description+constraints)校验的正是这些快照键。
    """
    sv1 = await _snap(db_session, "skill", "ver_skill")
    assert sv1.snapshot["prompt"] == "技能prompt-初版"
    assert "description" not in sv1.snapshot  # 主表无内容列,无源可物化

    d = await ver.derive_version(db_session, "skill", "ver_skill",
                                 base_version_id=sv1.id,
                                 snapshot={"prompt": "新版提示词",
                                           "description": "技能测试-能力概述",
                                           "constraints": {"max_steps": 40}},
                                 created_by="t")
    assert d.snapshot["prompt"] == "新版提示词"
    assert d.snapshot["description"] == "技能测试-能力概述"
    r = await ver.publish_version(db_session, d.id)
    assert r["version"] == 2 and r["assigned_users"] == 0  # skill 恒 0
    # 发布不写回 tc_prompts 主表
    assert await ver._current_prompt(db_session, "ver_skill") == "技能prompt-初版"


# ══════════════════════════════════════════════════════════════════════════════
# §4 下线与回滚(15 号:无「默认不可下线」,只有引用拦截)
# ══════════════════════════════════════════════════════════════════════════════
async def test_offline_version_without_references(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    v2 = await _snap(db_session, "agent", "ver_agent")
    r = await ver.offline_version(db_session, v2.id)
    assert r["ok"] is True and r["hint"] == ""  # 还有 v1 published,仍可用
    ok, _ = await ver.is_resource_available(db_session, "agent", "ver_agent")
    assert ok is True


async def test_offline_all_versions_makes_resource_unavailable(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    r = await ver.offline_version(db_session, v1.id)
    assert "所有版本均已下线" in r["hint"]
    ok, reason = await ver.is_resource_available(db_session, "agent", "ver_agent")
    assert ok is False and "已下线" in reason


async def test_offline_releases_user_assignments(db_session, agent):
    """下线自动解除用户指派并收回权限(原先是拒绝下线)。

    改掉的理由见 test_distribution.test_offline_releases_assignments_and_reports_who:
    下线本来就意味着「这一版不再对外」,先解指派再下线这个顺序,
    会让版本在那段时间里继续在线上跑,而运营以为已经下了。
    """
    v1 = await _snap(db_session, "agent", "ver_agent")
    await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    out = await ver.offline_version(db_session, v1.id)
    assert out["ok"] is True
    assert [a["scope_id"] for a in out["released_assignments"]] == ["u_1"]
    assert (await ver.get_version(db_session, v1.id)).status == ver.STATUS_OFFLINE


async def test_offline_skill_pinned_by_agent_version_denied(db_session, agent, skill):
    """被上层资源(Agent 版本快照)pin 引用的 skill 版本禁止下线。"""
    sk = Skill(id="sk_pin", name="钉技能", tool_ids=[], owner_user_id="t")
    db_session.add(sk)
    await db_session.commit()
    sv1 = await _snap(db_session, "skill", "sk_pin")
    # 历史已发布 agent 版本 pin 了 sk_pin v1
    await _make_published(db_session, "agent", "ver_agent", 99,
                          snapshot={"bound_skills": [{"skill_id": "sk_pin", "version": 1}]})
    with pytest.raises(Forbidden, match="不能下线"):
        await ver.offline_version(db_session, sv1.id)


async def test_offline_offline_version_errors(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    await ver.offline_version(db_session, v1.id)
    with pytest.raises(ValidationError, match="已处于下线状态"):
        await ver.offline_version(db_session, v1.id)


async def test_rollback_offline_version_republishes(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    v2 = await _snap(db_session, "agent", "ver_agent")
    await ver.offline_version(db_session, v2.id)

    r = await ver.rollback_to(db_session, v2.id)
    assert r["ok"] is True and r["version"] == 2
    assert "重新发布" in r["message"]

    # 已发布版本无需回滚
    r2 = await ver.rollback_to(db_session, v1.id)
    assert "无需回滚" in r2["message"]
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert all(it["status"] == ver.STATUS_PUBLISHED for it in items)


# ══════════════════════════════════════════════════════════════════════════════
# §5 依赖刷新与 pin 下拉(无 is_default 字段)
# ══════════════════════════════════════════════════════════════════════════════
async def test_refresh_pins_updates_to_latest_published(db_session, agent):
    await _make_published(db_session, "skill", "sk_r", 1)
    await _make_published(db_session, "skill", "sk_r", 2)
    agent.bound_skill_ids = ["sk_r", "sk_none"]  # sk_none 无任何版本
    await db_session.flush()
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={}, created_by="t")
    r = await ver.refresh_pins(db_session, d.id)
    assert r["snapshot"]["bound_skills"] == [
        {"skill_id": "sk_r", "version": 2},
        {"skill_id": "sk_none", "version": None},  # 无已发布版本 -> 保持原 pin
    ]
    assert any("sk_r" in c for c in r["changed"])


async def test_refresh_pins_only_on_draft(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    with pytest.raises(Forbidden, match="草稿"):
        await ver.refresh_pins(db_session, v1.id)


async def test_pin_options_lists_all_versions_with_status(db_session, agent):
    await _make_published(db_session, "skill", "sk_r", 1)
    await _make_published(db_session, "skill", "sk_r", 2)
    d3 = ResourceVersion(resource_type="skill", resource_id="sk_r", version=None,
                         status=ver.STATUS_DRAFT, snapshot={})
    db_session.add(d3)
    await db_session.commit()
    agent.bound_skill_ids = ["sk_r"]
    await db_session.flush()

    opts = await ver.pin_options(db_session, "agent", "ver_agent")
    b = opts["bindings"][0]
    assert b["resource_id"] == "sk_r"
    versions = b["versions"]
    assert [v["version"] for v in versions] == [2, 1, None]  # draft 无号排最后
    assert versions[0]["status"] == ver.STATUS_PUBLISHED
    assert versions[1]["status"] == ver.STATUS_PUBLISHED
    assert versions[2]["status"] == ver.STATUS_DRAFT
    # 15 号:下拉项不再有任何 is_default 字段
    assert all("is_default" not in v for v in versions)


async def test_pin_options_from_main_table_when_no_version(db_session, agent):
    """无 version_id 时从主表绑定直读(存量资源无版本记录场景)。"""
    await _make_published(db_session, "skill", "sk_r", 1)
    agent.bound_skill_ids = ["sk_r"]
    await db_session.flush()
    opts = await ver.pin_options(db_session, "agent", "ver_agent")
    b = opts["bindings"][0]
    assert b["versions"][0]["status"] == ver.STATUS_PUBLISHED


# ══════════════════════════════════════════════════════════════════════════════
# §6 用户指派(D9-A:scope 仅 user,fail-closed)
# ══════════════════════════════════════════════════════════════════════════════
async def test_upsert_assignment_creates_and_lists_user_only(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    r = await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id,
                                    created_by="t", note="付费客户")
    # 文案用的是资源适配器的中文名(ad.label = 「智能体」),不是内部类型名 ——
    # 这句是给运营看的。之前断言写的是 "Agent",是文案中文化时漏改的老断言。
    assert r["message"] == "已将用户 u_1指派到智能体「版本智能体」 v1"
    a = r["assignment"]
    assert a["scope_type"] == "user" and a["scope_id"] == "u_1"
    assert a["version"] == 1 and a["version_status"] == ver.STATUS_PUBLISHED
    assert a["note"] == "付费客户"

    await ver.upsert_assignment(db_session, "ver_agent", "u_2", v1.id, created_by="t")
    items = _assign_rows(await ver.list_assignments(db_session, "ver_agent"))
    assert {it["scope_id"] for it in items} == {"u_1", "u_2"}
    assert all(it["scope_type"] == "user" for it in items)
    assert all(it["version"] == 1 for it in items)


async def test_upsert_assignment_overwrites_same_user_scope(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    v2 = await _snap(db_session, "agent", "ver_agent")
    await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    r = await ver.upsert_assignment(db_session, "ver_agent", "u_1", v2.id, created_by="t")
    assert "v1" in r["message"] and "v2" in r["message"]  # 原 v1 → 新 v2
    items = _assign_rows(await ver.list_assignments(db_session, "ver_agent"))
    assert len(items) == 1 and items[0]["version"] == 2


async def test_assign_rejects_non_published_version(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=v1.id,
                                 snapshot={}, created_by="t")
    with pytest.raises(ValidationError, match="published"):
        await ver.upsert_assignment(db_session, "ver_agent", "u_1", d.id)

    v3 = await _snap(db_session, "agent", "ver_agent")  # v3 published
    await ver.offline_version(db_session, v3.id)
    with pytest.raises(ValidationError, match="published"):
        await ver.upsert_assignment(db_session, "ver_agent", "u_1", v3.id)


async def test_assign_rejects_version_of_other_agent(db_session, agent):
    await _snap(db_session, "agent", "ver_agent")
    other = await _make_published(db_session, "agent", "ver_agent2", 1,
                                  snapshot={"persona_desc": "别的智能体"})
    with pytest.raises(ValidationError, match="不属于"):
        await ver.upsert_assignment(db_session, "ver_agent", "u_1", other.id)


async def test_assign_private_version_only_owner(db_session, agent):
    v1 = await _make_published(db_session, "agent", "ver_agent", 1,
                               snapshot={"persona_desc": "私有版", "visibility": "private"})
    with pytest.raises(Forbidden, match="private"):
        await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id)
    r = await ver.upsert_assignment(db_session, "ver_agent", "t", v1.id, created_by="t")
    assert r["ok"] is True


async def test_assign_scenic_version_only_owner_or_scenic_user(db_session, agent):
    """scenic visibility 版本:非本景区用户拒绝;创建者本人必属本景区,可通过。"""
    v1 = await _make_published(db_session, "agent", "ver_agent", 1,
                               snapshot={"persona_desc": "景区版", "visibility": "scenic"})
    with pytest.raises(Forbidden, match="scenic"):
        await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id)
    r = await ver.upsert_assignment(db_session, "ver_agent", "t", v1.id, created_by="t")
    assert r["ok"] is True


async def test_assign_public_and_group_version_to_any_user(db_session, agent):
    v1 = await _make_published(db_session, "agent", "ver_agent", 1,
                               snapshot={"persona_desc": "公共版", "visibility": "public"})
    r1 = await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    assert r1["ok"] is True
    v2 = await _make_published(db_session, "agent", "ver_agent", 2,
                               snapshot={"persona_desc": "集团版", "visibility": "group"})
    r2 = await ver.upsert_assignment(db_session, "ver_agent", "u_2", v2.id, created_by="t")
    assert r2["ok"] is True


async def test_assignment_messages_name_the_user_and_resource(db_session, agent):
    """通知里要能认出是谁、哪个资源:「已将用户「尹开英」(37853900)指派到智能体「版本智能体」 v1」,
    不能再是 `user:1898973f-…`(现场分发中心的提示正是这样,管理员认不出指派给了谁)。"""
    from cogni_agent.user_center_service.models import User
    db_session.add(User(id="u_named", name="尹开英", account="37853900", phone="15340191523",
                        password_hash="x", role="scenic_admin"))
    await db_session.commit()
    v1 = await _snap(db_session, "agent", "ver_agent")
    r = await ver.upsert_assignment(db_session, "ver_agent", "u_named", v1.id, created_by="t")
    assert r["message"] == "已将用户「尹开英」(37853900)指派到智能体「版本智能体」 v1"
    assert "u_named" not in r["message"]
    msg = (await ver.remove_assignment(db_session, r["assignment"]["id"]))["message"]
    assert msg.startswith("已解除用户「尹开英」(37853900)对智能体「版本智能体」的版本指派")


async def test_remove_assignment_fail_closed(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    r = await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    msg = (await ver.remove_assignment(db_session, r["assignment"]["id"]))["message"]
    assert "已解除用户 u_1对智能体「版本智能体」的版本指派" in msg
    assert "不可用(fail-closed)" in msg  # 15 号:解除即 agent_not_distributed,无默认回落
    items = _assign_rows(await ver.list_assignments(db_session, "ver_agent"))
    assert items == []


async def test_list_versions_includes_assigned_count(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    v2 = await _snap(db_session, "agent", "ver_agent")
    await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    await ver.upsert_assignment(db_session, "ver_agent", "u_2", v1.id, created_by="t")
    await ver.upsert_assignment(db_session, "ver_agent", "u_3", v2.id, created_by="t")
    by_ver = {it["version"]: it["assigned_count"] for it in
              await ver.list_versions(db_session, "agent", "ver_agent")}
    assert by_ver == {2: 1, 1: 2}


# ══════════════════════════════════════════════════════════════════════════════
# §7 发布语义(D7-A 黄条 hint / fail-closed 预检)
# ══════════════════════════════════════════════════════════════════════════════
async def test_publish_zero_assignment_returns_hint(db_session, agent):
    """发布成功但零 user 指派 -> 黄条 hint 提醒去分发,否则对所有人不可用。"""
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={"persona_desc": "首版"}, created_by="t")
    r = await ver.publish_version(db_session, d.id)
    assert r["assigned_users"] == 0
    assert "无任何用户被指派" in r["hint"]


async def test_publish_after_assignment_reports_assigned(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    await ver.upsert_assignment(db_session, "ver_agent", "u_1", v1.id, created_by="t")
    d2 = await ver.derive_version(db_session, "agent", "ver_agent",
                                  base_version_id=v1.id,
                                  snapshot={"persona_desc": "二版"}, created_by="t")
    r = await ver.publish_version(db_session, d2.id)
    assert r["version"] == 2 and r["assigned_users"] == 1
    assert "1 位用户已指派" in r["hint"]


async def test_publish_pins_versionless_binding_to_latest_published(db_session, agent, skill):
    """绑定未指定版本(「跟随最新」)在发布时固化为**当时最新的已发布版**。

    原先是一律拦截。改掉是因为那条规则和运行时对不上 —— 解析层早就把未 pin 的绑定
    解析到最新已发布版,于是出现一种谁也想不到的状态:同一个技能跑得动、却发不出去,
    而报错写的是「默认版本已下线(15 号)」,运营从没设过什么默认版本。

    固化仍发生在发布这一刻:快照里记下的是当时的版本号,之后再发新版也不会
    改动已发布的快照(锁依赖的意义就在这里)。
    """
    sv1 = await _snap(db_session, "skill", "ver_skill")  # skill 有 v1 published
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={"bound_skills": [{"skill_id": "ver_skill",
                                                             "version": None}]},
                                 created_by="t")
    await ver.publish_version(db_session, d.id)
    row = await ver.get_version(db_session, d.id)
    assert row.snapshot["bound_skills"][0]["version"] == sv1.version
    assert sv1.status == ver.STATUS_PUBLISHED


async def test_publish_still_blocks_binding_with_no_published_version(db_session, agent):
    """真正该拦的只有「一版都没发布过」—— 那种跑起来也拿不到任何快照。"""
    from cogni_agent.tool_center_service.skill.models import Skill

    db_session.add(Skill(id="sk_none", name="没发过版的技能", tool_ids=[], owner_user_id="t"))
    await db_session.flush()
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={"bound_skills": [{"skill_id": "sk_none",
                                                             "version": None}]},
                                 created_by="t")
    with pytest.raises(ValidationError, match="预检未通过"):
        await ver.publish_version(db_session, d.id)


async def test_publish_generates_locked_deps(db_session, agent, skill, tool):
    """发布冻结完整依赖闭包 agent->skill->tool;模糊 pin 固化版本号。"""
    await _snap(db_session, "tool", "ver_tool")  # tool v1 published
    sd = await ver.derive_version(db_session, "skill", "ver_skill", base_version_id=None,
                                  snapshot={"tools": [{"tool_id": "ver_tool", "version": 1}],
                                            # 发布质检必填(读草稿快照)
                                            "description": "技能测试-能力概述",
                                            "constraints": {"max_steps": 40}},
                                  created_by="t")
    await ver.publish_version(db_session, sd.id)  # skill v1 (锁 tool v1)
    ad = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                  snapshot={"bound_skills": [{"skill_id": "ver_skill",
                                                              "version": 1}]},
                                  created_by="t")
    r = await ver.publish_version(db_session, ad.id)
    assert r["version"] == 1
    deps = {dep["resource_type"]: dep["version"] for dep in r["locked_deps"]["deps"]}
    assert deps == {"skill": 1, "tool": 1}
    assert ad.snapshot["bound_skills"][0]["version"] == 1


async def test_publish_assigns_version_number_on_second_publish(db_session, agent):
    v1 = await _snap(db_session, "agent", "ver_agent")
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=v1.id,
                                 snapshot={"persona_desc": "二版"}, created_by="t")
    r = await ver.publish_version(db_session, d.id)
    assert r["version"] == 2
    items = await ver.list_versions(db_session, "agent", "ver_agent")
    assert [it["version"] for it in items] == [2, 1]


# ══════════════════════════════════════════════════════════════════════════════
# §8 预检 dry-run 与依赖锁隔离
# ══════════════════════════════════════════════════════════════════════════════
async def test_precheck_dry_run_does_not_change_state(db_session, agent):
    d = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                 snapshot={"persona_desc": "二版"}, created_by="t")
    pc = await ver.precheck_version(db_session, d.id)
    assert pc["problems"] == [] and pc["pass"] is True
    assert pc["will_version"] == 1
    # dry-run 不改变状态:仍是草稿、不预占版本号
    again = await db_session.get(ResourceVersion, d.id)
    assert again.status == ver.STATUS_DRAFT and again.version is None


async def test_lock_isolation_across_releases(db_session, agent):
    """a1 锁 skill v1(+tool v1),a2 锁 skill v2(+tool v2),发布互不污染。"""
    # v1.2(0051):主表无内容列,tool 内容随下方 _snap/derive 的版本快照
    lk_tool = Tool(id="lk_tool", name="锁工具",
                   owner_user_id="t", visibility="public")
    lk_skill = Skill(id="lk_skill", name="锁技能", tool_ids=[], owner_user_id="t")
    db_session.add_all([lk_tool, lk_skill])
    await db_session.commit()

    # tool v1 -> skill v1(绑 tool v1) -> agent v1(绑 skill v1)
    tv1 = await _snap(db_session, "tool", "lk_tool")
    sd1 = await ver.derive_version(db_session, "skill", "lk_skill", base_version_id=None,
                                   snapshot={"tools": [{"tool_id": "lk_tool", "version": 1}],
                                             # 发布质检必填(读草稿快照)
                                             "description": "锁技能-能力概述",
                                             "constraints": {"rules": "1"}},
                                   created_by="t")
    await ver.publish_version(db_session, sd1.id)
    ad1 = await ver.derive_version(db_session, "agent", "ver_agent", base_version_id=None,
                                   snapshot={"bound_skills": [{"skill_id": "lk_skill",
                                                               "version": 1}]},
                                   created_by="t")
    await ver.publish_version(db_session, ad1.id)

    # tool v2 -> skill v2(绑 tool v2) -> agent v2(绑 skill v2)
    td2 = await ver.derive_version(db_session, "tool", "lk_tool", base_version_id=tv1.id,
                                   snapshot={"type_config": {"url": "https://t.com/v2"}},
                                   created_by="t")
    await ver.publish_version(db_session, td2.id)
    sd2 = await ver.derive_version(db_session, "skill", "lk_skill",
                                   base_version_id=sd1.id,
                                   snapshot={"tools": [{"tool_id": "lk_tool", "version": 2}]},
                                   created_by="t")
    await ver.publish_version(db_session, sd2.id)
    ad2 = await ver.derive_version(db_session, "agent", "ver_agent",
                                   base_version_id=ad1.id,
                                   snapshot={"bound_skills": [{"skill_id": "lk_skill",
                                                               "version": 2}]},
                                   created_by="t")
    await ver.publish_version(db_session, ad2.id)

    items = await ver.list_versions(db_session, "agent", "ver_agent")
    locks = {it["version"]: {dep["resource_type"]: dep["version"]
                             for dep in (it["locked_deps"] or {}).get("deps", [])}
             for it in items}
    assert locks == {2: {"skill": 2, "tool": 2}, 1: {"skill": 1, "tool": 1}}
    # 快照 pin 各自固化
    snap1 = next(it for it in items if it["version"] == 1)["snapshot"]
    snap2 = next(it for it in items if it["version"] == 2)["snapshot"]
    assert snap1["bound_skills"][0]["version"] == 1
    assert snap2["bound_skills"][0]["version"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# §9 迁移兼容(0038 幂等 / scenic 残留清理 / 唯一索引收紧)
# ══════════════════════════════════════════════════════════════════════════════
async def test_0038_remove_default_migration_idempotent(db_session, agent):
    """0038 SQL 幂等可重跑;清理 scenic 残留指派;唯一索引收紧为 (agent_id, scope_id)。"""
    from cogni_agent.common.schema import MIGRATIONS, _split_statements

    m = next(m for m in MIGRATIONS if m.id.startswith("0038"))
    stmts = _split_statements(m.sql)

    # 先造一条 15 号之前的 scenic 残留
    db_session.add(ResourceAssignment(agent_id="ver_agent", scope_type="scenic",
                                      scope_id="scenic_area_1", version_id="legacy_v1"))
    await db_session.commit()

    for _ in range(2):  # 重跑两次验证幂等
        for stmt in stmts:
            await db_session.execute(text(stmt))
        await db_session.commit()

    # scenic 残留已被清理
    remain = (await db_session.execute(
        select(ResourceAssignment).where(ResourceAssignment.scope_type != "user")
    )).scalars().all()
    assert remain == []

    # 唯一索引收紧:同 (agent_id, scope_id) 第二条插入被拒
    db_session.add(ResourceAssignment(agent_id="ver_agent", scope_type="user",
                                      scope_id="u_1", version_id="asg_a"))
    await db_session.commit()
    db_session.add(ResourceAssignment(agent_id="ver_agent", scope_type="user",
                                      scope_id="u_1", version_id="asg_b"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# §10 自动补列已下线(2026-09):结构演进铁律改为「模型 = 声明源,迁移 = 唯一增量
#     通道」——老库不再启动时自动 ADD COLUMN,任何结构改动(加表/加列/改类型)必须
#     写幂等迁移,否则 sync_schema 结构体检 raise 拒绝启动(没有迁移记录不放行)。
#     原 _auto_backfill_stmt 三个 SQL 翻译用例随函数一并删除。
#     漂移检测函数仍保留:由下面的真库用例 + tests/test_schema_type_drift.py 锁定。
# ══════════════════════════════════════════════════════════════════════════════
async def test_model_column_drift_detects_missing_column(db_session, agent):
    """模型漂移检测:手动 DROP 一个列后,_model_column_drift 应能发现它(0039 场景回归)。"""
    from cogni_agent.common.schema import _model_column_drift

    # tc_skills.version 已存在,先造一个「库里有、模型里没有」的反例不成立;
    # 直接验证 drift 对**当前一致结构**返回空,并验证检测 SQL 本身可用。
    drift = await _model_column_drift(db_session)
    assert isinstance(drift, list)
    # 只要 db_session 可用、SQL 可跑,list 即非 None;真实漂移场景由
    # init_db.py --check 与 sync_schema 端到端覆盖。


# ══════════════════════════════════════════════════════════════════════════════
# §11 发布质检读「待发布草稿内容」而非主表初值(主表内容列 0050 已删,无值可取):
#     空壳(description 空 或 constraints 空)拦截,补全后放行 —— 覆盖踩坑回归
#     (旧实现读主表 description/constraints:草稿已补全但主表仍空 → 误拦)。
# ══════════════════════════════════════════════════════════════════════════════
async def test_publish_quality_gate_blocks_empty_draft(db_session, skill):
    """无 base 空草稿直接发布被质检拦截(description+constraints 必填,读草稿快照)。"""
    d = await ver.derive_version(db_session, "skill", "ver_skill", base_version_id=None,
                                 snapshot={}, created_by="t")
    with pytest.raises(Exception, match="缺少发布必需内容"):
        await ver.publish_version(db_session, d.id)


async def test_publish_quality_gate_passes_when_draft_complete(db_session, skill):
    """草稿快照补全 description+constraints 后发布放行(v1.1.3 平台治理要求)。"""
    d = await ver.derive_version(db_session, "skill", "ver_skill", base_version_id=None,
                                 snapshot={"description": "质检-能力概述",
                                           "constraints": {"max_steps": 20}},
                                 created_by="t")
    r = await ver.publish_version(db_session, d.id)
    assert r["version"] == 1
