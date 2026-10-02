"""
M2-02/03/05:登录鉴权核心逻辑——密码校验、短信验证、JWT 签发/刷新。
这是脚手架中第一条完整跑通的垂直切面:gateway → user_center → db,
其余模块可参照本文件的分层方式(routes 薄、逻辑在 service 函数里)实现。
"""
from __future__ import annotations

import base64
import random
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.backends import default_backend
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import Unauthorized
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.user_center_service.models import LoginDevice, SmsCode, User

log = get_logger("user_center_service.auth")
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return _pwd_ctx.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return _pwd_ctx.verify(raw, hashed)


async def send_sms_code(db: AsyncSession, phone: str, purpose: str) -> None:
    """
    M2-03:发送短信验证码。
    真实实现应对接 config.yaml: user_center.sms.provider(阿里云/腾讯云),
    此处脚手架仅落库(mock 场景直接打印到日志,方便本地联调不用真实发短信)。
    """
    code = f"{random.randint(0, 999999):06d}"
    record = SmsCode(
        phone=phone, code=code, purpose=purpose, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)
    )
    db.add(record)
    await db.commit()
    log.info(f"[mock-sms] phone={phone} purpose={purpose} code={code}")  # TODO: 接真实短信厂商 SDK


async def verify_sms_code(db: AsyncSession, phone: str, code: str, purpose: str) -> bool:
    stmt = (
        select(SmsCode)
        .where(SmsCode.phone == phone, SmsCode.code == code, SmsCode.purpose == purpose, SmsCode.consumed.is_(False))
        .order_by(SmsCode.created_at.desc())
    )
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()
    if not record or record.expires_at < datetime.now(timezone.utc):
        return False
    record.consumed = True
    await db.commit()
    return True


def issue_tokens(user: User) -> tuple[str, str]:
    """M2-05:签发 access_token(短期)+ refresh_token(长期),内含 user_id/scenic_id/role。"""
    cfg = get_settings().get("user_center", default={})
    secret = cfg["jwt_secret"]
    algo = cfg.get("jwt_algorithm", "HS256")
    now = datetime.now(timezone.utc)

    access_payload = {
        "sub": user.id,
        "scenic_id": user.scenic_id,
        "role": user.role,
        "exp": now + timedelta(minutes=cfg.get("access_token_expire_minutes", 60)),
        # 签发时间。校验时与 users.credentials_changed_at 比对——改密后旧令牌立即失效
        "iat": now,
    }
    refresh_payload = {"sub": user.id, "type": "refresh", "iat": now,
                       "exp": now + timedelta(days=cfg.get("refresh_token_expire_days", 14))}
    return jwt.encode(access_payload, secret, algorithm=algo), jwt.encode(refresh_payload, secret, algorithm=algo)


async def login(
    db: AsyncSession,
    phone: str,
    password: str,
    sms_code: str | None,
    device_info: str,
    ip: str,
    device_id: str | None = None,
    device_os: str | None = None,
    push_token: str | None = None,
    app_version: str | None = None,
):
    """M2-02:完整登录流程,对应「登录页参照」页(P02)右侧的五步时间线。
    传入的 phone 参数按账号或手机号双路匹配(前端登录框标注的是"账号/手机号",
    种子超级管理员账号 pftroot 用的就是占位手机号 00000000000,不支持账号登录的话
    这个字段名会跟前端提示对不上,所以这里两种都认)。"""
    stmt = select(User).where(or_(User.phone == phone, User.account == phone), User.is_active.is_(True))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise Unauthorized("账号/手机号或密码错误")

    # 判断是否新设备/首次登录 -> 按配置决定是否强制短信验证(M2-03)。
    # 默认关闭(user_center.require_sms_on_first_login=false)——短信通道目前是
    # mock 实现,没有真实送达能力前强制要求只会把人挡在门外,不是真的安全加固。
    require_sms = get_settings().get("user_center", "require_sms_on_first_login", default=False)
    is_first_login = user.last_login_at is None
    if require_sms and is_first_login:
        if not sms_code or not await verify_sms_code(db, user.phone, sms_code, "login"):
            raise Unauthorized("需要短信验证码")

    # 记录登录设备(M2-04,v1.0 增强:存储设备完整指纹用于多端管理和消息推送)
    db.add(LoginDevice(
        user_id=user.id,
        device_info=device_info,
        ip=ip,
        device_id=device_id,
        device_os=device_os,
        push_token=push_token,
        app_version=app_version,
    ))
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    access, refresh = issue_tokens(user)
    log.info(f"user {user.account} logged in from {ip}")
    return access, refresh, user.must_change_password


