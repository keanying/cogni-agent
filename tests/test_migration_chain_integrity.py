"""
迁移链完整性:**任何一条迁移都不能引用后面迁移才创建的列/表**。

**真实事故**:0060 建 tc_context_params 表并 INSERT 种子数据,
INSERT 里用了 `scope` 列 —— 而 `scope` 是 0061 才 ADD 的。

全新库不报错(create_all 按最终模型建表,列本来就在,0060 的
`CREATE TABLE IF NOT EXISTS` 直接跳过),**只有从旧版本升级的老库**
会按顺序执行到这里,报 `column "scope" does not exist` 而中断整条链。

这类 bug 的特点:开发和 CI 都用全新库,永远发现不了;
只有客户现场从旧版本升级时才炸,而且炸在半路,库处于中间状态。
"""
from __future__ import annotations

import re

from cogni_agent.common.schema import MIGRATIONS, _split_statements


def test_create_table_covers_columns_used_in_same_migration():
    """同一条迁移里 CREATE TABLE 之后紧跟 INSERT 时,
    INSERT 用到的列必须都在 CREATE TABLE 里声明了。"""
    problems: list[str] = []
    for m in MIGRATIONS:
        created: dict[str, set[str]] = {}
        for stmt in _split_statements(m.sql):
            flat = " ".join(stmt.split())

            ct = re.search(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\S+)\s*\((.+)\)\s*$",
                           flat, re.I)
            if ct:
                table = ct.group(1)
                cols = set()
                for line in ct.group(2).split(","):
                    tok = line.strip().split()
                    # 跳过 PRIMARY KEY(...) / UNIQUE(...) 这类表级约束
                    if tok and tok[0].upper() not in ("PRIMARY", "UNIQUE", "FOREIGN",
                                                      "CONSTRAINT", "CHECK"):
                        cols.add(tok[0].strip('"').lower())
                created[table] = cols
                continue

            ins = re.search(r"INSERT INTO\s+(\S+)\s*\(([^)]+)\)", flat, re.I)
            if ins and ins.group(1) in created:
                used = {c.strip().strip('"').lower() for c in ins.group(2).split(",")}
                missing = used - created[ins.group(1)]
                if missing:
                    problems.append(
                        f"{m.id}: INSERT INTO {ins.group(1)} 用了 {sorted(missing)},"
                        f"但同条迁移的 CREATE TABLE 没有声明这些列"
                    )

    assert not problems, (
        "迁移在建表后立刻 INSERT,却用了表里没有的列。\n"
        "全新库不会报错(create_all 已按最终模型建好表),**只有老库升级时才炸**:\n  "
        + "\n  ".join(problems)
    )


def test_migration_ids_never_go_backwards():
    """迁移 id 必须唯一,且编号不能倒退。

    **允许同号并存**:历史上 0007 有两条(0007_model_default_flag 与
    0007_model_config_is_default),它们已经在各环境执行过并记录在
    schema_migrations 里 —— 改编号会让老库认为是「新迁移」而重放,
    比留着重号危险得多。所以这里只禁止**倒退**(后面的编号小于前面的),
    那才是真正会让人误判执行顺序的情况。

    新增迁移一律用比当前最大值大 1 的编号。
    """
    ids = [m.id for m in MIGRATIONS]
    assert len(ids) == len(set(ids)), f"存在完全重复的迁移 id:{[i for i in ids if ids.count(i) > 1]}"

    nums = [int(i.split("_")[0]) for i in ids]
    backwards = [(ids[i - 1], ids[i]) for i in range(1, len(nums)) if nums[i] < nums[i - 1]]
    assert not backwards, f"迁移编号出现倒退(会让人误判执行顺序):{backwards}"


def test_new_migrations_use_fresh_numbers():
    """最新一条必须是全局最大编号 —— 防止新迁移插到中间。"""
    nums = [int(m.id.split("_")[0]) for m in MIGRATIONS]
    assert nums[-1] == max(nums), (
        f"最后一条迁移编号 {nums[-1]} 不是最大值 {max(nums)} —— "
        f"新迁移应追加在末尾并使用最大编号 + 1"
    )


