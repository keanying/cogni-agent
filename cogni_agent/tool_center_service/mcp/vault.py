"""M5-09:凭证保管库——加密存储外部工具/API 凭证,调用时注入,前端/Agent 不可见明文。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cryptography.fernet import Fernet

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.tool_center_service.tool.models import ToolCredential

log = get_logger("tool_center_service.mcp.vault")

_PLACEHOLDER = "{{credential}}"

# §10.1 M0(设计收紧 v0.1.4):凭证一律归属具体用户,**不存在共享凭证**。
# user_id 必须非空;scope 缺省为 default。降级链仅两级:
#   ① (user_id, scope)  用户私有 × 指定场景
#   ② (user_id, default) 用户私有 × 默认场景
DEFAULT_CREDENTIAL_SCOPE = "default"

_fernet_singleton: Fernet | None = None


def _get_fernet() -> Fernet:
    """
    关键修复:此前每次调用在密钥未配置时都会 `Fernet.generate_key()` 现生成一个
    随机密钥——同一进程内两次调用生成的密钥都不一样,意味着刚加密的凭证下一次
    (甚至同一请求的下一步)就解密不出来,进程重启后更是必然全部作废(相当于凭证库
    静默丢数据)。现在密钥未配置时直接抛错,倒逼必须在 .env 里配置一个真实、稳定的
    CREDENTIAL_FERNET_KEY(生成方法:`python -c "from cryptography.fernet import
    Fernet; print(Fernet.generate_key().decode())"`),而不是带着一个"看起来能跑但
    过会儿就会莫名其妙解密失败"的定时炸弹。
    """
    global _fernet_singleton
    if _fernet_singleton is not None:
        return _fernet_singleton
    key = get_settings().get("tool_center", "credential_encrypt_key")
    if not key:
        raise RuntimeError(
            "未配置 CREDENTIAL_FERNET_KEY(凭证库加密密钥)。请生成一个并写入 .env:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "绝不要在未配置真实密钥的情况下使用凭证加密功能——每次进程重启都会导致已加密的凭证永久无法解密。"
        )
    _fernet_singleton = Fernet(key.encode())
    return _fernet_singleton


def encrypt_credential(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_credential(cipher: str) -> str:
    return _get_fernet().decrypt(cipher.encode()).decode()


def _substitute(value, secret: str):
    if isinstance(value, str):
        return value.replace(_PLACEHOLDER, secret) if _PLACEHOLDER in value else value
    if isinstance(value, dict):
        return {k: _substitute(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, secret) for v in value]
    return value


async def _pick_credential(db, tool_id: str, user_id: str | None, scope: str) -> ToolCredential | None:
    """按 §10.1(v0.1.4 收紧)两级降级链取凭证:

    1. (user_id, scope)   用户私有 × 指定场景
    2. (user_id, default) 用户私有 × 默认场景

    user_id 为空(无调用者身份)时直接视为无凭证——**不存在共享凭证,不能降级到
    别人的或公共的账号**,调用方应引导用户交互提供。

    命中即返回该行,不再继续下探。
    """
    if not user_id:
        return None
    levels = [
        (user_id, scope),
        (user_id, DEFAULT_CREDENTIAL_SCOPE),
    ]
    for uid, sc in levels:
        stmt = select(ToolCredential).where(
            ToolCredential.tool_id == tool_id,
            ToolCredential.scope == sc,
            ToolCredential.user_id == uid,
        )
        row = (await db.execute(stmt.order_by(ToolCredential.created_at.desc()))).scalars().first()
        if row is not None:
            return row
    return None


async def inject_credential(
    tool_id: str,
    type_config: dict,
    params: dict,
    *,
    user_id: str | None = None,
    scope: str | None = None,
) -> tuple[dict, dict]:
    """
    工具的 headers/请求体等配置里凡是写了字面量 `{{credential}}` 占位符的地方,
    在真正发起调用前替换成解密后的凭证明文——替换只发生在这个函数内部构造出的
    临时副本上,原始 type_config(数据库里的那份)永远不会被改写成明文。
    没有为该工具登记凭证(tc_credentials 无记录)时原样返回,不阻断调用(允许
    无需鉴权的公开接口)。

    §10.1 M0(v0.1.4):凭证必属具体用户,按 (user_id, scope) 走两级降级链
    (见 _pick_credential)。无调用者身份(user_id 为空)或该用户无凭证时不注入,
    原样返回——由模型引导用户交互提供账号密码,绝不降级到共享/他人凭证。
    """
    scope = scope or DEFAULT_CREDENTIAL_SCOPE
    async with get_sessionmaker()() as db:
        row = await _pick_credential(db, tool_id, user_id, scope)
    if row is None:
        return type_config, params
    try:
        secret = decrypt_credential(row.cipher_text)
    except Exception:  # noqa: BLE001 - 凭证损坏/密钥轮换不一致时不应让整次调用崩溃
        log.exception(f"tool {tool_id} 凭证解密失败,按无凭证处理")
        return type_config, params
    return _substitute(type_config, secret), _substitute(params, secret)


async def save_credential(
    tool_id: str,
    secret: str,
    *,
    user_id: str | None = None,
    scope: str | None = None,
    created_by: str = "",
    db: AsyncSession | None = None,
) -> None:
    """§10.2(v0.1.4)保存/覆盖指定三元组 (tool_id, user_id, scope) 的凭证。

    - **user_id 必填**:凭证一律归属具体用户,拒绝保存共享凭证(NULL);
    - secret 为组合明文(约定 `username:password`),调用处负责拼装;
    - 明文在此函数内立即 Fernet 加密,不外泄;
    - upsert 语义:同一三元组已存在则覆盖密文(用户改密码场景);
    - 传入 db 时复用调用方会话(与 §10.3 动作 3 的 Task 脱敏同事务),由调用方负责 commit;
      未传时用独立会话并自行 commit(兼容既有调用方式)。
    """
    scope = scope or DEFAULT_CREDENTIAL_SCOPE
    uid = user_id.strip() if isinstance(user_id, str) else None
    if not uid:
        raise ValueError("凭证必须归属具体用户(user_id 不能为空),平台不保存共享凭证")
    cipher = encrypt_credential(secret)

    own_session = db is None
    session = db if db is not None else get_sessionmaker()()
    try:
        stmt = select(ToolCredential).where(
            ToolCredential.tool_id == tool_id,
            ToolCredential.scope == scope,
        )
        if uid is None:
            stmt = stmt.where(ToolCredential.user_id.is_(None))
        else:
            stmt = stmt.where(ToolCredential.user_id == uid)
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            session.add(ToolCredential(
                tool_id=tool_id, user_id=uid, scope=scope,
                cipher_text=cipher, created_by=created_by,
            ))
        else:
            row.cipher_text = cipher
            row.created_by = created_by
        if own_session:
            await session.commit()
    finally:
        if own_session:
            await session.close()
