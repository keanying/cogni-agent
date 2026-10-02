"""APP 运营后台(19 号文档 S4 管理端):APP 端业务配置的管理接口。

定位:框架管理端(tool_center 等)管「能力」(工具/技能/Agent/编排),
本模块管「APP 端业务」——首期:固定问题库(问题/路由/预热/参数模板)。
后续 APP 端业务(预热任务状态、缓存查看等)也挂这里。

权限:与 admin 聚合同款——group_viewer 只读(可列表,不可写)。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.gateway_service.param_audit import question_param_gaps
from cogni_agent.gateway_service.models import AppMetricDef, AppQuestionBank
from cogni_agent.gateway_service.question_bank import (
    enrich_chart_guidance, question_chart_guidance,
)

# 后台预热任务的强引用:不持有的话协程可能在跑完之前被 GC 回收
_PREWARM_TASKS: set = set()

router = APIRouter(prefix="/admin/app-ops", tags=["APP 运营"])

THEMES = ("passenger_flow", "ticket", "public_opinion", "ota_direct_sales")
PERIODS = ("today", "yesterday", "week_to_date", "last_7_days", "month_to_date",
           "next_3_days", "tencent_report")
# 问题级图表返回形态(22 号文档):auto=不干预/固定图表形态(白名单见 chart_templates)
RETURN_FORMATS = ("auto", "card", "line", "bar", "pie", "radar")


def _assert_writable(user: CurrentUser, what: str = "该配置") -> None:
    """写操作的统一门禁。判定见 common/permissions.py(角色 × 景区)。"""
    from cogni_agent.common import permissions as perm

    perm.assert_can_write(user.role, what)


def _assert_deletable(user: CurrentUser, obj_scenic: str | None = None,
                      what: str = "该配置") -> None:
    """删除门禁:景区操作员不能删;景区角色只能删本景区的。"""
    from cogni_agent.common import permissions as perm

    perm.assert_can_delete_obj(user.role, user.scenic_id, obj_scenic, what)


def _assert_scope(user: CurrentUser, obj_scenic: str | None, what: str = "该配置") -> None:
    """改某个具体对象前的景区范围校验(集团级内容景区角色不能改)。"""
    from cogni_agent.common import permissions as perm

    perm.assert_in_scope(user.role, user.scenic_id, obj_scenic, what)


def _can_write(role: str) -> bool:
    from cogni_agent.common import permissions as perm

    return perm.can_create(role)


def _q_dict(q: AppQuestionBank, skill_name: str | None = None) -> dict:
    return {
        "id": q.id, "scenic_id": q.scenic_id, "theme_code": q.theme_code,
        "question_text": q.question_text, "metric_code": q.metric_code,
        "scenario": q.scenario, "time_period": q.time_period, "priority": q.priority,
        "dimension_name": q.dimension_name, "metric_name": q.metric_name,
        "return_format": q.return_format,
        "skill_id": q.skill_id, "skill_name": skill_name,
        "param_template": q.param_template,
        "prewarm_enabled": q.prewarm_enabled, "prewarm_point": q.prewarm_point,
        "sort_order": q.sort_order, "active": q.active,
        "created_at": q.created_at, "updated_at": q.updated_at,
    }


class QuestionCreateRequest(BaseModel):
    theme_code: Literal["passenger_flow", "ticket", "public_opinion", "ota_direct_sales"]
    question_text: str = Field(..., min_length=2, max_length=512)
    time_period: Literal["today", "yesterday", "week_to_date", "last_7_days",
                          "month_to_date", "next_3_days", "tencent_report"]
    priority: Literal["P0", "P1", "P2"] = "P1"
    metric_code: str | None = None
    scenario: str | None = None
    dimension_name: str | None = None       # 非 None = 进三级选择器展示(第二级维度)
    metric_name: str | None = None          # 选择器第三级「指标」展示名(一个指标=一个叶子问题=该行)
    return_format: Literal["auto", "card", "line", "bar", "pie", "radar"] = "auto"
    skill_id: str | None = None             # 非空 = 命中后直跳该技能(空 = 走 Triager 动态链路)
    prewarm_enabled: bool = False
    prewarm_point: str | None = None
    param_template: dict | None = None
    sort_order: int = 0
    active: bool = True


class QuestionUpdateRequest(BaseModel):
    theme_code: Literal["passenger_flow", "ticket", "public_opinion", "ota_direct_sales"] | None = None
    question_text: str | None = Field(default=None, min_length=2, max_length=512)
    time_period: Literal["today", "yesterday", "week_to_date", "last_7_days",
                          "month_to_date", "next_3_days", "tencent_report"] | None = None
    priority: Literal["P0", "P1", "P2"] | None = None
    metric_code: str | None = None
    scenario: str | None = None
    dimension_name: str | None = None        # 传空串 = 移出选择器
    metric_name: str | None = None           # 传空串 = 清空指标名(回退问题原文)
    return_format: Literal["auto", "card", "line", "bar", "pie", "radar"] | None = None
    skill_id: str | None = None              # 传空串 = 解绑技能(回落 Triager 动态链路)
    prewarm_enabled: bool | None = None
    prewarm_point: str | None = None
    param_template: dict | None = None
    sort_order: int | None = None
    active: bool | None = None


async def _skill_names(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    """技能名映射(列表展示「执行技能」用)。"""
    if not ids:
        return {}
    from cogni_agent.tool_center_service.skill.models import Skill

    rows = (await db.execute(
        select(Skill).where(Skill.id.in_(ids))
    )).scalars().all()
    return {s.id: s.name for s in rows}


@router.get("/questions")
async def list_questions(
    theme: str | None = None, priority: str | None = None, active: bool | None = None,
    db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user),
):
    """固定问题列表(通用模板 scenic_id IS NULL;景区专属行为空)。"""
    stmt = select(AppQuestionBank).where(AppQuestionBank.scenic_id.is_(None))
    if theme:
        stmt = stmt.where(AppQuestionBank.theme_code == theme)
    if priority:
        stmt = stmt.where(AppQuestionBank.priority == priority)
    if active is not None:
        stmt = stmt.where(AppQuestionBank.active == active)
    rows = (await db.execute(
        stmt.order_by(AppQuestionBank.sort_order, AppQuestionBank.created_at)
    )).scalars().all()
    names = await _skill_names(db, {r.skill_id for r in rows if r.skill_id})
    # 22 号:固定形态问题附「绑定技能图表引导」状态(return_format 标识自检,见
    # question_bank.enrich_chart_guidance);auto/未绑技能的行该字段为 None。
    bindings = await enrich_chart_guidance(db, list(rows))
    return {"items": [
        {**_q_dict(q, names.get(q.skill_id)), "chart_guidance": bindings.get(q.id)}
        for q in rows]}


@router.post("/questions")
async def create_question(req: QuestionCreateRequest,
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    _assert_writable(user, "问题")
    # 这张表存的是 scenic_id IS NULL 的通用模板(所有景区共用一份),
    # 所以景区角色只能用不能建 —— 否则一个景区建一条,全平台都跟着多一条
    _assert_scope(user, None, "通用问题模板")
    if req.skill_id:
        from cogni_agent.tool_center_service.skill.models import Skill
        _sk = await db.get(Skill, req.skill_id)
        if _sk is None or _sk.deleted_at:
            raise ValidationError("执行技能不存在或已删除,请先在技能中心确认")
    q = AppQuestionBank(
        id=str(uuid.uuid4()), scenic_id=None,
        theme_code=req.theme_code, question_text=req.question_text,
        metric_code=req.metric_code or None, scenario=req.scenario or None,
        time_period=req.time_period, priority=req.priority,
        dimension_name=req.dimension_name or None, metric_name=req.metric_name or None,
        return_format=req.return_format,
        skill_id=req.skill_id or None,
        prewarm_enabled=req.prewarm_enabled,
        prewarm_point=req.prewarm_point or None, param_template=req.param_template or None,
        sort_order=req.sort_order, active=req.active,
    )
    db.add(q)
    # 32 号:参数覆盖门禁(同 update:预热+模板空硬拒绝;激活软警告)
    _gaps, _empty = await question_param_gaps(db, q)
    if q.prewarm_enabled and _empty:
        raise ValidationError(
            "预热问题未配置参数模板(param_template):取数参数将全部依赖模型"
            "执行期自填,违背装配期全量生成设计。请先配齐模板再开启预热。")
    # commit 前构造返回值:commit 默认使对象属性过期,commit 后再访问
    # created_at 等会触发同步懒加载(MissingGreenlet),异步路由里直接 500
    await db.flush()
    result = _q_dict(q)
    result["param_warnings"] = _gaps if (q.active and _gaps) else []
    # 22 号:新建即回显「绑定技能图表引导」状态(commit 后访问 q 属性会懒加载报错,必须取在前)
    result["chart_guidance"] = await question_chart_guidance(db, q)
    # 同步进统一问题表:APP 前台的问题树只读那张表,不同步等于「后台建了、前台没有」
    from cogni_agent.gateway_service import issue_service as _issues

    await _issues.sync_from_question_bank(db, q)
    await db.commit()
    return result


@router.put("/questions/{question_id}")
async def update_question(question_id: str, req: QuestionUpdateRequest,
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    _assert_writable(user, "问题")
    _assert_scope(user, None, "通用问题模板")  # 同上:通用模板是集团级的
    q = await db.get(AppQuestionBank, question_id)
    if not q:
        raise NotFound("问题不存在")
    # 空串语义:解绑/移出选择器(前端清空控件传 "")
    updates = {
        "theme_code": req.theme_code, "question_text": req.question_text,
        "time_period": req.time_period, "priority": req.priority,
        "metric_code": req.metric_code, "scenario": req.scenario,
        "prewarm_point": req.prewarm_point, "sort_order": req.sort_order,
        "prewarm_enabled": req.prewarm_enabled, "active": req.active,
        "return_format": req.return_format,
    }
    for field, val in updates.items():
        if val is not None:
            setattr(q, field, val)
    if req.dimension_name is not None:
        q.dimension_name = req.dimension_name or None
    if req.metric_name is not None:
        q.metric_name = req.metric_name or None
    if req.skill_id is not None:
        q.skill_id = req.skill_id or None
        if q.skill_id:
            from cogni_agent.tool_center_service.skill.models import Skill
            _sk = await db.get(Skill, q.skill_id)
            if _sk is None or _sk.deleted_at:
                raise ValidationError("执行技能不存在或已删除,请先在技能中心确认")
    if req.param_template is not None:
        q.param_template = req.param_template or None
    # 32 号:参数覆盖门禁——
    # · 硬规则:预热开启且模板空 → 拒绝(无模板的预热问题全靠模型执行期自填,
    #   违背「装配期全量生成」,预热是自动链路无人在环,缺口直达产出);
    # · 软警告:激活时返回 param_warnings(技能工具必填参数 vs 模板的差集,
    #   并集口径含语义甄别空间,由运营判断是否需要补)。
    _gaps, _empty = await question_param_gaps(db, q)
    if q.prewarm_enabled and _empty:
        raise ValidationError(
            "预热问题未配置参数模板(param_template):取数参数将全部依赖模型"
            "执行期自填,违背装配期全量生成设计。请先配齐模板再开启预热。")
    if q.active and _gaps:
        from cogni_agent.common.logging import get_logger as _gl
        _gl("gateway.param_audit").warning(
            f"问题「{q.question_text[:24]}」激活但参数覆盖有缺口: {_gaps}")
    # commit 前构造返回值(同 create:避免属性过期后的同步懒加载)
    result = _q_dict(q)
    result["param_warnings"] = _gaps if (q.active and _gaps) else []
    # 22 号:改 return_format/绑定技能后重算「图表引导」状态,运营即时看到缺口
    result["chart_guidance"] = await question_chart_guidance(db, q)
    from cogni_agent.gateway_service import issue_service as _issues

    await _issues.sync_from_question_bank(db, q)
    await db.commit()
    return result


@router.delete("/questions/{question_id}")
async def delete_question(question_id: str,
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """物理删除(通用模板由脚本全量重建,单条删除=运营主动下架选择)。

    预热缓存若有残留按 question_id 级联失效(删除即失效,prewarm_loop 不会复活它)。
    """
    # 删除单列判定:景区操作员不能删(不可逆)
    _assert_deletable(user, None, "问题")
    from sqlalchemy import delete as sa_delete

    from cogni_agent.gateway_service.models import AppPrewarmCache

    q = await db.get(AppQuestionBank, question_id)
    if not q:
        raise NotFound("问题不存在")
    await db.execute(sa_delete(AppPrewarmCache).where(AppPrewarmCache.question_id == question_id))
    # 统一问题表里的镜像行一起删 —— 留着的话前台问题树上还挂着一个点不动的问题
    from cogni_agent.gateway_service import issue_service as _issues

    await _issues.drop_issue_of_source(db, question_id)
    await db.delete(q)
    await db.commit()
    return {"ok": True}


@router.get("/metrics")
async def list_metrics(db: AsyncSession = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    """指标定义列表(问题编辑时关联 metric_code 用)。"""
    rows = (await db.execute(select(AppMetricDef).order_by(
        AppMetricDef.theme_code, AppMetricDef.metric_code))).scalars().all()
    return {"items": [
        {"id": m.id, "theme_code": m.theme_code, "metric_code": m.metric_code,
         "metric_name": m.metric_name, "warning_rule": m.warning_rule,
         "danger_rule": m.danger_rule, "update_strategy": m.update_strategy,
         "priority": m.priority}
        for m in rows]}


# ── 景区配置(25 号方案:承载量与景区级参数,本批=配置管理) ───────────────────

class ScenicConfigSaveRequest(BaseModel):
    """日承载量(人次/天);None=清空(未配置=消费侧维持现状,不猜默认值)。"""
    daily_capacity: int | None = Field(default=None, gt=0)


def _cfg_dict(c: "AppScenicConfig | None") -> dict:
    return {
        "configured": c is not None,
        "daily_capacity": c.daily_capacity if c else None,
        "extra": c.extra if c else {},
        "updated_at": c.updated_at if c else None,
        "updated_by": c.updated_by if c else None,
    }


def _master_dict(s: "SysScenic | None") -> dict:
    return {
        "scenic_name": s.scenic_name if s else None,
        "scenic_grade_level": s.scenic_grade_level if s else None,
        "is_enabled": s.is_enabled if s else None,
        "synced_at": s.synced_at if s else None,
    }


async def _load_scenic_row(db: AsyncSession, scenic_id: str) -> tuple[AppScenicConfig | None, SysScenic | None, int]:
    """单景区:配置行 + 主数据行 + 绑定用户数。"""
    from cogni_agent.gateway_service.models import AppScenicConfig
    from cogni_agent.user_center_service.models import SysScenic, User

    cfg = (await db.execute(select(AppScenicConfig).where(
        AppScenicConfig.scenic_id == scenic_id))).scalar_one_or_none()
    master = (await db.execute(select(SysScenic).where(
        SysScenic.scenic_id == scenic_id))).scalar_one_or_none()
    bound = (await db.execute(select(User.id).where(
        User.scenic_id == scenic_id, User.is_active.is_(True))))\
        .scalars().all()
    return cfg, master, len(bound)


@router.get("/scenic-configs")
async def list_scenic_configs(
    kw: str = "", state: Literal["all", "configured", "unconfigured"] = "all",
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """景区配置列表(25 号):前置条件=被用户绑定过的景区(在用景区)。

    列表行 = 在用景区 × 主数据只读展示(sys_scenics LEFT JOIN)× 配置状态;
    搜索/状态过滤只作用于在用景区子集内,绝不从万级主数据补位。
    """
    from cogni_agent.gateway_service.models import AppScenicConfig
    from cogni_agent.user_center_service.models import SysScenic, User

    # 在用景区(active 用户绑定,Python 侧去重计数)——与预热派单同口径
    usage = (await db.execute(
        select(User.scenic_id, User.id)
        .where(User.scenic_id.isnot(None), User.scenic_id != "",
               User.is_active.is_(True))
    )).all()
    bound_cnt: dict[str, int] = {}
    for scenic_id, _uid in usage:
        bound_cnt[scenic_id] = bound_cnt.get(scenic_id, 0) + 1
    # 景区账号只看自己那一行:这张表一行就是一个租户(名称、等级、绑了几个人、日承载量),
    # 不收口的话,一个景区的运营在这儿能看到全平台的景区花名册
    from cogni_agent.common import permissions as perm

    mine = perm.read_scope(user.role, user.scenic_id)
    if mine is not None:
        bound_cnt = {k: v for k, v in bound_cnt.items() if k == mine}
    if not bound_cnt:
        return {"items": []}

    ids = list(bound_cnt)
    masters = {s.scenic_id: s for s in (await db.execute(
        select(SysScenic).where(SysScenic.scenic_id.in_(ids)))).scalars()}
    configs = {c.scenic_id: c for c in (await db.execute(
        select(AppScenicConfig).where(
            AppScenicConfig.scenic_id.in_(ids)))).scalars()}

    k = kw.strip().lower()
    items = []
    for sid in sorted(ids):
        m, c = masters.get(sid), configs.get(sid)
        if k and k not in sid.lower() and k not in (m.scenic_name if m else "").lower():
            continue
        if state == "configured" and c is None:
            continue
        if state == "unconfigured" and c is not None:
            continue
        items.append({
            "scenic_id": sid,
            **_master_dict(m),
            "bound_users": bound_cnt[sid],
            **_cfg_dict(c),
        })
    return {"items": items[:50]}


@router.get("/scenic-configs/{scenic_id}")
async def get_scenic_config(scenic_id: str,
                            db: AsyncSession = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    """景区配置详情(无配置行返回空壳 configured=false,前端按空态渲染)。"""
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service.models import SysScenic

    # 同路径的 PUT 早就判了「景区账号只能配自己景区」,GET 一直没判 ——
    # 于是改不了、但读得到:把别人景区的 ID 填进地址栏就行
    perm.assert_can_read(user.role, user.scenic_id, scenic_id, "景区配置")
    cfg, master, bound = await _load_scenic_row(db, scenic_id)
    if master is None and bound == 0:
        raise NotFound("景区不存在(既无主数据记录也无绑定用户)")
    return {"scenic_id": scenic_id, **_master_dict(master),
            "bound_users": bound, **_cfg_dict(cfg)}


@router.put("/scenic-configs/{scenic_id}")
async def save_scenic_config(scenic_id: str, req: ScenicConfigSaveRequest,
                             db: AsyncSession = Depends(get_db),
                             user: CurrentUser = Depends(get_current_user)):
    """保存景区配置(upsert):日承载量整数 >0;None=清空。"""
    from cogni_agent.gateway_service.models import AppScenicConfig
    from cogni_agent.user_center_service.models import SysScenic
    from sqlalchemy.dialects.postgresql import insert as _pg_insert

    if not _can_write(user.role):
        raise Forbidden("只读账号不可修改景区配置")
    # 景区账号只能配自己景区 —— 路径里的 scenic_id 是别人的就直接拒
    _assert_scope(user, scenic_id, "景区配置")
    cfg, master, bound = await _load_scenic_row(db, scenic_id)
    if master is None and bound == 0:
        raise NotFound("景区不存在(既无主数据记录也无绑定用户)")

    if req.daily_capacity is None:
        # 清空 = 删配置行回到「未配置」态(不留空行,徽章语义才准确)
        if cfg is not None:
            from sqlalchemy import delete as _sa_delete
            await db.execute(_sa_delete(AppScenicConfig).where(
                AppScenicConfig.scenic_id == scenic_id))
            await db.commit()
            db.expire_all()
            cfg, _, _ = await _load_scenic_row(db, scenic_id)
        return {"scenic_id": scenic_id, **_master_dict(master),
                "bound_users": bound, **_cfg_dict(cfg)}

    stmt = _pg_insert(AppScenicConfig).values(
        scenic_id=scenic_id,
        daily_capacity=req.daily_capacity,
        extra={"schema_version": 1},
        updated_by=user.user_id,
    ).on_conflict_do_update(
        index_elements=[AppScenicConfig.scenic_id],
        set_={"daily_capacity": req.daily_capacity, "updated_by": user.user_id,
              "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.commit()
    # upsert 是原生 SQL,不会更新会话身份映射里的既有 ORM 实例——提交后统一
    # 过期,否则同一请求内「先读后写再回读」会返回旧值(身份映射缓存)。
    db.expire_all()
    cfg, _, _ = await _load_scenic_row(db, scenic_id)
    return {"scenic_id": scenic_id, **_master_dict(master),
            "bound_users": bound, **_cfg_dict(cfg)}


# ═══════════════════════════════════════════════════════════════════════
# 维度标签 + 景区智能体问题(前后端改造 §1/§2)
#
# 维度标签(主题→维度→指标)是问题的挂载点,放在「知识中心」维护;
# 问题表统一承载 Quick BI 与本地两类问题,business_class 决定怎么发起。
# 两者都在这里开接口,是因为它们是同一件事的两面:先有标签,才谈得上挂问题。
# ═══════════════════════════════════════════════════════════════════════

class DimensionLabelReq(BaseModel):
    """三级标签。六个字段都必填 —— 中文名给人看,编码给程序分组,缺一个都用不了。"""
    topic: str = Field(..., description="主题中文名,如 客流")
    topic_id: str = Field(..., description="主题编码,如 visitorFlow")
    dimension: str = Field(..., description="维度中文名,如 在园人员")
    dimension_id: str = Field(..., description="维度编码,如 inParkVisitor")
    metric: str = Field(..., description="指标中文名,如 实时人数")
    metric_id: str = Field(..., description="指标编码,如 liveVisitorCnt")
    sort_order: int | None = Field(default=None, description="同维度下展示顺序,小的在前")
    active: bool | None = Field(default=None, description="是否启用")


class DimensionLabelPatch(BaseModel):
    """改标签:只传要改的字段。改了名称会同步到已挂在它下面的问题上。"""
    topic: str | None = None
    topic_id: str | None = None
    dimension: str | None = None
    dimension_id: str | None = None
    metric: str | None = None
    metric_id: str | None = None
    sort_order: int | None = None
    active: bool | None = None


@router.get("/dimension-labels")
async def list_dimension_labels(
    active: bool | None = None,
    topic_id: str | None = None,
    tree: bool = False,
    with_usage: bool = False,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """维度标签列表。`tree=true` 返回折好的三级树(前台选择器直接用);
    `with_usage=true` 每行带上挂在它下面的问题数(知识中心 · 维度标签管理页用)。"""
    from cogni_agent.gateway_service import issue_service as issues

    if tree:
        return {"topics": await issues.label_tree(db, active=active)}
    return {"items": await issues.list_labels(db, active=active, topic_id=topic_id,
                                              with_usage=with_usage)}


@router.post("/dimension-labels")
async def create_dimension_label(req: DimensionLabelReq,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """新增标签。名称三元组或编码三元组重复都会被拒(并说明是哪一组撞了)。

    标签是**全平台共用**的一套(所有景区的问题都挂在同一棵树上),所以只有集团角色能维护 ——
    景区自己改一下,别的景区的问题树跟着变。
    """
    _assert_writable(user, "维度标签")
    _assert_scope(user, None, "维度标签")
    from cogni_agent.gateway_service import issue_service as issues

    return await issues.create_label(db, req.model_dump(exclude_none=True))


@router.patch("/dimension-labels/{label_id}")
async def update_dimension_label(label_id: int, req: DimensionLabelPatch,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """改标签(全平台共用,仅集团角色)。"""
    _assert_writable(user, "维度标签")
    _assert_scope(user, None, "维度标签")
    from cogni_agent.gateway_service import issue_service as issues

    return await issues.update_label(db, label_id, req.model_dump(exclude_none=True))


@router.delete("/dimension-labels/{label_id}")
async def delete_dimension_label(label_id: int,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """删除标签。下面还挂着问题就不让删 —— 删了那些问题会在前台凭空消失。"""
    _assert_deletable(user, None, "维度标签")
    from cogni_agent.gateway_service import issue_service as issues

    return await issues.delete_label(db, label_id)


class IssueReq(BaseModel):
    """景区智能体问题。

    `business_class=quick_bi` 时必须给 `connection_id`(操练台上那个 Quick BI 链接),
    否则发起时不知道拿哪套凭证去问 —— 与其让用户点了才报错,不如保存这一步就拦下。
    """
    business_class: Literal["quick_bi", "local_system"] = Field(
        default="local_system", description="quick_bi=直连 Quick BI;local_system=走对话引擎")
    scenic_id: str | None = Field(default=None, description="归属景区;为空=全景区通用")
    title: str = Field(..., description="问题标题(列表上显示的短文案)")
    issue: str = Field(..., description="问题原文(真正发出去的那句话)")
    # 三级标签(挂载点):按编码指定,中文名由服务端补
    topic_id: str | None = None
    dimension_id: str | None = None
    metric_id: str | None = None
    # Quick BI 专属
    connection_id: str | None = Field(default=None, description="Quick BI 连接 ID(= quickbiLinkId)")
    ask_type: str | None = Field(default=None, description="qa/insight/dashboard/report")
    capability: str | None = Field(default=None, description="dataset_qa/dashboard_qa/file_qa/insight/report")
    target: dict | None = Field(default=None, description="目标数据,如 {\"asset_id\": \"…\"}")
    target_data_id: str | None = Field(default=None, description="目标数据在 Quick BI 侧的编码")
    target_data_name: str | None = Field(default=None, description="目标数据名称(展示用)")
    # 本地问答专属
    agent_id: str | None = None
    skill_id: str | None = None
    model_instruction: str | None = Field(default=None, description="给模型的口径说明")
    param_template: dict | None = Field(default=None, description="参数模板,占位符由代码展开")
    time_period: str | None = None
    sort_order: int | None = None
    active: bool | None = None


class IssuePatch(IssueReq):
    """改问题:字段全可选(继承下来后把必填项放开)。"""
    business_class: Literal["quick_bi", "local_system"] | None = None
    title: str | None = None
    issue: str | None = None


class ThemeIssueRequest(BaseModel):
    """给某个景区生成「主题解读」问题(概览/客群/票务/舆情/OTA直销 五条)。"""

    scenic_id: str | None = Field(default=None, description="给哪个景区生成;留空 = 生成集团级通用的")
    connection_id: str | None = Field(default=None, description="绑哪条 Quick BI 连接;留空 = 按景区自动解析")
    overwrite: bool = Field(default=False, description="已存在的是否按当前连接/数据重新绑一遍")


@router.post("/theme-issues")
async def generate_theme_issues(req: ThemeIssueRequest,
                                user: CurrentUser = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    """一键生成主题解读问题(报告类,绑 Quick BI)。

    **为什么需要它**:前台那 5 张主题卡片点下去应该出一份该主题的解读报告,
    但它们原先只是一句文本「【主题解读】客群」,没有绑任何 Quick BI 能力 ——
    点下去走通用对话,回一句「请告诉我您需要查询什么」。
    这个接口按景区把这 5 条问题建出来:绑该景区的连接、挑该主题的仪表板、挂上三级标签。
    """
    _assert_writable(user, "主题解读")
    from cogni_agent.gateway_service import scenic_binding as binding

    # effective_scenic 已经拦住「景区账号指定别的景区」;这里再拦一种:
    # 景区角色不能生成**集团级**的那套(留空 = 集团级,所有景区共用)
    scenic_id = binding.effective_scenic(user, req.scenic_id)
    _assert_scope(user, scenic_id, "主题解读")
    return await binding.ensure_theme_issues(db, scenic_id, req.connection_id, req.overwrite)


class ThemePrewarmRequest(BaseModel):
    scenic_id: str | None = Field(default=None, description="给哪个景区预热;留空 = 按当前账号所属景区")
    theme_code: str | None = Field(default=None, description="只跑某一个主题;留空 = 该景区未预热的全部")
    force: bool = Field(default=False, description="忽略 12 小时新鲜度,强制重跑")


@router.post("/theme-issues/prewarm")
async def prewarm_theme_issues(req: ThemePrewarmRequest,
                               user: CurrentUser = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """现在就把主题解读报告跑一遍(定时预热之外的手动入口)。

    报告一条要几分钟,所以**不等它跑完**:这里只把任务丢到后台,立刻返回本次
    安排了哪几条。运营点完可以继续干别的,过几分钟回来刷新看「最近运行」。
    等它跑完再返回的话,浏览器那边早就超时了,而报告其实还在跑 ——
    看着像失败,实际上成功了,这种不一致最难解释。
    """
    _assert_writable(user, "主题解读")
    import asyncio

    from cogni_agent.cronjobs import theme_reports
    from cogni_agent.gateway_service import scenic_binding as binding

    scenic_id = binding.effective_scenic(user, req.scenic_id)
    _assert_scope(user, scenic_id, "主题解读")

    themes = [t for t in binding.THEMES
              if not req.theme_code or t["code"] == req.theme_code]
    planned = []
    for t in themes:
        row = await binding.theme_issue(db, scenic_id, t["code"])
        if row is None:
            continue
        if not req.force:
            # strict_target:与预热同口径 —— 绑的看板换过之后,旧报告不算「跑过了」
            fresh = await binding.recent_success_run(
                db, row.issue_id, scenic_id, within_hours=theme_reports._FRESH_HOURS,
                expect_target_data_id=row.target_data_id, strict_target=True)
            if fresh is not None:
                continue
        planned.append((t, row))

    for _t, row in planned:
        # fire-and-forget:强引用挂在模块上防止被 GC 掉(与 cronjobs 同一套做法)
        # 带上 scenic_id:集团级那条是模板,这里要为**当前这个景区**跑一份,
        # 否则跑出来的那份挂在集团级下,这个景区的卡片照样是空的
        task = asyncio.create_task(theme_reports._run_one(row, scenic_id))
        _PREWARM_TASKS.add(task)
        task.add_done_callback(_PREWARM_TASKS.discard)

    return {"scenic_id": scenic_id,
            "started": [t["code"] for t, _ in planned],
            "skipped": [t["code"] for t in themes
                        if t["code"] not in {x["code"] for x, _ in planned}],
            "message": (f"已开始生成 {len(planned)} 份报告,一份通常要 3~10 分钟;"
                        "跑完后前台点主题卡片直接出结果"
                        if planned else "这些主题的报告都还新鲜(12 小时内跑过),无需重跑")}


@router.get("/theme-issues/default-scenic")
async def theme_default_scenic(user: CurrentUser = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """面板一进来该落在哪个景区。

    原先前端自己取「第一个已登记的景区」,结果挑中的往往是一个还没绑连接的景区 ——
    一进来就是 5 条全红 + 一句「没有可用连接」,看着像坏了,其实只是选错了范围。
    这里按「能用」排序:**有可用 Quick BI 连接的景区优先**,其中又优先已经配过
    主题解读的那个。景区账号就是它自己那个景区,没得挑。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.gateway_service import scenic_binding as binding
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.user_center_service.models import SysScenic

    mine = perm.read_scope(user.role, user.scenic_id)
    if mine:                                  # 景区账号:就是自己那个
        return {"scenic_id": mine, "reason": "account_bound"}

    enabled = (await db.execute(
        select(SysScenic.scenic_id).where(SysScenic.is_enabled.is_(True))
        .order_by(SysScenic.created_at))).scalars().all()
    if not enabled:
        return {"scenic_id": None, "reason": "no_enabled_scenic"}

    configured = set((await db.execute(
        select(ScenicAgentIssueDetail.scenic_id).where(
            ScenicAgentIssueDetail.topic_id == "themeReport",
            ScenicAgentIssueDetail.scenic_id.isnot(None)).distinct())).scalars().all())

    fallback = None
    for sid in enabled:
        if not await binding.quickbi_connection_for(db, sid):
            continue                          # 没连接的景区选中了也只能看红字
        if sid in configured:
            return {"scenic_id": sid, "reason": "configured"}
        fallback = fallback or sid
    if fallback:
        return {"scenic_id": fallback, "reason": "has_connection"}
    # 一个有连接的景区都没有:退回第一个已开通的,让面板把「去绑连接」那句话说出来
    return {"scenic_id": enabled[0], "reason": "no_connection_anywhere"}


