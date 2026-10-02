"""
M6-02:数据源内省——列出某个数据源下的表、查看建表 DDL、预览前 N 行数据。
供管理后台「知识中心 · 数据库知识 · 表登记」页面使用,是纯读操作(不落库,
每次点击都是实时查询),和 sandbox/local.py 面向"Skill/Agent 运行时按模板调用"的
只读查询是两类不同的使用场景,因此这里单独实现,不复用 sandbox 的参数化模板逻辑。
"""
from __future__ import annotations

from cogni_agent.common.logging import get_logger

log = get_logger("knowledge_service.introspect")

_PREVIEW_LIMIT = 100


async def list_tables(db_type: str, connection_config: dict) -> list[str]:
    if db_type == "postgres":
        return await _pg_list_tables(connection_config)
    if db_type in ("mysql", "selectdb", "starrocks"):
        return await _mysql_list_tables(connection_config)
    if db_type == "clickhouse":
        return await _clickhouse_list_tables(connection_config)
    raise ValueError(f"不支持的数据源类型: {db_type}")


async def get_table_ddl(db_type: str, connection_config: dict, table: str) -> str:
    if db_type == "postgres":
        return await _pg_table_ddl(connection_config, table)
    if db_type in ("mysql", "selectdb", "starrocks"):
        return await _mysql_show_create(connection_config, table)
    if db_type == "clickhouse":
        return await _clickhouse_show_create(connection_config, table)
    raise ValueError(f"不支持的数据源类型: {db_type}")


async def preview_table(db_type: str, connection_config: dict, table: str, limit: int = _PREVIEW_LIMIT) -> dict:
    """返回 {"columns": [...], "rows": [...]},限制最多 preview_limit 行(默认100,与功能清单一致)。"""
    if db_type == "postgres":
        return await _pg_query(connection_config, f'SELECT * FROM "{table}" LIMIT {int(limit)}')
    if db_type in ("mysql", "selectdb", "starrocks"):
        return await _mysql_query(connection_config, f"SELECT * FROM `{table}` LIMIT {int(limit)}")
    if db_type == "clickhouse":
        return await _clickhouse_query(connection_config, f"SELECT * FROM {table} LIMIT {int(limit)}")
    raise ValueError(f"不支持的数据源类型: {db_type}")


# ── Postgres ──
async def _pg_connect(cfg: dict):
    import asyncpg

    return await asyncpg.connect(host=cfg["host"], port=cfg.get("port", 5432), user=cfg["user"], password=cfg.get("password"), database=cfg["database"])


async def _pg_list_tables(cfg: dict) -> list[str]:
    conn = await _pg_connect(cfg)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        )
        return [r["table_name"] for r in rows]
    finally:
        await conn.close()


async def _pg_table_ddl(cfg: dict, table: str) -> str:
    conn = await _pg_connect(cfg)
    try:
        cols = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",
            table,
        )
        if not cols:
            raise ValueError(f"表 {table} 不存在或无权限查看")
        lines = []
        for c in cols:
            null_str = "" if c["is_nullable"] == "YES" else " NOT NULL"
            default_str = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
            lines.append(f'  "{c["column_name"]}" {c["data_type"]}{null_str}{default_str}')
        return f'CREATE TABLE "{table}" (\n' + ",\n".join(lines) + "\n);"
    finally:
        await conn.close()


async def _pg_query(cfg: dict, sql: str) -> dict:
    conn = await _pg_connect(cfg)
    try:
        rows = await conn.fetch(sql)
        return {"columns": list(rows[0].keys()) if rows else [], "rows": [dict(r) for r in rows]}
    finally:
        await conn.close()


# ── MySQL 协议兼容(MySQL/SelectDB/StarRocks) ──
async def _mysql_connect(cfg: dict):
    try:
        import aiomysql
    except ImportError as exc:
        raise RuntimeError("需要安装可选依赖 aiomysql(`uv sync --extra db-connectors`)") from exc
    return await aiomysql.connect(
        host=cfg["host"], port=cfg.get("port", 3306), user=cfg["user"], password=cfg.get("password"),
        db=cfg["database"], cursorclass=aiomysql.cursors.DictCursor,
    )


async def _mysql_list_tables(cfg: dict) -> list[str]:
    conn = await _mysql_connect(cfg)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SHOW TABLES")
            rows = await cur.fetchall()
            return [list(r.values())[0] for r in rows]
    finally:
        conn.close()


async def _mysql_show_create(cfg: dict, table: str) -> str:
    conn = await _mysql_connect(cfg)
    try:
        async with conn.cursor() as cur:
            await cur.execute(f"SHOW CREATE TABLE `{table}`")
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"表 {table} 不存在或无权限查看")
            return row.get("Create Table") or row.get("Create View") or str(row)
    finally:
        conn.close()


async def _mysql_query(cfg: dict, sql: str) -> dict:
    conn = await _mysql_connect(cfg)
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
            columns = [d[0] for d in cur.description] if cur.description else []
            return {"columns": columns, "rows": rows}
    finally:
        conn.close()


# ── ClickHouse ──
def _clickhouse_client(cfg: dict):
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError("需要安装可选依赖 clickhouse-connect(`uv sync --extra db-connectors`)") from exc
    return clickhouse_connect.get_client(
        host=cfg["host"], port=cfg.get("port", 8123), username=cfg.get("user"), password=cfg.get("password"), database=cfg.get("database")
    )


async def _clickhouse_list_tables(cfg: dict) -> list[str]:
    import asyncio

    def _run():
        client = _clickhouse_client(cfg)
        result = client.query("SHOW TABLES")
        return [row[0] for row in result.result_rows]

    return await asyncio.to_thread(_run)


async def _clickhouse_show_create(cfg: dict, table: str) -> str:
    import asyncio

    def _run():
        client = _clickhouse_client(cfg)
        result = client.query(f"SHOW CREATE TABLE {table}")
        return result.result_rows[0][0] if result.result_rows else ""

    return await asyncio.to_thread(_run)


async def _clickhouse_query(cfg: dict, sql: str) -> dict:
    import asyncio

    def _run():
        client = _clickhouse_client(cfg)
        result = client.query(sql)
        return {"columns": list(result.column_names), "rows": [dict(zip(result.column_names, row)) for row in result.result_rows]}

    return await asyncio.to_thread(_run)
