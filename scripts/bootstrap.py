#!/usr/bin/env python
"""一条命令完成「环境体检 → 数据库结构检查/迁移 → 种子数据」,新老库都适用。

    # 老库(表已经存在):检查 + 补迁移 + 保证超管可登录
    uv run python scripts/bootstrap.py --admin-password pftroot123 --no-force-change

    # 全新环境:顺带建库、生成缺失密钥
    uv run python scripts/bootstrap.py --create-db --fix-env --admin-password pftroot123

    # 只体检不改任何东西(先看看差什么)
    uv run python scripts/bootstrap.py --check

做的事,按顺序:
  1. 环境变量体检(scripts/init_env.py):缺 JWT/凭证主密钥/RSA 密钥对时可 --fix-env 自动生成写回 .env;
     RSA 密钥对缺失时登录页拿不到公钥,GET /api/admin/auth/public-key 会一直 401。
  2. 数据库结构同步(cogni_agent/common/schema.sync_schema):
     全新库 = create_all 建基线 + 记录整条迁移链;老库 = 只按 MIGRATIONS 增量演进,
     跑完做只读体检(缺表/冗余表/缺列/冗余列/类型漂移),不一致直接失败并打印清单。
  3. 种子数据:权限策略(Casbin)+ 超级管理员账号(可指定密码)。

失败时不会留下半截状态:结构同步在一个事务里,失败整体回滚,补好迁移重跑即可。
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _step(n: int, title: str) -> None:
    print(f"\n{'═' * 62}\n[{n}/3] {title}\n{'═' * 62}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="cogni-agent 初始化 / 维护")
    ap.add_argument("--check", action="store_true", help="只体检,不做任何修改")
    ap.add_argument("--fix-env", action="store_true", help="自动生成缺失的密钥并写回 .env")
    ap.add_argument("--create-db", action="store_true", help="数据库不存在时自动创建并装 pgvector")
    ap.add_argument("--skip-seed", action="store_true", help="跳过种子数据(权限策略 + 超管账号)")
    ap.add_argument("--admin-password", default=None, help="超管密码(账号已存在时等同重置)")
    ap.add_argument("--no-force-change", action="store_true", help="超管首次登录不强制改密")
    ap.add_argument("--account", default=None, help="超管账号名(默认取配置)")
    ap.add_argument("--env", default=None,
                    help="环境名,对应 .env.{env};不传则跟随 ZHILV_ENV(未设置即 .env,与服务读取口径一致)")
    args = ap.parse_args()

    # ── 1. 环境变量 ────────────────────────────────────────────────
    _step(1, "环境变量体检")
    env_cmd = [sys.executable, str(ROOT / "scripts" / "init_env.py")]
    # 不传 --env 就让 init_env.py 自己跟随 ZHILV_ENV —— 必须与服务读 .env 的口径一致,
    # 否则密钥写进了一个服务根本不读的文件(登录页仍旧 401,而脚本显示「已生成」)。
    if args.env is not None:
        env_cmd += ["--env", args.env]
    if args.fix_env and not args.check:
        env_cmd.append("--write")
    env_rc = subprocess.run(env_cmd, cwd=str(ROOT)).returncode
    if env_rc != 0:
        if not args.check:
            print("\n✗ 环境变量未就绪。补齐后重跑;密钥类可加 --fix-env 自动生成。")
            return 1
        # --check 是诊断模式:环境有问题也继续往下查数据库,一次把问题看全
        print("\n⚠ 环境变量未就绪(见上),继续检查数据库 …")
    else:
        # 「文件里有值」不等于「服务能用」:环境名不对 / 值格式不对都会让前者成立而后者不成立。
        # 这一步走服务自身的 config+auth 链路做加解密往返,把登录页 401 挡在部署阶段。
        from scripts.init_env import verify_rsa

        verify_rsa()

    # ── 2. 数据库结构 ──────────────────────────────────────────────
    _step(2, "数据库结构检查 / 迁移")
    # 延迟导入:上一步可能刚写入 .env,这里才第一次加载配置
    from cogni_agent.common.schema import DatabaseUnreachable, MIGRATIONS, sync_schema

    if args.check:
        from scripts.init_db import check_only

        rc = await check_only()
        print(f"\n(迁移链共 {len(MIGRATIONS)} 条;--check 模式不做任何修改)")
        return rc or env_rc

    if args.create_db:
        from scripts.init_db import ensure_database_exists

        try:
            await ensure_database_exists()
        except DatabaseUnreachable as exc:
            print(f"\n✗ {exc}\n")
            return 1
    try:
        stats = await sync_schema()
    except DatabaseUnreachable as exc:
        print(f"\n✗ {exc}\n")
        return 1
    except RuntimeError as exc:
        # 结构体检不通过:清单已在异常里,原样打出来,不要被堆栈埋掉
        print(f"\n✗ {exc}\n")
        print("处理方式:按清单到 cogni_agent/common/schema.py 的 MIGRATIONS 末尾追加幂等迁移后重跑。")
        return 1
    print(f"✓ 结构就绪:共 {stats['total_tables']} 张表,"
          + (f"全新库建基线 {len(stats['created_tables'])} 张," if stats["created_tables"] else "")
          + f"本次应用迁移 {len(stats['applied_migrations'])} 条"
          + (f"({', '.join(stats['applied_migrations'])})" if stats["applied_migrations"] else "(已是最新)")
          + ",结构体检通过")

    # ── 3. 种子数据 ────────────────────────────────────────────────
    if args.skip_seed:
        print("\n· 按 --skip-seed 跳过种子数据")
    else:
        _step(3, "种子数据(权限策略 + 超级管理员)")
        from cogni_agent.governance_event_bus_service.policy import seed_policies

        pol = seed_policies()
        print(f"✓ 权限策略:{pol['total_policies']} 条权限、{pol['total_grouping']} 条角色继承")
        from scripts.seed_superadmin import main as seed_admin

        await seed_admin(password=args.admin_password,
                         force_change=False if args.no_force_change else None,
                         account=args.account)

    print("\n完成。下一步:")
    print("  uv run uvicorn cogni_agent.main:app --reload      # 启动(启动时会再自动跑一次结构同步)")
    print("  uv run python scripts/bootstrap.py --check        # 随时体检")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