@router.get("/theme-issues")
async def list_theme_issues(scenic_id: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    """看这个景区的主题解读配好了没(前台卡片能不能出报告,就看这里)。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    sid = binding.effective_scenic(user, scenic_id)
    conn = await binding.quickbi_connection_for(db, sid)
    # 连接名而不是一串 UUID:界面上给人看的东西不该是主键
    conn_row = await db.get(QbiConnection, conn) if conn else None
    items = []
    for t in binding.THEMES:
        row = await binding.theme_issue(db, sid, t["code"])
        items.append({
            "theme_code": t["code"], "theme": t["name"],
            "ready": row is not None and row.business_class == "quick_bi",
            "issue_id": row.issue_id if row else None,
            "scope": ("景区专属" if (row and row.scenic_id) else "集团级" if row else "未配置"),
            "connection_id": row.connection_id if row else None,
            "target_data_name": row.target_data_name if row else None,
            "last_run_status": row.last_run_status if row else None,
            "last_run_at": row.last_run_time.isoformat() if (row and row.last_run_time) else None,
            # 提示词随列表一起给出:面板上要能就地编辑,单独再查一次没必要
            "model_instruction": row.model_instruction if row else None,
        })
    return {"scenic_id": sid, "connection_id": conn,
            "connection_name": conn_row.name if conn_row else None, "items": items}


@router.get("/issues")
async def list_scenic_issues(
    scenic_id: str | None = None,
    business_class: str | None = None,
    topic_id: str | None = None,
    dimension_id: str | None = None,
    metric_id: str | None = None,
    keyword: str | None = None,
    active: bool | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """问题列表(管理端全字段)。scenic_id 不传时:集团账号看全部,景区账号看本景区+通用。

    **scenic_id 只能收窄、不能放宽**。原先写的是
    `scenic_id if scenic_id is not None else (user.scenic_id or None)`,
    于是调用方传什么就是什么 —— 景区账号传别人的景区就看别人的问题,
    更糟的是传一个**空串**:`"" is not None` 为真,scope 变成空串,
    而下游 `_scenic_clause("")` 对空值是「不加条件」→ 整个平台的问题全出来。
    收口成 effective_scenic:景区账号传别的景区直接拒,空串按没传处理。
    """
    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.gateway_service import scenic_binding as binding

    scope = binding.effective_scenic(user, scenic_id)
    return {"items": await issues.list_issues(
        db, scenic_id=scope, business_class=business_class, topic_id=topic_id,
        dimension_id=dimension_id, metric_id=metric_id, keyword=keyword, active=active)}


@router.post("/issues")
async def create_scenic_issue(req: IssueReq,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    _assert_writable(user, "问题")
    from cogni_agent.gateway_service import issue_service as issues

    data = req.model_dump(exclude_none=True)
    # 景区账号建的问题**强制**落在自己景区下:允许它建集团级问题的话,
    # 一个景区能往所有景区的问题树里塞东西
    from cogni_agent.common import permissions as perm

    if not perm.is_group_role(user.role):
        if not user.scenic_id:
            raise Forbidden("当前账号未绑定景区,无法新建问题;请联系管理员在「组织管理」绑定")
        data["scenic_id"] = user.scenic_id
    else:
        data.setdefault("scenic_id", user.scenic_id or None)
    return await issues.create_issue(db, data)


@router.patch("/issues/{issue_id}")
async def update_scenic_issue(issue_id: str, req: IssuePatch,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    _assert_writable(user, "问题")
    from cogni_agent.gateway_service import issue_service as issues

    row = await issues.get_issue(db, issue_id)
    _assert_scope(user, row.scenic_id, "问题")      # 景区角色只能改本景区的
    data = req.model_dump(exclude_none=True)
    from cogni_agent.common import permissions as perm

    if not perm.is_group_role(user.role):
        data.pop("scenic_id", None)                 # 景区角色不能把问题挪到别的景区
    return await issues.update_issue(db, issue_id, data)


@router.delete("/issues/{issue_id}")
async def delete_scenic_issue(issue_id: str,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    from cogni_agent.gateway_service import issue_service as issues

    row = await issues.get_issue(db, issue_id)
    _assert_deletable(user, row.scenic_id, "问题")   # 操作员不能删;景区角色只能删本景区的
    return await issues.delete_issue(db, issue_id)


# ── 景区的系统参数取值(0090 sys_param_value) ─────────────────────────────
#
# 参数在「系统设置 · 参数配置」里定义(routes_params);这里按景区覆盖取值,可以多个值、各带备注。
# 不覆盖 = 用系统默认值。注入逻辑见 runtime_service/engine/scenic_params.py。


class ScenicParamOverride(BaseModel):
    param_key: str
    values: list[dict] | None = Field(None, description="[{value, label}];None = 不覆盖(用系统默认)")
    keep_secret: bool = Field(False, description="敏感参数:true = 保持原值不动(界面上只看得到末四位,没法回填)")


class ScenicParamsSaveRequest(BaseModel):
    items: list[ScenicParamOverride] = Field(default_factory=list)


async def _scenic_param_view(db: AsyncSession, scenic_id: str) -> list[dict]:
    from cogni_agent.gateway_service.models import SysParamDef, SysParamValue
    from cogni_agent.gateway_service.routes_params import values_out

    defs = (await db.execute(select(SysParamDef).order_by(SysParamDef.param_key))).scalars().all()
    ov = {r.param_key: r for r in (await db.execute(
        select(SysParamValue).where(SysParamValue.scenic_id == scenic_id))).scalars().all()}
    out = []
    for d in defs:
        r = ov.get(d.param_key)
        out.append({
            "param_key": d.param_key, "ref": "{{" + d.param_key + "}}", "label": d.label,
            "description": d.description or "", "multi_mode": d.multi_mode, "is_secret": bool(d.is_secret),
            "enabled": bool(d.enabled), "arg_names": list(d.arg_names or []),
            "default_values": values_out(d.default_values, d.is_secret),
            "overridden": r is not None,
            "values": values_out(r.values, d.is_secret) if r is not None else [],
            "updated_at": r.updated_at.isoformat() if r is not None and r.updated_at else None,
            "updated_by": r.updated_by if r is not None else None,
        })
    return out


@router.get("/scenic-configs/{scenic_id}/params")
async def get_scenic_params(scenic_id: str,
                            db: AsyncSession = Depends(get_db),
                            user: CurrentUser = Depends(get_current_user)):
    """本景区的系统参数:每个参数的系统默认值、本景区覆盖值(敏感值只回末四位)。"""
    from cogni_agent.common import permissions as perm

    perm.assert_can_read(user.role, user.scenic_id, scenic_id, "景区接口参数")
    return {"scenic_id": scenic_id, "items": await _scenic_param_view(db, scenic_id)}


@router.put("/scenic-configs/{scenic_id}/params")
async def save_scenic_params(scenic_id: str, req: ScenicParamsSaveRequest,
                             db: AsyncSession = Depends(get_db),
                             user: CurrentUser = Depends(get_current_user)):
    """保存本景区的覆盖值。只处理请求里列出的参数;values=None → 删掉覆盖、回到系统默认。"""
    from cogni_agent.gateway_service.models import SysParamDef, SysParamValue
    from cogni_agent.gateway_service.routes_params import ParamValueItem, clean_values
    from cogni_agent.runtime_service.engine import scenic_params as sp

    if not _can_write(user.role):
        raise Forbidden("只读账号不可修改景区接口参数")
    _assert_scope(user, scenic_id, "景区接口参数")
    _cfg, master, bound = await _load_scenic_row(db, scenic_id)
    if master is None and bound == 0:
        raise NotFound("景区不存在(既无主数据记录也无绑定用户)")
    defs = {d.param_key: d for d in (await db.execute(select(SysParamDef))).scalars().all()}
    rows = {r.param_key: r for r in (await db.execute(
        select(SysParamValue).where(SysParamValue.scenic_id == scenic_id))).scalars().all()}
    for it in req.items:
        d = defs.get(it.param_key)
        if d is None:
            raise ValidationError(f"参数「{it.param_key}」没有定义,请先到「系统设置 · 参数配置」里添加")
        r = rows.get(it.param_key)
        if d.is_secret and it.keep_secret:
            continue
        if it.values is None:
            if r is not None:
                await db.delete(r)
            continue
        vals = clean_values([ParamValueItem(**v) for v in it.values], multi=d.multi_mode != "single",
                            what=f"「{d.label}」")
        if not vals:
            if r is not None:
                await db.delete(r)
            continue
        if r is None:
            r = SysParamValue(param_key=it.param_key, scenic_id=scenic_id)
            db.add(r)
        r.values = vals
        r.updated_by = user.user_id
    await db.commit()
    sp.invalidate(scenic_id)      # 下一次工具调用立即读到新值
    db.expire_all()
    return {"scenic_id": scenic_id, "items": await _scenic_param_view(db, scenic_id)}
