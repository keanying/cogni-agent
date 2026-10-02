#!/usr/bin/env python
"""
G-ROLE-02:超级管理员种子账号初始化 / 密码重置。

    uv run python scripts/seed_superadmin.py                      # 不存在则创建(用配置里的初始密码)
    uv run python scripts/seed_superadmin.py --reset-password     # 已存在时把密码重置回配置里的初始密码
    uv run python scripts/seed_superadmin.py --password pftroot123 --no-force-change
                                                                  # 指定密码,且登录后不强制改密
    uv run python scripts/seed_superadmin.py --account pftroot --phone 13800000000

账号默认 pftroot、密码取 config.yaml 的 user_center.super_admin_seed.initial_password
(默认 0000@123),scenic_id 置空(平台级,不属于任何景区)。
重置会同时:解禁账号(is_active=True)、清空 last_login_at(视同首次登录)、
更新 credentials_changed_at(旧 token 立即失效)。重复执行安全。
"""
import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.config import get_settings
from cogni_agent.user_center_service.auth import hash_password
from cogni_agent.user_center_service.models import User


async def main(reset_password: bool = False, password: str | None = None,
               force_change: bool | None = None, account: str | None = None,
               phone: str | None = None) -> None:
    """创建超管种子账号,或重置它的密码。

    reset_password / password:账号已存在时才有意义——把密码重置成指定值(或配置里的初始密码),
    并重新置上「首次登录须改密」(除非显式 --no-force-change)。忘记密码时用这个,
    而不是去手工改数据库里的哈希(手工改极易写进一个格式不对的哈希,之后表现为"密码怎么都不对")。
    """
    seed_cfg = get_settings().get("user_center", "super_admin_seed", default={})
    account = account or seed_cfg.get("account", "pftroot")
    initial_password = password or seed_cfg.get("initial_password", "0000@123")
    if force_change is None:
        force_change = bool(seed_cfg.get("force_change_on_first_login", True))
    reset = reset_password or bool(password)

    async with get_sessionmaker()() as db:
        existing = (await db.execute(select(User).where(User.account == account))).scalar_one_or_none()
        if existing:
            if not reset:
                print(f"✓ 超级管理员账号 {account} 已存在,跳过"
                      f"(忘记密码可加 --reset-password,或 --password <新密码> 指定)")
                return
            existing.password_hash = hash_password(initial_password)
            existing.must_change_password = force_change
            existing.is_active = True
            existing.last_login_at = None   # 重置后视同首次登录
            existing.credentials_changed_at = datetime.now(timezone.utc)   # 旧令牌立即失效
            await db.commit()
            print(f"✓ 已把 {account} 的密码重置为:{initial_password}"
                  + ("(首次登录会强制要求修改)" if force_change else "(不强制改密,可直接使用)"))
            return
        user = User(
            name="平台超级管理员",
            account=account,
            phone=phone or "00000000000",  # 占位手机号,建议部署后立即在系统设置里改为真实手机号
            password_hash=hash_password(initial_password),
            scenic_id="",            # 平台级,不属于任何景区
            role="super_admin",
            must_change_password=force_change,
        )
        db.add(user)
        await db.commit()
        print(f"✓ 已创建超级管理员账号 {account} / 初始密码 {initial_password}"
              + ("(首次登录将强制要求修改密码)" if force_change else "(不强制改密)"))


def cli() -> None:
    ap = argparse.ArgumentParser(description="超级管理员种子账号初始化 / 密码重置")
    ap.add_argument("--reset-password", action="store_true", help="账号已存在时重置为配置里的初始密码")
    ap.add_argument("--password", default=None, help="指定密码(等价于 --reset-password 并用这个值)")
    ap.add_argument("--no-force-change", action="store_true", help="不要求首次登录修改密码")
    ap.add_argument("--account", default=None, help="账号名(默认取配置 super_admin_seed.account)")
    ap.add_argument("--phone", default=None, help="新建账号时的手机号(默认 00000000000 占位)")
    args = ap.parse_args()
    asyncio.run(main(
        reset_password=args.reset_password,
        password=args.password,
        force_change=False if args.no_force_change else None,
        account=args.account,
        phone=args.phone,
    ))


if __name__ == "__main__":
    cli()
