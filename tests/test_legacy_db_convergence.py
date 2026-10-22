"""老库(带历史结构漂移)能否被迁移链收敛到与模型一致 —— 迁移 0067~0070 的回归测试。

背景:2026-09-17 现场执行 `uv run python scripts/init_db.py` 时,0014~0065 全部应用成功,
但收尾的只读体检报了 17 项不一致后整体回滚(缺 6 张表 / 冗余 3 表 3 列 / 5 处类型漂移)——
这些结构都是 create_all 时代随模型自动建出/删除的,迁移链里从来没有对应条目,
于是「全新库有、老库没有」。0067(补建缺表)、0068(归档冗余表列)、0069(收敛类型)补上这段。
0070 是同一套手法的又一次应用:统一上下文参数机制整体下线后,tc_context_params 成了冗余表。

本测试把那次现场的库状态原样造出来,再跑迁移链,断言:
  1. 五项体检(缺表/冗余表/缺列/冗余列/类型漂移)全部清零;
  2. 被清理的表/列数据没有直接丢:表改名为 legacy_*_0068 / legacy_tc_context_params_0070,
     列值抄进 legacy_dropped_columns;
  3. 补建出来的 6 张表与 create_all(模型)逐列一致;
  4. 迁移可重复执行(再跑一遍不报错、体检仍然干净)。
需要真实 PostgreSQL(与其他集成测试同一套测试库)。
"""
from __future__ import annotations

import asyncio
import functools

import pytest
from sqlalchemy import text

from cogni_agent.common import schema as sc
from cogni_agent.common.db import Base, get_engine

P = sc.P

# 2026-09-17 现场那台库相对当前模型的差异(照抄体检清单)
MISSING_TABLES = ["sys_group", "sys_scenics", "sys_model_providers", "sys_model_configs",
                  "app_followed_questions", "app_message_feedback"]
NEW_MIGRATIONS = ["0067_missing_sys_and_app_tables", "0068_drop_legacy_tables_and_columns",
                  "0069_column_type_drift_to_model", "0070_drop_context_params",
                  "0071_qbi_fixed_question_domain_and_ask_type", "0072_qbi_ask_type_dashboard",
                  "0073_qbi_run_debug_trace", "0074_assignment_resource_type",
                  "0075_qbi_connection_grants",
                  "0076_issue_detail_and_dimension_label",
                  "0077_qbi_fixed_question_dimension_label",
                  "0078_scenic_master_data_convergence",
                  "0079_quickbi_fixed_questions_into_issue_detail",
                  "0080_widen_qbi_run_fixed_question_id",
                  "0081_theme_report_dimension_labels",
                  "0082_qbi_asset_file_active",
                  "0083_qbi_dataset_source_board",
                  "0084_issue_prewarm_settings",
                  "0085_agent_files",
                  "0086_platform_secrets",
                  "0087_resource_version_updated_by",
                  "0088_qbi_asset_category", "0089_app_scenic_param", "0090_system_params",
                  "0091_qbi_prompts_scenic_datasets_run_task", "0092_issue_qbi_cache_cleared_at"]

