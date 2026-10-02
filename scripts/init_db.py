#!/usr/bin/env python
"""
数据库一键初始化 —— **这是你唯一需要跑的脚本**。

    uv run python scripts/init_db.py              # 全新库建基线 + 迁移 + 结构体检 + 注释
    uv run python scripts/init_db.py --with-seed  # 再灌种子数据(超管账号 + 权限策略)
    uv run python scripts/init_db.py --with-seed --admin-password pftroot123 --no-force-change
                                                  # 顺带指定超管密码(已存在则重置)
    uv run python scripts/init_db.py --create-db     # 库不存在时自动建库 + 装 pgvector
    uv run python scripts/init_db.py --check      # 只体检(模型 vs 库 + 迁移覆盖),只读

幂等:随便跑多少次都安全。应用启动时也会自动跑一次同样的逻辑(见 main.py::lifespan),
所以正常情况下你甚至不需要手动执行——但升级版本后手动跑一次能立刻看到都改了什么。

结构演进铁律(模型 = 结构声明源,迁移 = 唯一增量通道):
  1. 全新库(库里还没有本项目任何表)=> create_all 一次性建全量基线,并把整条
     迁移链记录为已应用(新库天然带完整迁移记录,可追溯);
  2. 老库(已有表)=> **不会自动补列/建缺表**。任何结构改动(加表/加列/改类型)
     必须到 cogni_agent/common/schema.py 的 MIGRATIONS 追加**幂等**迁移;
  3. 迁移全跑完后做只读体检:模型与库仍对不上(改了没写迁移)=> 直接报错退出,
     没有迁移文件记录的一律不放行。
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from cogni_agent.common.db import get_engine
from cogni_agent.common.db import Base
from cogni_agent.common.schema import (
    DatabaseUnreachable,
    MIGRATIONS,
    P,
    _model_column_drift,
    _model_column_type_drift,
    _model_extra_column_drift,
    _model_extra_table_drift,
    sync_schema,
)


async def check_only() -> int:
    engine = get_engine()
    async with engine.begin() as conn:
        db_name = (await conn.execute(text("SELECT current_database()"))).scalar()
        tables = [r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"))]
        ours = [t for t in tables if t.startswith(P)]
        try:
            applied = {r[0] for r in await conn.execute(text(f"SELECT id FROM {P}schema_migrations"))}
        except Exception:  # noqa: BLE001 - 记录表还不存在
            applied = set()
        # 五项只读体检,与 sync_schema 收尾时完全同一套判定(以前这里只查「缺列」,
        # 缺表/冗余表/冗余列/类型漂移全是盲区 —— --check 说没问题,真跑却被拦下)
        db_tables = set(tables)
        missing_tables = sorted({t.name for t in Base.metadata.sorted_tables} - db_tables)
        extra_tables = await _model_extra_table_drift(conn)
        missing_columns = await _model_column_drift(conn)
        extra_columns = await _model_extra_column_drift(conn)
        type_drift = await _model_column_type_drift(conn)

    print(f"当前数据库 : {db_name}")
    print(f"schema     : public")
    print(f"本项目表数 : {len(ours)}(全部以 {P} 为前缀)")
    if not ours:
        print("\n⚠ 一张表都没有。请先运行:uv run python scripts/init_db.py")
        return 1
    pending = [m.id for m in MIGRATIONS if m.id not in applied]
    print(f"已应用迁移 : {len(applied)}/{len(MIGRATIONS)}")
    if pending:
        print(f"⚠ 待应用迁移: {pending}\n  运行 uv run python scripts/init_db.py 即可补齐")
        return 1
    print("✓ 迁移已是最新")

    problems = (
        [(f"缺表 {t}", "CREATE TABLE IF NOT EXISTS …(按模型定义写全)") for t in missing_tables]
        + [(f"冗余表 {t}", "DROP TABLE IF EXISTS …(或先 RENAME 归档,参照 0068)") for t in extra_tables]
        + [(f"缺列 {c}", "ALTER TABLE … ADD COLUMN IF NOT EXISTS …") for c in missing_columns]
        + [(f"冗余列 {c}", "ALTER TABLE … DROP COLUMN IF EXISTS …(参照 0068 先备份再删)") for c in extra_columns]
        + [(f"类型漂移 {d}", "ALTER TABLE … ALTER COLUMN … TYPE …(参照 0069)") for d in type_drift]
    )
    if problems:
        print(f"\n⚠ 模型与库不一致:{len(problems)} 项(结构改动必须有迁移文件,没有迁移记录一律不放行)")
        for what, how in problems:
            print(f"   - {what}\n     → {how}")
        print("\n  处理:到 cogni_agent/common/schema.py 的 MIGRATIONS 末尾追加幂等迁移后,"
              "\n       重跑 uv run python scripts/init_db.py(应用启动时也会跑同一套判定)。")
        return 1
    print("✓ 模型与库结构一致(无缺表/冗余表/缺列/冗余列/类型漂移)")
    print(f"\n提示:在数据库客户端里看不到表时,先确认连的是 `{db_name}` 库、public schema,"
          f"\n     并且没有开启「只显示我创建的对象」之类的过滤。查询验证:"
          f"\n     SELECT table_name FROM information_schema.tables WHERE table_name LIKE '{P}%';")
    return 0


async def ensure_database_exists() -> bool:
    """按 .env 里的配置建库并装 pgvector 扩展。返回是否真的新建了。

    **为什么值得做**:脚本本来就知道该建什么库、装什么扩展(配置就在手上),
    却让人照着提示手工敲两条命令 —— 而这两条命令还容易敲错(漏了扩展、
    连错端口、用错用户)。新环境部署的第一道摩擦应该由工具消化掉。

    连的是 `postgres` 维护库(CREATE DATABASE 不能在目标库自身的连接里执行)。
    没有建库权限时不静默失败,而是明确告诉用户去找 DBA 要哪条语句。
    """
    from cogni_agent.config import get_settings

    cfg = get_settings().get("database", default={}) or {}
    host, port = cfg.get("host"), int(cfg.get("port", 5432))
    user, pwd, db = cfg.get("user"), cfg.get("password"), cfg.get("db")

    import asyncpg

    try:
        maintenance = await asyncpg.connect(host=host, port=port, user=user,
                                            password=pwd, database="postgres", timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnreachable(
            f"连不上 {host}:{port} 的 postgres 维护库,无法自动建库。\n"
            f"请确认 PostgreSQL 已启动、账号密码正确;或手工建库后去掉 --create-db 重跑。\n"
            f"原始错误:{exc}"
        ) from exc

    created = False
    try:
        exists = await maintenance.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
        if not exists:
            # 库名来自配置文件而非用户输入,但仍然加引号——库名含连字符时不加会语法错
            await maintenance.execute(f'CREATE DATABASE "{db}"')
            created = True
            print(f"  ✓ 已创建数据库 {db}")
        else:
            print(f"  · 数据库 {db} 已存在")
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnreachable(
            f"创建数据库 {db!r} 失败:{exc}\n"
            f"通常是账号 {user!r} 没有 CREATEDB 权限。请让 DBA 执行:\n"
            f'  CREATE DATABASE "{db}";\n'
            f'  \\c "{db}"\n'
            f"  CREATE EXTENSION IF NOT EXISTS vector;"
        ) from exc
    finally:
        await maintenance.close()

    # pgvector 装在目标库里,不是维护库
    try:
        target = await asyncpg.connect(host=host, port=port, user=user,
                                       password=pwd, database=db, timeout=10)
        try:
            await target.execute("CREATE EXTENSION IF NOT EXISTS vector")
            print("  ✓ pgvector 扩展就绪")
        finally:
            await target.close()
    except Exception as exc:  # noqa: BLE001
        # 扩展装不上不算致命:没用到向量检索时系统照样能跑。
        # 但要明确告警,否则等到第一次用向量知识才发现,那时更难定位。
        print(f"  ! pgvector 扩展安装失败:{exc}")
        print(f"    向量知识功能将不可用。需要时请让 DBA 执行:")
        print(f'    CREATE EXTENSION IF NOT EXISTS vector;  -- 在库 {db} 中')

    return created


async def main() -> int:
    if "--check" in sys.argv:
        return await check_only()

    if "--create-db" in sys.argv:
        print("正在确认数据库存在 …")
        try:
            await ensure_database_exists()
        except DatabaseUnreachable as exc:
            print(f"\n✗ {exc}\n")
            return 1

    print("正在同步数据库结构 …")
    try:
        stats = await sync_schema()
    except DatabaseUnreachable as exc:
        # 只打这一句。完整堆栈里真正有用的只有最后一行,其余是 SQLAlchemy/asyncpg
        # 的内部调用 —— 打出来只会把根因埋掉。
        print(f"\n✗ {exc}\n")
        return 1
    print(f"✓ 结构同步完成:共 {stats['total_tables']} 张表,"
          + (f"全新库建基线 {len(stats['created_tables'])} 张," if stats["created_tables"] else "")
          + f"应用迁移 {len(stats['applied_migrations'])} 条,"
          + f"写入注释 {stats['comments_applied']} 条,"
          + "结构体检通过(模型与库一致)")

    if "--with-seed" in sys.argv:
        print("\n正在灌入种子数据 …")
        from cogni_agent.governance_event_bus_service.policy import seed_policies

        pol = seed_policies()
        print(f"  ✓ 权限策略:{pol['total_policies']} 条权限、{pol['total_grouping']} 条角色继承")
        from scripts.seed_superadmin import main as seed_admin

        # --admin-password <pw>:顺带把超管密码设成指定值(账号已存在时等同重置)
        # --no-force-change:不要求首次登录改密
        pw = None
        if "--admin-password" in sys.argv:
            idx = sys.argv.index("--admin-password")
            pw = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        await seed_admin(password=pw, force_change=False if "--no-force-change" in sys.argv else None)

    print("\n下一步:uv run uvicorn cogni_agent.main:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
