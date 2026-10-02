"""M2 对外路由:auth 登录相关(公开),users 用户管理等(需鉴权)。router prefix="/admin"。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.config import get_settings
from cogni_agent.common.errors import Forbidden
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.user_center_service import auth as auth_service
from cogni_agent.user_center_service import users as users_service
from cogni_agent.user_center_service.auth import send_sms_code
from cogni_agent.user_center_service.schemas import (
    CreateUserRequest, LoginRequest, LoginResponse, UpdateUserRequest,
    ModelConfigCreate, ModelConfigUpdate, ModelProviderCreate, ModelProviderUpdate,
)

router = APIRouter(prefix="/admin", tags=["用户中心"])


# ═══════════════════════════════════════════════════════════════
# RSA 公钥下发：前端登录前获取公钥，加密密码后传输。
# ═══════════════════════════════════════════════════════════════

@router.get("/auth/public-key")
async def get_public_key():
    """返回 RSA 公钥，前端用此公钥加密密码后提交登录/改密请求。无需鉴权。"""
    return {
        "public_key": auth_service.get_public_key_pem(),
        "algorithm": "RSA-OAEP",
        "key_size": 2048,
    }


@router.post("/auth/sms/send")
async def sms_send(phone: str, purpose: str = "login", db: AsyncSession = Depends(get_db)):
    """
    G-ROLE-02 场景说明:登录框标注的是"账号/手机号",这里传进来的 phone 参数实际上
    可能是账号(如种子超管账号 pftroot,真实手机号是占位的 00000000000)。必须先按
    账号或手机号解析出真实手机号再落库/发送验证码,否则验证码会被存到一个不存在的
    "手机号"(账号字符串)下面,后续 login() 用真实手机号去核对永远对不上——这正是
    之前"验证码发了但登录还是 401"的根因。找不到对应用户时,静默按原样处理(不暴露
    账号是否存在,避免用户枚举),但这种情况下验证码本来就不会被任何真实用户用到。
    """
    from sqlalchemy import or_, select

    from cogni_agent.user_center_service.models import User

    resolved = (await db.execute(select(User).where(or_(User.phone == phone, User.account == phone)))).scalar_one_or_none()
    real_phone = resolved.phone if resolved else phone
    await send_sms_code(db, real_phone, purpose)
    return {"ok": True}


@router.post("/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    device_info = request.headers.get("User-Agent", "unknown")
    ip = request.client.host if request.client else "unknown"
    plain_password = auth_service.try_decrypt_password(req.password, req.rsa_encrypt)
    access, refresh, must_change = await auth_service.login(db, req.phone, plain_password, req.sms_code, device_info, ip)
    return LoginResponse(access_token=access, refresh_token=refresh, must_change_password=must_change)


def _require_admin(user: CurrentUser):
    """G-ROLE-03/04:系统设置类/用户管理类接口仅 group_admin、super_admin 可调用。
    (原判断写的是字面量 "admin",但 usr_users.role 的真实取值里没有这个值——
    实际枚举是 super_admin/group_admin/group_developer/group_viewer/scenic_admin/
    scenic_operator,写成 "admin" 会导致 group_admin 永远被拒绝,已修正。)"""
    if user.role not in ("super_admin", "group_admin"):
        raise Forbidden("需要 group_admin 或 super_admin 权限")  # 中间件/异常处理器统一记入审计 UnauthorizedAccess


@router.get("/auth/login-policy")
async def login_policy():
    """登录页需要知道的策略。**不需要鉴权**——它在登录之前就要用。

    没有这个接口时,前端只能把验证码输入框藏在一个「点此获取」链接后面,让用户自己
    猜要不要验证码;开启强制验证码的部署里,不点那个链接就会一直 401,而错误提示
    (「需要短信验证码」)出现得太晚。现在登录页直接按策略决定要不要展示验证码区域。
    """
    return {
        "require_sms_on_first_login": bool(
            get_settings().get("user_center", "require_sms_on_first_login", default=False)
        ),
        "sms_provider": get_settings().get("user_center", "sms", "provider", default="mock"),
    }


@router.get("/auth/permissions")
async def read_permissions(
    user: CurrentUser = Depends(get_current_user),
    domain: str = Query("admin", description="权限域: admin=管理端, app=APP端"),
):
    """当前角色在指定域下的能力清单(菜单可见性 + 按钮级能力)。

    管理端前端: GET /auth/permissions           → 返回 admin 域菜单
    APP 端前端: GET /auth/permissions?domain=app → 返回 APP 域 tab 菜单

    前端据此隐藏菜单和按钮,而不是在 Sidebar 里硬编码一份角色白名单——硬编码那份
    和后端判断迟早会漂移,改权限还得同时改两处。
    注意:这只是体验优化,真正的拦截永远在服务端;两者读同一份 Casbin 策略,是为了
    不再出现"菜单看得见、一点就 403"。
    """
    from cogni_agent.governance_event_bus_service.policy import permissions_for

    return await permissions_for(user.role, domain=domain)


@router.get("/auth/me")
async def read_me(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """当前登录用户的基本信息,供前端侧边栏/个人信息展示使用。"""
    from cogni_agent.user_center_service.models import User

    row = await db.get(User, user.user_id)
    if not row:
        raise Forbidden("用户不存在")
    return {
        "id": row.id, "name": row.name, "account": row.account, "phone": row.phone,
        "role": row.role, "scenic_id": row.scenic_id, "position": row.position,
    }


def _user_dict(u) -> dict:
    """绝不能把 password_hash 带出接口——之前 create/list/update 三个接口都是直接把
    SQLAlchemy User 对象扔给 FastAPI 序列化,连同 bcrypt 哈希一起吐出去了,已修正。"""
    return {
        "id": u.id, "name": u.name, "account": u.account, "phone": u.phone,
        "scenic_id": u.scenic_id, "role": u.role, "position": u.position,
        "is_active": u.is_active, "must_change_password": u.must_change_password,
        "last_login_at": u.last_login_at, "created_at": u.created_at,
    }


@router.post("/users")
async def admin_create_user(req: CreateUserRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    created = await users_service.create_user(db, req)
    # 回传初始密码给管理员:短信是 mock 时,不回传就没人知道新账号的密码
    return {**_user_dict(created),
            "initial_password": created.__dict__.get("_initial_password"),
            "message": f"初始密码为「{created.__dict__.get('_initial_password')}」(手机号后4位+@123),使用者首次登录会被强制修改"}


@router.get("/users")
async def admin_list_users(keyword: str | None = None, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    # super_admin 的 scenic_id 约定为空字符串/None 代表平台级,可见全平台(G-ISO-03)
    scope_org = None if user.role == "super_admin" else user.scenic_id
    rows = await users_service.list_users(db, scope_org, keyword)
    return [_user_dict(u) for u in rows]


@router.patch("/users/{user_id}")
async def admin_update_user(user_id: str, req: UpdateUserRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    updated = await users_service.update_user(db, user_id, req)
    return _user_dict(updated)


class ResetPasswordRequest(BaseModel):
    password: str | None = None   # 留空则用默认规则「手机号后4位@123」


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, req: ResetPasswordRequest | None = None,
                               user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    new_pwd = await users_service.reset_password(db, user_id, req.password if req else None)
    # 把新密码回给管理员:短信通道是 mock 时,不回传就没人知道密码被重置成了什么
    return {"ok": True, "new_password": new_pwd,
            "message": "密码已重置,使用者首次登录会被要求修改"}


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:删除、分页、角色聚合、批量分配
# ═══════════════════════════════════════════════════════════════

@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除用户(不可删自己),级联清理登录设备记录。"""
    _require_admin(user)
    await users_service.delete_user(db, user_id, user.user_id)
    return {"ok": True, "message": "用户已删除"}


