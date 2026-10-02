#!/usr/bin/env python3
"""迁移链「全新库」门禁:在临时库上按 create_all 基线重放整条迁移链。

背景(为什么需要这道门):
  check_db_migration_gap.py 比对的是「模型 vs 目标库」的结构差,能挡住「改了模型
  却没写迁移」;但它挡不住另一类坑——**迁移语句引用了最终模型里已不存在的表/列**。
  这类语句在存量库上永远不会暴露(老库 numeri_core_schema_migrations 已记录该迁移
  → 直接跳过),只有全新库首次部署(create_all 按最终模型建好基线后,再顺序执行整条
  迁移链)才会炸,而且一炸就是「容器起不来」。

  历史真实踩到的 5 处(2026-09 一次性暴露):
    - 0029 改 tc_agents.description  → 该列已被 0045 整体删除
    - 0040 改 tc_tools.description   → 该列已被 0051 整体删除
    - 0050 回填 INSERT...SELECT 读取本迁移即将删除的 5 个列
    - 0051 同上(4 个列)
    - 0054 ADD CONSTRAINT 无幂等保护 → 新库 create_all 已建同名约束,报 already exists

判定逻辑(与业务库完全隔离,只读业务库):
  1. 在同一 PG 实例建临时库(TEMPLATE template0),不碰业务库一行数据;
  2. CREATE EXTENSION vector + create_all 建全量基线(= 全新库首次部署的起点);
  3. 逐条执行整条 MIGRATIONS 链:每条独立事务、失败不中断,一次拿全失败清单;
  4. 收尾做结构体检(缺表/冗余表/缺列/冗余列/类型漂移),确认链跑完仍与模型一致
     ——这是「各环境最终结构一致」的直接断言;
  5. 无论成败,最后 DROP 临时库。

用法:
  python scripts/check_migration_chain_fresh_db.py [--env local|dev|prod]
  # 默认 local;读 .env.{env} 的 POSTGRES_* 连接信息。需该账号有 CREATEDB 权限。

退出码:
  0 = 通过(整链可在全新库跑通且结构收敛)   
  2 = BLOCK(有语句失败 / 结构体检不过,禁止部署,先修迁移)
  3 = 环境/连接/权限错误(如账号无 CREATEDB)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

SCRATCH_DB = "cogni_migchain_check"


def _main():
    parser = argparse.ArgumentParser(
        description="迁移链全新库门禁(部署前在临时库重放整条迁移链)")
    parser.add_argument("--env", default="local",
                        help="目标环境(local/dev/prod),读取 .env.{env} 连接,默认 local")
    args = parser.parse_args()

    # 必须在 import 项目模块前定好环境,get_settings 按 ZHILV_ENV 加载 .env.{env}
    os.environ["ZHILV_ENV"] = args.env
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)

    from sqlalchemy import text
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    from cogni_agent.common.db import Base
    from cogni_agent.common.schema import (
        MIGRATIONS,
        _model_column_drift,
        _model_column_type_drift,
        _model_extra_column_drift,
        _model_extra_table_drift,
        _split_statements,
    )
    from cogni_agent.config import get_settings

    scratch = f"{SCRATCH_DB}_{args.env}"

    def _dsn(dbname: str) -> URL:
        db = get_settings().get("database", default={})
        return URL.create(
            drivername="postgresql+asyncpg",
            username=db["user"], password=db["password"],
            host=db["host"], port=int(db["port"]), database=dbname,
        )

    async def run() -> int:
        # ── 1. 建临时库(与业务库同实例、完全隔离) ──
        try:
            admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
            async with admin.connect() as conn:
                await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
                await conn.exec_driver_sql(f'CREATE DATABASE {scratch} TEMPLATE template0')
        except Exception as exc:  # noqa: BLE001
            print(f"[{args.env}] 无法建临时库 {scratch}: {exc!r}", file=sys.stderr)
            print(f"[{args.env}] 该账号需有 CREATEDB 权限,且本机到该库网络可达"
                  f"(dev 库走 VPN/内网)。", file=sys.stderr)
            return 3
        print(f"===== 迁移链全新库门禁 [{args.env}] =====")
        print(f"  ① 临时库已建:{scratch}(TEMPLATE template0,与业务库隔离)")

        engine = create_async_engine(_dsn(scratch))
        failures: list[tuple[str, str, str]] = []
        drift: list[str] = []
        total = 0
        try:
            # ── 2. 建全量基线(= 全新库首次部署的起点) ──
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)
            print(f"  ② create_all 基线完成(最终模型 {len(Base.metadata.sorted_tables)} 张表)")

            # ── 3. 逐条重放整条迁移链 ──
            for m in MIGRATIONS:
                for stmt in _split_statements(m.sql):
                    total += 1
                    try:
                        async with engine.begin() as conn:
                            await conn.exec_driver_sql(stmt)
                    except Exception as exc:  # noqa: BLE001
                        failures.append((m.id, type(exc).__name__,
                                         str(exc).splitlines()[0][:200]))
            print(f"  ③ 整链重放完成:共 {total} 条语句")

            # ── 4. 结构体检:链跑完必须仍与模型一致 ──
            async with engine.connect() as conn:
                tables = {r[0] for r in await conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"))}
                drift = (
                    [f"缺表 {t}" for t in sorted(
                        {t.name for t in Base.metadata.sorted_tables} - tables)]
                    + [f"冗余表 {t}" for t in await _model_extra_table_drift(conn)]
                    + [f"缺列 {c}" for c in await _model_column_drift(conn)]
                    + [f"冗余列 {c}" for c in await _model_extra_column_drift(conn)]
                    + [f"类型漂移 {d}" for d in await _model_column_type_drift(conn)]
                )
            print(f"  ④ 结构体检完成(模型与库差异 {len(drift)} 处)")
        finally:
            await engine.dispose()
            # ── 5. 清理临时库 ──
            try:
                async with admin.connect() as conn:
                    await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
                print(f"  ⑤ 临时库已清理")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ 临时库 {scratch} 清理失败(可手动 DROP): {exc!r}",
                      file=sys.stderr)
            await admin.dispose()

        # ── 判定 ──
        print("")
        if failures:
            print(f"  ✗ BLOCK:整链在全新库上有 {len(failures)} 条语句执行失败!")
            print("    这类语句在存量库上永远不暴露(schema_migrations 已记录 → 跳过),")
            print("    只有全新库首次部署才会炸,直接导致容器起不来。请逐条修复:")
            for mid, etype, msg in failures:
                print(f"      - [{mid}] {etype}: {msg}")
            print("    常见修法:")
            print("      · 引用了已被后续迁移删除的列/表 => 摘掉该语句,或整条降级为 no-op")
            print("        (参照 0029 / 0040 的写法与注释)")
            print("      · 回填 SELECT 要读本迁移即将删除的列 => 回填前先 ADD COLUMN IF NOT EXISTS")
            print("        按历史形态补齐,回填后随 DROP 一并删掉(参照 0050 / 0051)")
            print("      · ADD CONSTRAINT / ADD 无 IF NOT EXISTS 语义 => 先 DROP ... IF EXISTS")
            print("        同名对象再 ADD(参照 0054)")
            return 2
        if drift:
            print(f"  ✗ BLOCK:整链跑完后结构仍与模型不一致({len(drift)} 处)!")
            print("    说明迁移链没能把结构收敛到模型声明,请补迁移或修迁移:")
            for d in drift:
                print(f"      - {d}")
            return 2
        print(f"  ✓ 整链 {total} 条语句在全新库上全部执行通过,且结构收敛到模型")
        print(f"    => [{args.env}] 全新库首次部署路径可用,门禁通过")
        return 0

    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    _main()
