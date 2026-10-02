"""
审批中心/系统日志/成本/评测中心 与「工作台」看板的读接口——把已经是真实实现的
approvals.py/audit.py/cost.py/evals.py/control_plane_service.queue 数据面暴露给管理后台。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.governance_event_bus_service.approvals import Approval, resolve_approval
from cogni_agent.governance_event_bus_service.audit import AuditLog
from cogni_agent.governance_event_bus_service.cost import LlmUsage
from cogni_agent.governance_event_bus_service.evals import EvalCase, EvalRun, run_eval_batch
from cogni_agent.control_plane_service.models import Task

router = APIRouter(prefix="/admin", tags=["审计中心"])


def _can_write(role: str) -> bool:
    from cogni_agent.common import permissions as perm

    return perm.can_create(role)


def _assert_can_delete(user: CurrentUser, what: str) -> None:
    """评测用例没有景区归属(全平台共用一套),所以删除按集团口径:
    集团三角色可删,集团只读和景区角色都不行 —— 否则一个景区删一条,所有景区的评测都少一条。"""
    from cogni_agent.common import permissions as perm

    if not perm.can_manage_all(user.role):
        label = perm.ROLE_LABELS.get(user.role or "", user.role or "当前角色")
        raise Forbidden(f"{what}为全平台共用,{label}不能删除;请联系集团管理员")


def _scoped(query, model, user: CurrentUser):
    """G-ISO-01:景区角色强制按 scenic_id 过滤;集团角色(scenic_id=None)看全平台。

    这里**刻意**不像配置类那样带上「scenic_id IS NULL 的集团级行」:
    任务、审批、用量都是运行记录,一条集团级的任务不是某个景区的事,
    让景区看见它只会把别人的业务数据(payload 里就是用户原话)漏过去。
    配置类(连接、问题、白名单)才需要「本景区 + 集团级」,见 permissions.scenic_read_clause。
    """
    if user.scenic_id:
        return query.where(model.scenic_id == user.scenic_id)
    return query


def _assert_record_scope(user: CurrentUser, obj_scenic: str | None, what: str) -> None:
    """运行记录类的详情门禁,与 _scoped 同口径(不放行集团级)。"""
    if user.scenic_id and (obj_scenic or None) != user.scenic_id:
        raise NotFound(f"{what}不存在或无权访问")


# ───────────────────────────── 审批中心 ─────────────────────────────

@router.get("/approvals")
async def list_approvals(status: str | None = "pending", user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Approval).order_by(Approval.created_at.desc())
    stmt = _scoped(stmt, Approval, user)
    if status:
        stmt = stmt.where(Approval.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_approval_dict(a) for a in rows]}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    approval = await db.get(Approval, approval_id)
    if not approval:
        raise NotFound("审批记录不存在")
    # 列表用了 _scoped,详情一直没判 —— 而详情比列表多带一样东西:
    # 关联任务的完整 payload,也就是用户当时问的原话
    _assert_record_scope(user, approval.scenic_id, "审批记录")
    context = None
    if approval.related_task_id:
        task = await db.get(Task, approval.related_task_id)
        if task and not (user.scenic_id and (task.scenic_id or None) != user.scenic_id):
            context = {"task_id": task.id, "executor_type": task.executor_type, "executor_id": task.executor_id,
                       "status": task.status, "payload": task.payload}
    return {**_approval_dict(approval), "context": context}


class ResolveApprovalRequest(BaseModel):
    approved: bool
    opinion: str | None = None
    # 第三种审批动作:**改参数后批准**。只传要改的字段,其余沿用原调用参数。
    # 审批人看到「要取消订单 A」时,常见需求是「改成取消订单 B 再执行」——
    # 没有这一项就只能驳回,让用户从头再问一遍。
    # 改过的参数在执行侧会重新过 Schema 与租户校验,而且是按**任务的**景区/角色,
    # 不是审批人的:任务恢复后仍以原用户身份执行,拿审批人的权限放行等于开了条提权路径。
    params: dict | None = None


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval_route(approval_id: str, req: ResolveApprovalRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """批准 / 驳回 / **改参数后批准**一条审批。

    驳回必须填意见;`params` 非空即为「改参数后批准」(此时 approved 必须为 true)。
    """
    if not req.approved and not (req.opinion or "").strip():
        raise Forbidden("驳回必须填写意见")
    if req.params and not req.approved:
        raise Forbidden("驳回时不能同时修改参数;要改参数请选择「改参数后批准」")
    approval = await db.get(Approval, approval_id)
    if not approval:
        raise NotFound("审批记录不存在")
    await resolve_approval(approval_id, req.approved, req.opinion or "", user.user_id,
                           params=req.params or None)
    return {"ok": True}


def _approval_dict(a: Approval) -> dict:
    return {
        "id": a.id, "type": a.type, "related_task_id": a.related_task_id,
        "related_resource_type": a.related_resource_type, "related_resource_id": a.related_resource_id,
        "risk_level": a.risk_level, "scenic_id": a.scenic_id, "applicant_user_id": a.applicant_user_id,
        "status": a.status, "opinion": a.opinion, "resolved_by": a.resolved_by, "resolved_at": a.resolved_at,
        "created_at": a.created_at,
    }


# ───────────────────────────── 系统日志(简化:按审计表查,非全链路 trace) ─────────────────────────────

@router.get("/audit-logs")
async def list_audit_logs(event_type: str | None = None, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.role not in ("super_admin", "group_admin"):
        raise Forbidden("仅 super_admin/group_admin 可查看系统日志(模块⑥)")
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [{"id": a.id, "event_type": a.event_type, "scenic_id": a.scenic_id, "actor_user_id": a.actor_user_id,
                        "detail": a.detail, "created_at": a.created_at} for a in rows]}


# ───────────────────────────── 工作台看板 ─────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(period_days: int = 7, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(days=period_days)

    total_stmt = _scoped(select(func.count(Task.id)), Task, user)
    total = (await db.execute(total_stmt)).scalar_one()

    success_stmt = _scoped(select(func.count(Task.id)).where(Task.status == "COMPLETED", Task.created_at >= since), Task, user)
    success = (await db.execute(success_stmt)).scalar_one()
    period_total_stmt = _scoped(select(func.count(Task.id)).where(Task.created_at >= since), Task, user)
    period_total = (await db.execute(period_total_stmt)).scalar_one()

    pending_stmt = _scoped(select(func.count(Approval.id)).where(Approval.status == "pending"), Approval, user)
    pending_approvals = (await db.execute(pending_stmt)).scalar_one()

    cost_stmt = _scoped(
        select(func.coalesce(func.sum(LlmUsage.prompt_tokens), 0), func.coalesce(func.sum(LlmUsage.completion_tokens), 0))
        .where(LlmUsage.created_at >= since), LlmUsage, user,
    )
    prompt_tokens, completion_tokens = (await db.execute(cost_stmt)).one()

    mode_stmt = _scoped(select(Task.payload).where(Task.created_at >= since), Task, user)
    mode_rows = (await db.execute(mode_stmt)).scalars().all()

    return {
        "total_tasks": total,
        "success_rate": round(success / period_total, 4) if period_total else None,
        "period_days": period_days,
        "pending_approvals": pending_approvals,
        "token_usage": {"prompt_tokens": int(prompt_tokens), "completion_tokens": int(completion_tokens)},
    }


@router.get("/dashboard/recent-tasks")
async def recent_tasks(limit: int = 20, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = _scoped(select(Task).order_by(Task.created_at.desc()).limit(limit), Task, user)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [
        {"id": t.id, "executor_type": t.executor_type, "executor_id": t.executor_id, "status": t.status,
         "created_at": t.created_at, "scenic_id": t.scenic_id} for t in rows
    ]}


# ───────────────────────────── 评测中心 ─────────────────────────────

class EvalCaseCreateRequest(BaseModel):
    name: str
    target_type: str  # skill / agent
    target_id: str
    test_input: str
    expected_keywords: list[str] = []
    tags: list[str] = []


@router.get("/eval-cases")
async def list_eval_cases(target_type: str | None = None, target_id: str | None = None,
                           user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(EvalCase).order_by(EvalCase.created_at.desc())
    if target_type:
        stmt = stmt.where(EvalCase.target_type == target_type)
    if target_id:
        stmt = stmt.where(EvalCase.target_id == target_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [
        {"id": c.id, "name": c.name, "target_type": c.target_type, "target_id": c.target_id,
         "test_input": c.test_input, "expected_keywords": c.expected_keywords, "tags": c.tags}
        for c in rows
    ]}


@router.post("/eval-cases")
async def create_eval_case(req: EvalCaseCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增评测用例")
    case = EvalCase(
        name=req.name, target_type=req.target_type, target_id=req.target_id, test_input=req.test_input,
        expected_keywords=req.expected_keywords, tags=req.tags, created_by=user.user_id,
    )
    db.add(case)
    await db.commit()
    return {"id": case.id, "name": case.name}


@router.delete("/eval-cases/{case_id}")
async def delete_eval_case(case_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _assert_can_delete(user, "评测用例")
    case = await db.get(EvalCase, case_id)
    if not case:
        raise NotFound("评测用例不存在")
    await db.delete(case)
    await db.commit()
    return {"ok": True}


class RunEvalRequest(BaseModel):
    target_type: str
    target_id: str
    case_ids: list[str] | None = None


@router.post("/eval-runs")
async def trigger_eval_run(req: RunEvalRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """M7-06:运行报告——真实跑一遍用例集(与对话页同一条执行引擎链路),立即返回聚合结果。
    用例数较多时这个请求会等到全部跑完才返回(和评测本身"离线批量跑"的性质一致,不做成
    后台异步任务是为了让管理后台能直接展示完整报告,不需要再轮询)。"""
    report = await run_eval_batch(req.target_type, req.target_id, scenic_id=user.scenic_id, user_id=user.user_id, case_ids=req.case_ids)
    return report


@router.get("/eval-runs")
async def list_eval_run_batches(target_type: str | None = None, target_id: str | None = None,
                                 user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """历史运行报告列表——按 batch_id 聚合展示每次运行的通过率/耗时。"""
    stmt = select(EvalRun)
    if target_type:
        stmt = stmt.where(EvalRun.target_type == target_type)
    if target_id:
        stmt = stmt.where(EvalRun.target_id == target_id)
    rows = (await db.execute(stmt.order_by(EvalRun.created_at.desc()).limit(500))).scalars().all()
    batches: dict[str, dict] = {}
    for r in rows:
        b = batches.setdefault(r.batch_id, {"batch_id": r.batch_id, "target_type": r.target_type, "target_id": r.target_id,
                                             "total": 0, "passed": 0, "created_at": r.created_at})
        b["total"] += 1
        b["passed"] += 1 if r.passed else 0
        b["created_at"] = max(b["created_at"], r.created_at)
    items = sorted(batches.values(), key=lambda b: b["created_at"], reverse=True)
    for b in items:
        b["pass_rate"] = b["passed"] / b["total"] if b["total"] else None
    return {"items": items}


@router.get("/eval-runs/{batch_id}")
async def get_eval_run_detail(batch_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(EvalRun).where(EvalRun.batch_id == batch_id))).scalars().all()
    if not rows:
        raise NotFound("运行报告不存在")
    return {"batch_id": batch_id, "items": [
        {"case_id": r.case_id, "passed": r.passed, "actual_output": r.actual_output, "latency_ms": r.latency_ms,
         "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "error": r.error}
        for r in rows
    ]}


# ───────────────────────────── 系统设置:模型与预算 ─────────────────────────────

class ModelConfigRequest(BaseModel):
    # id 是主键(UUID):编辑时前端传 id 定位(可改 name),新增时不传。
    # name 是展示名(给人看的,唯一),model_id 是供应商提供的调用 ID(必填,运行时用)。
    # 兼容旧前端:model_id 不传时回落到 name。
    id: str | None = None
    name: str
    model_id: str | None = None
    provider: str
    base_url: str | None = None
    api_key: str | None = None
    # 模型类型:chat(对话模型)/embedding(嵌入模型,向量入库/检索用)。两种类型各有一个默认。
    model_type: str = "chat"
    # 兼容旧前端:勾选 is_embedding 等价于 model_type='embedding'
    is_embedding: bool = False
    # 真实计费单价(元/百万 token,可空):填了按该价计费;未填自动查 litellm 价格表,再兜底估算
    input_price_cny_per_m: float | None = None
    output_price_cny_per_m: float | None = None


def _require_group_admin(user: CurrentUser):
    """系统设置(模块⑥)是集团级的:集团三角色可管,集团只读和景区角色都不行。

    原先漏了 group_developer(集团开发),导致同一个「登记景区」动作在这里被拒、
    在用户中心那条路径却放行 —— 同一件事两套口径,用户看到的是「有时能点有时报错」。
    """
    from cogni_agent.common import permissions as perm

    if not perm.can_manage_all(user.role):
        label = perm.ROLE_LABELS.get(user.role or "", user.role or "当前角色")
        raise Forbidden(f"系统设置为集团级配置,{label}无权管理;请联系集团管理员")


class PlatformSecretRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=512, description="密钥明文;只写不读,接口永不回显")


@router.get("/settings/platform-secrets")
async def list_platform_secrets(user: CurrentUser = Depends(get_current_user)):
    """平台共用密钥(目前只有数据平台 xpftkey):配没配、来源(后台 / 环境变量)、末四位。永不回明文。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    return {"items": await ps.describe()}