def test_all_migrations_split_cleanly():
    """每条迁移都能被正确拆分成语句。

    拆分器要处理两种容易出错的情况:字符串里的分号、**SQL 行注释里的单引号**。
    后者会把引号状态翻转,导致后面真正的分号不再被当成分隔符,
    报出来是 `syntax error at end of input` —— 完全指不到是注释的问题。
    """
    for m in MIGRATIONS:
        stmts = _split_statements(m.sql)
        assert stmts, f"迁移 {m.id} 拆分后为空"
        for st in stmts:
            # 去掉行注释后再数引号:注释里的单引号不参与配对
            code = re.sub(r"--[^\n]*", "", st)
            assert code.count("'") % 2 == 0, \
                f"迁移 {m.id} 拆出的语句引号不成对:{st[:100]}"


def test_splitter_ignores_quotes_in_comments():
    """注释里的单引号不能影响拆分 —— 这正是上面那条事故的第二重。"""
    sql = "-- 说明:column \"x\" doesn't exist\nCREATE TABLE t (a int);\nCREATE INDEX i ON t(a);"
    assert len(_split_statements(sql)) == 2


def test_tables_added_after_baseline_have_create_migrations():
    """**基线之后新增的模型表,必须有对应的 CREATE TABLE 迁移。**

    这是老库升级最容易踩的坑,而且用全新库永远测不出来:

    - 全新库走 `create_all`,按最终模型一次建好全部表,再把整条迁移链标记为已应用
    - 老库跳过 `create_all`,只按迁移链演进 —— **迁移里没有的表永远建不出来**,
      升级时体检报「缺表」,而且是在跑完几十条迁移之后才报

    真实事故:opinionhub_db 升级时一次性报出 6 张这样的表
    (app_followed_questions / app_message_feedback / sys_group /
    sys_model_configs / sys_model_providers / sys_scenics)。

    **基线表除外**:0001 之前就存在的表,任何老库在建库时都已由 create_all 建好,
    不需要建表迁移。判定方式是「库里已有」——所以这条检查只在有库连接时生效,
    没有库时退化为跳过(而不是误报几十张基线表)。
    """
    from cogni_agent.common import schema  # noqa: F401 - 触发全部模型注册
    from cogni_agent.common.db import Base
    from cogni_agent.common.schema import P

    created: set[str] = set()
    for m in MIGRATIONS:
        for stmt in _split_statements(m.sql):
            for hit in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\S+)",
                                   " ".join(stmt.split()), re.I):
                created.add(hit.group(1).strip('"').replace("{P}", P))

    # 基线表清单:从 0001 之前的模型推断不可行(模型一直在变),
    # 改用「迁移链里出现过的表 + 基线表」的补集来判定。
    # 基线表硬编码在这里是刻意的:它是一个**历史事实**(某个时间点的表集合),
    # 不应该跟着模型变化 —— 跟着变的话,新增表漏写迁移时这条检查会自动"放行"。
    BASELINE = {
        "conversations", "task_queue", "evt_outbox",
        "gov_approvals", "gov_audit", "gov_budget_thresholds", "gov_eval_cases",
        "gov_eval_runs", "gov_llm_model_configs", "gov_llm_usage", "gov_scenic_orgs",
        "kb_chunks", "kb_db_catalog", "kb_db_custom_sources", "kb_db_datasources",
        "kb_documents", "kb_knowledge_bases", "kb_vector_db_configs",
        "kb_vector_storage_configs", "kb_web_search_config", "kb_web_site_archive",
        "kb_web_whitelist",
        "tc_agents", "tc_api_keys", "tc_credentials", "tc_grants", "tc_mcp_servers",
        "tc_prompts", "tc_skills", "tc_tools", "tc_resource_versions",
        "usr_users", "usr_login_devices", "usr_sms_codes", "schema_migrations",
    }

    missing = sorted(
        t.name for t in Base.metadata.sorted_tables
        if t.name not in created and t.name.replace(P, "") not in BASELINE
    )
    assert not missing, (
        "这些表是基线之后新增的,只有模型、迁移链里没有 CREATE TABLE。\n"
        "全新库靠 create_all 能建出来,**老库升级时永远建不出来**:\n  "
        + "\n  ".join(missing)
        + "\n\n新增模型表时必须同时写建表迁移 —— 哪怕全新库不需要它。"
    )