_SIMULATE_OLD_DB = f"""
DROP TABLE IF EXISTS {P}sys_model_configs;
DROP TABLE IF EXISTS {P}sys_model_providers;
DROP TABLE IF EXISTS {P}sys_group;
DROP TABLE IF EXISTS {P}sys_scenics;
DROP TABLE IF EXISTS {P}app_followed_questions;
DROP TABLE IF EXISTS {P}app_message_feedback;

CREATE TABLE {P}usr_departments (id VARCHAR(36) PRIMARY KEY, name VARCHAR(64) NOT NULL);
INSERT INTO {P}usr_departments VALUES ('d1', '运营部');
ALTER TABLE {P}usr_users ADD COLUMN department_id VARCHAR(36) REFERENCES {P}usr_departments (id);
CREATE TABLE {P}chat_attachments (id VARCHAR(36) PRIMARY KEY, file_name VARCHAR(255));
INSERT INTO {P}chat_attachments VALUES ('a1', '旧附件.xlsx');
CREATE TABLE {P}turn_feedback (id VARCHAR(36) PRIMARY KEY, comment TEXT);
INSERT INTO {P}turn_feedback VALUES ('f1', '旧反馈');
ALTER TABLE {P}tc_tools ADD COLUMN category VARCHAR(64);
ALTER TABLE {P}tc_skills ADD COLUMN input_variables JSONB;

ALTER TABLE {P}app_config ALTER COLUMN config TYPE JSONB USING config::jsonb;
ALTER TABLE {P}app_competitor_requests ALTER COLUMN extra TYPE JSONB USING extra::jsonb;
ALTER TABLE {P}gov_daily_usage ALTER COLUMN prompt_tokens TYPE BIGINT;
ALTER TABLE {P}gov_daily_usage ALTER COLUMN completion_tokens TYPE BIGINT;
ALTER TABLE {P}tc_tool_daily_stats ALTER COLUMN total_duration_ms TYPE BIGINT;

INSERT INTO {P}usr_departments VALUES ('d2', '技术部');
INSERT INTO {P}usr_users (id, name, account, phone, password_hash, role, is_active,
                          must_change_password, department_id)
     VALUES ('u-legacy', '老用户', 'legacy', '13900000000', 'x', 'scenic_operator', true, false, 'd1');
INSERT INTO {P}tc_tools (id, name, active, locked, owner_user_id, visibility, category)
     VALUES ('t-legacy', '老工具', true, false, 'u-legacy', 'group', '数据查询');
INSERT INTO {P}tc_skills (id, name, tool_ids, owner_user_id, visibility, input_variables)
     VALUES ('s-legacy', '老技能', '[]'::json, 'u-legacy', 'group', '{{"a": 1}}'::jsonb);
INSERT INTO {P}gov_daily_usage (id, scenic_id, usage_date, prompt_tokens, completion_tokens, cost_cny)
     VALUES ('g1', '', CURRENT_DATE, 2000000000, 12345, 0);
INSERT INTO {P}tc_tool_daily_stats (tool_id, stat_date, call_count, success_count, total_duration_ms)
     VALUES ('t-legacy', CURRENT_DATE, 3, 3, 987654);
INSERT INTO {P}app_config (id, user_id, config_type, config)
     VALUES ('c1', 'u-legacy', 'workbench', '{{"theme": "dark"}}'::jsonb);
INSERT INTO {P}app_competitor_requests (id, user_id, schema_version, extra, status)
     VALUES ('cr1', 'u-legacy', 1, '{{"note": "旧需求"}}'::jsonb, 'pending');
"""


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    """本用例只建非向量表:pgvector 列在没装扩展的环境里建不出来,与本测试无关。"""
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


async def _drift(conn) -> dict[str, list[str]]:
    return {
        "missing_tables": sorted({t.name for t in _tables()} - {
            r[0] for r in await conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}),
        "extra_tables": await sc._model_extra_table_drift(conn),
        "missing_columns": await sc._model_column_drift(conn),
        "extra_columns": await sc._model_extra_column_drift(conn),
        "type_drift": await sc._model_column_type_drift(conn),
    }


async def _reset_to_model(conn) -> None:
    for legacy in ("legacy_chat_attachments_0068", "legacy_turn_feedback_0068",
                   "legacy_usr_departments_0068", "legacy_dropped_columns",
                   "legacy_tc_context_params_0070"):
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {legacy} CASCADE")
    await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
    await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    # 迁移记录表不是模型表,drop_all 不会碰它 —— 上一次跑完留下的 NEW_MIGRATIONS 记录
    # 会让这一次「已经应用过」而跳过,于是 applied 断言落空。
    # 不清的话这个用例只有在全新库上才通过,换谁第二次跑都会莫名其妙地红。
    await sc._ensure_migration_table(conn)
    ids = ", ".join(f"'{m}'" for m in NEW_MIGRATIONS)
    await conn.exec_driver_sql(f"DELETE FROM {P}schema_migrations WHERE id IN ({ids})")


async def _simulate_old_db(conn) -> None:
    """把库改成 2026-09-17 现场那台老库的形态,并塞几行数据用于验证「不丢数据」。"""
    for stmt in sc._split_statements(_SIMULATE_OLD_DB):
        await conn.exec_driver_sql(stmt)


async def _mark_applied_through_0066(conn) -> None:
    await sc._ensure_migration_table(conn)
    for m in sc.MIGRATIONS:
        if m.id in NEW_MIGRATIONS:
            continue
        await conn.exec_driver_sql(
            f"INSERT INTO {P}schema_migrations (id, note) VALUES ('{m.id}', 'simulated') "
            "ON CONFLICT (id) DO NOTHING")