@router.get("/users/by-role")
async def admin_users_by_role(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """按角色聚合用户列表,超级管理员专用。"""
    if user.role != "super_admin":
        raise Forbidden("仅超级管理员可查看")
    return await users_service.get_users_by_role(db)


class BatchRoleRequest(BaseModel):
    user_ids: list[str]
    role: str


@router.post("/users/batch-role")
async def admin_batch_update_role(req: BatchRoleRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """批量修改用户角色,仅超级管理员。"""
    if user.role != "super_admin":
        raise Forbidden("仅超级管理员可操作")
    count = await users_service.batch_update_role(db, req.user_ids, req.role)
    return {"ok": True, "count": count, "message": f"已更新 {count} 个用户的角色为 {req.role}"}


# 增强版用户列表(分页+多条件筛选+联表景区名)
@router.get("/users/paged")
async def admin_list_users_paged(
    keyword: str | None = None,
    role: str | None = None,
    scenic_id: str | None = None,
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    # 组织隔离:非超管只看本组织
    scope_scenic = None if user.role == "super_admin" else user.scenic_id
    if scope_scenic and scenic_id is None:
        scenic_id = scope_scenic
    items, total = await users_service.list_users_page(db, keyword, role, scenic_id, is_active, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0}


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:组织管理(景区/集团/角色)
# ═══════════════════════════════════════════════════════════════

@router.get("/scenics")
async def admin_list_scenics(
    keyword: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """分页查询景区列表,含用户数统计。"""
    _require_admin(user)
    from cogni_agent.user_center_service import scenics as scenics_service
    items, total = await scenics_service.list_scenics(db, keyword, is_enabled, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0}


@router.get("/auth/capabilities")
async def auth_capabilities(user: CurrentUser = Depends(get_current_user)):
    """当前账号能做什么:按钮显不显示、能不能点,前端照这个来。

    前端自己按角色硬编码的话,每加一个角色要改三处页面,而且改漏了是「能点但点了报错」——
    比按钮不显示更糟。这里给出的是**同一套**判定(common/permissions.py),
    与后端真正执行的判定同源,不会出现「前端让点、后端拒绝」。
    """
    from cogni_agent.common import permissions as perm

    return perm.capabilities(user.role, user.scenic_id)


@router.get("/scenics/catalog")
async def admin_scenic_catalog(
    keyword: str | None = None,
    province: str | None = None,
    grade: str | None = None,
    registered: bool | None = None,
    enabled: bool | None = None,
    user: CurrentUser = Depends(get_current_user),
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """景区主数据检索(dim_prd_scenic_base_yr_f)。

    「+ 登记景区」的候选池、给用户绑景区、给 Quick BI 连接/资产/问题绑景区,都从这里挑 ——
    手工敲景区 ID 迟早会敲错一个字符,而错了之后所有按 scenic_id 的关联都对不上。

    - `registered=false` 只看还没登记的(登记弹窗用);
    - `enabled=true` 只看已登记**且已开通**的 —— 各处的绑定下拉都该用它,
      停用的景区绑上去是一条配了也不生效的记录,列出来只会让人以为它能用。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service import scenic_catalog

    # 景区账号也要用这个接口(绑定框要显示自己景区的名字),但只能看见自己那一条:
    # 主数据有一万多个景区,整本给出去没有道理
    only = perm.read_scope(user.role, user.scenic_id)
    if only is None and not perm.is_group_role(user.role):
        raise Forbidden("当前账号未绑定景区,无法检索景区主数据;请联系管理员在「组织管理」绑定")
    items, total = await scenic_catalog.search_catalog(
        db, keyword=keyword, province=province, grade=grade, registered=registered,
        enabled=enabled, only_scenic_id=only, page=page, page_size=page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0}


@router.get("/scenics/catalog/filters")
async def admin_scenic_catalog_filters(user: CurrentUser = Depends(get_current_user),
                                       db: AsyncSession = Depends(get_db)):
    """主数据里实际出现过的省份 / 等级,供筛选下拉用(写死枚举会和主数据对不上)。"""
    _require_admin(user)
    from cogni_agent.user_center_service import scenic_catalog

    return {"provinces": await scenic_catalog.provinces(db),
            "grades": await scenic_catalog.grades(db)}


@router.get("/scenics/catalog/{scenic_id}")
async def admin_scenic_catalog_detail(scenic_id: str, user: CurrentUser = Depends(get_current_user),
                                      db: AsyncSession = Depends(get_db)):
    """单个景区的主数据明细(各处绑定框拿它把 ID 显示成名字)。"""
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service import scenic_catalog

    # 景区账号只能查自己那个景区 —— 但必须能查,否则绑定框里只剩一串 PFTSC… 编号
    perm.assert_can_read(user.role, user.scenic_id, scenic_id, "景区")
    return await scenic_catalog.get_catalog(db, scenic_id)


class ScenicRegisterRequest(BaseModel):
    """登记景区:从主数据里挑若干条写进平台。名称/等级不接受手填 —— 以主数据为准。"""

    scenic_ids: list[str]
    enable: bool = True


@router.post("/scenics/register")
async def admin_register_scenics(req: ScenicRegisterRequest,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """批量登记景区。已登记的会被跳过(不报错),主数据里查无此景区则整批拒绝。"""
    _require_admin(user)
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service import scenic_catalog

    # 登记景区 = 决定平台服务开给谁,是集团级动作。景区角色不该能给自己开通,
    # 否则「谁能用这个平台」这件事就没有把关人了
    if not perm.can_manage_all(user.role):
        raise Forbidden("登记景区需要集团管理员 / 集团开发者 / 超级管理员权限")
    return await scenic_catalog.register(db, req.scenic_ids, enable=req.enable)


@router.post("/scenics/sync")
async def admin_sync_scenics(scenic_id: str | None = None,
                             user: CurrentUser = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    """用主数据刷新已登记景区的名称/等级(不动开通状态与套餐配置)。"""
    _require_admin(user)
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service import scenic_catalog

    if not perm.can_manage_all(user.role):
        raise Forbidden("同步景区主数据需要集团管理员 / 集团开发者 / 超级管理员权限")
    return await scenic_catalog.sync_from_dim(db, [scenic_id] if scenic_id else None)


@router.patch("/scenics/{scenic_id}")
async def admin_update_scenic(scenic_id: str, is_enabled: bool,
                              user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """开通/停用景区平台服务。景区尚未登记但主数据里有的,会先按主数据自动登记。"""
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service import scenics as scenics_service

    if not perm.can_manage_all(user.role):
        raise Forbidden("开通/停用景区需要集团管理员 / 集团开发者 / 超级管理员权限")
    return await scenics_service.update_scenic_status(db, scenic_id, is_enabled)


@router.get("/group")
async def admin_get_group(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """获取集团配置信息。"""
    _require_admin(user)
    from cogni_agent.user_center_service.models import SysGroup
    from sqlalchemy import select
    row = (await db.execute(select(SysGroup).where(SysGroup.id == "default"))).scalar_one_or_none()
    if not row:
        return {"id": "default", "name": "", "config": {}}
    return {"id": row.id, "name": row.name, "config": row.config}


@router.get("/roles")
async def admin_list_roles(with_permissions: bool = False, domain: str = "admin",
                           user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """角色列表,可选含 Casbin 权限详情。domain 区分管理端(admin) / APP 端(app)。"""
    from cogni_agent.governance_event_bus_service.policy import permissions_for
    from cogni_agent.user_center_service.schemas import RoleInfo

    role_labels = {
        "super_admin": "超级管理员", "group_admin": "集团管理员",
        "group_developer": "集团开发者", "group_viewer": "集团查看者",
        "scenic_admin": "景区管理员", "scenic_operator": "景区运营",
    }
    roles = []
    for role_key, label in role_labels.items():
        perms = await permissions_for(role_key, domain=domain) if with_permissions else None
        roles.append(RoleInfo(role=role_key, label=label, permissions=perms))
    return roles


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:Casbin 权限策略 CRUD(仅 super_admin)
# ═══════════════════════════════════════════════════════════════

@router.get("/permissions")
async def admin_list_permissions(domain: str = "admin", user: CurrentUser = Depends(get_current_user)):
    """列出 Casbin 策略(policy + grouping)。domain 过滤管理端/APP 端。仅超级管理员。"""
    if user.role != "super_admin":
        raise Forbidden("仅超级管理员可查看权限策略")
    from cogni_agent.governance_event_bus_service.policy import get_enforcer
    _e = get_enforcer()
    _e.load_policy()
    all_policies = _e.get_policy()
    # policy 四元组:(sub, obj, act, domain)——按 domain 过滤
    if domain:
        all_policies = [p for p in all_policies if len(p) >= 4 and p[3] == domain]
    policies = [{"type": "p", "sub": p[0], "obj": p[1], "act": p[2], "domain": p[3] if len(p) >= 4 else "admin"} for p in all_policies]
    groupings = [{"type": "g", "sub": g[0], "role": g[1]} for g in _e.get_grouping_policy()]
    return {"policies": policies, "groupings": groupings}


class PermissionRequest(BaseModel):
    sub: str
    obj: str
    act: str
    domain: str = "admin"    # 权限域:admin=管理端, app=APP 端


@router.post("/permissions")
async def admin_add_permission(req: PermissionRequest, user: CurrentUser = Depends(get_current_user)):
    """新增 Casbin 策略。仅超级管理员。"""
    if user.role != "super_admin":
        raise Forbidden("仅超级管理员可修改权限策略")
    from cogni_agent.governance_event_bus_service.policy import get_enforcer
    _e = get_enforcer()
    _e.add_policy(req.sub, req.obj, req.act, req.domain)
    _e.save_policy()
    return {"ok": True, "message": f"已添加策略[{req.domain}]: {req.sub} → {req.obj}:{req.act}"}


@router.delete("/permissions")
async def admin_remove_permission(req: PermissionRequest, user: CurrentUser = Depends(get_current_user)):
    """删除 Casbin 策略。仅超级管理员。"""
    if user.role != "super_admin":
        raise Forbidden("仅超级管理员可修改权限策略")
    from cogni_agent.governance_event_bus_service.policy import get_enforcer
    _e = get_enforcer()
    _e.remove_policy(req.sub, req.obj, req.act, req.domain)
    _e.save_policy()
    return {"ok": True, "message": f"已删除策略[{req.domain}]: {req.sub} → {req.obj}:{req.act}"}


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:模型厂商与模型配置 CRUD
# ═══════════════════════════════════════════════════════════════

@router.post("/model-providers")
async def admin_create_model_provider(
    req: ModelProviderCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    return await mc.create_provider(db, req)


@router.get("/model-providers")
async def admin_list_model_providers(
    keyword: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    items, total = await mc.list_providers(db, keyword, is_enabled, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0}


@router.patch("/model-providers/{provider_id}")
async def admin_update_model_provider(
    provider_id: str, req: ModelProviderUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    from cogni_agent.common.errors import NotFound
    result = await mc.update_provider(db, provider_id, req)
    if not result:
        raise NotFound("厂商不存在")
    return result


@router.delete("/model-providers/{provider_id}")
async def admin_delete_model_provider(provider_id: str,
                                      user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    from cogni_agent.common.errors import NotFound, ValidationError
    try:
        ok = await mc.delete_provider(db, provider_id)
    except ValueError as e:
        raise ValidationError(str(e))
    if not ok:
        raise NotFound("厂商不存在")
    return {"ok": True, "message": "厂商已删除"}


@router.post("/model-configs")
async def admin_create_model_config(
    req: ModelConfigCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    return await mc.create_model_config(db, req)


@router.get("/model-configs")
async def admin_list_model_configs(
    keyword: str | None = None,
    provider_id: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    items, total = await mc.list_model_configs(db, keyword, provider_id, is_enabled, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0}


@router.get("/model-configs/{model_id}")
async def admin_get_model_config(model_id: str,
                                 user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)      # 列表接口判了、改删也判了,唯独详情漏了
    from cogni_agent.user_center_service import model_configs as mc
    from cogni_agent.common.errors import NotFound
    result = await mc.get_model_config(db, model_id)
    if not result:
        raise NotFound("模型配置不存在")
    return result


@router.patch("/model-configs/{model_id}")
async def admin_update_model_config(
    model_id: str, req: ModelConfigUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    from cogni_agent.common.errors import NotFound
    result = await mc.update_model_config(db, model_id, req)
    if not result:
        raise NotFound("模型配置不存在")
    return result


@router.delete("/model-configs/{model_id}")
async def admin_delete_model_config(model_id: str,
                                    user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    from cogni_agent.user_center_service import model_configs as mc
    from cogni_agent.common.errors import NotFound
    ok = await mc.delete_model_config(db, model_id)
    if not ok:
        raise NotFound("模型配置不存在")
    return {"ok": True, "message": "模型配置已删除"}


# 这里原先有一个 GET /model-configs/active/all,直接返回 get_enabled_models() ——
# 而那个函数是**给进程内 LLM Client 用的**,返回里带 provider.api_key 明文。
# 它既没有 _require_admin,也没有任何角色判定,等于任何登录账号(含景区操作员、集团只读)
# 都能把全部大模型供应商的密钥拉走。前端与后端都没有任何地方调它 —— LLM Client 是直接
# 调函数的,不走 HTTP。所以这条路由整个删掉:它只有攻击面,没有用处。
# 要看模型清单用 GET /model-configs(已 _require_admin,且不返回密钥)。


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:refresh_token 刷新、改密、手机号检查
# ═══════════════════════════════════════════════════════════════

class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/refresh")
async def refresh_token(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """refresh_token 换新 access_token(滚动刷新)。"""
    from cogni_agent.user_center_service.auth import refresh_access_token
    access, refresh, _ = await refresh_access_token(db, req.refresh_token)
    return LoginResponse(access_token=access, refresh_token=refresh, must_change_password=False)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    rsa_encrypt: bool = False    # true 时 old_password/new_password 均为 RSA 加密后的 Base64 密文


@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest,
                          user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """已登录用户修改密码。"""
    old_pwd = auth_service.try_decrypt_password(req.old_password, req.rsa_encrypt)
    new_pwd = auth_service.try_decrypt_password(req.new_password, req.rsa_encrypt)
    ok, code, message = await users_service.change_own_password(db, user.user_id, old_pwd, new_pwd)
    if not ok:
        return {"ok": False, "code": code, "message": message}
    return {"ok": True, "message": message}


class CheckPhoneRequest(BaseModel):
    phone: str


@router.post("/auth/check-phone")
async def check_phone(req: CheckPhoneRequest, db: AsyncSession = Depends(get_db)):
    """检查手机号是否已注册(供注册页和 login-policy 配合使用)。"""
    from cogni_agent.user_center_service.models import User
    from sqlalchemy import select
    row = (await db.execute(select(User).where(User.phone == req.phone))).scalar_one_or_none()
    return {"exists": row is not None}