async def refresh_access_token(db: AsyncSession, refresh_token: str) -> tuple[str, str, bool]:
    """用 refresh_token 换取新的 access_token + 滚动刷新 refresh_token。

    校验流程:
    1. 解码 refresh_token,确认 type=refresh
    2. 查找用户,确认账号存在且启用
    3. 确认令牌签发时间不早于 credentials_changed_at(改密后旧令牌失效)
    4. 签发全新的一对 token(旧 refresh_token 作废)
    """
    from datetime import timezone

    cfg = get_settings().get("user_center", default={})
    secret = cfg["jwt_secret"]
    algo = cfg.get("jwt_algorithm", "HS256")

    try:
        payload = jwt.decode(refresh_token, secret, algorithms=[algo])
    except Exception:
        raise Unauthorized("无效的 refresh_token")

    if payload.get("type") != "refresh":
        raise Unauthorized("非法的令牌类型")

    user_id = payload.get("sub")
    if not user_id:
        raise Unauthorized("无效的令牌载荷")

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise Unauthorized("用户不存在或已禁用")

    # 签发时间早于 credentials_changed_at 的令牌一律拒绝(改密/重置密码后旧 refresh 不应再生效)
    iat = payload.get("iat")
    if iat:
        changed = getattr(user, "credentials_changed_at", None)
        if changed:
            # 列是 timestamptz,驱动返回的通常已经带时区。对带时区的值用 replace(tzinfo=utc)
            # 会把「+08:00 的 10 点」当成「UTC 的 10 点」,凭空多出 8 小时 ——
            # 表现就是改密后一段时间内**刚签发的 refresh 也被判失效**。统一换算,不改挂钟。
            changed_utc = (changed.astimezone(timezone.utc) if changed.tzinfo
                           else changed.replace(tzinfo=timezone.utc))
            # 与 deps._reject_if_credentials_changed 同口径:iat 是整数秒,截断到秒再比,
            # 免得 sub-second 精度差误杀刚签发的令牌。
            if float(iat) < int(changed_utc.timestamp()):
                raise Unauthorized("令牌已失效(密码已变更)")

    access, refresh = issue_tokens(user)
    log.info(f"user {user.account} refreshed tokens")
    return access, refresh, user.must_change_password


# ═══════════════════════════════════════════════════════════════
# RSA 密码加密传输：前端用公钥加密，后端用私钥解密。
# 防被动网络嗅探和日志明文泄露，不等同于 HTTPS。
# ═══════════════════════════════════════════════════════════════

_rsa_private_key = None
_rsa_public_key_pem = None


def _load_rsa_keys():
    """懒加载 RSA 密钥对（各环境独立密钥对，不通用）。"""
    global _rsa_private_key, _rsa_public_key_pem
    if _rsa_private_key is not None:
        return
    cfg = get_settings().get("user_center", default={})
    private_b64 = cfg.get("rsa_private_key", "")
    public_b64 = cfg.get("rsa_public_key", "")
    if private_b64:
        private_pem = base64.b64decode(private_b64)
        _rsa_private_key = serialization.load_pem_private_key(private_pem, password=None, backend=default_backend())
    if public_b64:
        _rsa_public_key_pem = base64.b64decode(public_b64).decode()


def get_public_key_pem() -> str:
    """获取 PEM 格式公钥，供前端加密密码使用。"""
    _load_rsa_keys()
    if not _rsa_public_key_pem:
        raise Unauthorized("RSA 公钥未配置")
    return _rsa_public_key_pem


def decrypt_password(encrypted_b64: str) -> str:
    """用 RSA 私钥解密前端传来的加密密码。"""
    _load_rsa_keys()
    if not _rsa_private_key:
        raise Unauthorized("RSA 私钥未配置")
    ciphertext = base64.b64decode(encrypted_b64)
    plaintext = _rsa_private_key.decrypt(
        ciphertext,
        asymmetric_padding.OAEP(
            mgf=asymmetric_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plaintext.decode("utf-8")


def try_decrypt_password(field_value: str, rsa_encrypt: bool = False) -> str:
    """如果 rsa_encrypt 标记为 true，对字段值做 RSA 解密；否则原样返回。
    兼容老版本客户端未加密时的明文传输。
    """
    if not rsa_encrypt:
        return field_value
    return decrypt_password(field_value)
