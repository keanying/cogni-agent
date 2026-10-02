"""
鉴权依赖(M1-02):内部会话走 JWT,开放平台走 API Key,两套并行。

- get_current_user:供 /chat、/admin/*、/tasks/* 使用,解析 user-center 签发的 JWT。
- get_current_api_key:供 /v1/chat/completions 使用,解析开放平台 Authorization: Bearer sk-zhilv-*。
两者都会把解析出的 scenic_id 写入请求上下文(M1-04),下游模块统一读取。
集团级角色(super_admin/group_admin/group_developer/group_viewer)的 scenic_id 为 None,
代表数据归属集团而非某个具体景区,景区角色(scenic_user)则必填。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header
from jose import JWTError, jwt

from cogni_agent.common.errors import Unauthorized
from cogni_agent.common.logging import set_trace_context, get_trace_id
from cogni_agent.config import get_settings


@dataclass
class CurrentUser:
    user_id: str
    scenic_id: str | None  # None = 集团级(不归属具体景区),见 README「组织与角色模型」
    role: str


@dataclass
class CurrentApiKey:
    key_id: str
    scenic_id: str | None
    allowed_agent_ids: list[str]


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    """M2-05:校验 JWT,失败统一抛 Unauthorized(401)。"""
    if not authorization.startswith("Bearer "):
        raise Unauthorized("missing bearer token")
    token = authorization.removeprefix("Bearer ")
    cfg = get_settings().get("user_center", default={})
    try:
        payload = jwt.decode(token, cfg["jwt_secret"], algorithms=[cfg.get("jwt_algorithm", "HS256")])
    except JWTError as exc:
        raise Unauthorized("invalid token") from exc
    user = CurrentUser(user_id=payload["sub"], scenic_id=payload["scenic_id"], role=payload["role"])
    # **角色与归属景区以用户表为准,不以令牌为准**。
    #
    # JWT 是登录那一刻的快照:管理员在「组织管理」里把某人改成超级管理员、或把他换到另一个景区,
    # 那个人手里的令牌还带着旧角色 —— 于是「改了不生效」,而且完全不报错,只是照旧的权限判。
    # 现场原话:「这个我切换我自己为超级管理员也不行」。要他重新登录才生效,没人猜得到,
    # 也没有任何提示会这么说。
    #
    # 这一步不额外费一次查询:下面那道 credentials_changed_at 校验本来就要读这一行。
    user = await _authoritative_user(user, payload.get("iat"))
    set_trace_context(get_trace_id(), user.scenic_id)
    return user


async def _authoritative_user(user: CurrentUser, iat: int | float | None) -> CurrentUser:
    """用用户表里的当前值覆盖令牌里的角色/归属景区,顺带做失效校验。

    三件事一次查库做完:
    1. 令牌签发早于 credentials_changed_at 的一律拒绝(改密/重置/强制下线要能真正生效);
    2. 账号已删除或**已停用**的拒绝 —— 停用只写了库,不拦令牌的话,
       被停用的账号还能拿旧令牌照常用到过期,「停用」形同虚设;
    3. 角色与归属景区取库里的当前值(见上面的说明)。

    第 1 条是「改密码 / 管理员重置 / 强制下线」能真正生效的关键。JWT 是无状态的,
    不做这一步的话:账号被盗 → 用户改密码 → **攻击者手里的旧令牌照样能用到过期**,
    而这恰恰是最需要它失效的场景。

    查库失败时**放行并沿用令牌里的值** —— 数据库抖动不该让所有人瞬间掉线。
    代价是每次请求一次用户表查询(本来就有);真要优化,可以在这里加一层短 TTL 的内存缓存,
    但 TTL 有多长,角色变更就有多久不生效 —— 上面那条现场故障正是这么来的。
    """
    try:
        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.user_center_service.models import User

        async with get_sessionmaker()() as db:
            row = await db.get(User, user.user_id)
        if row is None:
            raise Unauthorized("用户不存在或已被删除")
        if not getattr(row, "is_active", True):
            raise Unauthorized("账号已停用,请联系管理员")
        fresh = CurrentUser(user_id=user.user_id,
                            scenic_id=getattr(row, "scenic_id", user.scenic_id),
                            role=getattr(row, "role", None) or user.role)
        # iat 缺失:旧版本签发的令牌没有它,平滑过渡不踢人 —— 但角色/归属照样刷新
        if iat is None:
            return fresh
        changed = getattr(row, "credentials_changed_at", None)
        if changed is None:
            return fresh
        # JWT iat 是整数秒,为免 sub-second 精度不一致误杀刚签发的 token,
        # 将 credentials_changed_at 也截断到秒级再做比较(向下取整,对用户更宽容)。
        if int(changed.timestamp()) > float(iat):
            raise Unauthorized("凭据已变更,请重新登录")
        return fresh
    except Unauthorized:
        raise
    except Exception:  # noqa: BLE001 - 查库失败不应让所有人掉线
        return user


# ── v1.1 阶段二/2A.6: APP 端鉴权 ──
# 与操练台 /chat 共用同一套 JWT 链路,独立命名便于后续 APP 端扩展设备绑定等逻辑。
get_current_app_user = get_current_user


async def get_current_api_key(authorization: str = Header(default="")) -> CurrentApiKey:
    """
    G-API-05:开放平台鉴权链路。
    真实实现应查 numeri_core_tc_api_keys 表校验 key 有效性/过期时间/所属组织,
    此处为脚手架桩实现,标注 TODO 供接入真实数据层。
    """
    if not authorization.startswith("Bearer sk-zhilv-"):
        raise Unauthorized("invalid api key")
    key_plain = authorization.removeprefix("Bearer ")
    # TODO: 查 numeri_core_tc_api_keys,校验有效期/吊销状态,取出 scenic_id(可能为 None=集团级)与 allowed_agent_ids
    return CurrentApiKey(key_id=key_plain[:16], scenic_id="demo-org", allowed_agent_ids=[])