@router.put("/settings/platform-secrets/{name}")
async def set_platform_secret(name: str, req: PlatformSecretRequest,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """设置 / 替换。存加密值;各进程最迟 30 秒生效(本进程立即生效),不需要重启。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    if name not in ps.KNOWN:
        raise NotFound(f"不支持的平台密钥:{name}")
    try:
        await ps.set_value(db, name, req.value, user_id=user.user_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"ok": True, "items": await ps.describe()}


@router.delete("/settings/platform-secrets/{name}")
async def clear_platform_secret(name: str, user: CurrentUser = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    """清除后台配置的值 —— 之后回退到环境变量(若有)。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    if name not in ps.KNOWN:
        raise NotFound(f"不支持的平台密钥:{name}")
    await ps.clear_value(db, name)
    return {"ok": True, "items": await ps.describe()}


@router.post("/settings/models/{config_id}/set-default")
async def set_model_default(config_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """把某个模型设为默认(按 id 定位)。未显式指定模型的任务会用它。

    优先级:调用方显式指定 > 这里设的默认 > config.yaml 的 runtime.default_model。
    改完立即生效(下一次模型解析最多延迟 30 秒的缓存 TTL,这里会主动失效掉)。
    """
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import set_default_model
    from cogni_agent.runtime_service.llm.client import invalidate_model_cache

    try:
        await set_default_model(db, config_id)
    except ValueError as exc:
        raise NotFound(str(exc)) from exc
    invalidate_model_cache()
    return {"ok": True, "config_id": config_id}


@router.get("/settings/model-providers")
async def list_model_providers(user: CurrentUser = Depends(get_current_user)):
    """支持的模型供应商清单(含默认地址与填写提示)。前端下拉据此渲染,不硬编码。"""
    from cogni_agent.runtime_service.llm.providers import all_providers

    return {"items": all_providers()}


@router.get("/settings/models")
async def list_model_configs(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import LlmModelConfig

    rows = (await db.execute(select(LlmModelConfig))).scalars().all()
    return {"items": [
        {"id": m.id, "name": m.name, "model_id": m.model_id or m.name, "provider": m.provider, "base_url": m.base_url,
         "active": m.active, "is_default": m.is_default, "model_type": m.model_type,
         "is_embedding": m.is_embedding, "has_api_key": bool(m.api_key_cipher),
         "input_price_cny_per_m": float(m.input_price_cny_per_m) if m.input_price_cny_per_m is not None else None,
         "output_price_cny_per_m": float(m.output_price_cny_per_m) if m.output_price_cny_per_m is not None else None}
        for m in rows
    ]}


@router.post("/settings/models")
async def upsert_model_config_route(req: ModelConfigRequest, user: CurrentUser = Depends(get_current_user)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import upsert_model_config

    model_type = "embedding" if req.is_embedding else req.model_type
    await upsert_model_config(req.name, req.provider, req.base_url, req.api_key, user.user_id,
                              model_type, req.model_id, config_id=req.id,
                              input_price_cny_per_m=req.input_price_cny_per_m,
                              output_price_cny_per_m=req.output_price_cny_per_m)
    # 模型解析有 30s TTL 缓存,改动(含单价)后主动失效,让下一次任务立刻按新配置计费
    from cogni_agent.runtime_service.llm.client import invalidate_model_cache

    invalidate_model_cache()
    return {"ok": True}


@router.post("/settings/models/{config_id}/toggle")
async def toggle_model_config(config_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import LlmModelConfig
    from cogni_agent.runtime_service.llm.client import invalidate_model_cache

    cfg = await db.get(LlmModelConfig, config_id)
    if not cfg:
        raise NotFound("模型配置不存在")
    cfg.active = not cfg.active
    await db.commit()
    invalidate_model_cache()
    return {"name": cfg.name, "active": cfg.active}


@router.delete("/settings/models/{config_id}")
async def delete_model_config(config_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import LlmModelConfig
    from cogni_agent.runtime_service.llm.client import invalidate_model_cache

    cfg = await db.get(LlmModelConfig, config_id)
    if not cfg:
        raise NotFound("模型配置不存在")
    await db.delete(cfg)
    await db.commit()
    invalidate_model_cache()
    return {"ok": True, "deleted": config_id}


# ───────────────────── 系统设置:模型单价(替代 litellm 实时计费表) ─────────────────────

class ModelPriceRequest(BaseModel):
    """模型单价配置(元/百万 token)。两个单价都可空,空则计费回落到兜底估算。"""
    model_id: str
    input_price_cny_per_m: float | None = None
    output_price_cny_per_m: float | None = None


@router.get("/settings/model-prices")
async def list_model_prices(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """模型单价配置清单:按 model_id 精确匹配,计费优先级高于模型配置手填单价。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import list_model_prices as _list

    rows = await _list(db)
    return {"items": [
        {"model_id": p.model_id,
         "input_price_cny_per_m": float(p.input_price_cny_per_m) if p.input_price_cny_per_m is not None else None,
         "output_price_cny_per_m": float(p.output_price_cny_per_m) if p.output_price_cny_per_m is not None else None,
         "updated_by": p.updated_by,
         "updated_at": p.updated_at.isoformat() if p.updated_at else None}
        for p in rows
    ]}


@router.post("/settings/model-prices")
async def upsert_model_price_route(req: ModelPriceRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """新增/更新某个 model_id 的单价。改完主动失效计费缓存,下一次计费立即生效。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import upsert_model_price
    from cogni_agent.runtime_service.llm.cost import invalidate_model_prices_cache

    if not req.model_id or not req.model_id.strip():
        raise ValueError("model_id 不能为空")
    await upsert_model_price(db, req.model_id.strip(), req.input_price_cny_per_m, req.output_price_cny_per_m, user.user_id)
    invalidate_model_prices_cache()
    return {"ok": True, "model_id": req.model_id.strip()}


@router.delete("/settings/model-prices/{model_id}")
async def delete_model_price_route(model_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除某个 model_id 的单价配置,计费回落到模型手填/兜底估算。"""
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import delete_model_price as _delete
    from cogni_agent.runtime_service.llm.cost import invalidate_model_prices_cache

    deleted = await _delete(db, model_id)
    if not deleted:
        raise NotFound(f"未找到模型单价配置: {model_id}")
    invalidate_model_prices_cache()
    return {"ok": True, "deleted": model_id}


class BudgetThresholdRequest(BaseModel):
    scope: str  # group / scenic
    scenic_id: str | None = None
    daily_token_budget: int
    alert_threshold_pct: int = 80


@router.get("/settings/budgets")
async def list_budget_thresholds(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import BudgetThreshold

    rows = (await db.execute(select(BudgetThreshold))).scalars().all()
    return {"items": [
        {"id": b.id, "scope": b.scope, "scenic_id": b.scenic_id, "daily_token_budget": b.daily_token_budget,
         "alert_threshold_pct": b.alert_threshold_pct} for b in rows
    ]}


@router.post("/settings/budgets")
async def create_budget_threshold(req: BudgetThresholdRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_group_admin(user)
    from cogni_agent.governance_event_bus_service.settings import BudgetThreshold

    entry = BudgetThreshold(scope=req.scope, scenic_id=req.scenic_id, daily_token_budget=req.daily_token_budget, alert_threshold_pct=req.alert_threshold_pct)
    db.add(entry)
    await db.commit()
    return {"id": entry.id}


# ───────────────────────────── 系统设置:组织管理(以景区主数据为准) ─────────────────────────────
# 改造前这里读的是 gov_scenic_orgs —— 一张只能手工一条条登记的表,
# 所以页面上永远只有人敲进去的那一两条,看起来像「景区没同步过来」。
# 现在列的是主数据维表 dim_prd_scenic_base_yr_f(见 user_center_service/scenic_catalog.py),
# 开通状态落在 sys_scenics 上(与成员管理共用一份),gov_scenic_orgs 已由 0078 并入并删除。


class ScenicOrgRequest(BaseModel):
    """登记景区。现在从主数据里挑,所以只要 ID;
    name 仅为兼容老前端保留,后端一律以主数据的名称为准(手填名早晚和主数据对不上)。"""

    scenic_id: str | None = None
    scenic_ids: list[str] | None = None
    name: str | None = None
    enable: bool = True


@router.get("/settings/orgs")
async def list_scenic_orgs(keyword: str | None = None, registered: bool | None = None,
                           page: int = 1, page_size: int = 50,
                           user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """景区列表 = 主数据 ⟕ 平台登记状态。

    默认把主数据里的景区都列出来(带 registered / service_enabled 标记),
    这样「有哪些景区」和「开通了哪些」在同一页里就能看清。
    `registered=true` 只看已登记的。
    """
    _require_group_admin(user)
    from cogni_agent.user_center_service import scenic_catalog

    items, total = await scenic_catalog.search_catalog(
        db, keyword=keyword, registered=registered, page=page, page_size=page_size)
    return {
        "items": [{
            "scenic_id": i["scenic_id"],
            "name": i["scenic_name"],
            "grade": i["scenic_grade_level"],
            "province": i["province_name"],
            "city": i["city_name"],
            "registered": i["registered"],
            "user_count": i["user_count"],
            # service_enabled 沿用老字段名(前端不必同步改):已登记且已开通才算开通
            "service_enabled": bool(i["registered"] and i["is_enabled"]),
        } for i in items],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("/settings/orgs")
async def register_scenic_org(req: ScenicOrgRequest, user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """把主数据里的景区登记到平台。已登记的跳过,主数据里没有的整批拒绝。"""
    _require_group_admin(user)
    from cogni_agent.user_center_service import scenic_catalog

    ids = list(req.scenic_ids or ([req.scenic_id] if req.scenic_id else []))
    if not ids:
        raise ValidationError("请至少选择一个景区")
    return await scenic_catalog.register(db, ids, enable=req.enable)


@router.post("/settings/orgs/{scenic_id}/toggle")
async def toggle_scenic_org(scenic_id: str, user: CurrentUser = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    """开通/停用。还没登记的景区点「开通」= 按主数据自动登记并开通。"""
    _require_group_admin(user)
    from cogni_agent.user_center_service import scenics as scenics_service
    from cogni_agent.user_center_service.models import SysScenic

    row = await db.get(SysScenic, scenic_id)
    target = not (row.is_enabled if row is not None else False)
    result = await scenics_service.update_scenic_status(db, scenic_id, target)
    return {"scenic_id": result["scenic_id"], "service_enabled": result["is_enabled"]}
