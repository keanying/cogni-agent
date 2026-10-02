"""T-22:return_format 标识自检——问题标注的图表形态 ↔ 绑定技能提示词图表引导。

22 号设计约束:return_format 只是问题行运营标注,运行时(graphs/网关/预热)不消费、
不注入提示词;图表引导必须内置于技能自身提示词。本测试验证:
1. 判定纯函数 has_chart_format_guidance 的形态矩阵(auto 恒过/空提示词报缺口);
2. 种子数据与技能模板一致性:create_p0_group_skills.PROMPT_TMPL 内置 PRD 3.2.2.4
   引导,覆盖问题库全部固定形态标注;摘要技能(无引导)能被判定如实报缺口;
3. 运营接口(列表/创建/更新)回显 chart_guidance 状态(DB 集成)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from httpx import AsyncClient

from sqlalchemy.ext.asyncio import AsyncSession

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.create_p0_group_skills import GROUPS, PROMPT_TMPL, SUMMARY_SKILL
from scripts.seed_app_question_bank import QUESTIONS

from cogni_agent.gateway_service.models import AppQuestionBank
from cogni_agent.gateway_service.question_bank import has_chart_format_guidance
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.skill.models import Skill
from cogni_agent.user_center_service.models import User

from tests.conftest import auth_headers

# 问题库 return_format 白名单(与 routes_app_ops.RETURN_FORMATS 对齐)
_ALLOWED_FORMATS = ("auto", "card", "line", "bar", "pie", "radar")


# ════════════════════════════════════════════════════════
# 1) 判定纯函数形态矩阵
# ════════════════════════════════════════════════════════

def test_guidance_matrix_on_skill_template():
    """P0 分组技能模板(PROMPT_TMPL)内置 PRD 3.2.2.4 段:charts+cards 双引导,
    全部固定形态都应判定通过。"""
    for fmt in ("card", "line", "bar", "pie", "radar"):
        assert has_chart_format_guidance(PROMPT_TMPL, fmt), f"模板应含 {fmt} 引导"


def test_guidance_missing_when_prompt_has_no_chart():
    """摘要技能只出 summary/metric JSON(无 charts/cards 教法)——判定必须如实报缺口。"""
    assert not has_chart_format_guidance(SUMMARY_SKILL["prompt"], "bar")
    assert not has_chart_format_guidance(SUMMARY_SKILL["prompt"], "card")
    assert not has_chart_format_guidance(SUMMARY_SKILL["prompt"], "line")


def test_auto_and_empty_never_flagged():
    """auto/未知形态不要求图表 → 恒 True;空提示词只在固定形态时报缺口。"""
    assert has_chart_format_guidance(None, "auto")
    assert has_chart_format_guidance("", "auto")
    assert has_chart_format_guidance("纯文本技能,无任何 json", "auto")
    assert has_chart_format_guidance(None, "")  # 空标注
    assert has_chart_format_guidance("任意内容", "table")  # 未知形态不干预
    assert not has_chart_format_guidance(None, "bar")  # 要图但提示词缺失 → 缺口
    assert not has_chart_format_guidance("只输出文本结论", "bar")


def test_card_needs_cards_not_charts():
    """card 形态看 cards 教法:只教 charts 不教 cards 的技能对 card 问题仍报缺口。"""
    charts_only = "回答末尾追加 ```json,charts 数组按数据场景输出折线/条形/饼图"
    assert not has_chart_format_guidance(charts_only, "card")
    assert has_chart_format_guidance(charts_only, "bar")


# ════════════════════════════════════════════════════════
# 2) 种子数据 ↔ 技能模板一致性(纯内存,不依赖 DB)
# ════════════════════════════════════════════════════════

def test_seed_formats_within_whitelist():
    """问题库 seed 的全部 return_format 标注在白名单内(与运营接口同口径)。"""
    formats = {r[8] for r in QUESTIONS}
    assert formats <= set(_ALLOWED_FORMATS), f"越界标注: {formats - set(_ALLOWED_FORMATS)}"


def test_workflow_questions_covered_by_skill_template():
    """GROUPS(直跳问题集)内标注固定形态的问题,其绑定技能同用 PROMPT_TMPL
    (create_p0_group_skills 建技能并把问题行 skill_id 绑到它),因此引导必须全部命中。
    SUMMARY_SKILL 是预热摘要专用、不承载问题库标注,不在覆盖断言内。"""
    fmt_by_question = {r[1]: r[8] for r in QUESTIONS}
    checked = 0
    for g in GROUPS:
        for qtext in g["questions"]:
            rfmt = fmt_by_question.get(qtext, "auto")
            if rfmt == "auto":
                continue
            checked += 1
            assert has_chart_format_guidance(PROMPT_TMPL, rfmt), \
                f"{qtext}({rfmt}) 绑定技能模板缺该形态引导"
    assert checked > 0, "测试前提失效:GROUPS 内应存在固定形态标注的问题"


# ════════════════════════════════════════════════════════
# 3) 运营接口回显 chart_guidance(DB 集成)
# ════════════════════════════════════════════════════════

def _make_question(qid: str, text: str, rfmt: str, skill_id: str | None = None) -> AppQuestionBank:
    return AppQuestionBank(
        id=qid, theme_code="passenger_flow", question_text=text,
        time_period="today", priority="P0", return_format=rfmt,
        skill_id=skill_id,
        sort_order=0, active=True,
    )


async def _seed_binding_data(db: AsyncSession, *, skill_a_prompt: str | None = PROMPT_TMPL) -> None:
    """基础桩:技能 A(发布模板 prompt)/技能 B(无任何版本),问题行直绑 skill_id。"""
    db.add_all([
        Skill(id="skill-a", name="客流图表技能", owner_user_id="u1"),
        Skill(id="skill-b", name="客流文本技能", owner_user_id="u1"),
    ])
    await db.flush()
    if skill_a_prompt is not None:
        db.add(ver.ResourceVersion(
            resource_type="skill", resource_id="skill-a", version=1,
            status="published", snapshot={"prompt": skill_a_prompt},
        ))
    await db.flush()
    db.add_all([
        _make_question("q-bar-ok", "今日各时段入园对比?", "bar", "skill-a"),
        _make_question("q-pie-ok", "当前游客来源构成?", "pie", "skill-a"),
        _make_question("q-bar-gap", "今日各票种销量对比?", "bar", "skill-b"),
        _make_question("q-auto", "今日客流是否触达预警?", "auto", "skill-a"),
        _make_question("q-no-skill", "今日哪些区域最拥挤?", "bar", None),
    ])
    await db.commit()


@pytest.mark.asyncio
async def test_list_questions_chart_guidance(db_session: AsyncSession,
                                             async_client: AsyncClient,
                                             super_admin: User):
    """列表:固定形态行带 chart_guidance;引导齐(ok)/技能未发布/未绑技能如实暴露;auto 行 None。"""
    await _seed_binding_data(db_session)

    res = await async_client.get("/api/admin/app-ops/questions",
                                 headers=auth_headers(super_admin))
    assert res.status_code == 200
    items = {it["id"]: it for it in res.json()["data"]["items"]}

    # 引导齐全:模板含 charts → ok
    ok = items["q-bar-ok"]["chart_guidance"]
    assert ok["ok"] is True and ok["skill_name"] == "客流图表技能"
    assert ok["guidance_ok"] is True and ok["published"] is True
    assert items["q-pie-ok"]["chart_guidance"]["ok"] is True

    # 技能未发布(prompt 空)→ 缺口,并标明 published=False
    gap = items["q-bar-gap"]["chart_guidance"]
    assert gap["ok"] is False
    assert gap["guidance_ok"] is False and gap["published"] is False

    # auto 标注不强制图表 → 无自检状态
    assert items["q-auto"]["chart_guidance"] is None

    # 未绑技能 → 无自检状态
    assert items["q-no-skill"]["chart_guidance"] is None


@pytest.mark.asyncio
async def test_create_question_returns_chart_guidance(db_session: AsyncSession,
                                                       async_client: AsyncClient,
                                                       super_admin: User):
    """创建固定形态问题即回显绑定技能引导状态;auto 问题不显示状态。"""
    await _seed_binding_data(db_session)

    payload = {
        "theme_code": "passenger_flow", "question_text": "今日渠道核销对比?",
        "time_period": "today", "priority": "P0", "return_format": "bar",
        "skill_id": "skill-a", "active": True,
    }
    res = await async_client.post("/api/admin/app-ops/questions",
                                  json=payload, headers=auth_headers(super_admin))
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["return_format"] == "bar"
    assert data["chart_guidance"]["ok"] is True

    # 绑定到缺引导的技能(技能 B 未发布)→ 创建后立刻见缺口
    payload["question_text"] = "今日各区域密度对比?"
    payload["skill_id"] = "skill-b"
    res = await async_client.post("/api/admin/app-ops/questions",
                                  json=payload, headers=auth_headers(super_admin))
    data = res.json()["data"]
    assert data["chart_guidance"]["ok"] is False

    # auto 标注的问题:chart_guidance 为空,不要求引导
    payload["question_text"] = "今日有无拥堵风险?"
    payload["return_format"] = "auto"
    payload["skill_id"] = "skill-a"
    res = await async_client.post("/api/admin/app-ops/questions",
                                  json=payload, headers=auth_headers(super_admin))
    assert res.json()["data"]["chart_guidance"] is None


@pytest.mark.asyncio
async def test_update_question_recomputes_chart_guidance(db_session: AsyncSession,
                                                         async_client: AsyncClient,
                                                         super_admin: User):
    """更新(改 return_format)后重算状态:card 形态在纯 charts 引导下报缺口。"""
    await _seed_binding_data(db_session, skill_a_prompt=(
        "回答末尾追加 ```json 代码块,charts 数组按数据场景输出折线/条形/饼图"))

    payload = {"return_format": "card"}
    res = await async_client.put("/api/admin/app-ops/questions/q-bar-ok",
                                 json=payload, headers=auth_headers(super_admin))
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["return_format"] == "card"
    assert data["chart_guidance"]["ok"] is False  # 技能只教 charts 不教 cards → 缺口

    payload = {"return_format": "bar"}
    res = await async_client.put("/api/admin/app-ops/questions/q-bar-ok",
                                 json=payload, headers=auth_headers(super_admin))
    assert res.json()["data"]["chart_guidance"]["ok"] is True


# ════════════════════════════════════════════════════════
# 4) 删技能引用守卫(23 号 D1:被固定问题引用时拦截 / force 解绑不级联删)
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_delete_skill_blocked_when_question_bound(db_session: AsyncSession,
                                                        async_client: AsyncClient,
                                                        super_admin: User):
    """非 force:技能被固定问题引用 → 403,提示引用数(含启用中数量)。"""
    await _seed_binding_data(db_session)

    res = await async_client.delete("/api/admin/skills/skill-a",
                                    headers=auth_headers(super_admin))
    assert res.status_code == 403, res.text
    assert "固定问题引用" in res.text


@pytest.mark.asyncio
async def test_delete_skill_force_unbinds_questions(db_session: AsyncSession,
                                                    async_client: AsyncClient,
                                                    super_admin: User):
    """force:删除成功;问题行保留、skill_id 置空(回落 Triager 动态链路),技能软删。"""
    await _seed_binding_data(db_session)

    res = await async_client.delete("/api/admin/skills/skill-a?force=true",
                                    headers=auth_headers(super_admin))
    assert res.status_code == 200, res.text

    db_session.expire_all()
    # 问题行未被级联删除,只是解绑
    q = await db_session.get(AppQuestionBank, "q-bar-ok")
    assert q is not None and q.question_text == "今日各时段入园对比?"
    assert q.skill_id is None
    # 技能已软删归档(版本记录保留)
    s = await db_session.get(Skill, "skill-a")
    assert s is not None and s.deleted_at is not None
