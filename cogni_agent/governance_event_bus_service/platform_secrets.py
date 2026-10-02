"""平台级共用密钥:在管理后台「系统设置」里配,不必改服务器环境变量。

目前只有一把:数据平台 xpftkey(data_platform_key)。它被这些地方用到:
  - 30 个数据指标接口工具的请求头 `xpftkey: {{data_platform_key}}`(沙箱发请求时替换);
  - 内置 sys_supplier_ids(票务类 supplierId 的来源)与 sys_quickbi_link。

**为什么不只靠环境变量**:环境变量要改部署、要重启,运营碰不到;
而配错或没配的表现是「票务数据全部查不了」—— 现场就是这样,问「在哪里设置」没有一个页面可以指。

取值顺序:后台配置 > 环境变量(config.yaml → tool_center.secrets)。
存储:加密落库(与模型 API Key 同一套 vault),接口只回「配没配 / 来源 / 末四位」,**永不回明文**。
多进程 / 多实例:每个进程缓存 30 秒,改了之后最迟 30 秒各处生效,不需要重启。
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import DateTime, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger

log = get_logger("governance.platform_secrets")

# 允许在后台配置的密钥(白名单:不接受任意名字,免得这里变成一个什么都能塞的保险箱)
KNOWN: dict[str, dict[str, str]] = {
    "data_platform_key": {
        "label": "数据平台密钥(xpftkey)",
        "env": "DATA_PLATFORM_XPFTKEY",
        "used_by": "数据指标接口工具(票务 / 客群 / 舆情 / OTA)、内置 sys_supplier_ids、sys_quickbi_link",
    },
}

_TTL = 30.0
_cache: dict[str, str] = {}
_loaded_at = 0.0


class PlatformSecret(Base):
    __tablename__ = table_name("gov_platform_secrets")

    name: Mapped[str] = mapped_column(String(64), primary_key=True)          # 密钥名,取值见 KNOWN
    value_cipher: Mapped[str] = mapped_column(String(2048))                   # 加密后的值(vault)
    updated_by: Mapped[str] = mapped_column(String(36), default="")           # 最后修改人
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


def _env_value(name: str) -> str:
    from cogni_agent.config import get_settings

    return str((get_settings().get("tool_center", "secrets", default={}) or {}).get(name) or "")


async def refresh(force: bool = False) -> None:
    """从库里重新载入(30 秒内不重复查)。库不可用时保留旧缓存,不影响调用。"""
    global _cache, _loaded_at
    if not force and time.monotonic() - _loaded_at < _TTL:
        return
    from cogni_agent.tool_center_service.mcp.vault import decrypt_credential

    try:
        async with get_sessionmaker()() as db:
            rows = (await db.execute(select(PlatformSecret))).scalars().all()
        fresh = {}
        for r in rows:
            try:
                fresh[r.name] = decrypt_credential(r.value_cipher)
            except Exception as exc:  # noqa: BLE001 - 密钥轮换过 vault 主密钥时解不开,按未配置处理
                log.warning(f"平台密钥 {r.name} 解密失败,按未配置处理:{type(exc).__name__}")
        _cache, _loaded_at = fresh, time.monotonic()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"平台密钥载入失败,沿用缓存:{type(exc).__name__}: {exc}")
        _loaded_at = time.monotonic()


def get(name: str) -> str:
    """当前生效的值(后台配置优先,其次环境变量);都没有返回空串。调用前先 await refresh()。"""
    return _cache.get(name) or _env_value(name)


def all_values() -> dict[str, str]:
    """沙箱替换请求头用:所有已知密钥的生效值(含环境变量里配了、后台没配的)。"""
    from cogni_agent.config import get_settings

    names = set(KNOWN) | set((get_settings().get("tool_center", "secrets", default={}) or {}).keys())
    return {n: get(n) for n in names}


def source(name: str) -> str:
    """admin(后台配置)/ env(环境变量)/ ''(都没配)"""
    if _cache.get(name):
        return "admin"
    return "env" if _env_value(name) else ""


def _mask(v: str) -> str:
    return f"****{v[-4:]}" if len(v) >= 8 else "****"


async def describe() -> list[dict]:
    await refresh(force=True)
    async with get_sessionmaker()() as db:
        rows = {r.name: r for r in (await db.execute(select(PlatformSecret))).scalars().all()}
    out = []
    for name, meta in KNOWN.items():
        v = get(name)
        r = rows.get(name)
        out.append({"name": name, **meta, "configured": bool(v), "source": source(name),
                    "masked": _mask(v) if v else "",
                    "updated_at": r.updated_at.isoformat() if r and r.updated_at else None,
                    "updated_by": r.updated_by if r else None})
    return out


async def set_value(db: AsyncSession, name: str, value: str, *, user_id: str) -> None:
    from cogni_agent.tool_center_service.mcp.vault import encrypt_credential

    if name not in KNOWN:
        raise ValueError(f"不支持的平台密钥:{name}")
    value = (value or "").strip()
    if not value:
        raise ValueError("值不能为空;要清除请用「清除」")
    row = await db.get(PlatformSecret, name)
    if row is None:
        db.add(PlatformSecret(name=name, value_cipher=encrypt_credential(value), updated_by=user_id))
    else:
        row.value_cipher, row.updated_by = encrypt_credential(value), user_id
    await db.commit()
    await refresh(force=True)
    _clear_dependent_caches()


async def clear_value(db: AsyncSession, name: str) -> None:
    row = await db.get(PlatformSecret, name)
    if row is not None:
        await db.delete(row)
        await db.commit()
    await refresh(force=True)
    _clear_dependent_caches()


def _clear_dependent_caches() -> None:
    """换了 key,按旧 key 查到(或没查到)的供应商结果不能再用。"""
    try:
        from cogni_agent.tool_center_service import builtin_tools as bi

        bi._SUPPLIER_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass
