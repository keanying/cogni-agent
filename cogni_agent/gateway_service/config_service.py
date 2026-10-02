"""v1.0 APP 端：用户配置存储业务逻辑。

config 字段 JSONB 透传存储，后端不关心 JSON 内部结构。
对于 ota_credentials 类型，自动对 password 字段做 Fernet 加解密。
"""
from __future__ import annotations

from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from functools import lru_cache

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.gateway_service.schemas_app import CONFIG_TYPE_WHITELIST
from cogni_agent.user_center_service.models import AppConfig

log = get_logger("gateway.config_service")


@lru_cache
def _get_fernet() -> Fernet:
    """延迟初始化 Fernet，避免在模块导入时报错。"""
    key = get_settings().get("user_center", "credential_encrypt_key", default="")
    if not key:
        log.warning("credential_encrypt_key 未配置，密码字段将不做加密存储")
        # 生成一个临时 key 避免 Fernet 初始化失败（实际加密/解密时会被跳过）
        return Fernet(Fernet.generate_key())
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        log.warning("credential_encrypt_key 格式无效，密码字段将不做加密存储")
        return Fernet(Fernet.generate_key())


def _encrypt_passwords(config: dict) -> dict:
    """递归遍历 config，对所有 key 为 'password' 的值执行 Fernet 加密。"""
    if not config or not isinstance(config, dict):
        return config
    f = _get_fernet()
    result = {}
    for k, v in config.items():
        if isinstance(v, dict):
            result[k] = _encrypt_passwords(v)
        elif isinstance(v, list):
            result[k] = [_encrypt_passwords(item) if isinstance(item, dict) else item for item in v]
        elif k == "password" and isinstance(v, str) and v:
            try:
                result[k] = f.encrypt(v.encode()).decode()
            except Exception:
                log.warning("fernet encrypt failed for password field, storing as-is")
                result[k] = v
        else:
            result[k] = v
    return result


def _decrypt_passwords(config: dict) -> dict:
    """递归遍历 config，对所有 key 为 'password' 的值尝试 Fernet 解密。
    解密失败则保留原值（可能是明文或早期未加密数据）。"""
    if not config or not isinstance(config, dict):
        return config
    f = _get_fernet()
    result = {}
    for k, v in config.items():
        if isinstance(v, dict):
            result[k] = _decrypt_passwords(v)
        elif isinstance(v, list):
            result[k] = [_decrypt_passwords(item) if isinstance(item, dict) else item for item in v]
        elif k == "password" and isinstance(v, str) and v:
            try:
                result[k] = f.decrypt(v.encode()).decode()
            except Exception:
                # 解密失败 → 可能是明文或非 Fernet 数据，保留原值
                result[k] = v
        else:
            result[k] = v
    return result


async def get_config(
    db: AsyncSession, user_id: str, config_type: str
) -> dict | None:
    """获取指定类型的用户配置。不存在返回 None。"""
    stmt = select(AppConfig).where(
        AppConfig.user_id == user_id, AppConfig.config_type == config_type
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None

    config = row.config if isinstance(row.config, dict) else {}
    # OTA 凭证自动解密密码
    if config_type == "ota_credentials" and config:
        config = _decrypt_passwords(config)
    return {"config": config, "updated_at": _format_dt(row.updated_at)}


async def save_config(
    db: AsyncSession, user_id: str, config_type: str, config: dict
) -> str:
    """保存/覆盖用户配置。返回更新时间 ISO 字符串。"""
    if config_type not in CONFIG_TYPE_WHITELIST:
        raise ValueError(f"不支持的配置类型: {config_type}")

    now = datetime.now(timezone.utc)

    # OTA 凭证自动加密密码
    if config_type == "ota_credentials" and config:
        config = _encrypt_passwords(config)

    stmt = select(AppConfig).where(
        AppConfig.user_id == user_id, AppConfig.config_type == config_type
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    if row:
        row.config = config
        row.updated_at = now
    else:
        row = AppConfig(
            user_id=user_id, config_type=config_type, config=config,
            created_at=now, updated_at=now,
        )
        db.add(row)

    await db.commit()
    return _format_dt(now)


async def list_config_types(db: AsyncSession, user_id: str) -> list[dict]:
    """列出用户已有的配置类型（仅含类型名和更新时间，不含配置内容）。"""
    stmt = select(AppConfig).where(AppConfig.user_id == user_id).order_by(
        AppConfig.updated_at.desc()
    )
    result = await db.execute(stmt)
    return [
        {"config_type": row.config_type, "updated_at": _format_dt(row.updated_at)}
        for row in result.scalars().all()
    ]


def _format_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()