@run_async
async def test_upgrade_survives_manually_dropped_legacy_fixed_questions():
    """现场实况:库升到一半,有人看 tc_qbi_fixed_questions「没用了」就手工删了。

    这张表在 0079 里确实会被删掉,但 0071/0072/0076/0077 都还要读写它 ——
    而 PostgreSQL 在**解析阶段**就要求表存在(ADD COLUMN IF NOT EXISTS 只管列、不管表),
    所以少了它,服务启动会直接卡在迁移上、整个应用起不来,报
    `relation "numeri_core_tc_qbi_fixed_questions" does not exist`。

    修法是这几条迁移开头都先建一张同形空壳(老库 no-op),最后由 0079 读空并删除。
    这条用例就钉死「表被删了也能升上去」,且升完老表仍然是删掉的状态。
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await _reset_to_model(conn)
        await _simulate_old_db(conn)
        await _mark_applied_through_0066(conn)
        # 先升到 0076 —— 现场就停在这里
        for m in sc.MIGRATIONS:
            if m.id not in NEW_MIGRATIONS or m.id > "0076_issue_detail_and_dimension_label":
                continue
            for stmt in sc._split_statements(m.sql):
                await conn.exec_driver_sql(stmt)
            await conn.exec_driver_sql(
                f"INSERT INTO {P}schema_migrations (id, note) VALUES ('{m.id}', 'simulated') "
                "ON CONFLICT (id) DO NOTHING")
        # 运维手工删表
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {P}tc_qbi_fixed_questions")

        applied = await sc.apply_migrations(conn, verbose=False)
        # 这一条测的是「老表被手工删掉之后 0079 还能走完」,所以只要求它被应用到 ——
        # 写死成 applied[-1] 的话,以后每追加一条迁移这个用例都会莫名其妙地红
        assert "0079_quickbi_fixed_questions_into_issue_detail" in applied

        left = (await conn.exec_driver_sql(
            f"SELECT to_regclass('{P}tc_qbi_fixed_questions')")).scalar()
        assert left is None, "升级完老表应当是删掉的状态(空壳只是过路用)"
        after = await _drift(conn)
        assert after["extra_tables"] == [], "空壳没被清掉的话,体检会报冗余表、启动照样失败"


@run_async
async def test_legacy_db_converges_and_keeps_data():
    engine = get_engine()
    async with engine.begin() as conn:
        await _reset_to_model(conn)
        await _simulate_old_db(conn)
        await _mark_applied_through_0066(conn)
        before = await _drift(conn)
        # 先确认「现场那台库」的差异被造出来了(否则后面的断言等于没测)
        assert set(before["missing_tables"]) == {P + t for t in MISSING_TABLES}
        assert set(before["extra_tables"]) == {P + "chat_attachments", P + "turn_feedback", P + "usr_departments"}
        assert set(before["extra_columns"]) == {P + "usr_users.department_id", P + "tc_tools.category",
                                                P + "tc_skills.input_variables"}
        assert len(before["type_drift"]) == 5

        applied = await sc.apply_migrations(conn, verbose=False)
        assert applied == NEW_MIGRATIONS

        after = await _drift(conn)
        assert after == {"missing_tables": [], "extra_tables": [], "missing_columns": [],
                         "extra_columns": [], "type_drift": []}, after

        # 数据没有直接丢:表改名归档,列值抄进 legacy_dropped_columns
        archived = {r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'legacy_%'"))}
        assert {"legacy_chat_attachments_0068", "legacy_turn_feedback_0068",
                "legacy_usr_departments_0068", "legacy_dropped_columns"} <= archived
        assert (await conn.execute(text("SELECT file_name FROM legacy_chat_attachments_0068"))).scalar() == "旧附件.xlsx"
        backup = {(r[0], r[1], r[2], r[3]) for r in await conn.execute(text(
            "SELECT table_name, row_id, column_name, value FROM legacy_dropped_columns"))}
        assert (P + "usr_users", "u-legacy", "department_id", "d1") in backup
        assert (P + "tc_tools", "t-legacy", "category", "数据查询") in backup
        assert any(c == "input_variables" and "\"a\"" in (v or "") for _, _, c, v in backup)

        # 类型收敛后原有数据仍在(bigint→integer 的值域够用:20 亿 token / 近 25 天毫秒都在 int4 内)
        assert (await conn.execute(text(f"SELECT prompt_tokens, completion_tokens FROM {P}gov_daily_usage WHERE id='g1'"))
                ).one() == (2000000000, 12345)
        assert (await conn.execute(text(f"SELECT total_duration_ms FROM {P}tc_tool_daily_stats"))).scalar() == 987654
        assert (await conn.execute(text(f"SELECT config->>'theme' FROM {P}app_config WHERE id='c1'"))).scalar() == "dark"
        assert (await conn.execute(text(f"SELECT extra->>'note' FROM {P}app_competitor_requests"))).scalar() == "旧需求"

        # 幂等:整段 SQL 再跑一遍不报错,体检仍然干净
        for mig in [m for m in sc.MIGRATIONS if m.id in NEW_MIGRATIONS]:
            for stmt in sc._split_statements(mig.sql):
                await conn.exec_driver_sql(stmt)
        assert await _drift(conn) == {"missing_tables": [], "extra_tables": [], "missing_columns": [],
                                      "extra_columns": [], "type_drift": []}


@run_async
async def test_new_migrations_are_noop_on_fresh_db():
    """全新库:create_all 建完基线后,这几条照样跑得通且什么都不改(整条链的全新库门禁见
    scripts/check_migration_chain_fresh_db.py,那条需要 pgvector,这里只覆盖后期新增的几条)。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await _reset_to_model(conn)
        await sc._ensure_migration_table(conn)
        for mig in [m for m in sc.MIGRATIONS if m.id in NEW_MIGRATIONS]:
            for stmt in sc._split_statements(mig.sql):
                await conn.exec_driver_sql(stmt)
        assert await _drift(conn) == {"missing_tables": [], "extra_tables": [], "missing_columns": [],
                                      "extra_columns": [], "type_drift": []}
        assert (await conn.execute(text("SELECT count(*) FROM legacy_dropped_columns"))).scalar() == 0
        renamed = (await conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'legacy_%_0068'"))).scalar()
        assert renamed == 0


@run_async
async def test_created_tables_match_model_definition():
    """0067 补建的 6 张表必须与 create_all(模型)逐列一致 —— 否则老库/新库结构永远对不齐。"""
    engine = get_engine()
    cols_sql = ("SELECT table_name, column_name, data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns WHERE table_schema='public' AND table_name = ANY(:names) "
                "ORDER BY 1, 2")
    names = [P + t for t in MISSING_TABLES]
    async with engine.begin() as conn:
        await _reset_to_model(conn)   # 模型视角(create_all)
        by_model = {(r[0], r[1]): tuple(r[2:]) for r in await conn.execute(text(cols_sql), {"names": names})}
        for n in names:                # 删掉再让迁移重建(老库视角)
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {n} CASCADE")
        mig = next(m for m in sc.MIGRATIONS if m.id == "0067_missing_sys_and_app_tables")
        for stmt in sc._split_statements(mig.sql):
            await conn.exec_driver_sql(stmt)
        by_migration = {(r[0], r[1]): tuple(r[2:]) for r in await conn.execute(text(cols_sql), {"names": names})}
    assert by_model and by_migration == by_model


def test_migration_ids_are_unique_and_ordered():
    ids = [m.id for m in sc.MIGRATIONS]
    assert len(ids) == len(set(ids))
    # 编号按数字前缀单调递增(历史上同一编号出现过不同后缀名,故只比前四位)
    nums = [i[:4] for i in ids]
    assert nums == sorted(nums), "迁移编号必须单调递增(新条目一律追加到末尾)"
    assert ids[-len(NEW_MIGRATIONS):] == NEW_MIGRATIONS


@pytest.mark.parametrize("mig_id", NEW_MIGRATIONS)
def test_new_migrations_are_idempotent_sql(mig_id):
    """幂等写法的静态检查:只允许出现带 IF EXISTS / IF NOT EXISTS / ON CONFLICT 的收敛语句。"""
    sql = next(m for m in sc.MIGRATIONS if m.id == mig_id).sql
    for stmt in sc._split_statements(sql):
        head = " ".join(stmt.split())[:60].upper()
        if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in head, stmt[:120]
        if head.startswith("DROP TABLE") or head.startswith("ALTER TABLE") and " RENAME TO " in stmt.upper():
            assert "IF EXISTS" in head, stmt[:120]
