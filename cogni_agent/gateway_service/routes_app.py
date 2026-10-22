"""
APP 端 API(/api/app/*)。

**与管理端共用同一套业务逻辑**,只是路由前缀和返回结构不同。这里不重新实现任何鉴权、
密码校验或用户查询——全部委托给 user_center_service 里管理端也在用的那几个函数。
一旦某天登录规则变了(比如加了设备风控),两端会同时生效,不会出现"网页端修了、
APP 端还是老逻辑"这种最难排查的不一致。

与管理端的差异(架构图 ② 明确「APP 端仅登录,不可自注册」):
- **没有注册接口**。账号由管理后台创建,APP 只能登录。
- **返回结构更扁平**,并附带 APP 关心的字段(是否需要改密、能否使用操练台等)。
- **不暴露管理能力**。用户管理、系统设置这些一律不在这个前缀下。

v1.1 阶段二新增(2A.1-2A.5):
- /api/app/agent/home    聚合首页
- /api/app/chat           发起对话
- /api/app/chat/{id}/stream SSE 流式
- /api/app/conversations  会话管理(3个端点)
- /api/app/messages/* + /api/app/followed-questions  消息操作(5个端点)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db, get_sessionmaker, get_sessionmaker
from cogni_agent.common.errors import NotFound
from cogni_agent.common.logging import get_logger
from cogni_agent.gateway_service.summary_line import strip_markdown, summary_line
from cogni_agent.runtime_service.engine import chart_view
from cogni_agent.common.schemas import TaskErrorKind, task_error
from cogni_agent.common.time import today_cst
from cogni_agent.control_plane_service import conversations as conv_svc
from cogni_agent.control_plane_service.models import (
    SOURCE_APP,
    AppFollowedQuestion,
    AppMessageFeedback,
    Conversation,
    Task,
)
from cogni_agent.control_plane_service.queue import cancel_task, create_task
from cogni_agent.user_center_service import auth as auth_service
from cogni_agent.user_center_service import users as users_service
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user, get_current_app_user
from cogni_agent.gateway_service.pre_params import resolve_pre_params
from cogni_agent.gateway_service.schemas_app import (
    AgentHomeResponse,
    AppChatRequest,
    AppChatResponse,
    AppConversationListItem,
    AppConversationListResponse,
    AppFollowQuestionRequest,
    AppFollowedQuestionItem,
    AppFollowedQuestionListResponse,
    AppGuideQuestionItem,
    AppGuideQuestionListResponse,
    AppMessageFeedbackRequest,
    AppPanelInsightResponse,
    AppPrewarmHitResult,
    AppTargetItem,
    GuideCard,
    OverviewInsightItem,
    SelectorMetricItem,
    SelectorQuestionRef,
    SelectorThemeItem,
)
from cogni_agent.runtime_service import streaming
from cogni_agent.tool_center_service.agent.models import Agent

log = get_logger("gateway.app")

# 主题解读报告的问题前缀(与 scenic_binding.THEME_PREFIX 同值)。
THEME_REPORT_PREFIX = "【主题解读】"

router = APIRouter(prefix="/api/app", tags=["app"])


class AppLoginRequest(BaseModel):
    account: str          # 账号或手机号,与管理端一致
    password: str
    sms_code: str | None = None
    rsa_encrypt: bool = False        # true 时 password 字段为 RSA 加密后的 Base64 密文
    device_id: str | None = None      # APP 侧设备 UUID，用于新设备识别
    device_name: str | None = None    # 如 "iPhone 15 / iOS 17.4"
    device_os: str | None = None      # iOS / Android
    push_token: str | None = None     # APNs(iOS) / FCM(Android) 推送 Token
    app_version: str | None = None    # APP 版本号，如 "1.0.0"


@router.get("/auth/public-key")
async def app_public_key():
    """返回 RSA 公钥，APP 端登录前获取并缓存，用于加密密码传输。无需鉴权。"""
    from cogni_agent.user_center_service.auth import get_public_key_pem
    return {
        "public_key": get_public_key_pem(),
        "algorithm": "RSA-OAEP",
        "key_size": 2048,
    }


@router.get("/login-policy")
async def app_login_policy():
    """APP 登录页据此决定要不要展示验证码输入框。无需鉴权。"""
    from cogni_agent.config import get_settings

    return {
        "require_sms_on_first_login": bool(
            get_settings().get("user_center", "require_sms_on_first_login", default=False)),
        "allow_self_register": False,   # 架构约定:APP 端不可自注册,写死返回 false
    }


@router.post("/auth/sms/send")
async def app_send_sms(account: str, db: AsyncSession = Depends(get_db)):
    """发送登录验证码。account 传账号或手机号都行,服务端会解析成真实手机号。"""
    from sqlalchemy import or_, select

    from cogni_agent.user_center_service.models import User

    user = (await db.execute(
        select(User).where(or_(User.phone == account, User.account == account))
    )).scalars().first()
    # 找不到用户也返回 ok:否则这个接口就成了"账号是否存在"的探测器
    if user:
        await auth_service.send_sms_code(db, user.phone, "login")
    return {"ok": True}


@router.post("/auth/login")
async def app_login(req: AppLoginRequest, db: AsyncSession = Depends(get_db)):
    """APP 登录。复用管理端 auth_service.login,规则完全一致。"""
    device = req.device_name or req.device_id or "mobile-app"
    plain_password = auth_service.try_decrypt_password(req.password, req.rsa_encrypt)
    access, refresh, must_change = await auth_service.login(
        db, req.account, plain_password, req.sms_code, device, ip="",
        device_id=req.device_id,
        device_os=req.device_os,
        push_token=req.push_token,
        app_version=req.app_version,
    )
    me = await users_service.get_by_account_or_phone(db, req.account)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "must_change_password": must_change,
        "user": await _app_profile(me, db) if me else None,
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/refresh")
async def app_refresh(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """用 refresh_token 换取新的 access_token + 滚动刷新 refresh_token。

    管理端 `/api/auth/refresh` 已有实现（routes.py），本端点复用同一业务逻辑
    `auth_service.refresh_access_token()`，规则完全一致。
    """
    access, refresh, must_change = await auth_service.refresh_access_token(db, req.refresh_token)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "must_change_password": must_change,
    }


@router.post("/auth/logout")
async def app_logout(user: CurrentUser = Depends(get_current_user)):
    """登出。

    当前 JWT 是无状态的,服务端不维护会话表,所以这里主要做两件事:记一条登出日志
    (审计需要),以及给 APP 一个明确的成功信号让它清本地 token。
    **需要"服务端强制失效"时**(如挂失设备),得引入 token 黑名单或改用短时 access +
    刷新令牌轮换 —— 那是另一项改造,现在没有做,不要以为调了这个接口旧 token 就失效了。
    """
    log.info(f"app user {user.user_id} logged out")
    return {"ok": True, "note": "请在客户端清除本地令牌"}


@router.get("/permissions")
async def app_permissions(user: CurrentUser = Depends(get_current_user)):
    """当前用户 APP 端的能力清单（数据导航 tab 菜单可见性）。

    返回 APP 域下 5 个 tab 的可见性（data_overview / ticket_manage / passenger_flow /
    public_opinion / ota_direct_sales），前端据此决定哪些导航 tab 展示。
    权限校验读的是 Casbin 策略（policy.py 中的 APP_BASE_POLICIES + ROLE_INHERITANCE），
    与管理端 `/auth/permissions` 同一份策略引擎，只是 domain 不同。

    注意：这只是体验优化，真正的拦截在服务端——两者读同一份策略。
    """
    from cogni_agent.governance_event_bus_service.policy import permissions_for

    perms = await permissions_for(user.role, domain="app")
    return perms if perms else None


@router.get("/me")
async def app_me(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """当前登录用户的个人信息。"""
    me = await users_service.get_by_id(db, user.user_id)
    if me is None:
        from cogni_agent.common.errors import NotFound

        raise NotFound("用户不存在")
    return await _app_profile(me, db)


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    position: str | None = None


@router.post("/me")
async def app_update_me(req: UpdateProfileRequest, user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """修改个人信息。**只允许改姓名和职位** —— 角色、所属景区、账号这些属于授权范畴,
    只能由管理后台变更,否则用户可以自己给自己升权。"""
    me = await users_service.get_by_id(db, user.user_id)
    if me is None:
        from cogni_agent.common.errors import NotFound

        raise NotFound("用户不存在")
    if req.name is not None:
        me.name = req.name
    if req.position is not None:
        me.position = req.position
    await db.commit()
    return await _app_profile(me, db)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    rsa_encrypt: bool = False    # true 时 old_password/new_password 均为 RSA 加密后的 Base64 密文


@router.post("/me/change-password")
async def app_change_password(req: ChangePasswordRequest, user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """修改自己的密码。首次登录被要求改密时走的也是这个接口。"""
    old_pwd = auth_service.try_decrypt_password(req.old_password, req.rsa_encrypt)
    new_pwd = auth_service.try_decrypt_password(req.new_password, req.rsa_encrypt)
    ok, code, message = await users_service.change_own_password(db, user.user_id, old_pwd, new_pwd)
    if not ok:
        from cogni_agent.common.logging import get_trace_id
        from cogni_agent.common.schemas import ApiResponse
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content=ApiResponse.fail(code=code, message=message, trace_id=get_trace_id()).model_dump(),
        )
    return {"ok": True, "message": message}


async def _app_profile(u, db) -> dict:
    """APP 侧的用户信息结构。

    刻意不直接把 ORM 对象序列化出去:那样会顺带带出 password_hash 这类字段。
    这个坑管理端踩过一次(create/list/update 三个接口都把哈希带出去了),这里从一开始
    就用白名单构造。
    """
    from cogni_agent.user_center_service.models import SysScenic
    from sqlalchemy import select

    scenic_name = None
    scenic_grade_level = None
    if u.scenic_id:
        try:
            row = (await db.execute(
                select(SysScenic.scenic_name, SysScenic.scenic_grade_level)
                .where(SysScenic.scenic_id == u.scenic_id)
            )).first()
            if row:
                scenic_name = row.scenic_name
                scenic_grade_level = row.scenic_grade_level
        except Exception:
            pass

    return {
        "id": u.id,
        "name": u.name,
        "account": u.account,
        "phone": _mask_phone(u.phone),
        "role": u.role,
        "scenic_id": u.scenic_id,
        "scenic_name": scenic_name,
        "scenic_grade_level": scenic_grade_level,
        "position": u.position,
        "must_change_password": u.must_change_password,
        "last_login_at": u.last_login_at,
    }


def _mask_phone(phone: str | None) -> str:
    """手机号打码。APP 界面上没有展示完整手机号的必要,少一处泄露面。"""
    if not phone or len(phone) < 7:
        return phone or ""
    return f"{phone[:3]}****{phone[-4:]}"


# ═══════════════════════════════════════════════════════════════
# v1.0 APP 端：用户配置存储（文档 05）
# ═══════════════════════════════════════════════════════════════

from cogni_agent.gateway_service import config_service
from cogni_agent.gateway_service.schemas_app import CONFIG_TYPE_WHITELIST


@router.get("/config")
async def app_get_config(
    type: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取指定类型的用户配置。type 不存在时返回 config: {}。"""
    if not type:
        from cogni_agent.common.errors import ValidationError
        raise ValidationError("缺少 type 参数")
    if type not in CONFIG_TYPE_WHITELIST:
        from cogni_agent.common.errors import ValidationError
        raise ValidationError(f"不支持的配置类型: {type}")

    data = await config_service.get_config(db, user.user_id, type)
    if data is None:
        return {"config_type": type, "config": {}, "updated_at": None}
    return {"config_type": type, **data}


