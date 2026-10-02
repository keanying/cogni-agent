"""登录态保持:access_token 过期后能不能靠 refresh_token 无感续上。

症状回归:用一会儿就跳回登录页 / 接口报 invalid token。
根因有两条,这里各锁一条 ——
① 前端拿到 refresh_token 后直接丢了(没存、更没用),过期只能重新登录;
② 后端 refresh 校验把带时区的 credentials_changed_at 当成 UTC 挂钟,
   改密后的一段时间里连刚签发的 refresh 也被判失效。
"""
from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jose import jwt

from cogni_agent.common.errors import Unauthorized
from cogni_agent.config import get_settings
from cogni_agent.user_center_service import auth as auth_svc

WEB = Path(__file__).resolve().parents[1] / "web" / "src"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class _User:
    """只带 refresh 校验会读到的字段,不碰数据库。"""

    def __init__(self, *, is_active=True, credentials_changed_at=None):
        self.id = "u-1"
        self.account = "pftroot"
        self.scenic_id = None
        self.role = "super_admin"
        self.is_active = is_active
        self.must_change_password = False
        self.credentials_changed_at = credentials_changed_at


class _DB:
    def __init__(self, user):
        self._user = user

    async def get(self, _model, _id):
        return self._user


@pytest.fixture(autouse=True)
def _jwt_secret():
    uc = get_settings().raw.setdefault("user_center", {})
    saved = uc.get("jwt_secret")
    uc["jwt_secret"] = "test-secret-for-session-keepalive"
    yield
    if saved is None:
        uc.pop("jwt_secret", None)
    else:
        uc["jwt_secret"] = saved


def _decode(token: str) -> dict:
    cfg = get_settings().get("user_center", default={})
    return jwt.decode(token, cfg["jwt_secret"], algorithms=[cfg.get("jwt_algorithm", "HS256")])


@run_async
async def test_refresh_returns_new_pair():
    user = _User()
    _, refresh = auth_svc.issue_tokens(user)
    access2, refresh2, must_change = await auth_svc.refresh_access_token(_DB(user), refresh)
    assert _decode(access2)["sub"] == user.id
    assert _decode(access2).get("type") != "refresh"      # 换回来的是 access,不是又一张 refresh
    assert _decode(refresh2)["type"] == "refresh"          # 滚动刷新:refresh 也换新的
    assert must_change is False


@run_async
async def test_access_token_cannot_be_used_as_refresh():
    """拿 access 换 access 等于把 60 分钟的令牌变成永动机,必须挡住。"""
    user = _User()
    access, _ = auth_svc.issue_tokens(user)
    with pytest.raises(Unauthorized):
        await auth_svc.refresh_access_token(_DB(user), access)


@run_async
async def test_refresh_survives_tz_aware_credentials_changed_at():
    """改密时间带 +08:00 时区时,刚签发的 refresh 不能被误判为失效。

    这是 astimezone 与 replace(tzinfo=...) 的区别:后者不换算,把 +08:00 的 10 点
    当成 UTC 的 10 点,凭空把「改密时刻」推后 8 小时。
    """
    changed = datetime.now(timezone(timedelta(hours=8))) - timedelta(minutes=5)
    user = _User(credentials_changed_at=changed)
    _, refresh = auth_svc.issue_tokens(user)      # 改密之后签发的,理应有效
    access2, _, _ = await auth_svc.refresh_access_token(_DB(user), refresh)
    assert _decode(access2)["sub"] == user.id


@run_async
async def test_refresh_rejected_after_password_change():
    """改密之前签发的 refresh 必须失效 —— 否则「改密下线」就是句空话。"""
    user = _User(credentials_changed_at=datetime.now(timezone.utc) + timedelta(hours=1))
    _, refresh = auth_svc.issue_tokens(user)
    with pytest.raises(Unauthorized):
        await auth_svc.refresh_access_token(_DB(user), refresh)


@run_async
async def test_refresh_rejected_for_disabled_user():
    user = _User(is_active=False)
    _, refresh = auth_svc.issue_tokens(user)
    with pytest.raises(Unauthorized):
        await auth_svc.refresh_access_token(_DB(user), refresh)


def test_refresh_endpoint_needs_no_auth():
    """续期接口自己要是需要登录,那就永远续不上了。"""
    import inspect

    from cogni_agent.user_center_service import routes

    params = inspect.signature(routes.refresh_token).parameters
    assert "user" not in params and "current_user" not in params


def test_frontend_stores_and_uses_refresh_token():
    """前端侧的回归:登录要存下 refresh_token,401 要拿它换新再重放,并提前续期。

    这几行没了,后端做得再对也还是「用一会儿就跳登录页」。
    """
    api = (WEB / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "db_refresh_token" in api, "refresh_token 没有持久化"
    assert "data.refresh_token" in api, "登录响应里的 refresh_token 被丢掉了"
    assert "/admin/auth/refresh" in api, "没有调用续期接口"
    assert "ensureFreshToken" in api, "没有临期提前续期(SSE 中途过期撞不到 401)"
    assert "res.status === 401" in api, "401 后没有重放原请求"
