"""OTA 账号(景区数字大脑):账号解析 + 存在性判定。

口径(25 号定稿,OTA 数据按「景区×账号」维度拆分——不同商户账号取到的数据不同):
- 账号标识 = app_config(config_type='ota_credentials').platforms.*.username
  (只看 username 字段,非空即有效账号;merchant_id 等其余字段不参与判定);
- 空壳配置(platforms 平台 dict 为空 / username 空)不算已配置——
  修复「{"platforms":{"ctrip":{}}} 也算有账号」的旧判定漏洞。

用途(预热侧与 APP 侧各取所需,**判定源唯一**):
- 预热侧(scenic_ota_accounts):该景区全部 active 用户的账号并集去重——
  ② 派单按「景区×账号」展开:OTA 主题问题每账号一行(零账号景区不派);
  概览每账号一行 + 一行账号无关基础版(未绑用户回退);
- APP 侧(user_ota_accounts):当前登录用户自己的账号——消费按
  「景区+当前用户账号」匹配缓存行,未绑定 = 未配置(NO_DATA 口径)。
  语义:账号是每个用户在 APP「我的→OTA账号配置」里自己填的商户账号。

边界说明(避免误读,承 22 号 §十):
  OTA 取数的实际维度是 supplierId——由「通过景区ID获取供应商ID列表」前置工具按
  x-param-source 执行期自动解析注入(见 runtime_service/engine/resolver.py)。
  account(商户账号)决定的是数据范围(账号看到自己的数据),supplierId 决定
  查询的供应商集合;两者共同构成一次 OTA 取数,故缓存维度 = 景区×账号。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.user_center_service.models import AppConfig, User

# app_config.config_type 取值(APP 端「我的 → OTA 账号配置」写入;白名单见 schemas_app
# CONFIG_TYPE_WHITELIST,password 字段由 config_service 自动 Fernet 加解密)
OTA_ACCOUNT_CONFIG_TYPE = "ota_credentials"

# 「未配置账号」无数据说明(APP 直出侧 no_data 结果用;未进 LLM,answer 即原因文案)
NO_OTA_ACCOUNT_ANSWER = "该景区尚未配置 OTA 账号,暂无数据。请在「我的 → OTA 账号配置」中完成配置后重试。"


def extract_accounts(config: dict | None) -> set[str]:
    """从 ota_credentials config 解析有效账号集合。

    只看 platforms.*.username(非空即有效);password/merchant_id 等不参与。
    空 config / 空平台 dict / username 全空 → 空集合(未配置)。
    """
    platforms = (config or {}).get("platforms") or {}
    accounts: set[str] = set()
    for cfg in platforms.values():
        if not isinstance(cfg, dict):
            continue
        username = (cfg.get("username") or "").strip()
        if username:
            accounts.add(username)
    return accounts


async def scenic_ota_accounts(db: AsyncSession, scenic_id: str | None) -> list[str]:
    """该景区全部 active 用户的 OTA 账号并集(去重,预热派单展开用)。

    无景区 / 无任何有效账号 → 空列表(预热侧据此跳过 OTA 主题派单)。
    """
    if not scenic_id:
        return []
    rows = (await db.execute(
        select(AppConfig.config)
        .join(User, User.id == AppConfig.user_id)
        .where(
            User.scenic_id == scenic_id,
            User.is_active.is_(True),
            AppConfig.config_type == OTA_ACCOUNT_CONFIG_TYPE,
        )
    )).scalars().all()
    accounts: set[str] = set()
    for cfg in rows:
        accounts |= extract_accounts(cfg if isinstance(cfg, dict) else None)
    return sorted(accounts)


async def user_ota_accounts(db: AsyncSession, user_id: str | None) -> list[str]:
    """当前登录用户自己配置的 OTA 账号(消费侧用户级判定与行匹配用)。

    APP 用户通常单账号;返回列表以兼容多平台/多账号演进。
    """
    if not user_id:
        return []
    rows = (await db.execute(
        select(AppConfig.config)
        .where(
            AppConfig.user_id == user_id,
            AppConfig.config_type == OTA_ACCOUNT_CONFIG_TYPE,
        )
    )).scalars().all()
    accounts: set[str] = set()
    for cfg in rows:
        accounts |= extract_accounts(cfg if isinstance(cfg, dict) else None)
    return sorted(accounts)


async def has_ota_account(db: AsyncSession, scenic_id: str | None) -> bool:
    """该景区下是否有任一 active 用户配置了有效 OTA 账号(景区级,预热侧用)。

    有效 = platforms.*.username 非空(空壳配置不算,2026-09-11 收紧)。
    消费侧请用 user_ota_accounts(用户级)。
    """
    return bool(await scenic_ota_accounts(db, scenic_id))


async def ota_no_data_answer(db: AsyncSession, user_id: str | None) -> str | None:
    """直出侧轻量拦截:用户未绑 OTA 账号 → 统一「未配置」文案(None = 正常)。

    预热缓存命中是零开销快路径,故这里只跑账号存在性单项判定,不做任何取数。
    """
    if not user_id:
        return None
    if await user_ota_accounts(db, user_id):
        return None
    return NO_OTA_ACCOUNT_ANSWER