@router.post("/config")
async def app_save_config(
    type: str,
    config: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """保存/覆盖用户配置。请求体即 config JSON，全量覆盖已有配置。"""
    if not type:
        from cogni_agent.common.errors import ValidationError
        raise ValidationError("缺少 type 参数")
    if type not in CONFIG_TYPE_WHITELIST:
        from cogni_agent.common.errors import ValidationError
        raise ValidationError(f"不支持的配置类型: {type}")

    updated_at = await config_service.save_config(db, user.user_id, type, config)
    return {"ok": True, "updated_at": updated_at}


@router.get("/config/types")
async def app_list_config_types(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户已有的配置类型。"""
    types = await config_service.list_config_types(db, user.user_id)
    return {"types": types}


# ═══════════════════════════════════════════════════════════════
# v1.0 APP 端：竞品需求提交（文档 06）
# ═══════════════════════════════════════════════════════════════

from cogni_agent.gateway_service import competitor_service
from cogni_agent.gateway_service.schemas_app import VALID_STATUSES, CompetitorRequestCreate


@router.post("/competitor-requests")
async def app_create_competitor_request(
    req: CompetitorRequestCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交竞品需求。请求体仅含 schema_version + extra，具体字段由 extra 承载。"""
    from cogni_agent.common.errors import ValidationError

    if not isinstance(req.extra, dict):
        raise ValidationError("extra 必须是合法的 JSON 对象")

    record = await competitor_service.create_request(
        db,
        user_id=user.user_id,
        scenic_id=user.scenic_id,
        schema_version=req.schema_version,
        extra=req.extra,
    )
    return {
        "id": record.id,
        "schema_version": record.schema_version,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
    }


@router.get("/competitor-requests")
async def app_list_competitor_requests(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询自己的竞品需求提交记录（分页 + 可选 status 筛选）。"""
    from cogni_agent.common.errors import ValidationError

    if page < 1:
        raise ValidationError("page 必须 >= 1")
    if page_size < 1 or page_size > 100:
        raise ValidationError("page_size 必须在 1-100 之间")
    if status and status not in VALID_STATUSES:
        raise ValidationError(f"无效的状态: {status}")

    items, total = await competitor_service.list_requests(
        db, user.user_id, page=page, page_size=page_size, status=status,
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/competitor-requests/{request_id}")
async def app_get_competitor_request(
    request_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查看单条竞品需求提交详情。只能查看自己的。"""
    detail = await competitor_service.get_request_detail(db, request_id, user.user_id)
    return detail


# ═══════════════════════════════════════════════════════════════
# v1.1 APP API: Agent Home / Chat / SSE / 会话 / 消息（阶段二 2A.1-2A.5）
# ═══════════════════════════════════════════════════════════════

# 引导卡片壳的静态兜底：5 张主题卡片（概览/客群/票务/舆情/OTA直销）。
# 只是「零配置也能看」的兜底，运营在问题库( AppQuestionBank )配了行就以库为准。
from cogni_agent.gateway_service.guide_data import GUIDE_CARDS as _GUIDE_CARDS_SOURCE

_DEFAULT_GUIDE_CARDS = [
    GuideCard(
        id=c["id"],
        title=c["title"],
        subtitle=c["subtitle"],
        question=c["question"],
        icon=c["icon"],
        theme_code=c["theme_code"],
    )
    for c in _GUIDE_CARDS_SOURCE
]


# ── 2A.1: GET /api/app/agent/home ──

async def _distributed_skill_targets(db, user) -> list["AppTargetItem"]:
    """当前用户可直接调用的技能(0074)。

    普通账号 = 管理员在「分发中心-按 Skill」指派给他的那些(指派即授权,与执行侧同口径);
    超管豁免分发校验,给可见范围内已发布的技能(限 50 条,首页不是资源管理页)。
    """
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.skill.models import Skill

    out: list[AppTargetItem] = []
    if ver.is_distribution_exempt(user.role):
        rows = (await db.execute(
            sa.select(Skill).where(
                Skill.deleted_at.is_(None),
                sa.select(ver.ResourceVersion.id).where(
                    ver.ResourceVersion.resource_type == "skill",
                    ver.ResourceVersion.resource_id == Skill.id,
                    ver.ResourceVersion.status == ver.STATUS_PUBLISHED,
                ).exists(),
            ).order_by(Skill.created_at.desc()).limit(50)
        )).scalars().all()
        snaps = await ver.latest_published_snapshots(db, "skill", [r.id for r in rows])
        for sk in rows:
            snap = snaps.get(sk.id) or {}
            out.append(AppTargetItem(
                target_type="skill", target_id=sk.id, name=snap.get("name") or sk.name,
                description=snap.get("description"), version=None, source="visible"))
        return out

    assigns = (await db.execute(
        sa.select(ver.ResourceAssignment).where(
            ver.ResourceAssignment.resource_type == "skill",
            ver.ResourceAssignment.scope_type == ver.SCOPE_USER,
            ver.ResourceAssignment.scope_id == user.user_id,
        )
    )).scalars().all()
    if not assigns:
        return out
    skills = {s.id: s for s in (await db.execute(
        sa.select(Skill).where(Skill.id.in_([a.agent_id for a in assigns]),
                               Skill.deleted_at.is_(None))
    )).scalars().all()}
    for a in assigns:
        sk = skills.get(a.agent_id)
        if sk is None:
            continue
        v = await db.get(ver.ResourceVersion, a.version_id)
        if v is None or v.status != ver.STATUS_PUBLISHED:
            # 指派版本被下线 = 该用户当前无可用版本,不列出来(点了也会 fail-closed)
            continue
        snap = v.snapshot or {}
        out.append(AppTargetItem(
            target_type="skill", target_id=sk.id, name=snap.get("name") or sk.name,
            description=snap.get("description"), version=v.version, source="assigned"))
    return out


@router.get("/agent/home", response_model=AgentHomeResponse)
async def app_agent_home(
    agent_id: str | None = Query(None),
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """聚合首页：Agent 信息 + 引导卡片 + 关注问题 + 选择器数据。

    Agent 归属规则（与工具中心四级可见性一致）：
    - 指定 agent_id：仅返回对当前用户可见且已发布(存在 published 版本 + active)的 Agent，
      不可见则 agent 信息为 null。
    - 未指定：优先返回当前景区(scenic_id 匹配)的已发布 Agent；当前用户为集团级角色
      (scenic_id 为 None) 或景区内无已发布 Agent 时，回退到当前用户可见范围内的最新发布。
    """
    from sqlalchemy import select

    from cogni_agent.tool_center_service.versioning import (
        SCOPE_USER, STATUS_PUBLISHED, ResourceAssignment, ResourceVersion,
    )
    # 已发布判定统一走版本表:存在 published 快照 = 已发布。主表 status 列已废弃(0043 删列,
    # 15 号文档后发布只写版本表,主表 status 对新数据恒 DRAFT、无写点),不再参与任何判定。
    pub_versions = sa.select(ResourceVersion.id).where(
        ResourceVersion.resource_type == "agent",
        ResourceVersion.resource_id == Agent.id,
        ResourceVersion.status == STATUS_PUBLISHED,
    )

    async def _visible_published(agent_id: str | None = None) -> Agent | None:
        """可见性过滤 + 已发布(published 版本存在) + (可选)按 id。复用管理端四级可见性算法。"""
        from cogni_agent.tool_center_service.tool.registry import GROUP_ROLES

        vis_conds = [Agent.owner_user_id == user.user_id, Agent.visibility == "public"]  # private：创建者 / public：全局公开
        if user.scenic_id:
            vis_conds.append(sa.and_(Agent.visibility == "scenic", Agent.scenic_id == user.scenic_id))  # scenic：同景区
        if user.role in GROUP_ROLES:
            vis_conds.append(Agent.visibility == "group")  # group：集团角色
        # 指派维度(独立 OR 分支):命中「指派给当前用户」且指派版本为 published 的
        # agent 即视为可用——与执行侧档位1 授权口径一致(15 号 D9-A:user 指派即授权)。
        assignment_exists = sa.select(ResourceAssignment.id).where(
            ResourceAssignment.resource_type == "agent",
            ResourceAssignment.agent_id == Agent.id,
            ResourceAssignment.scope_type == SCOPE_USER,
            ResourceAssignment.scope_id == user.user_id,
            ResourceAssignment.version_id.in_(pub_versions),
        ).exists()
        conds = [
            Agent.active == True,
            sa.or_(
                # ① 常规可见:存在 published 版本(发布唯一权威在版本表)+ 归属/可见性命中
                sa.and_(pub_versions.exists(), sa.or_(*vis_conds)),
                # ② 指派可见:published 指派版本命中(不依赖可见性;启停由上方 active 过滤)
                assignment_exists,
            ),
        ]
        if agent_id:
            conds.append(Agent.id == agent_id)
        stmt = select(Agent).where(*conds)
        if not agent_id:
            stmt = stmt.order_by(Agent.created_at.desc()).limit(1)
        return (await db.execute(stmt)).scalars().first()

    async def _home_hit_version(agent_: Agent) -> ResourceVersion | None:
        """该 agent 对当前用户的「命中版本」:user 指派命中优先(与执行侧档位 1 授权口径
        一致),否则可见范围内最新 published。首页门面(description/opening_line)以该
        版本快照为准 —— v1.1.6 起它们是版本化内容,主表仅作存量快照缺字段兜底。"""
        pub_ids = select(ResourceVersion.id).where(
            ResourceVersion.resource_type == "agent",
            ResourceVersion.resource_id == agent_.id,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
        asg = (await db.execute(
            select(ResourceAssignment).where(
                ResourceAssignment.resource_type == "agent",
                ResourceAssignment.agent_id == agent_.id,
                ResourceAssignment.scope_type == SCOPE_USER,
                ResourceAssignment.scope_id == user.user_id,
                ResourceAssignment.version_id.in_(pub_ids),
            )
        )).scalars().first()
        if asg is not None:
            return await db.get(ResourceVersion, asg.version_id)
        return (await db.execute(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == "agent",
                ResourceVersion.resource_id == agent_.id,
                ResourceVersion.status == STATUS_PUBLISHED,
            ).order_by(ResourceVersion.version.desc()).limit(1)
        )).scalars().first()

    if agent_id:
        agent = await _visible_published(agent_id)
    else:
        # 优先当前景区的已发布 Agent（景区角色）
        if user.scenic_id:
            agent = (await db.execute(
                select(Agent).where(
                    pub_versions.exists(),
                    Agent.active == True,
                    Agent.scenic_id == user.scenic_id,
                ).order_by(Agent.created_at.desc()).limit(1)
            )).scalars().first()
        else:
            agent = None
        # 回退：当前景区无已发布 Agent / 集团级角色 → 取可见范围内最新发布
        if agent is None:
            agent = await _visible_published()

    # 关注的问题列表（最近 10 条，不含已删除）
    followed = (await db.execute(
        select(AppFollowedQuestion).where(
            AppFollowedQuestion.user_id == user.user_id,
            AppFollowedQuestion.deleted_at.is_(None),
        ).order_by(AppFollowedQuestion.created_at.desc()).limit(10)
    )).scalars().all()

    # 三级选择器数据（20 号 P1:问题库 dimension_name 分组,静态兜底）
    from cogni_agent.gateway_service.guide_data import SELECTOR_DATA, THEME_NAMES
    from cogni_agent.gateway_service.models import AppQuestionBank, AppPrewarmCache

    q_rows = (await db.execute(
        sa.select(AppQuestionBank).where(
            AppQuestionBank.active.is_(True),
            AppQuestionBank.scenic_id.is_(sa.null())
            | (AppQuestionBank.scenic_id == (user.scenic_id or "")),
        ).order_by(AppQuestionBank.sort_order, AppQuestionBank.created_at)
    )).scalars().all()

    if q_rows:
        # 选择器(方案 A:主题→维度→指标):theme+dimension 分组,第三级叶子=指标名+问题行(锁 question_id)
        _sel: dict[str, dict[str, list]] = {}
        for q in q_rows:
            if q.dimension_name:
                _sel.setdefault(q.theme_code, {}).setdefault(
                    q.dimension_name, []).append(q)
        selector_data = [
            SelectorThemeItem(
                theme=THEME_NAMES.get(tc, tc),
                metrics=[
                    SelectorMetricItem(
                        name=dim,
                        questions=[q.question_text for q in qs],
                        items=[SelectorQuestionRef(id=q.id, text=q.question_text,
                                                   name=q.metric_name) for q in qs],
                    )
                    for dim, qs in dims.items()
                ],
            )
            for tc, dims in _sel.items()
        ]

        # 卡片:壳(标题/图标)=guide_data 5 主题展示配置;问题与摘要=问题库 P0 代表问题+缓存
        # 22 号复盘:缓存按业务日留存,消费只认当日行——今日占位/未出=空态,不回退昨日
        _scenic = user.scenic_id or ""
        import datetime as _dt

        _today = today_cst()  # 业务日=北京日期(容器时区 UTC,date.today() 凌晨会偏一天)
        # 25 号:缓存行带账号维度(OTA 主题/概览按「景区×账号」拆行)。
        # 行选择:当前用户账号匹配行优先,无则回退账号无关行(NULL——未绑用户的
        # 概览基础版 / 非账号主题行)。
        from cogni_agent.gateway_service.ota_accounts import user_ota_accounts

        _home_accts = await user_ota_accounts(db, user.user_id)
        _home_acct = _home_accts[0] if _home_accts else None
        # 27 号:卡片消费版本严格匹配(全部严格)——每个问题的期望版本 =
        # 用户对该问题锚定 agent(问题行 agent_id,缺省 home agent)的指派 pin;
        # 无指派/未绑 → 该问题无当日卡片数据(is_today=False 空态),不回退任意版本行。
        from cogni_agent.tool_center_service.versioning import assigned_skill_version

        _q_by_id = {q.id: q for q in q_rows}
        _pin_memo: dict[str, int | None] = {}
        _home_agent_id = agent.id if agent else None

        async def _expected_pin(qid: str) -> int | None:
            if qid not in _pin_memo:
                q = _q_by_id.get(qid)
                _pin_memo[qid] = (await assigned_skill_version(
                    db, q.agent_id or _home_agent_id, user.user_id, q.skill_id)
                    if (q and q.skill_id) else None)
            return _pin_memo[qid]

        _by_q: dict[str, list[AppPrewarmCache]] = {}
        for c in (await db.execute(
            sa.select(AppPrewarmCache).where(
                AppPrewarmCache.question_id.in_([q.id for q in q_rows]),
                AppPrewarmCache.scenic_id == _scenic,
                AppPrewarmCache.biz_date == _today,
                AppPrewarmCache.answer.isnot(None),  # 排除当日占位行(报告未出)
            )
        )).scalars().all():
            _by_q.setdefault(c.question_id, []).append(c)
        cache_map: dict[str, AppPrewarmCache] = {}
        for qid, _rows in _by_q.items():
            _pin = await _expected_pin(qid)
            _rows = [r for r in _rows if r.skill_version == _pin]  # 版本严格
            _pick = next((r for r in _rows if r.ota_account == _home_acct), None) \
                or next((r for r in _rows if r.ota_account is None), None)
            if _pick is not None:
                cache_map[qid] = _pick
        # 卡片代表问题:优先该主题「【主题解读】」报告问题(与预热缓存行一一对应,
        # 点击直出主题报告缓存);无则回退 sort_order 首条业务问句
        theme_first: dict[str, AppQuestionBank] = {}
        for q in q_rows:  # sort_order 序
            tc = q.theme_code
            if tc not in theme_first:
                theme_first[tc] = q
            if q.question_text.startswith("【主题解读】") and not (
                    theme_first[tc].question_text.startswith("【主题解读】")):
                theme_first[tc] = q
        # OTA 主题卡片仅当前用户自己绑定了 OTA 账号时返回(25 号用户级口径:
        # 景区下别人绑的不算,未配置则整卡不展示——零数据链路不暴露入口)
        show_ota = _home_acct is not None
        guide_cards = []
        for c in _DEFAULT_GUIDE_CARDS:  # 壳沿用静态 5 张(展示配置,非业务数据)
            if c.theme_code == "ota_direct_sales" and not show_ota:
                continue
            q = theme_first.get(c.theme_code)
            if q is None:
                guide_cards.append(c)  # 该主题无问题(如 overview 不在问题库) → 保留静态卡
                continue
            cache = cache_map.get(q.id)  # 当日行(answer 非空)命中即"今日已出"
            guide_cards.append(GuideCard(
                id=c.id, title=c.title, icon=c.icon, theme_code=c.theme_code,
                question=q.question_text, question_id=q.id,
                # 主题卡副标题也是纯文本槽位:预热摘要带的 Markdown 标记
                # (**加粗**、反引号、行首 -)原样显示出来就是一串乱码。
                subtitle=(strip_markdown(cache.summary) if cache and cache.summary
                          else c.subtitle),
                generated_at=(cache.generated_at.isoformat() if cache else None),
                is_today=(cache is not None),
            ))
    else:
        selector_data = [
            SelectorThemeItem(
                theme=t["theme"],
                metrics=[
                    SelectorMetricItem(name=m["name"], questions=list(m["questions"]))
                    for m in t["metrics"]
                ],
            )
            for t in SELECTOR_DATA
        ]
        # 静态兜底分支同口径:当前用户未绑 OTA 账号不返回 OTA 卡片
        from cogni_agent.gateway_service.ota_accounts import user_ota_accounts as _uoa

        _fb_accts = await _uoa(db, user.user_id)
        guide_cards = [c for c in _DEFAULT_GUIDE_CARDS
                       if c.theme_code != "ota_direct_sales" or _fb_accts]

    # 概览页各主题核心指标解读概要（异常分色）;25号:按当前用户账号匹配概览行
    from cogni_agent.gateway_service.insight_service import get_overview_insights
    from cogni_agent.gateway_service.ota_accounts import user_ota_accounts

    _ins_accts = await user_ota_accounts(db, user.user_id)
    overview_insights = [
        OverviewInsightItem(**item)
        for item in await get_overview_insights(
            db, user.scenic_id, _ins_accts[0] if _ins_accts else None,
            user_id=user.user_id,
            agent_hint=agent.id if agent else None)  # 27 号:版本严格匹配
    ]

    # v1.1.6/v1.1.9:description/opening_line 是版本化内容,从「命中版本」快照取 ——
    # 与执行侧一致,发布新版本后首页门面即时随版本切换。description 主表列已删
    # (0045):老版本快照缺 description / 未命中版本时为 None,不再回退主表。
    # name 是资源身份,始终主表。
    home_snapshot: dict = {}
    if agent is not None:
        hit = await _home_hit_version(agent)
        if hit is not None:
            home_snapshot = hit.snapshot or {}

    # ── 0074:该用户可调用的目标清单(Agent + Skill) ──
    # 为什么首页要给这个:分发是按人的,「这个账号能点哪些东西」只有服务端知道;
    # 前端自己猜不出来,猜错就是点了报 not_distributed。
    targets: list[AppTargetItem] = []
    if agent is not None:
        _agent_ver = await _home_hit_version(agent)
        targets.append(AppTargetItem(
            target_type="agent", target_id=agent.id, name=agent.name,
            description=home_snapshot.get("description"),
            opening_line=home_snapshot.get("opening_line") or agent.opening_line,
            version=_agent_ver.version if _agent_ver else None,
            source="assigned",
        ))
    targets.extend(await _distributed_skill_targets(db, user))

    # 关注问题 / 主题卡片都补上 issue_id:前端带着它发起,后端才能按 business_class 分流。
    # 只发文本或只发 question_id 的话,配在 Quick BI 上的问题会被本地 Agent 接走。
    from cogni_agent.gateway_service import scenic_binding as _binding

    followed_items = []
    for q in followed:
        hit = await _binding.match_issue_by_text(db, user.scenic_id or None, q.question_text)
        followed_items.append(AppFollowedQuestionItem(
            id=q.id,
            question_text=q.question_text,
            source_message_id=q.source_message_id,
            created_at=q.created_at.isoformat() if q.created_at else "",
            issue_id=hit.issue_id if hit else "",
            business_class=hit.business_class if hit else "",
        ))

    return AgentHomeResponse(
        target_type=("agent" if agent is not None
                     else (targets[0].target_type if targets else None)),
        target_id=(agent.id if agent is not None
                   else (targets[0].target_id if targets else None)),
        targets=targets,
        agent_id=agent.id if agent else None,
        agent_name=agent.name if agent else None,
        agent_description=home_snapshot.get("description") if agent else None,
        agent_opening_line=(home_snapshot.get("opening_line") or agent.opening_line) if agent else None,
        guide_cards=guide_cards,
        followed_questions=followed_items,
        selector_data=selector_data,
        overview_insights=overview_insights,
    )


# ── 景区智能体问题(前后端改造 §3) ──────────────────────────────────────

class BusinessIssueListRequest(BaseModel):
    """按景区取问题列表。scenicId 不传就用当前账号所属景区
    (集团级账号不传则拿到全部通用问题)。"""
    scenicId: str | None = None


@router.post("/businessIssueList")
async def app_business_issue_list(
    req: BusinessIssueListRequest,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """问题列表:主题 → 维度 → 指标 → 问题 的嵌套结构。

    前台选完「主题-维度-指标」就能拿到该指标下的问题,点问题直接发起 ——
    每条问题带 `businessClass`,前端据此选链路:
      - `quick_bi`     → `POST /api/app/quickbi/ask` { issue_id }
      - `local_system` → `POST /api/app/chat`        { issue_id, message }

    两类问题在同一棵树里,前端不用关心它们在后台是两套配置。
    """
    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.gateway_service import scenic_binding as binding

    # scenicId 只能收窄:景区账号传别的景区直接拒(集团账号不传 = 看全部)。
    # 原先是 `req.scenicId or user.scenic_id`,调用方传什么就是什么
    scenic_id = binding.effective_scenic(user, req.scenicId)
    data = await issues.business_issue_list(db, scenic_id)
    return {"scenicId": scenic_id, "topics": data}


# ── Quick BI:该账号被分配到什么(0075) ──────────────────────────────────

@router.get("/quickbi/targets")
async def app_quickbi_targets(
    connection_id: str | None = Query(None),
    ask_type: str | None = Query(None, description="按请求类别筛固定问题:qa/insight/dashboard/report"),
    domain: str | None = Query(None, description="按数据域筛固定问题:票务/财务/舆情/…"),
    include_assets: bool = Query(True),
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """当前账号能用的 Quick BI:连接 + 能问什么(固定问题)+ 能问哪份数据。

    前端据此渲染 Quick BI 入口 —— 「这个账号能不能用、能问什么」只有服务端知道,
    前端猜不出来,猜错就是点了报无权限。

    过滤口径与管理端完全一致:
      - 连接:先按景区/可见性,再过「分配」闸门(该连接一旦被分配过,就只对被分配的人开放);
      - 固定问题 / 数据资产:按景区 + 可见性(资产还跟着连接走)。
    返回空 connections = 这个账号没有可用的 Quick BI,前端应把入口藏起来。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    actor = qsvc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None,
                       role=user.role, source="console")
    views = await qsvc.list_connections(actor)
    connections = [{"id": v.id, "name": v.name, "is_default": v.is_default, "virtual": v.virtual}
                   for v in views]
    if not connections:
        return {"connections": [], "questions": [], "assets": [], "enabled": False}

    picked = connection_id or next((c["id"] for c in connections if c["is_default"]), connections[0]["id"])
    catalog = await qsvc.question_catalog(actor, connection_id=picked, ask_type=ask_type,
                                          domain=domain, include_assets=include_assets)
    return {
        "enabled": True,
        "connections": connections,
        "connection_id": picked,
        "scenic_id": catalog.get("scenic_id"),
        "ask_types": catalog.get("ask_types", []),
        "domains": catalog.get("domains", []),
        "questions": catalog.get("questions", []),
        "datasets": catalog.get("datasets", []),
        "dashboards": catalog.get("dashboards", []),
    }


class AppQuickbiAskRequest(BaseModel):
    """APP 端 Quick BI 提问:固定问题 id 与自由问题二选一(都给时以固定问题为准,
    自由文本作为补充追问)。

    `issue_id`(前后端改造 §3):问题列表里 businessClass=quick_bi 那条的 id。
    传了它就不用再填连接/能力/目标数据 —— 那些都在问题配置里,服务端自己带出来,
    省得前端把一份配置抄两遍(抄漏一个字段就是一次「问了但答非所问」)。"""
    issue_id: str | None = None
    fixed_question_id: str | None = None
    question: str = ""
    connection_id: str | None = None
    capability: str | None = None       # 空/auto = 按问题意图路由
    target: dict | None = None          # {asset_id} / {page_id} / {cube_id} / {file_id}
    session_id: str | None = None       # 传了就归到该 Quick BI 会话(多轮追问)
    scenic_id: str | None = None        # 景区角色不传即可(按账号所属景区)


@router.post("/quickbi/ask")
async def app_quickbi_ask(
    req: AppQuickbiAskRequest,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """APP 端发起 Quick BI 问数(SSE)。

    与 `POST /chat` 是两条链路,别混:
      - `/chat` 走任务引擎(Agent/Skill),返回 task_id,再用 GET 订阅事件流;
      - 这里直连 Quick BI,**响应本身就是事件流**(POST + text/event-stream),
        因为 Quick BI 的一问是它自己端到端完成的,没有任务队列那一层。

    事件与管理端完全一致(run / status / trace / reasoning / text / chart / report /
    usage / done),每个事件带 `channel`:thinking = 思考过程,data = 正式结果。
    权限:连接按「分配」闸门过滤(未分配的连接拿不到,报 404)。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.http_helpers import primed, sse

    connection_id, capability = req.connection_id, req.capability
    target = dict(req.target or {})
    question = req.question or ""
    scenic_id = req.scenic_id
    if req.issue_id:
        from cogni_agent.gateway_service import scenic_binding as binding

        row = await issues.get_issue(db, req.issue_id)
        if row.business_class != "quick_bi":
            # 走错门了:local_system 的问题要发 /app/chat。直接说清楚,
            # 别让它以 Quick BI 的方式跑出一个看起来像模像样的错答案。
            raise ValidationError(
                f"问题「{row.title}」是 {row.business_class} 类型,请改用 POST /api/app/chat 发起")
        capability = capability or row.capability
        target = target or dict(row.target or {})
        question = question or binding.expand_params(row, question)
        # 可见性与取数景区:与 /app/chat 同一口径(见 binding.assert_issue_askable / ask_scenic_for)
        binding.assert_issue_askable(user, row)
        scenic_id = binding.ask_scenic_for(user, row, scenic_id)
        # 连接按景区解析:问题上写死的那条若已改绑别的景区/停用,按当前景区重挑一条,
        # 而不是直接报「连接不存在」—— 改归属之后发起失败,根因就在这里
        connection_id = connection_id or await binding.connection_usable(db, row.connection_id, scenic_id)
        if not connection_id:
            raise ValidationError(
                f"景区 {scenic_id or '(集团级)'} 还没有可用的 Quick BI 连接,"
                "请先在「工具中心 · Quick BI · 连接配置」把连接绑到这个景区")
    # 0079 起「固定问题」和「问题」是同一张表的同一行,所以 issue_id 也按固定问题发起:
    # 这样才会记执行留痕(最后一次跑成什么样),管理端与 APP 端看到的状态是同一个。
    fixed_id = req.fixed_question_id or (req.issue_id if req.issue_id else None)
    agen = qsvc.run_stream(
        qsvc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None,
                   role=user.role, source="console",
                   # issue_id 发起的已按 APP 口径判过可见性;直接给 fixed_question_id 的仍按管理端口径判
                   trusted_refs=frozenset({req.issue_id}) if req.issue_id else frozenset()),
        connection_id=connection_id,
        capability=capability,
        question=question,
        target=target,
        mode="fixed" if fixed_id else "direct",
        scenic_id=scenic_id,
        fixed_question_id=fixed_id,
        session_id=req.session_id,
    )
    return sse(await primed(agen))


@router.get("/questions")
async def app_all_questions(
    theme_code: str | None = Query(None, description="APP 问题库按主题筛"),
    domain: str | None = Query(None, description="Quick BI 固定问题按数据域筛"),
    ask_type: str | None = Query(None, description="Quick BI 固定问题按请求类别筛"),
    include_quickbi: bool = Query(True),
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """一次拿全「这个账号能问什么」:APP 问题库 + Quick BI 固定问题。

    两者发起方式不同(前者走对话引擎、后者直连 Quick BI)—— 所以每条都带
    `source` 和 `ask`,前端照着走就行,不用自己判断:

      - `source=bank`    → `POST /app/chat`      { question_id, message, agent_id|skill_id }
      - `source=quickbi` → `POST /app/quickbi/ask` { fixed_question_id }

    以前 AI 问数页只读 `/app/guide-questions`(只有问题库那一半),
    所以「后台明明配了 Quick BI 固定问题,前端一条都看不到」。
    """
    from cogni_agent.gateway_service.guide_data import THEME_NAMES, list_guide_questions
    from cogni_agent.gateway_service.models import AppQuestionBank

    stmt = (
        sa.select(AppQuestionBank)
        .where(
            AppQuestionBank.active.is_(True),
            AppQuestionBank.scenic_id.is_(sa.null())
            | (AppQuestionBank.scenic_id == (user.scenic_id or "")),
            # 「【主题解读】概览」这类不是问题,是主题卡片本身的解读报告 ——
            # 它由卡片上的「AI总结 / AI解析」承载。混进常问问题的话,用户会看到
            # 一条「【主题解读】概览」当成可以问的问题,点下去跑的是一份几分钟的报告。
            ~AppQuestionBank.question_text.like(f"{THEME_REPORT_PREFIX}%"),
        )
        .order_by(AppQuestionBank.sort_order, AppQuestionBank.created_at)
    )
    if theme_code:
        stmt = stmt.where(AppQuestionBank.theme_code == theme_code)
    rows = (await db.execute(stmt)).scalars().all()
    items: list[dict] = []
    for q in rows:
        items.append({
            "source": "bank", "ask": "chat", "id": q.id,
            "question": q.question_text, "title": q.metric_name or q.question_text,
            "theme_code": q.theme_code, "theme": THEME_NAMES.get(q.theme_code, q.theme_code),
            "dimension": q.dimension_name, "metric": q.metric_name,
        })
    if not rows:
        # 问题库还没初始化:回退静态 18 条(与 /app/guide-questions 同口径)
        static_items, _ = list_guide_questions(theme_code=theme_code, page=1, page_size=100)
        items.extend({
            "source": "bank", "ask": "chat", "id": q["id"], "question": q["question"],
            "title": q["question"], "theme_code": q["theme_code"],
            "theme": THEME_NAMES.get(q["theme_code"], q["theme_code"]),
            "dimension": None, "metric": None,
        } for q in static_items)
    bank_total = len(items)

    qbi_items: list[dict] = []
    if include_quickbi:
        from cogni_agent.tool_center_service.quickbi import service as qsvc

        actor = qsvc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None,
                           role=user.role, source="console")
        try:
            # 没有可用连接(没配 / 没分配到)时不报错,只是这一半为空
            if await qsvc.list_connections(actor):
                fixed = await qsvc.list_fixed(actor, active=True, domain=domain, ask_type=ask_type,
                                              scenic_id=user.scenic_id or None)
                for f in fixed:
                    qbi_items.append({
                        "source": "quickbi", "ask": "quickbi", "id": f["id"],
                        "question": f["question"], "title": f["title"],
                        "domain": f.get("domain"), "ask_type": f.get("ask_type"),
                        "ask_type_label": f.get("ask_type_label"),
                        "capability": f.get("capability"),
                        "capability_label": f.get("capability_label"),
                        "connection_id": f.get("connection_id"),
                        # 三级标签:前端按 主题/维度/指标 分组展示,没有它就只能平铺一长串
                        "topic": f.get("topic"), "topic_id": f.get("topic_id"),
                        "dimension": f.get("dimension"), "dimension_id": f.get("dimension_id"),
                        "metric": f.get("metric"), "metric_id": f.get("metric_id"),
                        "target_data_name": f.get("target_data_name"),
                    })
        except Exception as exc:  # noqa: BLE001 - Quick BI 不可用不该拖垮问题列表
            log.warning(f"Quick BI 固定问题拉取失败(不影响问题库那一半):{exc}")

    return {"items": items + qbi_items, "total": len(items) + len(qbi_items),
            "bank_total": bank_total, "quickbi_total": len(qbi_items)}


# ── 2A.2: POST /api/app/chat ──

@router.post("/chat", response_model=AppChatResponse)
async def app_chat(
    req: AppChatRequest,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """发起对话。创建/复用会话 → 并发检查 → 创建任务 → 返回 task_id 供 SSE 订阅。

    19 号 S4/S7 + 23 号:固定问题分层路由——
      带 question_id(或归一化文本命中问题库) 且问题行绑定了 skill_id
        → 任务转 executor_type=skill + executor_id=skill_id 直跳该技能(L1,跳过 Triager)
      问题行未绑技能 / 未命中 → 现状(agent_id 走 agent 动态池)。
    会话归属不变:仍是 agent 会话(人格/开场白/列表展示),仅任务执行目标切换。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service import scenic_binding as binding

    # 前后端改造 §3:带 issue_id 就把问题配置带出来 —— 目标(agent/skill)、问题原文、
    # 给模型的口径、参数模板都在那一行里。前端只传一个 id,不必把配置抄一遍。
    issue_row = None
    if req.issue_id or req.question_id:
        from cogni_agent.gateway_service import issue_service as issues

        if req.issue_id:
            issue_row = await issues.get_issue(db, req.issue_id)
        else:
            # 老入口(「试试问我」/关注问题/兜底选择器)只带 question_id —— 尽力按它解一次
            # 问题行(新旧 ID 都认)。解不到就照常走原来的问题库命中逻辑,不报错;
            # 解到了才有分流可言:否则一条配在 Quick BI 上的问题会被本地 Agent 接走,
            # 答出来一个看着像模像样、其实没查过数的结果 —— 这种错最难发现。
            try:
                issue_row = await issues.get_issue(db, req.question_id)
            except NotFound:
                issue_row = None
        if issue_row is not None:
            if not (req.message or "").strip():
                req.message = issue_row.issue
            if not req.skill_id and issue_row.skill_id:
                req.skill_id = issue_row.skill_id
            if not req.agent_id and issue_row.agent_id:
                req.agent_id = issue_row.agent_id

    # 文本兜底:只发了问题原文、既没有 issue_id 也没有 question_id 的入口
    # (关注问题、历史消息里点回去、老前端的各种按钮)。
    # 不兜的话,一条配在 Quick BI 上的问题会被本地 Agent 接走 —— 答出来一个
    # 看着像模像样、其实没查过数的结果,这种错最难发现;更常见的是前端干脆
    # 拦在「暂无可用智能体」上,而用户点的明明是一条配好的问题。
    if issue_row is None and (req.message or "").strip():
        issue_row = await binding.match_issue_by_text(db, user.scenic_id or None, req.message)
        if issue_row is not None:
            if not req.skill_id and issue_row.skill_id:
                req.skill_id = issue_row.skill_id
            if not req.agent_id and issue_row.agent_id:
                req.agent_id = issue_row.agent_id

    # Quick BI 类问题**根本不需要 Agent**:它直连 Quick BI 跑,执行目标就是那条连接。
    # 这个判断必须排在下面「回落默认 Agent」之前 —— 放在后面的话,一个还没建 Agent 的
    # 环境里点一条 Quick BI 问题,会先撞上「当前没有可用的 Agent」而被挡下,
    # 而它本来一个 Agent 都不需要。(这正是现场报的那个错。)
    is_quickbi_issue = issue_row is not None and issue_row.business_class == "quick_bi"

    # 0074:目标二选一 —— 传了 skill_id 就是技能直调(不经任何 agent),
    # 版本与授权由「分发中心-按 Skill」的用户指派决定(未指派 fail-closed,超管豁免)。
    target_type = req.target_type
    target_id = req.target_id
    if is_quickbi_issue and not target_id:
        # 会话也按 quickbi 记账:历史回放时才知道这一轮是谁跑的
        target_type = "quickbi"
        target_id = issue_row.connection_id or issue_row.issue_id
    elif not target_id:
        # 问题没指定执行目标:回落到该账号的默认 Agent(与不带 issue_id 的自由提问同口径)
        from sqlalchemy import select as _select

        # 回落到「这个账号真的能跑的」Agent,而不是「表里最新的那个」。
        # 原先只判 active:软删掉的、没发布过版本的、别的景区的,统统可能被选中 ——
        # 然后任务死在 resolver 的 agent_not_distributed 上,把管理员指向一个
        # 完全不相干的方向(去分发中心找一个他根本没打算用的 Agent)。
        default_agent = await _default_agent_for(db, user)
        if not default_agent:
            # 说清楚两条出路,否则用户只看到「没有可用的 Agent」,不知道该去建 Agent
            # 还是该去给这条问题配目标
            raise ValidationError(
                "这条问题没有配置执行目标(Agent / 技能),而系统里也没有任何已启用的 Agent。"
                "请先在「智能体」里创建并启用一个 Agent,或给这条问题指定 Agent / 技能;"
                "若它本应走 Quick BI,请在问题配置里把业务渠道类别选成 Quick BI 并绑定连接。")
        target_type, target_id = "agent", default_agent
        req.agent_id = default_agent

    conv, _diagnostics = await conv_svc.get_or_create(
        conversation_id=req.conversation_id,
        scenic_id=user.scenic_id,  # 新建会话时写入用户当前绑定景区(deps 每次请求从库里重读)
        user_id=user.user_id,
        executor_type=target_type,
        executor_id=target_id,
        first_message=req.message,
    )

    await conv_svc.assert_no_active_turn(conv.id)

    # 本轮景区:景区账号 = 当前归属;集团账号 = 消息里点名 > 本会话此前指定 > 留空待澄清。
    # 结果写回会话,下游(pre_params.resolve_scenic_id → create_task)照旧读 conv.scenic_id,
    # 不必各处再判一遍 —— 两处各判一遍迟早对不上。
    from cogni_agent.gateway_service import scenic_binding as _binding

    _before = conv.scenic_id
    _turn_scenic, _scenic_src = await _binding.resolve_turn_scenic(
        db, user=user, conv=conv, message=req.message or "")
    if _turn_scenic and _turn_scenic != _before:
        await conv_svc.set_scenic(conv.id, _turn_scenic)
        log.info(f"conv {conv.id} 本轮景区 {_before} → {_turn_scenic}({_scenic_src})")

    # ── Quick BI 类问题:后端自己分流,前端不必换接口 ────────────────────
    # 用户只是点了一个问题,他不该关心这条问题在后台是配在 Quick BI 还是本地技能上。
    # 所以这里照常返回 task_id,由桥接把 Quick BI 的事件翻译成 APP 已有的事件
    # (token/stage/structured/done)推到同一个通道 —— 前端零改动,历史回放也照常。
    if is_quickbi_issue:
        from cogni_agent.gateway_service import qbi_bridge
        from cogni_agent.tool_center_service.quickbi import service as qsvc
        from cogni_agent.tool_center_service.quickbi.http_helpers import primed

        _actor = qsvc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None,
                            role=user.role, source="console",
                            # 可见性已按 APP 列表的口径判过(assert_issue_askable),run_stream 里
                            # 再按管理端的口径判一遍会把本景区别人建的私有问题拦掉 —— APP 上明明列着
                            trusted_refs=frozenset({issue_row.issue_id}))
        binding.assert_issue_askable(user, issue_row)
        # ① 这次算哪个景区:景区账号 = 自己的景区;集团账号 = 问题挂的景区(没有则集团级)
        ask_scenic = binding.ask_scenic_for(user, issue_row)
        # ② 这个景区现在能用哪条连接:问题上写死的那条若已改绑别处/停用,按景区重挑 ——
        #    「改了归属就发起失败」的根因就在这里,与其报「连接不存在」不如解析出一条能用的
        conn_id = await binding.connection_usable(db, issue_row.connection_id, ask_scenic)
        if not conn_id:
            raise ValidationError(
                f"景区 {ask_scenic or '(集团级)'} 还没有可用的 Quick BI 连接:"
                "请在「工具中心 · Quick BI · 连接配置」新建连接并把归属景区绑到这个景区,"
                "或建一条集团级连接供所有景区共用")

        qbi_task = Task(
            scenic_id=conv.scenic_id, user_id=user.user_id,
            executor_type="quickbi", executor_id=conn_id,
            conversation_id=conv.id, source=SOURCE_APP, status="RUNNING", priority=5,
            # 租约:巡检会把过期的 RUNNING 判死,桥接里定期续租(报告能跑几十分钟)
            lease_until=datetime.now(timezone.utc) + timedelta(seconds=qbi_bridge.LEASE_SECONDS),
            payload={"message": req.message, "issue_id": issue_row.issue_id,
                     "target_type": "quickbi", "target_id": issue_row.issue_id,
                     "connection_id": conn_id, "scenic_id": ask_scenic,
                     "capability": issue_row.capability, "user_id": user.user_id, "role": user.role},
        )
        db.add(qbi_task)
        await db.commit()

        # ③ 加速(预热)回放由 run_stream 自己判:问题开了加速、存储时间内、
        #    且在最近一次「清除缓存」之后跑成功过,才回放。以前这里另有一套 12 小时回放,
        #    不看加速开关、不看清除缓存 —— 改完口径 APP 上仍是旧答案,就是它
        agen = qsvc.run_stream(
            _actor,
            connection_id=conn_id,
            capability=issue_row.capability,
            question=binding.expand_params(issue_row, req.message),
            target=dict(issue_row.target or {}),
            mode="fixed",
            fixed_question_id=issue_row.issue_id,
            scenic_id=ask_scenic,
            session_id=None,
            task_id=qbi_task.id,
        )
        # primed:鉴权/入参错误在这里就以 HTTP 错误返回,而不是塞进一个 200 的事件流里
        # ——前端拿到 task_id 才开流,错误藏在流里等于白等一场。
        try:
            agen = await primed(agen)
        except Exception as exc:
            # 发起即失败(连接鉴权/入参不对):任务行已经以 RUNNING 落库,不收尾的话它要等
            # 租约过期(数分钟)才被巡检判死,这期间同一会话再问会被「上一轮尚未结束」挡住 ——
            # 用户改完配置想立刻重试,却被一条早就没人在跑的任务锁着。
            async with get_sessionmaker()() as _db:
                await _db.execute(sa.update(Task).where(Task.id == qbi_task.id).values(
                    status="FAILED", lease_until=None, updated_at=sa.func.now(),
                    result={"answer": "", "error": str(getattr(exc, "message", None) or exc)}))
                await _db.commit()
            raise
        qbi_bridge.spawn(qbi_task.id, agen, issue_id=issue_row.issue_id)
        await conv_svc.bump_turn(conv.id)
        return AppChatResponse(task_id=qbi_task.id, conversation_id=conv.id,
                               target_type="quickbi", target_id=issue_row.issue_id)

    # ── 前置参数处理(APP 端) ──
    # 独立成环:身份(token) + 请求结构(date/question_id) + 会话景区 → 标准上下文。
    # 目的:参数在进 LLM 之前就凑齐,避免「意图已定、技能已选,却反过来问用户」的往返。
    # 端专属逻辑见 gateway_service/pre_params/app_pre_params.py。
    pc = await resolve_pre_params(source=SOURCE_APP, user=user, req=req, conv=conv)
    if pc.unresolved:
        # 本轮只统计不干预(入口 fail-fast 策略待定),先让缺参在日志里可见。
        log.info(f"conv {conv.id} 前置参数未解析: {pc.unresolved}")

    # ── 固定问题命中判定(19 号 S4) ──
    executor_type = target_type
    executor_id = target_id
    payload: dict = {"message": pc.message, "agent_id": req.agent_id,
                     "user_id": pc.user_id, "role": pc.role,
                     # 0074:目标类型随 payload 落库,执行链/统计里一眼看出这轮打的是谁
                     "target_type": target_type, "target_id": target_id,
                     # v1.1 智能问数 Phase B：date 上下文透传，executor 注入执行上下文
                     "date": pc.date}
    if req.skill_id:
        payload["skill_id"] = req.skill_id
    if issue_row is not None:
        # 口径与参数模板随本轮下发:口径决定模型怎么答(取哪个窗口、什么算异常),
        # 参数模板由代码展开日期,省掉模型推日期这一步(推错了很难发现)。
        payload["issue_id"] = issue_row.issue_id
        if issue_row.model_instruction:
            payload["question_instruction"] = issue_row.model_instruction
        if issue_row.param_template:
            from cogni_agent.gateway_service import question_bank as _qb

            expanded = _qb.expand_param_template(issue_row.param_template)
            if expanded:
                payload["question_params"] = expanded
    if req.question_id or req.message:
        from cogni_agent.gateway_service import question_bank as qb

        hit = await qb.match_question(
            db, question_id=pc.question_id, scenic_id=pc.scenic_id)
        if hit:
            # 命中信息透传(SSE 执行链/预热统计/运营分析)
            payload["question_id"] = hit.id
            # 「问题描述」:运营在问题维护里填的、**真正给模型的口径与约束**
            # (取哪个时间范围、用哪个工具、什么算异常、答复多长)。
            #
            # 作为**附加约束**下发,不替换用户消息 —— 替换的话,
            # 用户在固定问题基础上追问的那句话就丢了(「今日入园人数」→
            # 追问「那昨天呢」,替换后模型只看到问题描述,看不到「昨天」)。
            if getattr(hit, "model_instruction", None):
                payload["question_instruction"] = hit.model_instruction
            # 这一轮打的是哪个技能。
            #
            # **`and target_type == "agent"` 这个条件曾经把整条链路打死**:问题行上带了
            # skill_id 时,上面 1142 行已经把 req.skill_id 填好,于是 target_type 早就是
            # "skill",这个分支根本进不来 —— 版本 pin 没解析、executor_version_id 没设,
            # 而 payload["question_id"] 在分支外**照样**设了,于是 resolver 那边
            # question_authorized=True → 跳过版本解析 → 拿着一个没有版本的技能,
            # 直接抛「Skill 未指定版本,不可执行」。
            # 现场表现:卡片、关注问题、AI 解析(这三个入口都同时带 issue_id 和 question_id)
            # 一点就失败,而同一个技能在操练台里跑得好好的(操练台显式传了版本)。
            # 连带被跳过的还有:参数模板展开、编排直出、以及**整段预热直出** ——
            # 预热跑了也用不上,每次都现跑。
            _skill_target = (hit.skill_id if qb.workflow_target(hit)
                             else (executor_id if executor_type == "skill" else None))
            if _skill_target:
                # 23 号:问题行直接绑技能(原 composition_id → 组合 → skill_ids_ordered[0]
                # 的恒等转发层已删)。executor_type=skill + executor_id=skill_id 单技能直达,
                # 跳过 Triager 意图识别(省一次 LLM 调用,降首字延迟);单技能无需 overrides。
                executor_type = "skill"
                executor_id = _skill_target
                # 27 号:版本随指派(fail-closed)——技能版本 = 用户对该问题锚定 agent
                # (问题行 agent_id,缺省会话 agent)的指派版本快照里该技能的 pin;
                # 无指派/未绑该技能/pin 版本已下线 → 降级 agent 动态池(其自身同样
                # fail-closed),**绝不回退「最新 published」**——版本体系的意义就是可指定。
                # APP 端交互必带 agent_id(分配时给的,指派即带全套版本 pin)。
                from cogni_agent.tool_center_service.versioning import (
                    assigned_skill_version, latest_published_version, published_version_by_no,
                )

                _anchor_agent = hit.agent_id or req.agent_id
                _skill_pin = await assigned_skill_version(
                    db, _anchor_agent, user.user_id, executor_id)
                _pin_ver = (await published_version_by_no(db, "skill", executor_id, _skill_pin)
                            if _skill_pin is not None else None)
                if _pin_ver is None and req.agent_id:
                    log.warning(f"question {hit.id} 技能 {executor_id} 指派链无版本"
                                f"(锚 agent={(_anchor_agent or '-')[:8]}, pin={_skill_pin}),降级 agent 执行")
                    executor_type, executor_id = "agent", req.agent_id
                elif _pin_ver is None:
                    # 没有可降级的 agent。**不能就这么把没版本的技能丢给 resolver** ——
                    # 它会抛一句「未指定版本,请通过 Agent 绑定(pin)」,而这条问题是运营
                    # 在后台配好、并且明确指到这个技能上的:它本来就该能跑。
                    # 这里退到「该技能最新的已发布版本」,并把这件事记下来:
                    # 固定问题是运营背书过的入口(question_authorized),不是用户随手直调,
                    # 没有指派并不意味着不许跑,只意味着没人指定跑哪一版。
                    _latest = await latest_published_version(db, "skill", executor_id)
                    if _latest is not None:
                        payload["executor_version_id"] = _latest.id
                        log.info(f"question {hit.id} 技能 {executor_id} 无指派版本,"
                                 f"按最新已发布 v{_latest.version} 执行")
                        if (_latest.snapshot or {}).get("tools"):
                            payload["direct_dispatch"] = True
                    else:
                        raise ValidationError(
                            f"这条问题绑定的技能还没有已发布的版本,跑不起来。"
                            f"请在「工具中心 · Skill」里把它发布一个版本;"
                            f"若要按用户控制版本,再到「分发中心」给账号指派。")
                else:
                    payload["executor_version_id"] = _pin_ver.id
                    # 19 号 S7.5 编排直出的开关:技能绑了工具 → 置标记。
                    # 这里只判断「有没有绑工具」——_pin_ver 已取到(零额外查询),且只用
                    # 版本快照里原始的 tools 列表做存在性判断;真正调用用的 ToolSpec
                    # 由 resolver 按同一个版本快照解析产出(唯一真相),网关不在这里翻译
                    # 一份清单,免得两处口径各自漂移。
                    if (_pin_ver.snapshot or {}).get("tools"):
                        payload["direct_dispatch"] = True
                    # 参数模板展开(19 号 S7.5):基准日期与对比窗口全部装配期
                    # 代码生成(零 LLM,无运行期默认补全——缺=执行层报错暴露)
                    q_params = qb.expand_param_template(hit.param_template)
                    if q_params:
                        payload["question_params"] = q_params
                    # 20 号 P4 + 22 号复盘:预热缓存直出(L1 前置)——prewarm_enabled
                    # 固定问题命中后先查「当日行」(biz_date=今天 且 answer 非空),
                    # 命中则直出完整结果(卡片入口点击 → chat 直出,0 LLM);
                    # 今日未出(占位/无行)=空态回落实时执行,绝不回退昨日缓存。
                    # 直出也补一条终态 COMPLETED task(task_id 真实返回),原因:
                    #   - 历史会话以 task 为轮次数据源(list_turns_page 读 task_queue),
                    #     不补行则这轮在历史/刷新回放里凭空消失;
                    #   - 前端按 hit_prewarm 直出渲染、不开 SSE,故无需 publish done;
                    #   - source 沿用 "app" 不引入新枚举(统计口径不变),本轮来源用
                    #     payload/result.prewarm_hit=true 溯源,终态任务不进队列。
                    if getattr(hit, "prewarm_enabled", False):
                        from cogni_agent.gateway_service.models import AppPrewarmCache
                        from cogni_agent.gateway_service.ota_accounts import user_ota_accounts
                        from cogni_agent.gateway_service.ota_accounts import ota_no_data_answer

                        _today_d = today_cst()
                        # 25 号:缓存行维度 = (question, scenic, ota_account, biz_date)。
                        # OTA 维度主题(OTA/概览)按「当前用户账号」匹配行;用户未绑账号
                        # → OTA 主题直出「未配置账号」(判定与文案收口在
                        # ota_accounts,此处只调 helper;用户级口径,景区下
                        # 别人绑的不算);概览账号行未命中回退账号无关基础行
                        # (NULL,未绑用户看三域概览)。非账号主题行匹配 IS NULL。
                        _theme = hit.theme_code or ""
                        _acct_split = _theme in ("ota_direct_sales", "overview")
                        _acct = None
                        if _acct_split:
                            _uaccts = await user_ota_accounts(db, user.user_id)
                            _acct = _uaccts[0] if _uaccts else None
                        _hit: dict | None = None
                        _no_data_answer = (await ota_no_data_answer(db, user.user_id)
                                           if _theme == "ota_direct_sales" else None)
                        if _no_data_answer is not None:
                            # OTA 问题且当前用户未绑账号:不建任务、不进 LLM(零 token),
                            # 直接返回「未配置账号」无数据说明(no_data=true)。
                            log.info(f"conv {conv.id} 固定问题「{hit.question_text[:20]}」"
                                     f"当前用户未配置 OTA 账号 → 直返无数据说明(未进 LLM)")
                            _hit = {
                                "answer": _no_data_answer,
                                "structured": None,
                                "data_as_of": None,
                                "generated_at": None,
                                "period": hit.time_period,
                                "no_data": True,
                                "is_today": True,
                            }
                        else:
                            # 当日行命中即直出(biz_date=今天 且 answer 非空);
                            # 27 号:skill_version 严格匹配指派 pin(全部严格,不回退
                            # 任意版本行);未命中=空态回落实时执行,绝不回退昨日缓存。
                            _cache_row = (await db.execute(
                                sa.select(AppPrewarmCache).where(
                                    AppPrewarmCache.question_id == hit.id,
                                    AppPrewarmCache.scenic_id == conv.scenic_id,
                                    AppPrewarmCache.ota_account == _acct,
                                    AppPrewarmCache.skill_version == _skill_pin,
                                    AppPrewarmCache.biz_date == _today_d,
                                    AppPrewarmCache.answer.isnot(None),
                                ).limit(1)
                            )).scalars().first()
                            # 概览账号行未命中 → 回退账号无关基础行(NULL;版本仍严格)
                            if (_cache_row is None and _theme == "overview" and _acct):
                                _cache_row = (await db.execute(
                                    sa.select(AppPrewarmCache).where(
                                        AppPrewarmCache.question_id == hit.id,
                                        AppPrewarmCache.scenic_id == conv.scenic_id,
                                        AppPrewarmCache.ota_account.is_(None),
                                        AppPrewarmCache.skill_version == _skill_pin,
                                        AppPrewarmCache.biz_date == _today_d,
                                        AppPrewarmCache.answer.isnot(None),
                                    ).limit(1)
                                )).scalars().first()
                            if _cache_row is not None:
                                log.info(f"conv {conv.id} 固定问题「{hit.question_text[:20]}」"
                                         f"命中当日预热缓存直出(biz_date={_today_d},"
                                         f"acct={_cache_row.ota_account or '-'})")
                                _hit = {
                                    "answer": _cache_row.answer,
                                    # 预热缓存行是早先跑出来的,里面的图可能只带一种形态
                                    # (只有 ECharts option、没有中性行列表)。直出这条路
                                    # 不经过执行引擎,不在这里补的话,命中缓存的那一轮
                                    # 图在管理端就是空的 —— 而它同时也被写进 task.result
                                    # 当历史轮次,于是连刷新回放都一起丢。
                                    "structured": chart_view.normalize_structured(
                                        _cache_row.structured),
                                    "data_as_of": (_cache_row.data_as_of.isoformat()
                                                   if _cache_row.data_as_of else None),
                                    "generated_at": (_cache_row.generated_at.isoformat()
                                                     if _cache_row.generated_at else None),
                                    # 24号:取数范围随行返回(time_period 枚举,与 biz_date 解耦
                                    # ——行归属今天,period 说明取的是哪个窗口的数据),前端展示用
                                    "period": hit.time_period,
                                    "no_data": False,
                                    "is_today": True,          # 当日行命中
                                }
                        if _hit is not None:
                            # 直出也补一条终态 COMPLETED task:历史会话以 task 为轮次数据源
                            # (list_turns_page 读 task_queue),不补行则该轮在历史/刷新回放里
                            # 凭空消失;结果与实时任务 result 同构(answer/structured 前端同一套
                            # 渲染,data_as_of/generated_at 供「X日数据」标注),本轮来源用
                            # result.prewarm_hit=true 溯源。
                            hit_task = Task(
                                scenic_id=conv.scenic_id, user_id=user.user_id,
                                executor_type=executor_type, executor_id=executor_id,
                                conversation_id=conv.id, source=SOURCE_APP, status="COMPLETED",
                                priority=5, payload={**payload, "prewarm_hit": True},
                                result={**_hit, "prewarm_hit": True},
                            )
                            db.add(hit_task)
                            await db.commit()
                            # 轮次计数/会话列表排序与实时路径一致(历史按 task 回放)
                            await conv_svc.bump_turn(conv.id)
                            return AppChatResponse(
                                task_id=hit_task.id, conversation_id=conv.id,
                                hit_prewarm=True,
                                prewarm_result=AppPrewarmHitResult(**_hit),
                                target_type=executor_type, target_id=executor_id,
                            )
                    log.info(f"conv {conv.id} 固定问题命中「{hit.question_text[:24]}」"
                             f"→ 技能 {executor_id}(L1 直跳, v{_pin_ver.version})")
            # 命中但 skill_id 为空:不干预执行,仅记录命中(供预热/统计)

    task_info = await create_task(
        # 执行上下文跟随会话自身景区:续聊旧会话时沿用会话创建时的景区(历史数据一致),
        # 不受用户换绑影响;新建会话时 conv.scenic_id 即用户当前绑定景区。
        scenic_id=pc.scenic_id,
        source=pc.source,
        payload=payload,
        executor_type=executor_type,
        executor_id=executor_id,
        conversation_id=conv.id,
        user_id=user.user_id,
    )

    await conv_svc.bump_turn(conv.id)

    return AppChatResponse(task_id=task_info["id"], conversation_id=conv.id,
                           target_type=executor_type, target_id=executor_id)


# ── 2A.2.5: POST /api/app/chat/{task_id}/cancel ──

@router.post("/chat/{task_id}/cancel")
async def app_cancel_chat(task_id: str, user: CurrentUser = Depends(get_current_app_user)):
    """停止输出/取消任务：APP 对话执行中点「停止」按钮时调用。

    复用管理端 cancel_task（含同景区权限校验，无权限/不存在抛 404），
    成功后广播 cancelled 事件——正在连接的 SSE 流立即收到终态事件并关闭，
    未在连接中的任务同样置为 CANCELLED（断线后恢复即可看到）。

    真取消（v1.1.8）：同时登记进程内取消标记，执行侧的流式循环/watcher
    感知后协作式中断正在进行的 LLM 交互与工具调用，并保留部分回答——
    下次进入会话，历史里这一轮显示为「已停止」而不是「已完成」。
    """
    result = await cancel_task(task_id, scenic_id=user.scenic_id)
    if result.get("status") == "CANCELLED":
        # 真正发生了取消(任务原本在执行中)才登记标记/广播;对终态任务的幂等空转
        # (重复点停止)不广播,避免向已完成任务的流上再发 cancelled。
        streaming.mark_user_cancelled(task_id)
        await streaming.publish(task_id, "cancelled", {"message": "任务已取消"})
    return result


# ── 2A.3: GET /api/app/chat/{task_id}/stream ──

@router.get("/chat/{task_id}/stream")
async def app_chat_stream(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    """SSE 流式事件推送。APP 端 EventSource 连接此端点接收实时执行事件。

    增强特性（v1.1 Phase 4.4）：
    - Last-Event-ID 断线重连
    - 终态兜底：任务已在终态但流中无事件时，自动查询 DB 补充 done/error
    - 心跳保活：空闲时每 15s 发送 ping 事件，防止反向代理断开
    - 超时保护：单次连接最长 300s，超时后优雅关闭
    """
    _MAX_STREAM_SECONDS = 300
    _PING_INTERVAL = 15

    stream_start = asyncio.get_event_loop().time()
    sent_terminal = False

    async def event_generator():
        nonlocal sent_terminal
        # 心跳保活不能靠"遍历中插队":async for 的每个 __anext__ 都会阻塞直到有事件,
        # 空闲期根本轮不到发 ping。正确做法与 Admin 端 routes_chat.py 一致——
        # 订阅跑在独立 pump 任务里、事件塞队列,主循环只对取队列做超时,
        # 超时即空闲 → 发 ping 心跳(不能用 wait_for 直接包 __anext__:超时取消会让
        # 异步生成器进入损坏状态,之后一直抛 StopAsyncIteration,流就断了)。
        queue: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        async def _pump() -> None:
            try:
                async for eid, event, data in streaming.subscribe(task_id, last_id=last_event_id):
                    await queue.put((eid, event, data))
            except Exception:  # noqa: BLE001 - 订阅侧异常不能让连接挂死
                log.exception(f"task {task_id} 事件订阅异常")
                await queue.put(Exception("事件订阅中断"))
            finally:
                await queue.put(_DONE)

        pump = asyncio.create_task(_pump())
        try:
            while True:
                # request.is_disconnected() 在某些运行时下会阻塞等待连接消息,
                # 长任务期间会把主循环卡住 → 心跳、终局事件全停发。给极短超时,
                # 只做"顺带看一眼",永远不让它阻塞事件下发。
                try:
                    disconnected = await asyncio.wait_for(request.is_disconnected(), timeout=0.1)
                except asyncio.TimeoutError:
                    disconnected = False
                if disconnected:
                    break
                # 超时保护:单次连接最长 _MAX_STREAM_SECONDS
                if asyncio.get_event_loop().time() - stream_start > _MAX_STREAM_SECONDS:
                    err_payload = json.dumps(
                        task_error(TaskErrorKind.TASK_TIMEOUT, message='流超时,请重新获取'), ensure_ascii=False)
                    yield f"event: error\ndata: {err_payload}\n\n"
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL)
                except asyncio.TimeoutError:
                    # 心跳保活:空闲期每 _PING_INTERVAL 秒发 ping,防止反向代理/网关断开长连接
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    err_payload = json.dumps(
                        task_error(TaskErrorKind.STREAM_ERROR, message=f"事件流异常:{item}"), ensure_ascii=False)
                    yield f"event: error\ndata: {err_payload}\n\n"
                    break
                eid, event, data = item
                if event in streaming.TERMINAL_EVENTS:
                    sent_terminal = True
                yield f"id: {eid}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            pump.cancel()
            # 终态兜底：流结束但无终态事件时，从 DB 查询任务状态补充关闭事件
            # 终态兜底：流结束但无终态事件时，从 DB 查询任务状态补充关闭事件
            if not sent_terminal:
                try:
                    async with get_sessionmaker()() as sess:
                        task = await sess.get(Task, task_id)
                        if task and task.status in ("COMPLETED", "FAILED", "CANCELLED"):
                            data = {"status": task.status}
                            end_event = {
                                "COMPLETED": "done",
                                "FAILED": "error",
                                "CANCELLED": "cancelled",
                            }.get(task.status, "done")
                            if task.status == "COMPLETED":
                                result = task.result if isinstance(task.result, dict) else {}
                                data["result_summary"] = result.get("answer") or ""
                                # 结构化结果随 result 落库后,断线重连兜底一并回放图表
                                data["structured"] = result.get("structured")
                            elif task.status == "FAILED":
                                result = task.result if isinstance(task.result, dict) else {}
                                data.update(task_error(
                                    result.get("kind") or TaskErrorKind.FAILED,
                                    message=result.get("error") or "任务执行失败"))
                            elif task.status == "CANCELLED":
                                # 用户主动停止:终态携带已产出的部分回答,断线重连兜底
                                # 也能显示「已停止 + answer(半截)」(与历史接口同口径)。
                                result = task.result if isinstance(task.result, dict) else {}
                                data["answer"] = result.get("answer") or ""
                            yield f"event: {end_event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 2A.4: 会话管理 ──

@router.get("/conversations", response_model=AppConversationListResponse)
async def app_list_conversations(
    user: CurrentUser = Depends(get_current_app_user),
):
    """会话列表（最近 50 条，不含已归档）。只按用户过滤,换绑景区后历史会话仍可见。"""
    items = await conv_svc.list_conversations(user_id=user.user_id)
    return AppConversationListResponse(
        # 0074:target_* 由 executor_* 派生 —— 会话本来就记了执行目标,
        # 这里只是换成与 chat/home 一致的字段名,省得前端两套叫法各记一遍。
        items=[AppConversationListItem(
            **item,
            target_type=item.get("executor_type") or "agent",
            target_id=item.get("executor_id"),
        ) for item in items],
        total=len(items),
    )


@router.delete("/conversations/{conversation_id}")
async def app_archive_conversation(
    conversation_id: str,
    user: CurrentUser = Depends(get_current_app_user),
):
    """归档会话（软删除，不物理删除数据）。"""
    await conv_svc.archive(conversation_id, user_id=user.user_id)
    return {"ok": True}


# ── 2A.5: 消息操作 ──

@router.get("/conversations/{conversation_id}/messages")
async def app_list_messages(
    conversation_id: str,
    limit: int = Query(10, ge=1, le=50),
    cursor: str | None = Query(default=None),
    user: CurrentUser = Depends(get_current_app_user),
):
    """获取会话历史轮次（游标分页，时间正序）。

    - 无 cursor：返回最近 limit 轮（默认 10）；向上滑加载更早历史时带上次响应的 next_cursor；
    - has_more=false / next_cursor=null = 已到最早，停止上滑加载；
    - 无 total：轮数随时间变化、翻页会误导（要展示轮数用会话列表的 turn_count）。
    """
    try:
        return await conv_svc.list_turns_page(
            conversation_id, user_id=user.user_id, limit=limit, cursor=cursor,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的分页游标(cursor)")


@router.post("/messages/{message_id}/feedback")
async def app_message_feedback(
    message_id: str,
    req: AppMessageFeedbackRequest,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """对消息点赞/踩（重复提交覆盖前值）。

    feedback_type 为 null 表示取消：删除该条反馈记录（前端"再点一次取消高亮"）。
    """
    from sqlalchemy import select

    existing = (await db.execute(
        select(AppMessageFeedback).where(
            AppMessageFeedback.message_id == message_id,
            AppMessageFeedback.user_id == user.user_id,
        )
    )).scalars().first()

    if req.feedback_type is None:
        # 取消：删除已有反馈记录
        if existing is not None:
            await db.delete(existing)
        await db.commit()
        return {"ok": True}

    if existing:
        existing.feedback_type = req.feedback_type
    else:
        db.add(AppMessageFeedback(
            message_id=message_id,
            user_id=user.user_id,
            feedback_type=req.feedback_type,
        ))
    await db.commit()
    return {"ok": True}


@router.post("/followed-questions")
async def app_follow_question(
    req: AppFollowQuestionRequest,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """关注一个问题。"""
    q = AppFollowedQuestion(
        user_id=user.user_id,
        question_text=req.question_text,
        source_message_id=req.source_message_id,
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return {"id": q.id, "created_at": q.created_at.isoformat() if q.created_at else None}


@router.get("/followed-questions", response_model=AppFollowedQuestionListResponse)
async def app_list_followed_questions(
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """已关注问题列表。"""
    from sqlalchemy import select

    items = (await db.execute(
        select(AppFollowedQuestion).where(
            AppFollowedQuestion.user_id == user.user_id,
            AppFollowedQuestion.deleted_at.is_(None),
        ).order_by(AppFollowedQuestion.created_at.desc())
    )).scalars().all()

    # 与首页同口径:带上 issue_id,前端点关注问题时才能走对分流
    from cogni_agent.gateway_service import scenic_binding as _binding

    out = []
    for q in items:
        hit = await _binding.match_issue_by_text(db, user.scenic_id or None, q.question_text)
        out.append(AppFollowedQuestionItem(
            id=q.id,
            question_text=q.question_text,
            source_message_id=q.source_message_id,
            created_at=q.created_at.isoformat() if q.created_at else "",
            issue_id=hit.issue_id if hit else "",
            business_class=hit.business_class if hit else "",
        ))
    return AppFollowedQuestionListResponse(items=out, total=len(out))


@router.delete("/followed-questions/{question_id}")
async def app_unfollow_question(
    question_id: str,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """取消关注（软删除）。"""
    from sqlalchemy import select

    q = (await db.execute(
        select(AppFollowedQuestion).where(
            AppFollowedQuestion.id == question_id,
            AppFollowedQuestion.user_id == user.user_id,
            AppFollowedQuestion.deleted_at.is_(None),
        )
    )).scalars().first()
    if q is None:
        raise NotFound("关注问题不存在")
    q.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}


# ── 2A.6: 试试问我问题列表（分页/换一批） ──

@router.get("/guide-questions", response_model=AppGuideQuestionListResponse)
async def app_guide_questions(
    theme_code: str | None = Query(None, description="按主题筛选：passenger_flow/ticket/public_opinion/ota_direct_sales"),
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),  # 上限 100:一次拉全量(问题库通用+景区行 ≤56),原 20 过紧致前端 99 误报 422
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
):
    """试试问我问题列表。

    - theme_code 为空返回全部主题
    - 前端"换一批"通过 page 翻页实现;每主题随机取一条归前端(20 号 §八 定稿)
    - 20 号 P1:数据源切问题库(app_question_bank,id=question_id,点击后 chat 携带);
      问题库为空时回退 guide_data 静态 18 条(初始化前兜底)
    """
    from cogni_agent.gateway_service.guide_data import THEME_NAMES, list_guide_questions
    from cogni_agent.gateway_service.models import AppQuestionBank

    stmt = (
        sa.select(AppQuestionBank)
        .where(
            AppQuestionBank.active.is_(True),
            AppQuestionBank.scenic_id.is_(sa.null())
            | (AppQuestionBank.scenic_id == (user.scenic_id or "")),
            # 「【主题解读】概览」这类不是问题,是主题卡片本身的解读报告 ——
            # 它由卡片上的「AI总结 / AI解析」承载。混进常问问题的话,用户会看到
            # 一条「【主题解读】概览」当成可以问的问题,点下去跑的是一份几分钟的报告。
            ~AppQuestionBank.question_text.like(f"{THEME_REPORT_PREFIX}%"),
        )
        .order_by(AppQuestionBank.sort_order, AppQuestionBank.created_at)
    )
    if theme_code:
        stmt = stmt.where(AppQuestionBank.theme_code == theme_code)
    rows = (await db.execute(stmt)).scalars().all()
    if rows:
        total = len(rows)
        start = (page - 1) * page_size
        items = rows[start:start + page_size]
        return AppGuideQuestionListResponse(
            items=[
                AppGuideQuestionItem(
                    id=q.id,
                    theme=THEME_NAMES.get(q.theme_code, q.theme_code),
                    question=q.question_text,
                )
                for q in items
            ],
            total=total,
            page=page,
            page_size=page_size,
        )
    # 回退:静态数据
    items, total = list_guide_questions(theme_code=theme_code, page=page, page_size=page_size)
    return AppGuideQuestionListResponse(
        items=[
            AppGuideQuestionItem(
                id=q["id"],
                theme=THEME_NAMES.get(q["theme_code"], q["theme_code"]),
                question=q["question"],
            )
            for q in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── 2A.7: 看板 AI 解读结论概要 ──

async def _default_agent_for(db, user) -> str | None:
    """这个账号回落时该用哪个 Agent。

    原先是 `select(Agent.id).where(Agent.active)` 取最新的一个 —— 只判了启用:
    软删掉的、从没发布过版本的、别的景区的、没指派给这个人的,统统可能被选中。
    然后任务死在 resolver 的 agent_not_distributed 上,把管理员指向一个
    他根本没打算用的 Agent —— 排查方向从第一步就错了。

    这里按「这个人真的能跑」来挑:① 指派给他的(指派即授权,最准)
    ② 其次是他可见且已发布的。两条都没有就返回 None,由调用方给一句说得清的话。
    """
    from cogni_agent.tool_center_service.tool.registry import GROUP_ROLES

    pub = sa.select(ResourceVersion.id).where(
        ResourceVersion.resource_type == "agent",
        ResourceVersion.resource_id == Agent.id,
        ResourceVersion.status == "published")
    assigned = sa.select(ResourceAssignment.id).where(
        ResourceAssignment.resource_type == "agent",
        ResourceAssignment.agent_id == Agent.id,
        ResourceAssignment.scope_type == SCOPE_USER,
        ResourceAssignment.scope_id == user.user_id,
        ResourceAssignment.version_id.in_(pub)).exists()

    base = [Agent.active.is_(True), Agent.deleted_at.is_(None)]
    # ① 指派给这个人的
    row = (await db.execute(
        select(Agent.id).where(*base, assigned)
        .order_by(Agent.created_at.desc()).limit(1))).scalars().first()
    if row:
        return row
    # ② 可见 + 已发布
    vis = [Agent.owner_user_id == user.user_id, Agent.visibility == "public"]
    if user.scenic_id:
        vis.append(sa.and_(Agent.visibility == "scenic", Agent.scenic_id == user.scenic_id))
    if user.role in GROUP_ROLES:
        vis.append(Agent.visibility == "group")
    return (await db.execute(
        select(Agent.id).where(*base, pub.exists(), sa.or_(*vis))
        .order_by(Agent.created_at.desc()).limit(1))).scalars().first()


def _first_sentence(text: str, limit: int = 120) -> str:
    """报告正文 → 首页卡片那一行(纯文本,取结论不取计划)。见 summary_line 模块。

    保留这个名字是为了不动调用点;它早先真的只是"取第一句",而报告往往以一段
    执行计划或模型的自言自语开头,于是卡片上写的是「使用 researcher 在互联网上
    搜索…」—— 用户看到的是系统打算干什么,而不是它查出了什么。
    """
    return summary_line(text, limit)


@router.get("/panel/{theme}/insight", response_model=AppPanelInsightResponse)
async def app_panel_insight(
    theme: str,
    user: CurrentUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = None,  # 27 号:版本锚的缺省 agent(问题行 agent_id 优先)
):
    """看板 AI 解读结论概要卡片。

    支撑 M3 各主题看板页顶部的"AI 解读结论概要"：
    - summary: 命中缓存 = 预热完整解读报告(answer);未命中 = 空串
    - sentence: 命中缓存 = 一句话总结(cache.summary);未命中 = None
    - level: 异常分色 danger/warning/normal(取缓存二次加工 overview_metrics)
    - metrics: 概览四维彩色指标(与 agent/home overview_insights 同源)
    点击概要卡片 → 前端发送【主题解读】消息走普通对话链路。

    20 号 P5:缓存优先——命中该主题「【主题解读】」预热缓存行则直出;
    缓存缺失直接返回空内容(insight 是纯读缓存接口,非 SSE 对话链路,
    不回退实时 LLM 生成,避免慢模型拖垮卡片渲染;预热点位由
    prewarm CronJob 负责填充)。
    """
    from cogni_agent.gateway_service.models import AppPrewarmCache, AppQuestionBank
    from cogni_agent.gateway_service.insight_service import get_overview_metric_items
    from cogni_agent.gateway_service.ota_accounts import user_ota_accounts

    # 25 号:缓存行维度含 ota_account——行选择 = 当前用户账号匹配行优先,
    # 回退账号无关行(NULL:概览基础版/非账号主题行)。多账号景区不串数据。
    _u_accts = await user_ota_accounts(db, user.user_id)
    _u_acct = _u_accts[0] if _u_accts else None

    theme_q = (await db.execute(
        sa.select(AppQuestionBank).where(
            AppQuestionBank.theme_code == theme,
            AppQuestionBank.question_text.like("【主题解读】%"),
        ).limit(1)
    )).scalars().first()
    if theme_q is not None:
        # 27 号:版本严格匹配(全部严格)——该主题问题的期望版本 = 用户对锚 agent
        # (问题行 agent_id,缺省 panel 请求的 agent 参数)的指派 pin
        from cogni_agent.tool_center_service.versioning import assigned_skill_version

        _panel_pin = (await assigned_skill_version(
            db, theme_q.agent_id or agent_id, user.user_id, theme_q.skill_id)
            if theme_q.skill_id else None)
        # 22 号复盘:只读当日行(biz_date=今天 且 answer 非空),今日未出=空,不回退昨日
        cache = (await db.execute(
            sa.select(AppPrewarmCache).where(
                AppPrewarmCache.question_id == theme_q.id,
                AppPrewarmCache.scenic_id == (user.scenic_id or ""),
                AppPrewarmCache.ota_account == _u_acct,
                AppPrewarmCache.skill_version == _panel_pin,
                AppPrewarmCache.biz_date == today_cst(),
                AppPrewarmCache.answer.isnot(None),
            ).limit(1)
        )).scalars().first()
        # 账号行未命中 → 回退账号无关行(概览基础版;OTA 主题无 NULL 行,未绑用户=空态)
        if cache is None:
            cache = (await db.execute(
                sa.select(AppPrewarmCache).where(
                    AppPrewarmCache.question_id == theme_q.id,
                    AppPrewarmCache.scenic_id == (user.scenic_id or ""),
                    AppPrewarmCache.ota_account.is_(None),
                    AppPrewarmCache.skill_version == _panel_pin,
                    AppPrewarmCache.biz_date == today_cst(),
                    AppPrewarmCache.answer.isnot(None),
                ).limit(1)
            )).scalars().first()
        if cache is not None:
            metric = cache.overview_metrics if isinstance(cache.overview_metrics, dict) else {}
            # 25 号:概览行按当前用户账号匹配(账号行优先,回退 NULL 基础行);
            # 四维条 OTA 项在当前用户未绑账号时不返回(用户级口径)
            metric_items = await get_overview_metric_items(
                db, user.scenic_id, _u_acct,
                user_id=user.user_id, agent_hint=agent_id)  # 27 号:版本严格匹配
            if not _u_acct:
                metric_items = [i for i in metric_items
                                if i["theme"] != "ota_direct_sales"]
            # 主题级分色:优先缓存二次加工单条;缺失则取概览四维中本主题项
            level = metric.get("level") or next(
                (i["level"] for i in metric_items if i["theme"] == theme), "normal")
            return AppPanelInsightResponse(
                theme=theme,
                summary=cache.answer,             # 完整解读报告
                # 一句话总结(预热摘要产出)。同样过一遍清洗:它也可能带 **加粗**、
                # 反引号这类标记,而卡片是纯文本槽位,原样显示出来就是一串乱码。
                sentence=(strip_markdown(cache.summary) or None) if cache.summary else None,
                level=level,
                # 四维彩色指标条只属于概览面板(数据源=概览行二次加工 items,
                # 24号口径:非概览主题自身无 overview_metrics,不透出概览的四维数据;
                # level 分色仍取概览 items 中本主题项作兜底)
                metrics=([{
                    "name": i["name"], "metric": i["metric"],
                    "value": i["value"], "level": i["level"],
                } for i in metric_items] if theme == "overview" else []),
                updated_at=(cache.generated_at.isoformat() if cache.generated_at else ""),
                question_id=str(theme_q.id),
                question=theme_q.question_text,
            )
    # 缓存未命中(无该主题库行或该景区维度无预热行):纯读缓存接口不回退实时
    # LLM(insight 非对话链路,不在此实时生成),直接返回空内容。
    return AppPanelInsightResponse(
        theme=theme,
        summary="",
        sentence=None,
        level="normal",
        metrics=[],
        updated_at="",
        question_id=str(theme_q.id) if theme_q is not None else "",
        question=theme_q.question_text if theme_q is not None else "",
        report_state="none",
    )
