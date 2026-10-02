#!/usr/bin/env python3
"""数据库结构改动门禁:部署前检查「本地改了数据库结构,却没写迁移文件」。

背景(为什么需要这道门):
  sync_schema 的自动补列/create_all 自愈**已下线**(见 cogni_agent/common/schema.py
  docstring):老库不再静默对齐模型,「改了模型不写迁移」在本地启动、dev 部署时都会
  被结构体检直接 raise 拒绝。但那个失败发生在**部署之后**——合入 merge/dev、构建、
  容器起来才发现,浪费一轮。所以这道门禁在**合入 merge/dev 之前**连目标库(默认 dev)
  做只读体检,把"没写迁移"挡在部署前。

判定逻辑(模型 = 唯一事实源,迁移 = 唯一增量通道):
  1. 全新库(目标库还没有任何本项目表)=> 首次部署 create_all 建基线,放行。
  2. 结构 diff: 缺表 / 缺列 / 冗余列 / 类型漂移(见 schema.py 只读体检,与启动时同一套)。
     冗余列(库里有、模型已删):不写 DROP 迁移的老库残留列,NOT NULL 无默认会卡写入,
     如 tc_prompts.version——跨环境迁移能力时 POST /skills 500(见 0042)。
  3. pending = 代码 MIGRATIONS 里目标库还没应用的迁移:
     - pending 非空 => 正常演进(写了迁移),放行;若结构差不在迁移覆盖内,
       部署时 sync_schema 会拒绝启动——那是兜底防线,这里只提示;
     - pending 为空 + 结构仍有差 => 唯一解释是"改了结构没写迁移" => BLOCK,
       **没有迁移文件记录的一律不放行**。

用法:
  python scripts/check_db_migration_gap.py [--env dev]
  # 默认 dev(未来 prod 同机制);读 .env.{env} 的 POSTGRES_* 连接信息,只读。

退出码:
  0 = 通过(可部署)    2 = BLOCK(禁止部署,先补迁移)    3 = 环境/连接错误
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _main():
    parser = argparse.ArgumentParser(description="数据库结构改动门禁(部署前检查)")
    parser.add_argument("--env", default="dev",
                        help="目标环境(local/dev/prod),读取 .env.{env} 连接,默认 dev")
    parser.add_argument("--no-write", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # 必须在 import 项目模块前定好环境,get_settings 按 ZHILV_ENV 加载 .env.{env}
    os.environ["ZHILV_ENV"] = args.env
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)

    from sqlalchemy import text

    from cogni_agent.common.db import get_engine
    from cogni_agent.common.schema import (
        Base,
        MIGRATIONS,
        P,
        _applied_ids,
        _model_column_drift,
        _model_column_type_drift,
        _model_extra_column_drift,
        _model_extra_table_drift,
    )

    PENDING_OK = "OK"
    PENDING_NEW = "NEW"

    async def run() -> int:
        engine = get_engine()
        try:
            async with engine.connect() as conn:
                # ── 0. 全新库判定:还没有任何本项目业务表 => 首次部署,create_all 建基线 ──
                tables_in_db = {r[0] for r in await conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"))}
                if not any(t.startswith(P) for t in tables_in_db):
                    print(f"[{args.env}] 目标库还没有任何本项目表 => 视为全新库,"
                          "首次部署由 create_all 一次性建全量基线,不设门禁。")
                    return 0

                # ── 1. 代码迁移链 vs 库已应用迁移 ──
                has_mig_tbl = f"{P}schema_migrations" in tables_in_db
                if has_mig_tbl:
                    dev_applied = await _applied_ids(conn)
                else:
                    # 老库但从未启用迁移链(纯 create_all 时代):整条链都算待应用,首次建立基线
                    dev_applied = set()
                    print(f"[{args.env}] 目标库有业务表但无迁移记录表 => 整条迁移链将作为"
                          "首次基线应用,下面按正常门禁判定。")
                code_ids = [m.id for m in MIGRATIONS]
                pending = [i for i in code_ids if i not in dev_applied]
                mig_state = PENDING_NEW if pending else PENDING_OK
                table_gap = sorted(
                    t.name for t in Base.metadata.sorted_tables
                    if t.name not in tables_in_db
                )
                column_gap = await _model_column_drift(conn)
                type_gap = await _model_column_type_drift(conn)
                extra_col_gap = await _model_extra_column_drift(conn)
                extra_table_gap = await _model_extra_table_drift(conn)
        except Exception as exc:  # noqa: BLE001 - 连接/查询失败统一引导
            print(f"[{args.env}] 无法连接目标库做检查: {exc!r}", file=sys.stderr)
            print(f"[{args.env}] 请确认 .env.{args.env} 的 POSTGRES_* 与本机到 "
                  f"该库的网络可达(dev 库走 VPN/内网)。", file=sys.stderr)
            return 3

        # ── 3. 汇总与判定 ──
        print(f"===== 数据库结构改动门禁 [{args.env}] =====")
        if mig_state == PENDING_NEW:
            print(f"  ✓ 代码有新迁移待部署: {len(pending)} 条")
            for i in pending:
                print(f"      - {i}")
            print("  部署时会依次应用这些迁移,结构差由迁移收敛,放行。")
            print("  ⚠ 若下方结构差并不在这些迁移覆盖范围内,请人工复核:")
        else:
            print("  ✓ 代码未新增迁移(迁移链与库已应用记录一致)")

        def _fmt(items, head):
            if not items:
                return
            print(f"  {head}")
            for it in items:
                print(f"      - {it}")

        _fmt(table_gap, "缺表(模型声明、库里没有;自动建表已下线,必须由迁移 CREATE TABLE):")
        _fmt(extra_table_gap, "冗余表(库残留、模型已删;必须由迁移 DROP TABLE 收敛):")
        _fmt(column_gap, "缺列(模型声明、库里没有;自动补列已下线,必须由迁移 ADD COLUMN):")
        _fmt(extra_col_gap, "冗余列(库残留、模型已删;不写 DROP 迁移会卡写入,必须由迁移 DROP COLUMN):")
        _fmt(type_gap, "类型漂移(库列存在但类型与模型不符;无自动修复机制,必须由迁移 ALTER TYPE):")

        no_gap = not (table_gap or extra_table_gap or column_gap or extra_col_gap or type_gap)
        if no_gap:
            print(f"  ✓ 结构与模型完全一致 => [{args.env}] 部署门禁通过")
            return 0
        if mig_state == PENDING_NEW:
            print(f"  ✓ 存在待部署迁移,放行。")
            print("    ⚠ 若上述结构差不在这些迁移的覆盖范围内,部署时 sync_schema 的只读")
            print("      结构体检会 raise 拒绝启动——那是兜底防线,请人工复核下方 WARNING。")
            return 0

        # pending 为空 + 有 gap => 改了结构没写迁移
        print("")
        print("  ✗ BLOCK:结构改动没有任何迁移文件覆盖!")
        print("    sync_schema 已下线自动补列/建缺表自愈——这些结构差不会在部署时被")
        print("    悄悄补齐,而是直接拒绝启动。请到 cogni_agent/common/schema.py 的")
        print("    MIGRATIONS 末尾追加幂等迁移:")
        print("      - 缺表     => CREATE TABLE IF NOT EXISTS ...(按模型定义写全)")
        print("      - 冗余表   => DROP TABLE IF EXISTS ...(模型已删整表,库残留)")
        print("      - 缺列     => ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...")
        print("      - 冗余列   => ALTER TABLE ... DROP COLUMN IF EXISTS ...(参照 0042)")
        print("      - 类型漂移 => ALTER TABLE ... ALTER COLUMN ... TYPE ...(参照 0040)")
        print("    补完迁移后重跑本脚本,通过后再合入 merge/dev 触发部署。")
        return 2

    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    _main()
