"""系统参数(0090):系统设置 · 参数配置。

参数在这里**定义一次**(标识、说明、单值/多值、多值怎么用、默认值、自动填入的工具入参名),
景区在「APP 运营 · 景区配置 · 接口参数」里覆盖取值(见 routes_app_ops 末尾)。
Tool / MCP / Skill 编辑时用 `{{参数标识}}` 引用;解析与注入见 runtime_service/engine/scenic_params.py。

接口:
  GET    /api/admin/params            参数定义列表(含各景区覆盖数;敏感默认值只回末四位)
  PUT    /api/admin/params/{key}      新建 / 修改一个参数定义
  DELETE /api/admin/params/{key}      删除定义(连同各景区取值)
  GET    /api/admin/params/refs       编辑 Tool / MCP / Skill 时可插入的 {{参数}} 清单(任何能编辑工具的人都能看)
  GET    /api/admin/params/{key}/assignments   这个参数分配给了哪些景区、各是什么值
  PUT    /api/admin/params/{key}/assignments   集团统一给各景区分配取值(整组保存)
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/admin/params", tags=["系统参数"])

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ARG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ParamValueItem(BaseModel):
    value: str = Field(..., description="取值")
    label: str = Field("", max_length=64, description="备注,如「携程主账号」")


class ParamDefSave(BaseModel):
    label: str = Field("", max_length=128, description="显示名,如「OTA 商户账号」")
    description: str = Field("", description="干什么用、值找谁要、哪些工具用")
    multi_mode: Literal["single", "fanout", "list", "join"] = Field(
        "single", description="single=单值;fanout=多值,每个值各调用一次;list=多值,作为数组一次传入;join=多值,逗号拼接一次传入")
    default_values: list[ParamValueItem] | None = Field(
        None, description="系统默认值;None = 不改(敏感参数改说明时不必重填)")
    arg_names: list[str] = Field(default_factory=list, description="命中即自动填入的工具入参名,如 [\"account\"]")
    fill_mode: Literal["fill", "force"] = "fill"
    is_secret: bool = False
    enabled: bool = True


def mask(v: str) -> str:
    v = v or ""
    return ("•" * 4 + v[-4:]) if len(v) > 4 else "•" * len(v)


def values_out(values, secret: bool) -> list[dict]:
    return [{"value": mask(str(x.get("value") or "")) if secret else str(x.get("value") or ""),
             "label": str(x.get("label") or "")}
            for x in (values or []) if isinstance(x, dict)]


def def_dict(d, overrides: int = 0) -> dict:
    return {
        "param_key": d.param_key, "label": d.label, "description": d.description or "",
        "multi_mode": d.multi_mode, "default_values": values_out(d.default_values, d.is_secret),
        "arg_names": list(d.arg_names or []), "fill_mode": d.fill_mode, "is_secret": bool(d.is_secret),
        "enabled": bool(d.enabled), "scenic_overrides": overrides, "ref": "{{" + d.param_key + "}}",
        "updated_at": d.updated_at.isoformat() if d.updated_at else None, "updated_by": d.updated_by,
    }


def clean_values(items: list[ParamValueItem], *, multi: bool, what: str) -> list[dict]:
    out, seen = [], set()
    for it in items:
        v = (it.value or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append({"value": v, "label": (it.label or "").strip()})
    if not multi and len(out) > 1:
        raise ValidationError(f"{what}是单值参数,只能填一个值;要填多个,先把参数改成多值")
    return out


def _require_admin(user: CurrentUser) -> None:
    from cogni_agent.common import permissions as perm

    if not perm.can_manage_all(user.role):
        label = perm.ROLE_LABELS.get(user.role or "", user.role or "当前角色")
        raise Forbidden(f"参数定义是集团级配置,{label}无权修改;景区取值请到「景区配置 · 接口参数」里改")


def _reserved() -> set[str]:
    from cogni_agent.governance_event_bus_service import platform_secrets as ps
    from cogni_agent.runtime_service.engine.scenic_params import RESERVED_KEYS

    return set(RESERVED_KEYS) | set(ps.KNOWN)


async def _override_counts(db: AsyncSession) -> dict[str, int]:
    from cogni_agent.gateway_service.models import SysParamValue

    rows = (await db.execute(select(SysParamValue.param_key, func.count())
                             .group_by(SysParamValue.param_key))).all()
    return {k: n for k, n in rows}


@router.get("")
async def list_params(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """参数定义列表。"""
    from cogni_agent.gateway_service.models import SysParamDef

    from cogni_agent.gateway_service.models import SysParamValue
    from cogni_agent.user_center_service.models import SysScenic

    rows = (await db.execute(select(SysParamDef).order_by(SysParamDef.param_key))).scalars().all()
    counts = await _override_counts(db)
    vals = (await db.execute(select(SysParamValue))).scalars().all()
    names = {s.scenic_id: s.scenic_name for s in (await db.execute(
        select(SysScenic).where(SysScenic.scenic_id.in_({v.scenic_id for v in vals} or {""})))).scalars().all()}
    out = []
    for d in rows:
        # 按「值」汇总:每个值分配给了哪些景区、是不是默认值(列表页一眼看出谁用哪个账号)
        groups: dict[tuple, dict] = {}
        for x in values_out(d.default_values, d.is_secret):
            groups.setdefault((x["value"], x["label"]), {**x, "scenics": [], "is_default": False})["is_default"] = True
        for v in (v for v in vals if v.param_key == d.param_key):
            for x in values_out(v.values, d.is_secret):
                groups.setdefault((x["value"], x["label"]), {**x, "scenics": [], "is_default": False})["scenics"].append(
                    names.get(v.scenic_id) or v.scenic_id)
        out.append({**def_dict(d, counts.get(d.param_key, 0)), "value_groups": list(groups.values())})
    return {"items": out}


@router.get("/refs")
async def list_refs(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """编辑 Tool / MCP / Skill 时可插入的 {{参数}}。不含值 —— 编辑工具的人不一定该看到商户账号。"""
    from cogni_agent.gateway_service.models import SysParamDef

    rows = (await db.execute(select(SysParamDef).where(SysParamDef.enabled.is_(True))
                             .order_by(SysParamDef.param_key))).scalars().all()
    return {"items": [{"param_key": d.param_key, "ref": "{{" + d.param_key + "}}", "label": d.label,
                       "description": d.description or "", "multi_mode": d.multi_mode,
                       "is_secret": bool(d.is_secret), "arg_names": list(d.arg_names or [])} for d in rows]}


@router.put("/{param_key}")
async def save_param(param_key: str, req: ParamDefSave,
                     db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """新建 / 修改参数定义。"""
    from cogni_agent.gateway_service.models import SysParamDef
    from cogni_agent.runtime_service.engine import scenic_params as sp

    _require_admin(user)
    key = (param_key or "").strip()
    if not KEY_RE.match(key):
        raise ValidationError(f"参数标识「{key}」不合法:小写字母开头,只能用小写字母、数字、下划线")
    if key in _reserved():
        raise ValidationError(f"「{key}」是系统内置变量或平台密钥的名字,换一个(否则 {{{{{key}}}}} 取谁说不清)")
    args = [a.strip() for a in req.arg_names if a.strip()]
    for a in args:
        if not ARG_RE.match(a):
            raise ValidationError(f"工具参数名「{a}」不合法")
    # 两个参数抢同一个入参名:谁生效取决于排序,配的人看不出来 —— 直接拒
    others = (await db.execute(select(SysParamDef).where(SysParamDef.param_key != key))).scalars().all()
    for o in others:
        clash = set(args) & set(o.arg_names or [])
        if clash:
            raise ValidationError(f"工具参数名 {sorted(clash)} 已经归参数「{o.label or o.param_key}」自动填入,不能同时给两个参数")
    d = (await db.execute(select(SysParamDef).where(SysParamDef.param_key == key))).scalars().first()
    if d is None:
        d = SysParamDef(param_key=key)
        db.add(d)
    d.label = req.label.strip() or key
    d.description = req.description.strip()
    d.multi_mode = req.multi_mode
    if req.default_values is not None:
        d.default_values = clean_values(req.default_values, multi=req.multi_mode != "single", what=f"「{d.label}」")
    elif req.multi_mode == "single" and len(d.default_values or []) > 1:
        d.default_values = list(d.default_values)[:1]
    d.arg_names = args
    d.fill_mode = req.fill_mode
    d.is_secret = req.is_secret
    d.enabled = req.enabled
    d.updated_by = user.user_id
    await db.commit()
    sp.invalidate()                        # 定义影响所有景区
    await db.refresh(d)
    return def_dict(d, (await _override_counts(db)).get(key, 0))


@router.delete("/{param_key}")
async def delete_param(param_key: str, db: AsyncSession = Depends(get_db),
                       user: CurrentUser = Depends(get_current_user)):
    """删除定义,连同各景区的取值。引用了它的工具再执行会明确报「需要系统参数 {{…}}」。"""
    from cogni_agent.gateway_service.models import SysParamDef, SysParamValue
    from cogni_agent.runtime_service.engine import scenic_params as sp

    _require_admin(user)
    d = (await db.execute(select(SysParamDef).where(SysParamDef.param_key == param_key))).scalars().first()
    if d is None:
        raise NotFound(f"参数「{param_key}」不存在")
    await db.execute(delete(SysParamValue).where(SysParamValue.param_key == param_key))
    await db.delete(d)
    await db.commit()
    sp.invalidate()
    return {"ok": True}


# ── 按景区分配取值(集团统一管理) ─────────────────────────────────────────
#
# 同一个标识 {{ota_account}},景区 A、景区 B 各分配各的值;执行时只按**本次任务所属景区**取,
# 景区里的人拿不到别的景区的值(取值键是会话的景区,不是模型或用户能填的参数)。
# 集团在这里一处分配,不用挨个进景区配置页;景区配置页看到 / 改的是同一份数据。


class ScenicAssignment(BaseModel):
    scenic_id: str
    values: list[ParamValueItem] = Field(default_factory=list)


class AssignmentsSave(BaseModel):
    """整组保存:列表里没有的景区 = 取消分配(回到系统默认值)。"""
    items: list[ScenicAssignment] = Field(default_factory=list)


async def _assignments(db: AsyncSession, d) -> list[dict]:
    from cogni_agent.gateway_service.models import SysParamValue
    from cogni_agent.user_center_service.models import SysScenic

    rows = (await db.execute(select(SysParamValue).where(SysParamValue.param_key == d.param_key)
                             .order_by(SysParamValue.scenic_id))).scalars().all()
    names = {}
    if rows:
        names = {s.scenic_id: s.scenic_name for s in (await db.execute(
            select(SysScenic).where(SysScenic.scenic_id.in_([r.scenic_id for r in rows])))).scalars().all()}
    return [{"scenic_id": r.scenic_id, "scenic_name": names.get(r.scenic_id) or "",
             "values": values_out(r.values, d.is_secret),
             "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]


@router.get("/{param_key}/assignments")
async def list_assignments(param_key: str, db: AsyncSession = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """这个参数分配给了哪些景区、各是什么值(敏感值只回末四位)。"""
    from cogni_agent.gateway_service.models import SysParamDef

    _require_admin(user)
    d = (await db.execute(select(SysParamDef).where(SysParamDef.param_key == param_key))).scalars().first()
    if d is None:
        raise NotFound(f"参数「{param_key}」不存在")
    return {"param_key": param_key, "items": await _assignments(db, d)}


@router.put("/{param_key}/assignments")
async def save_assignments(param_key: str, req: AssignmentsSave, db: AsyncSession = Depends(get_db),
                           user: CurrentUser = Depends(get_current_user)):
    """整组保存各景区的取值。敏感参数:某个景区 values 为空 = 保持该景区原值(界面看不到明文,没法回填)。"""
    from cogni_agent.gateway_service.models import SysParamDef, SysParamValue
    from cogni_agent.runtime_service.engine import scenic_params as sp
    from cogni_agent.user_center_service.models import SysScenic

    _require_admin(user)
    d = (await db.execute(select(SysParamDef).where(SysParamDef.param_key == param_key))).scalars().first()
    if d is None:
        raise NotFound(f"参数「{param_key}」不存在")
    ids = [it.scenic_id.strip() for it in req.items if it.scenic_id.strip()]
    if len(ids) != len(set(ids)):
        raise ValidationError("同一个景区出现了两次")
    known = {s for (s,) in (await db.execute(select(SysScenic.scenic_id).where(SysScenic.scenic_id.in_(ids or [""])))).all()}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValidationError(f"景区不存在:{'、'.join(unknown)}")
    rows = {r.scenic_id: r for r in (await db.execute(
        select(SysParamValue).where(SysParamValue.param_key == param_key))).scalars().all()}
    for sid, r in rows.items():
        if sid not in ids:
            await db.delete(r)
    for it in req.items:
        sid = it.scenic_id.strip()
        vals = clean_values(it.values, multi=d.multi_mode != "single", what=f"「{d.label}」在景区 {sid} 的取值")
        r = rows.get(sid)
        if not vals:
            if r is not None and d.is_secret:
                continue                      # 敏感参数没填新值 = 保持原值
            if r is not None:
                await db.delete(r)
            continue
        if r is None:
            r = SysParamValue(param_key=param_key, scenic_id=sid)
            db.add(r)
        r.values = vals
        r.updated_by = user.user_id
    await db.commit()
    sp.invalidate()
    return {"param_key": param_key, "items": await _assignments(db, d)}
