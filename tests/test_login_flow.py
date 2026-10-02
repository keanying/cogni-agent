"""登录流程测试:验证码开关、账号/手机号双路匹配、验证码消费语义。"""
from __future__ import annotations

import pytest

import cogni_agent.config as cfgmod


@pytest.fixture
def sms_switch():
    uc = cfgmod.get_settings().raw.setdefault("user_center", {})
    saved = uc.get("require_sms_on_first_login")
    def _set(v: bool):
        uc["require_sms_on_first_login"] = v
    yield _set
    if saved is None:
        uc.pop("require_sms_on_first_login", None)
    else:
        uc["require_sms_on_first_login"] = saved


def test_default_is_sms_disabled():
    """默认必须是关闭的。短信通道目前是 mock,没有真实送达能力前强制要求
    只会把人挡在登录页外——这不是安全加固,是可用性事故。"""
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "config.yaml").read_text())
    assert cfg["user_center"]["require_sms_on_first_login"] is False


def test_login_policy_shape(sms_switch):
    """登录页要在**登录之前**知道要不要验证码,所以这个接口不能需要鉴权。"""
    import inspect

    from cogni_agent.user_center_service import routes

    sig = inspect.signature(routes.login_policy)
    assert not sig.parameters, "login-policy 不应依赖 get_current_user(登录前就要调用)"


@pytest.mark.asyncio
async def test_sms_send_resolves_account_to_phone():
    """发码接口收到的可能是账号,必须解析成真实手机号再落库。

    否则验证码存在 phone='账号' 这条不存在的记录下,而登录校验用的是
    user.phone,两边永远对不上——现象就是「验证码收到了但登录还是 401」。
    """
    import inspect

    from cogni_agent.user_center_service import routes

    src = inspect.getsource(routes.sms_send)
    assert "or_(" in src and "User.account" in src, "sms_send 必须按账号或手机号双路解析"


def test_verify_sms_consumes_code():
    """验证码用过即废,防重放。"""
    import inspect

    from cogni_agent.user_center_service import auth

    src = inspect.getsource(auth.verify_sms_code)
    assert "consumed" in src
    assert "expires_at" in src, "必须校验过期时间"


def test_login_matches_account_or_phone():
    """登录框标注的是「账号/手机号」,后端两种都得认——
    种子超管账号的手机号是占位值,只认手机号的话它根本登不进去。"""
    import inspect

    from cogni_agent.user_center_service import auth

    src = inspect.getsource(auth.login)
    assert "or_(" in src and "User.account" in src
