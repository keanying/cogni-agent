#!/usr/bin/env python
"""
登录问题一键诊断。

    uv run python scripts/diagnose_login.py pftroot 0000@123

登录返回 401 只有两种原因(账号密码不对 / 需要短信验证码),但从日志上看是一样的
HTTP 401,很难判断。这个脚本直接查你**当前连的那个库**和**当前生效的配置**,
把每一环都验一遍,明确告诉你卡在哪。不会修改任何数据(只读 + 一次可选的发码)。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import or_, select, text

from cogni_agent.common.db import get_engine, get_sessionmaker
from cogni_agent.config import get_settings
from cogni_agent.user_center_service.models import SmsCode, User

OK, BAD, WARN = "  ✓", "  ✗", "  !"


async def main(account: str, password: str | None) -> int:
    print("=" * 68)
    print("登录诊断")
    print("=" * 68)

    # ── 1. 连的是哪个库 ──
    async with get_engine().begin() as conn:
        db_name = (await conn.execute(text("SELECT current_database()"))).scalar()
        n_users = (await conn.execute(text("SELECT count(*) FROM numeri_core_usr_users"))).scalar()
    print(f"\n[1] 数据库")
    print(f"{OK} 当前连接:{db_name}(用户表共 {n_users} 条记录)")
    if n_users == 0:
        print(f"{BAD} 一个用户都没有 —— 先运行:uv run python scripts/init_db.py --with-seed")
        return 1

    # ── 2. 账号是否存在、是否可用 ──
    print(f"\n[2] 账号")
    async with get_sessionmaker()() as db:
        user = (await db.execute(
            select(User).where(or_(User.phone == account, User.account == account))
        )).scalars().first()
    if not user:
        print(f"{BAD} 库里找不到账号或手机号为「{account}」的用户")
        async with get_sessionmaker()() as db:
            rows = (await db.execute(select(User.account, User.phone).limit(10))).all()
        print(f"     现有账号:{[f'{a}({p})' for a, p in rows]}")
        return 1
    print(f"{OK} 找到用户:account={user.account} phone={user.phone} role={user.role}")
    if not user.is_active:
        print(f"{BAD} 该账号 is_active=False,登录一定失败(会报「账号/手机号或密码错误」)")
        return 1
    print(f"{OK} is_active=True")
    print(f"     last_login_at={user.last_login_at}"
          f"  → {'首次登录' if user.last_login_at is None else '非首次登录'}")
    if user.must_change_password:
        print(f"{WARN} must_change_password=True:登录能成功,但前端应引导修改密码")

    # ── 3. 密码哈希是否可用(bcrypt 版本问题会在这里暴露)──
    print(f"\n[3] 密码校验")
    try:
        from cogni_agent.user_center_service.auth import verify_password

        if password is None:
            print(f"{WARN} 未提供密码,跳过校验(用法:diagnose_login.py <账号> <密码>)")
        elif verify_password(password, user.password_hash):
            print(f"{OK} 密码正确")
        else:
            print(f"{BAD} 密码不匹配 —— 这就是 401 的原因")
            print(f"     种子账号初始密码见 config/config.yaml 的 super_admin_seed;若已改过密码请用新密码。")
            print(f"     要重置:uv run python scripts/seed_superadmin.py --reset-password")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} 密码校验过程本身报错:{type(exc).__name__}: {exc}")
        print(f"     这通常是 passlib 与 bcrypt 版本不兼容(bcrypt>=4.1 会破坏 passlib 1.7.4)。")
        print(f"     修复:uv sync(pyproject.toml 已钉住 bcrypt>=4.0,<4.1)")
        return 1

    # ── 4. 验证码策略 ──
    print(f"\n[4] 短信验证码策略")
    require_sms = get_settings().get("user_center", "require_sms_on_first_login", default=False)
    print(f"     config.yaml: user_center.require_sms_on_first_login = {require_sms}")
    is_first = user.last_login_at is None
    if not require_sms:
        print(f"{OK} 已关闭 —— 登录只需账号+密码,不会因为验证码被拒")
        if password:
            print(f"\n{OK} 结论:凭据没问题,应该能登录成功。若仍 401,请确认前端请求打到的是"
                  f"\n     同一个后端实例(检查 vite 代理 / VITE_API_BASE_URL)。")
        return 0
    if not is_first:
        print(f"{OK} 已开启,但该用户不是首次登录 —— 本次不会要求验证码")
        return 0

    print(f"{WARN} 已开启且该用户是首次登录 —— **必须**带上有效验证码才能登录")

    # ── 5. 现有验证码状态 ──
    print(f"\n[5] 该手机号最近的验证码(注意:是 user.phone={user.phone},不是你输入的账号)")
    async with get_sessionmaker()() as db:
        codes = (await db.execute(
            select(SmsCode).where(SmsCode.phone == user.phone, SmsCode.purpose == "login")
            .order_by(SmsCode.created_at.desc()).limit(5)
        )).scalars().all()
    if not codes:
        print(f"{BAD} 一条都没有。若你调用过 /auth/sms/send 却没有记录,检查请求里的 phone 参数。")
        print(f"     提示:发码接口会把账号解析成真实手机号再落库,所以传 account 也是可以的。")
        return 1
    now = datetime.now(timezone.utc)
    for c in codes:
        state = "已使用" if c.consumed else ("已过期" if c.expires_at < now else "可用")
        print(f"     code={c.code} 状态={state} 过期时间={c.expires_at}")
    usable = [c for c in codes if not c.consumed and c.expires_at >= now]
    if usable:
        print(f"\n{OK} 有 {len(usable)} 个可用验证码,最新的是 {usable[0].code}")
        print(f"     用它登录:phone={account} password=*** sms_code={usable[0].code}")
    else:
        print(f"\n{BAD} 没有可用验证码(都已使用或已过期,有效期 5 分钟)")
        print(f"     重新获取:POST /api/auth/sms/send?phone={account}&purpose=login")
    print(f"\n     不想用验证码?把 config.yaml 里 user_center.require_sms_on_first_login 改成 false")
    return 0


if __name__ == "__main__":
    acct = sys.argv[1] if len(sys.argv) > 1 else "pftroot"
    pwd = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(asyncio.run(main(acct, pwd)))
