"""
数据库结构的**唯一事实源**:建表 + 增量迁移 + DDL 注释,全部幂等,可反复运行。

背景(这是一次真实踩到的坑):`Base.metadata.create_all()` 只会创建**缺失的表**,
它**从不给已存在的表加列**。所以早期版本建过表的库,后续版本新增的列(如
task_queue.conversation_id)永远不会自动出现,运行时就会报
`column "conversation_id" does not exist`。

结构演进铁律(**模型 = 结构声明源,迁移 = 唯一增量通道**,见 sync_schema docstring):
  1. 全新库   -> create_all 一次性建全量基线(仅首次部署),随后把整条迁移链
                 记录为已应用——新库天然带完整迁移记录,可追溯;
  2. MIGRATIONS -> **唯一**的结构演进通道。加表/加列/改类型/索引/约束/数据回填,
                 一律必须写幂等迁移,没有例外;
  3. 老库     -> 不再 create_all、不再自动补列。历史版本曾在启动时自动
                 ADD COLUMN 兜底,导致「改了模型不写迁移」在本地/dev 静默通过、
                 无迁移记录不可追溯(dev 一重置就打回原形)。现在迁移跑完后做
                 只读体检:模型与库仍对不上 = 改了没写迁移,**一律 raise 拒绝启动**。

结构变更(含普通加列/加表)只改这里的模型与 MIGRATIONS,缺一不可。

**为什么放在包内而不是 scripts/**:应用启动时要自动同步结构,如果这段代码放在
scripts/ 目录下,`uv run uvicorn ...` 这种通过**控制台脚本**启动的方式不会把工作目录
加入 sys.path(只有 `python -m xxx` 才会),于是 `import scripts.schema` 直接
ModuleNotFoundError —— 建表被静默跳过,库里只剩 LangGraph checkpointer 自建的那 4 张表。
这个坑真实发生过,所以结构定义必须跟着包走。

迁移记录在 numeri_core_schema_migrations 表里,已执行过的不会重复执行。
每条迁移的 SQL 都写成幂等形式(IF NOT EXISTS / IF EXISTS),即使记录表被清掉、
重跑一次也不会炸。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from cogni_agent.common.db import Base, get_engine

# ── 显式 import 全部模型,确保 Base.metadata 里注册齐全 ──
from cogni_agent.user_center_service import models as _m1          # noqa: F401
from cogni_agent.control_plane_service import models as _m2         # noqa: F401
from cogni_agent.tool_center_service.tool import models as _m3       # noqa: F401
from cogni_agent.tool_center_service.skill import models as _m4       # noqa: F401
from cogni_agent.tool_center_service.mcp import models as _m5          # noqa: F401
from cogni_agent.tool_center_service.agent import models as _m6         # noqa: F401
from cogni_agent.tool_center_service.quickbi import models as _m6q       # noqa: F401  # tc_qbi_*(Quick BI 智能小Q 集成)
from cogni_agent.tool_center_service import versioning as _m6b          # noqa: F401  # tc_resource_versions/tc_agent_assignments 曾漏注册!
from cogni_agent.knowledge_service.database_knowledge import catalog as _m7    # noqa: F401
from cogni_agent.knowledge_service.websearch_knowledge import search as _m8     # noqa: F401
from cogni_agent.knowledge_service.websearch_knowledge import fetch as _m9       # noqa: F401
from cogni_agent.knowledge_service.multi2vec_clip_knowledge import store as _m10  # noqa: F401
from cogni_agent.governance_event_bus_service.events import outbox as _m11         # noqa: F401
from cogni_agent.control_plane_service import memory as _m_mem                        # noqa: F401  # cp_context_memory(跨轮次上下文记忆)
from cogni_agent.tool_center_service.tool import stats as _m_stats                     # noqa: F401  # tc_tool_daily_stats(工具调用按天汇总)
from cogni_agent.gateway_service import models as _m12b                              # noqa: F401  # app_question_bank/app_metric_def/app_prewarm_cache(19号文档)
from cogni_agent.gateway_service import issue_models as _m12c                        # noqa: F401  # scenic_agent_issue_detail/public_problem_dimension_label(前后端改造:问题统一表 + 三级标签)
from cogni_agent.governance_event_bus_service import audit as _m12                  # noqa: F401
from cogni_agent.governance_event_bus_service import cost as _m13                    # noqa: F401
from cogni_agent.governance_event_bus_service import approvals as _m14                # noqa: F401
from cogni_agent.governance_event_bus_service import evals as _m15                     # noqa: F401
from cogni_agent.governance_event_bus_service import settings as _m16                   # noqa: F401
from cogni_agent.governance_event_bus_service import platform_secrets as _m16b          # noqa: F401  # gov_platform_secrets(后台配置的平台密钥)

P = "numeri_core_"   # 全局表前缀


# ══════════════════════════════════════════════════════════════════════════
# 已退场的老表:tc_qbi_fixed_questions(Quick BI 固定问题)
#
# 它在 0079 里被并入统一问题表 scenic_agent_issue_detail 并删除,模型也一并移除。
# 但 0071 / 0072 / 0076 / 0077 这几条**早于 0079 的迁移**还要读写它 —— 于是出现两种
# 它不存在的场景,而 PostgreSQL 在**解析阶段**就要求表存在(ALTER / SELECT 一律先报
# "relation does not exist",IF NOT EXISTS 只管列、不管表):
#   ① 全新库:模型删了,create_all 不会再建它;
#   ② 已升级过一半的库:有人看它没用了,手工 DROP 掉。
# 两种情况都会让服务启动时卡在迁移上、整个应用起不来。
#
# 所以凡是碰它的迁移,开头先建一张同形空壳:老库里是 no-op(表还在,数据一行不动),
# 新库/被删过的库里建出一张空表,走完 0071~0077 再由 0079 读空并删掉,净效果为零。
# ══════════════════════════════════════════════════════════════════════════
_LEGACY_FIXED_QUESTIONS_STUB = f"""
CREATE TABLE IF NOT EXISTS {P}tc_qbi_fixed_questions (
    id            VARCHAR(36) PRIMARY KEY,
    connection_id VARCHAR(36) NOT NULL DEFAULT '',
    title         VARCHAR(128) NOT NULL DEFAULT '',
    question      TEXT NOT NULL DEFAULT '',
    capability    VARCHAR(24) NOT NULL DEFAULT 'dataset_qa',
    domain        VARCHAR(32),
    ask_type      VARCHAR(16) NOT NULL DEFAULT 'qa',
    target        JSONB,
    topic         VARCHAR(64),
    topic_id      VARCHAR(64),
    dimension     VARCHAR(64),
    dimension_id  VARCHAR(64),
    metric        VARCHAR(64),
    metric_id     VARCHAR(64),
    locale        VARCHAR(8),
    category      VARCHAR(64),
    sort_order    INTEGER NOT NULL DEFAULT 0,
    active        BOOLEAN NOT NULL DEFAULT true,
    owner_user_id VARCHAR(36) NOT NULL DEFAULT '',
    scenic_id     VARCHAR(64),
    visibility    VARCHAR(16) NOT NULL DEFAULT 'group',
    last_run_id   VARCHAR(36),
    last_run_status VARCHAR(16),
    last_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    id: str          # 唯一编号,一旦发布不可修改
    note: str        # 这条迁移在解决什么问题(会写进记录表,便于日后追溯)
    sql: str         # 幂等 SQL,允许多条语句


# ══════════════════════════════════════════════════════════════════════════
# 增量迁移。**新增结构变更一律在末尾追加,不要修改或删除已有条目。**
# ══════════════════════════════════════════════════════════════════════════
MIGRATIONS: list[Migration] = [
    Migration(
        "0001_multi_turn_conversation",
        "多轮对话记忆:新增 conversations 表与 task_queue.conversation_id",
        f"""
        ALTER TABLE {P}task_queue ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(48);
        CREATE INDEX IF NOT EXISTS ix_{P}task_queue_conversation_id
            ON {P}task_queue (conversation_id);
        """,
    ),
    Migration(
        "0002_skill_scenic_nullable",
        "集团级 Skill 修复:scenic_id 允许为空(与 Tool/Agent 一致,None=集团级资源)。"
        "注:原对 tc_skill_compositions 的 ALTER 已随该表删除(0055)一并移除,"
        "否则全新库/清表重跑时该表已不存在会报错",
        f"ALTER TABLE {P}tc_skills ALTER COLUMN scenic_id DROP NOT NULL;",
    ),
    Migration(
        "0003_skill_composition_dag",
        "已由 0055 取代(tc_skill_compositions 整表删除),本迁移降级为 no-op:"
        "全新库/清表重跑时该表已不存在,原 ALTER 会直接报列/表不存在;"
        "存量语义已随组合层一并作废,无需再执行。老库(schema_migrations 已记录 0003)同样跳过",
        "SELECT 1;",
    ),
    Migration(
        "0004_tool_name_tenant_scoped_unique",
        "多租户修复:工具名从全局唯一改为租户范围内唯一"
        "(原来武夷山建了「查询订单」,厦门就再也建不了同名工具)",
        f"""
        DROP INDEX IF EXISTS ix_{P}tc_tools_name;
        CREATE INDEX IF NOT EXISTS ix_{P}tc_tools_name ON {P}tc_tools (name);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_tools_scope_name
            ON {P}tc_tools (coalesce(scenic_id, ''), name);
        """,
    ),
    Migration(
        "0005_conversation_id_width",
        "会话 id 带 conv_ 前缀(conv_+32位hex=37字符),列宽放到 48",
        f"""
        ALTER TABLE {P}conversations ALTER COLUMN id TYPE VARCHAR(48);
        ALTER TABLE {P}task_queue ALTER COLUMN conversation_id TYPE VARCHAR(48);
        """,
    ),
    Migration(
        "0006_websearch_config_page_managed",
        "网络知识配置页面化:新增 name/api_key_cipher/base_url/active,密钥不再只能写在 .env 里",
        f"""
        ALTER TABLE {P}kb_web_search_config ADD COLUMN IF NOT EXISTS name VARCHAR(64) DEFAULT '';
        ALTER TABLE {P}kb_web_search_config ADD COLUMN IF NOT EXISTS api_key_cipher VARCHAR(1024);
        ALTER TABLE {P}kb_web_search_config ADD COLUMN IF NOT EXISTS base_url VARCHAR(255);
        ALTER TABLE {P}kb_web_search_config ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;
        """,
    ),
    Migration(
        "0007_model_default_flag",
        "模型配置支持在页面上设默认:新增 is_default / remark",
        f"""
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS remark VARCHAR(255) NOT NULL DEFAULT '';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_model_single_default
            ON {P}gov_llm_model_configs (is_default) WHERE is_default = true;
        """,
    ),
    Migration(
        "0007_model_config_is_default",
        "模型配置支持「设为默认」:新增 is_default,优先级高于 config.yaml 的 runtime.default_model",
        f"ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false;",
    ),
    Migration(
        "0008_group_level_scenic_nullable",
        "集团级账号修复:evt_outbox / gov_audit / gov_llm_usage 的 scenic_id 允许为空。"
        "此前 NOT NULL 导致超管等集团级账号**发起对话直接 500**",
        f"""
        ALTER TABLE {P}evt_outbox     ALTER COLUMN scenic_id DROP NOT NULL;
        ALTER TABLE {P}gov_audit      ALTER COLUMN scenic_id DROP NOT NULL;
        ALTER TABLE {P}gov_llm_usage  ALTER COLUMN scenic_id DROP NOT NULL;
        """,
    ),
    Migration(
        "0009_long_text_columns",
        "长内容字段改 TEXT:分块原文/评测输入/错误堆栈/审批意见。"
        "原来的 VARCHAR(N) 会静默截断——检索出来的资料缺一半,模型基于残缺内容作答很难发现",
        f"""
        ALTER TABLE {P}kb_chunks        ALTER COLUMN content_ref TYPE TEXT;
        ALTER TABLE {P}gov_eval_cases   ALTER COLUMN test_input  TYPE TEXT;
        ALTER TABLE {P}gov_eval_runs    ALTER COLUMN error       TYPE TEXT;
        ALTER TABLE {P}gov_approvals    ALTER COLUMN opinion     TYPE TEXT;
        """,
    ),
    Migration(
        "0010_model_is_embedding",
        "嵌入模型与对话模型共用同一张注册表:新增 is_embedding 标记,页面可勾选",
        f"ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS is_embedding BOOLEAN NOT NULL DEFAULT false;",
    ),
    Migration(
        "0011_credentials_changed_at",
        "改密后旧令牌立即失效:usr_users 新增 credentials_changed_at,"
        "签发时间早于它的 JWT 一律拒绝(此前改密码踢不掉已泄露的令牌)",
        f"ALTER TABLE {P}usr_users ADD COLUMN IF NOT EXISTS credentials_changed_at TIMESTAMPTZ;",
    ),
    Migration(
        "0012_scenic_base_dim_table",
        "景区基础信息维表(dim_prd_scenic_base_yr_f):用户绑定景区时从这里选,"
        "同时作为 Text2SQL / 知识问答的景区主数据",
        """
        CREATE TABLE IF NOT EXISTS dim_prd_scenic_base_yr_f (
            scenic_id                VARCHAR(64)  PRIMARY KEY,
            scenic_name              VARCHAR(255),
            scenic_alias             VARCHAR(255),
            scenic_name_en           VARCHAR(255),
            scenic_appid             VARCHAR(64),
            country_name             VARCHAR(64),
            province_name            VARCHAR(64),
            city_name                VARCHAR(64),
            district_name            VARCHAR(64),
            scenic_address           VARCHAR(512),
            scenic_lng               NUMERIC(18,6),
            scenic_lat               NUMERIC(18,6),
            scenic_area_mu           NUMERIC(18,6),
            scenic_mountain_river    VARCHAR(255),
            scenic_grade_level       VARCHAR(16),
            scenic_grade_announce_no VARCHAR(255),
            scenic_grade_first_time  TIMESTAMP,
            scenic_grade_review_time TIMESTAMP,
            scenic_heritage_cert     VARCHAR(255),
            scenic_national_title    VARCHAR(255),
            scenic_operator_name     VARCHAR(255),
            scenic_superior_dept     VARCHAR(255),
            scenic_tel_consult       VARCHAR(128),
            scenic_tel_complaint     VARCHAR(128),
            scenic_tel_emergency     VARCHAR(128),
            scenic_website_url       VARCHAR(512),
            scenic_official_appid    VARCHAR(128),
            is_free_flag             SMALLINT,
            ticket_adult_weekday_amt NUMERIC(18,6),
            ticket_adult_peak_amt    NUMERIC(18,6),
            ticket_adult_low_amt     NUMERIC(18,6),
            ticket_preferential_amt  NUMERIC(18,6),
            ticket_transit_amt       NUMERIC(18,6),
            ticket_combo_amt         NUMERIC(18,6),
            ticket_advance_days_qty  INTEGER,
            has_online_ticket_flag   SMALLINT,
            open_status_type         VARCHAR(32),
            open_time_peak           VARCHAR(32),
            close_time_peak          VARCHAR(32),
            open_time_low            VARCHAR(32),
            close_time_low           VARCHAR(32),
            last_entry_time          VARCHAR(32),
            special_open_rule_desc   VARCHAR(2048),
            suggest_duration_desc    VARCHAR(128),
            best_season_desc         VARCHAR(128),
            scenic_main_type         VARCHAR(32),
            scenic_sub_type          VARCHAR(64),
            scenic_feature_tags      VARCHAR(512),
            scenic_desc              TEXT,
            scenic_core_spots        VARCHAR(2048),
            daily_capacity_max_qty   INTEGER,
            instant_capacity_max_qty INTEGER,
            last_year_visitor_qty    NUMERIC(18,6),
            has_parking_flag         SMALLINT,
            parking_space_qty        INTEGER,
            has_guide_flag           SMALLINT,
            guide_language_type      VARCHAR(255),
            create_time              TIMESTAMPTZ NOT NULL DEFAULT now(),
            update_time              TIMESTAMPTZ NOT NULL DEFAULT now(),
            travel_date              VARCHAR(8)  NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scenic_grade_level  ON dim_prd_scenic_base_yr_f (scenic_grade_level);
        CREATE INDEX IF NOT EXISTS idx_scenic_main_type    ON dim_prd_scenic_base_yr_f (scenic_main_type);
        CREATE INDEX IF NOT EXISTS idx_scenic_province_city ON dim_prd_scenic_base_yr_f (province_name, city_name);
        CREATE INDEX IF NOT EXISTS idx_scenic_name_dim     ON dim_prd_scenic_base_yr_f (scenic_name);
        CREATE INDEX IF NOT EXISTS idx_scenic_travel_date  ON dim_prd_scenic_base_yr_f (travel_date);

        COMMENT ON TABLE dim_prd_scenic_base_yr_f IS '产品域-景区基础信息POI全量日表。用户绑定景区时从这里选;也是 Text2SQL 的景区主数据';
        COMMENT ON COLUMN dim_prd_scenic_base_yr_f.scenic_id IS '景区ID,命名规则 PFT+SC+主类型代码+子类型代码+6位自增';
        COMMENT ON COLUMN dim_prd_scenic_base_yr_f.scenic_grade_level IS 'A级景区等级。枚举:5A,4A,3A,2A,1A,0-未定级';
        COMMENT ON COLUMN dim_prd_scenic_base_yr_f.open_status_type IS '全年开放状态。枚举:all-全年开放,seasonal-季节性开放,closed-当前关闭';
        COMMENT ON COLUMN dim_prd_scenic_base_yr_f.scenic_main_type IS '景区主类型(9类):自然风光/历史文化/文化场馆/主题乐园/城市休闲/乡村农业/度假康养/工业旅游/综合景区';
        COMMENT ON COLUMN dim_prd_scenic_base_yr_f.travel_date IS '数据日期分区,格式 YYYYMMDD';
        """,
    ),
    Migration(
        "0013_resource_versions_and_tool_usage",
        "统一版本管理(tc_resource_versions)+ Skill 就近维护工具使用说明(tool_usage)",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_resource_versions (
            id            VARCHAR(48) PRIMARY KEY,
            resource_type VARCHAR(16) NOT NULL,
            resource_id   VARCHAR(64) NOT NULL,
            version       INTEGER     NOT NULL,
            status        VARCHAR(16) NOT NULL DEFAULT 'draft',
            snapshot      JSON        NOT NULL DEFAULT '{{}}'::json,
            note          TEXT        NOT NULL DEFAULT '',
            created_by    VARCHAR(64) NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at  TIMESTAMPTZ,
            offlined_at   TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS ix_res_ver_lookup ON {P}tc_resource_versions (resource_type, resource_id);
        CREATE INDEX IF NOT EXISTS ix_res_ver_status ON {P}tc_resource_versions (status);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_res_ver_no ON {P}tc_resource_versions (resource_type, resource_id, version);
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS tool_usage JSON NOT NULL DEFAULT '{{}}'::json;
        """,
    ),
    Migration(
        "0014_login_device_enhance",
        "APP 端登录设备增强:usr_login_devices 新增 device_id/device_os/push_token/app_version 四字段，"
        "支持多端管理与消息推送",
        f"""
        ALTER TABLE {P}usr_login_devices ADD COLUMN IF NOT EXISTS device_id   VARCHAR(64);
        ALTER TABLE {P}usr_login_devices ADD COLUMN IF NOT EXISTS device_os   VARCHAR(32);
        ALTER TABLE {P}usr_login_devices ADD COLUMN IF NOT EXISTS push_token  VARCHAR(255);
        ALTER TABLE {P}usr_login_devices ADD COLUMN IF NOT EXISTS app_version VARCHAR(16);
        """,
    ),
    Migration(
        "0015_app_config",
        "v1.0 APP 端：用户个人配置存储表（JSONB 透传）。用户按 config_type 存取个人偏好/OTA凭证等",
        f"""
        CREATE TABLE IF NOT EXISTS {P}app_config (
            id          VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     VARCHAR(36) NOT NULL REFERENCES {P}usr_users(id),
            config_type VARCHAR(64) NOT NULL,
            config      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_app_config_user_type ON {P}app_config (user_id, config_type);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_config_user_type ON {P}app_config (user_id, config_type);
        """,
    ),
    Migration(
        "0016_app_competitor_requests",
        "v1.0 APP 端：竞品需求提交记录表。用户提交竞品需求，供产品团队导出调研",
        f"""
        CREATE TABLE IF NOT EXISTS {P}app_competitor_requests (
            id              VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         VARCHAR(36) NOT NULL,
            scenic_id       VARCHAR(64),
            competitor_name VARCHAR(128) NOT NULL,
            feature_desc    TEXT NOT NULL,
            extra           JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            priority        VARCHAR(16) NOT NULL DEFAULT 'normal',
            status          VARCHAR(16) NOT NULL DEFAULT 'pending',
            remark          TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_app_competitor_user_id ON {P}app_competitor_requests(user_id);
        CREATE INDEX IF NOT EXISTS idx_app_competitor_status  ON {P}app_competitor_requests(status);
        CREATE INDEX IF NOT EXISTS idx_app_competitor_scenic   ON {P}app_competitor_requests(scenic_id);
        """,
    ),
    Migration(
        "0017_competitor_requests_generic",
        "竞品需求提交表改为通用 schema_version + extra 结构，去掉固定业务字段",
        f"""
        ALTER TABLE {P}app_competitor_requests
            ADD COLUMN IF NOT EXISTS schema_version INTEGER NOT NULL DEFAULT 1,
            DROP COLUMN IF EXISTS competitor_name,
            DROP COLUMN IF EXISTS feature_desc,
            DROP COLUMN IF EXISTS priority,
            DROP COLUMN IF EXISTS remark;
        """,
    ),
    Migration(
        "0018_task_and_usage_user_id",
        "多账号绑定同一景区:task_queue / gov_llm_usage 新增 user_id,任务与成本按账号归因",
        f"""
        ALTER TABLE {P}task_queue ADD COLUMN IF NOT EXISTS user_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_{P}task_queue_user_id ON {P}task_queue (user_id);
        ALTER TABLE {P}gov_llm_usage ADD COLUMN IF NOT EXISTS user_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_{P}gov_llm_usage_user_id ON {P}gov_llm_usage (user_id);
        """,
    ),
    Migration(
        "0019_model_type",
        "模型清单按类型区分:新增 model_type(chat/embedding),每种类型各有一个默认。"
        "原来全表唯一默认(0007 的 uq_llm_model_single_default)改为同类型唯一默认",
        f"""
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS model_type VARCHAR(16) NOT NULL DEFAULT 'chat';
        UPDATE {P}gov_llm_model_configs SET model_type = 'embedding' WHERE is_embedding = true AND model_type = 'chat';
        DROP INDEX IF EXISTS uq_llm_model_single_default;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_model_type_single_default
            ON {P}gov_llm_model_configs (model_type) WHERE is_default = true;
        """,
    ),
    Migration(
        "0020_model_id",
        "模型清单拆分「展示名 / 供应商模型 ID」:新增 model_id 列,运行时用它调用模型;"
        "历史数据回填 model_id = name",
        f"""
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS model_id VARCHAR(128) NOT NULL DEFAULT '';
        UPDATE {P}gov_llm_model_configs SET model_id = name WHERE model_id = '';
        """,
    ),
    Migration(
        "0021_model_config_id_pk",
        "模型配置身份与展示名解耦:新增 id 主键(UUID 回填 md5(name)),name 降级为普通唯一字段(可改名)。"
        "业务记录(spec.model_override / gov_llm_usage.model)持久化的是 model_id,改名不再破坏历史归因",
        f"""
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS id VARCHAR(36);
        UPDATE {P}gov_llm_model_configs SET id = md5(name) WHERE id IS NULL OR id = '';
        ALTER TABLE {P}gov_llm_model_configs ALTER COLUMN id SET NOT NULL;
        ALTER TABLE {P}gov_llm_model_configs DROP CONSTRAINT IF EXISTS {P}gov_llm_model_configs_pkey;
        ALTER TABLE {P}gov_llm_model_configs ADD PRIMARY KEY (id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_{P}gov_llm_model_configs_name ON {P}gov_llm_model_configs (name);
        """,
    ),
    Migration(
        "0022_tc_credentials_user_scope",
        "凭证三维归属:tc_credentials 新增 user_id(空=共享)/scope(默认 default),"
        "唯一键由「按 tool 一条」升级为 (tool_id, user_id, scope) 三维。"
        "存量数据自动成为 共享(user_id=NULL) × default 场景(server_default),无需回填。"
        "对应实施记录 02 任务 1(文档 系统说明/框架优化-凭证采集与脱敏.md §10.1)",
        f"""
        ALTER TABLE {P}tc_credentials ADD COLUMN IF NOT EXISTS user_id VARCHAR(36);
        ALTER TABLE {P}tc_credentials ADD COLUMN IF NOT EXISTS scope VARCHAR(64) NOT NULL DEFAULT 'default';
        DROP INDEX IF EXISTS uq_{P}tc_credentials_tool_user_scope;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_{P}tc_credentials_tool_user_scope
            ON {P}tc_credentials (tool_id, coalesce(user_id, ''), scope);
        """,
    ),
    Migration(
        "0023_drop_shared_credentials",
        "凭证归属收紧:删除存量共享凭证(user_id 为空的行)。"
        "v0.1.4 起凭证一律归属具体用户,不存在共享凭证;运行时 _pick_credential "
        "对无 user_id 调用者直接视为无凭证,存量共享行不可见、不被使用,此处清理。"
        "对应实施记录 02 §9.4 设计调整",
        f"""
        DELETE FROM {P}tc_credentials WHERE user_id IS NULL OR trim(user_id) = '';
        """,
    ),
    Migration(
        "0024_versioning_parallel",
        "版本体系并行化(对应设计文档 04-版本体系并行化-设计.md):"
        "tc_resource_versions 支持多 published 并行 + is_default 默认版本标记 + "
        "base_version_id 草稿来源;新增版本指派表 tc_agent_assignments。"
        "老字段 is_default 存量默认 false,由 set-default / 首次发布写 True;"
        "存量已发布资源后续用 rollback 或 set-default 补齐默认标记即可",
        f"""
        ALTER TABLE {P}tc_resource_versions ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false;
        CREATE INDEX IF NOT EXISTS ix_res_ver_is_default ON {P}tc_resource_versions (is_default);
        ALTER TABLE {P}tc_resource_versions ADD COLUMN IF NOT EXISTS base_version_id VARCHAR(48);
        CREATE TABLE IF NOT EXISTS {P}tc_agent_assignments (
            id         VARCHAR(48) PRIMARY KEY,
            agent_id   VARCHAR(64) NOT NULL,
            scope_type VARCHAR(16) NOT NULL,
            scope_id   VARCHAR(64) NOT NULL DEFAULT '',
            version_id VARCHAR(48) NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            created_by VARCHAR(64) NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_agent_assign_agent ON {P}tc_agent_assignments (agent_id);
        CREATE INDEX IF NOT EXISTS ix_agent_assign_version ON {P}tc_agent_assignments (version_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_assign_scope
            ON {P}tc_agent_assignments (agent_id, scope_type, scope_id);
        """,
    ),
    Migration(
        "0025_backfill_default_version",
        "已由 0038 取代(0038_remove_default_version_and_user_only_assign 物理删除 is_default "
        "列,15 号文档取消默认版本概念),本迁移降级为 no-op:全新库顺序执行时表已无 is_default "
        "列,原 UPDATE 会直接报列不存在;存量回填语义已随默认概念一并作废,无需再执行。"
        "老库(schema_migrations 已记录 0025)同样跳过,不受影响",
        "SELECT 1;",
    ),
    Migration(
        "0026_agent_related_questions",
        "关联推荐追问按 agent 配置化:tc_agents 新增 related_questions_enabled(默认关闭)"
        "与 related_questions(预设问题 JSON 列表)。v1.1 阶段二/2C:推荐追问不再由 LLM 现生成"
        "(曾阻塞收尾、拖慢 29s+),改为管理端预设、resolver 随 done 事件直接下发,"
        "零额外 LLM 调用、零延迟。幂等:全新库 create_all 已含两列,老库由本条补齐"
        "(IF NOT EXISTS 跳过)",
        f"""
        ALTER TABLE {P}tc_agents ADD COLUMN IF NOT EXISTS related_questions_enabled BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE {P}tc_agents ADD COLUMN IF NOT EXISTS related_questions JSONB;
        """,
    ),
    Migration(
        "0027_version_locked_deps_nullable_draft",
        "版本体系改造(按推荐方案):草稿不再预占正式版本号——version 列改为可空,"
        "draft 行的 version 置 NULL,正式版本号在 publish 时按 max(published)+1 分配;"
        "新增 locked_deps 依赖锁 JSONB——发布时冻结 agent->skill->tool 完整闭包,"
        "不允许 version:null 模糊 pin(发布预检自动固化为绑定资源默认版本或主表 published)。"
        "幂等:全新库 create_all 已含两列,老库由本条补齐;DROP NOT NULL 幂等",
        f"""
        ALTER TABLE {P}tc_resource_versions ALTER COLUMN version DROP NOT NULL;
        ALTER TABLE {P}tc_resource_versions ADD COLUMN IF NOT EXISTS locked_deps JSONB;
        """,
    ),
    Migration(
        "0028_model_price_columns",
        "模型真实计费:gov_llm_model_configs 新增输入/输出单价(元/百万 token,可空)。"
        "手填单价优先级最高,其次 litellm 内置价格表自动匹配(deepseek/gpt/claude/qwen 等),"
        "兜底统一估算价(runtime.fallback_price_cny_per_m)。幂等:全新库 create_all 已含两列",
        f"""
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS input_price_cny_per_m NUMERIC(10,4);
        ALTER TABLE {P}gov_llm_model_configs ADD COLUMN IF NOT EXISTS output_price_cny_per_m NUMERIC(10,4);
        """,
    ),
    Migration(
        "0029_descriptive_fields_to_text",
        "描述性字段统一改 TEXT:Agent 人格/开场白、Prompt 正文、自定义数据源 SQL、"
        "评测实际输出。与 0009 同理——VARCHAR(N) 会静默截断,长文本(人格设定/提示词/SQL)"
        "可能被截断导致模型基于残缺内容作答;actual_output 存的是 LLM 完整输出。"
        "注:原对 tc_agents.description 的 ALTER 已随 0045(该列整体删除,description 改由"
        "版本快照承载)一并移除——否则全新库按 create_all 基线顺序执行到本条时该列已不存在,"
        "直接报 column does not exist。存量库(schema_migrations 已记录 0029)照常跳过;"
        "各环境最终结构仍由模型 + 全链收敛一致(description 的终态由 0045 决定:不存在)",
        f"""
        ALTER TABLE {P}tc_agents             ALTER COLUMN persona_desc TYPE TEXT;
        ALTER TABLE {P}tc_agents             ALTER COLUMN opening_line TYPE TEXT;
        ALTER TABLE {P}tc_prompts            ALTER COLUMN content TYPE TEXT;
        ALTER TABLE {P}kb_db_custom_sources  ALTER COLUMN build_sql TYPE TEXT;
        ALTER TABLE {P}gov_eval_runs         ALTER COLUMN actual_output TYPE TEXT;
        """,
    ),
    Migration(
        "0030_model_price_table",
        "模型单价配置表(gov_llm_model_prices):由「系统设置 · 模型单价」卡片统一管理,"
        "替代 litellm 每次进程启动实时拉取 GitHub 计费表的逻辑。cost.py 按 model_id "
        "精确匹配取价(元/百万 token),优先级:单价表 > 模型配置手填 > 兜底估算。"
        "幂等:全新库 create_all 已含本表",
        f"""
        CREATE TABLE IF NOT EXISTS {P}gov_llm_model_prices (
            id VARCHAR(36) PRIMARY KEY,
            model_id VARCHAR(128) UNIQUE NOT NULL,
            input_price_cny_per_m NUMERIC(10,4),
            output_price_cny_per_m NUMERIC(10,4),
            updated_by VARCHAR(36) DEFAULT '',
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        """,
    ),
    Migration(
        "0031_gov_daily_usage",
        "当日用量日汇总表(gov_daily_usage):计费后置后预算检查 O(1) 读取的基础,"
        "替代实时聚合 gov_llm_usage 大表(表越大聚合越慢,且挡在每次对话第一环)。"
        "cost_subscriber 在 usage 事件落库时原子 UPSERT 累加;scenic_id 空串=集团级。"
        "幂等:全新库 create_all 已含本表",
        f"""
        CREATE TABLE IF NOT EXISTS {P}gov_daily_usage (
            id VARCHAR(64) PRIMARY KEY,
            scenic_id VARCHAR(64) DEFAULT '',
            usage_date DATE NOT NULL,
            prompt_tokens BIGINT NOT NULL DEFAULT 0,
            completion_tokens BIGINT NOT NULL DEFAULT 0,
            cost_cny NUMERIC(10, 4) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_gov_daily_usage_scenic_date UNIQUE (scenic_id, usage_date)
        );
        CREATE INDEX IF NOT EXISTS ix_{P}gov_daily_usage_usage_date
            ON {P}gov_daily_usage (usage_date);
        """,
    ),
    Migration(
        "0032_skill_description",
        "Skill 新增 description:能力一句话概述(面向意图识别路由器的能力清单,"
        "行业描述驱动路由器的标准元数据)。留空时运行时回退 system_prompt 前120字,"
        "不破坏存量 Skill。物化时由版本快照 apply 写回,与 name 同属资源元数据",
        f"ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS description TEXT;",
    ),
    Migration(
        "0033_agent_intent_config",
        "意图识别 agent 化(v1.1.5):intent_config 挂到每个 Agent 下,而非框架级。"
        "一个 Agent 一个意图识别,由它来分配 Agent 下的技能(L1 工作流 / L2 意图识别)。"
        "结构 {enabled, mode: always|side_effects|off, model, prompt, recheck_before_clarify},"
        "未配置(None)时运行时回退框架默认模板/模型/判定策略",
        f"ALTER TABLE {P}tc_agents ADD COLUMN IF NOT EXISTS intent_config JSONB;",
    ),
    Migration(
        "0034_deleted_at_soft_delete",
        "删除保护加固:Tool/Skill/Agent 统一软删除标记 deleted_at。删除=归档归档,"
        "列表/详情/更新均过滤;版本记录(tc_resource_versions)永久保留(发布物不可变,"
        "支持审计/回滚/成本归因)。active 保留「启用/停用」语义,与删除归档分离",
        f"""
        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE {P}tc_agents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        """,
    ),
    Migration(
        "0035_tool_name_unique_excludes_deleted",
        "软删工具名释放:uq_tc_tools_scope_name 改为部分唯一索引(WHERE deleted_at IS NULL),"
        "唯一性只约束未删除行;软删归档行不再占用 (coalesce(scenic_id,''), name),"
        "删除后同租户可重建同名工具。行业做法:让 deleted_at 参与唯一索引。"
        "先 DROP 旧全表唯一索引,再 CREATE 同定义部分索引(IF NOT EXISTS 幂等)",
        f"""
        DROP INDEX IF EXISTS uq_tc_tools_scope_name;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_tools_scope_name
            ON {P}tc_tools (coalesce(scenic_id, ''), name) WHERE deleted_at IS NULL;
        """,
    ),
    Migration(
        "0036_llm_prompt_logs",
        "LLM 提示词日志表(llm_prompt_logs):v1.1.6 会话信息技能增强——技能要能查到"
        "「当前会话每次大模型调用,LLM 真正收到的最终提示词」并做提示词质量分析"
        "(业务无关噪音 / 内部信息泄漏排查)。engine 各节点在真正发出调用前把最终"
        "messages 沉淀为一行:system_prompt 完整(主 prompt + extra_systems 拼接),"
        "history 逐条截断控制体积;step=intent_check/generation/drift_check。"
        "记录失败不影响主流程;查询按 conversation_id 聚合即还原「某会话每轮 LLM 看到什么」。"
        "幂等:全新库 create_all 已含本表,老库由本条补齐",
        f"""
        CREATE TABLE IF NOT EXISTS {P}llm_prompt_logs (
            id              SERIAL PRIMARY KEY,
            task_id         VARCHAR(36)  NOT NULL,
            conversation_id VARCHAR(48),
            scenic_id       VARCHAR(64),
            user_id         VARCHAR(36),
            step            VARCHAR(24)  NOT NULL,
            model           VARCHAR(64),
            prompt_tokens   INTEGER,
            system_prompt   TEXT,
            history         TEXT,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_{P}llm_prompt_logs_conversation_id
            ON {P}llm_prompt_logs (conversation_id);
        CREATE INDEX IF NOT EXISTS ix_{P}llm_prompt_logs_task_id
            ON {P}llm_prompt_logs (task_id);
        CREATE INDEX IF NOT EXISTS ix_{P}llm_prompt_logs_scenic_id
            ON {P}llm_prompt_logs (scenic_id);
        CREATE INDEX IF NOT EXISTS ix_{P}llm_prompt_logs_created_at
            ON {P}llm_prompt_logs (created_at);
        """,
    ),
    Migration(
        "0037_skill_composition_created_at",
        "已由 0055 取代(tc_skill_compositions 整表删除),本迁移降级为 no-op:"
        "全新库/清表重跑时该表已不存在,原 ALTER 会报表不存在;"
        "存量排序语义已随组合层一并作废。老库(schema_migrations 已记录 0037)同样跳过",
        "SELECT 1;",
    ),
    Migration(
        "0038_remove_default_version_and_user_only_assign",
        "15 号文档(默认版本删除与分发中心以人为维度设计):"
        "D1-B 物理删除 is_default 列(无默认版本概念,发布不再自动成为任何人的当前版本);"
        "D9-A 分发收敛按用户:清理存量 scenic/global 指派行(运行时已无此维度),"
        "唯一索引 uq_agent_assign_scope 由 (agent_id, scope_type, scope_id) 收紧为 (agent_id, scope_id)。"
        "幂等:IF EXISTS / IF NOT EXISTS,老库执行一次,新库(create_all 已无 is_default)直接跳过",
        f"""
        DROP INDEX IF EXISTS ix_res_ver_is_default;
        ALTER TABLE {P}tc_resource_versions DROP COLUMN IF EXISTS is_default;
        DELETE FROM {P}tc_agent_assignments WHERE scope_type IS DISTINCT FROM 'user';
        DROP INDEX IF EXISTS uq_agent_assign_scope;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_assign_scope
            ON {P}tc_agent_assignments (agent_id, scope_id);
        """,
    ),
    Migration(
        "0039_skill_version_column",
        "Skill 版本号落地:tc_skills 补 version 列。模型 Skill.version(String(32) "
        "NOT NULL DEFAULT 'v1')早已声明,但 dev 等老库从未加过该列,导致列表查询 "
        "SELECT skills.version 报 column does not exist(管理员进 Skill 菜单 500)。"
        "幂等:IF NOT EXISTS,老库由本条补齐,新库 create_all 已含该列",
        f"""
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS version VARCHAR(32) NOT NULL DEFAULT 'v1';
        """,
    ),
    Migration(
        "0040_tc_tools_description_to_text",
        "修复列类型漂移:tc_tools.description 对齐模型 Text。模型在提交 7753315 "
        "(v1.1 阶段三)已从 String(255) 放宽为 Text 并注明'长度不定',但当时只改了模型、"
        "漏写了 ALTER 迁移,dev 等老库列至今仍是 varchar(255)。本地 16 个工具中 15 个 "
        "description 超过 255 字符,迁移 Agent 到 dev 时 INSERT 直接 500 "
        "(StringDataRightTruncationError)。同类场景(0009/0029/0032/0036)当年都写了迁移,"
        "唯独此列遗漏。"
        "注:该列已由 0051(删除 tc_tools 主表内容列,description 改由版本快照承载)整体删除,"
        "本条随之降级为 no-op——否则全新库按 create_all 基线顺序执行到本条时该列已不存在,"
        "直接报 column does not exist。存量库(schema_migrations 已记录 0040)照常跳过;"
        "各环境最终结构仍由模型 + 全链收敛一致(description 的终态由 0051 决定:不存在)",
        "SELECT 1;",
    ),
    Migration(
        "0041_resource_version_locked_deps_to_json",
        "修复列类型漂移:tc_resource_versions.locked_deps 对齐模型 JSON。模型(versioning.py "
        "ResourceVersion)自 0027 起一直声明标准 SQLAlchemy JSON(PG json),0027 建列时却误用 "
        "JSONB 且未补 ALTER,属历史存量漂移——过去只读体检仅告警、启动自动补列兜底时代未拦截,"
        "2026-09 自动补列下线后体检改硬拦截暴露。jsonb->json 无损(规范化后信息不丢失),运行时"
        "读写无差异。幂等:已是 json 的列重复执行是 no-op(PostgreSQL 直接跳过),新库 create_all "
        "本就用模型建列,老库由本条收敛",
        f"""
        ALTER TABLE {P}tc_resource_versions ALTER COLUMN locked_deps TYPE JSON USING locked_deps::json;
        """,
    ),
    Migration(
        "0042_drop_tc_prompts_stale_version_columns",
        "清理 tc_prompts 废弃列 version/is_current:早期 create_all 时代 Prompt 曾带独立版本化"
        "(version 标记版本号、is_current 标记当前生效行),后模型改为由 Skill.version 统一管理"
        "(Prompt 仅 id/skill_id/content,见 skill/models.py),但迁移链从未对老库 DROP 这两列。"
        "version 为 NOT NULL 且无默认,任何插入新 prompt 的路径(如跨环境能力迁移 POST /skills)"
        "都会 IntegrityError 500——dev 迁移 Agent 时当场暴露。冗余列不触发缺列/类型体检盲区,"
        "按铁律用迁移收敛。幂等:IF EXISTS,新库(create_all 按模型建表)无此列直接跳过",
        f"""
        ALTER TABLE {P}tc_prompts DROP COLUMN IF EXISTS version;
        ALTER TABLE {P}tc_prompts DROP COLUMN IF EXISTS is_current;
        """,
    ),
    Migration(
        "0043_drop_master_status_columns",
        "废弃三张主表(tc_agents/tc_skills/tc_tools)的 status 列:15 号文档后版本表是发布唯一权威"
        "(发布只写 tc_resource_versions.status='published'),主表 status 无任何写点"
        "(创建即定值后不再转态),运行期判定已改为查版本表 published 存在性,停用由 active 承担,"
        "status 沦为恒不变的身份字段故删除。保留 active/visibility/deleted_at 等管理位。"
        "幂等:IF EXISTS,新库(create_all 按模型建表,模型已删列)无此列直接跳过",
        f"""
        ALTER TABLE {P}tc_agents DROP COLUMN IF EXISTS status;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS status;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS status;
        """,
    ),
    Migration(
        "0044_drop_master_version_columns",
        "废弃主表(tc_skills/tc_tools)的 version 列:15 号文档后版本号唯一权威在统一版本表"
        "(tc_resource_versions.version,发布号),主表 version 自草稿化改造后无任何写点"
        "(创建 default='v1' 落初值后永不递增——update 只写版本草稿,不再直改主表),"
        "读点仅剩 get_tool/get_skill 序列化残留,前端已不消费。删除后对齐 Agent"
        "(主表本无 version)。15 号 §4.3 已列 D8-A 删除候选;并顺带清理遗留表"
        "tc_tool_versions(工具版本已统一收敛到 tc_resource_versions,模型已删,库残留冗余表)。"
        "幂等:IF EXISTS,新库(create_all 按模型建表)无此列/此表直接跳过",
        f"""
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS version;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS version;
        DROP TABLE IF EXISTS {P}tc_tool_versions;
        """,
    ),
    Migration(
        "0045_drop_master_agent_description",
        "删除 tc_agents.description 主表列:description 自 v1.1.6 版本化(编辑=草稿快照、"
        "发布=冻结快照)后,主表列仅剩「新建初值 / 老快照缺键展示兜底」两处读点;"
        "v1.1.8 列表/详情/API Key 已统一读最新 published 快照,v1.1.9 移除兜底、"
        "create_agent 改为创建即派生首草稿(description 进草稿快照),主表列彻底无读写点,"
        "删除以杜绝对其误读误写(同 0043 删 status 模式)。未发布 / 老版本快照无"
        "description 属预期,发布快照中必有。幂等:IF EXISTS,新库(create_all 按模型建表,"
        "模型已删列)无此列直接跳过",
        f"ALTER TABLE {P}tc_agents DROP COLUMN IF EXISTS description;",
    ),
    Migration(
        "0046_app_question_bank_metric_def_prewarm_cache",
        "19 号文档(APP 固定问题工作流实施定稿):APP 问题库/指标定义/预热缓存三表。"
        "问题库用 dimension_name 吸收三级选择器,composition_id 统一指向 tc_skill_compositions"
        "(单技能组合是退化情形);指标定义承接 insight_service 硬编码的分色阈值;"
        "预热缓存 (question_id, scenic_id) 唯一。幂等:IF NOT EXISTS,新库 create_all 已含",
        f"""
        CREATE TABLE IF NOT EXISTS {P}app_question_bank (
            id              VARCHAR(36) PRIMARY KEY,
            scenic_id       VARCHAR(64),
            theme_code      VARCHAR(32) NOT NULL,
            question_text   VARCHAR(512) NOT NULL,
            metric_code     VARCHAR(64),
            scenario        VARCHAR(32),
            time_period     VARCHAR(32) NOT NULL,
            priority        VARCHAR(4) NOT NULL DEFAULT 'P1',
            dimension_name  VARCHAR(64),
            route_mode      VARCHAR(16) NOT NULL DEFAULT 'auto',
            composition_id  VARCHAR(36),
            agent_id        VARCHAR(36),
            prewarm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            prewarm_point   VARCHAR(16),
            next_run_at     TIMESTAMPTZ,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS idx_qbank_theme ON {P}app_question_bank(theme_code, active);
        CREATE INDEX IF NOT EXISTS idx_qbank_text ON {P}app_question_bank(question_text);
        CREATE INDEX IF NOT EXISTS idx_qbank_next_run ON {P}app_question_bank(next_run_at);
        CREATE TABLE IF NOT EXISTS {P}app_metric_def (
            id              VARCHAR(36) PRIMARY KEY,
            theme_code      VARCHAR(32) NOT NULL,
            metric_code     VARCHAR(64) NOT NULL,
            metric_name     VARCHAR(128) NOT NULL,
            metric_desc     TEXT,
            warning_rule    VARCHAR(128),
            danger_rule     VARCHAR(128),
            time_node       VARCHAR(32),
            update_strategy VARCHAR(16) NOT NULL,
            priority        VARCHAR(4) NOT NULL DEFAULT 'P1',
            created_at      TIMESTAMPTZ DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_metric_theme_code UNIQUE (theme_code, metric_code)
        );
        CREATE TABLE IF NOT EXISTS {P}app_prewarm_cache (
            id           VARCHAR(36) PRIMARY KEY,
            question_id  VARCHAR(36) NOT NULL,
            scenic_id    VARCHAR(64) NOT NULL,
            task_id      VARCHAR(36) NOT NULL,
            answer       TEXT NOT NULL,
            structured   JSON,
            data_as_of   TIMESTAMPTZ NOT NULL,
            generated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_prewarm_question_scenic UNIQUE (question_id, scenic_id)
        );
        """,
    ),
    Migration(
        "0047_agents_composition_mode",
        "19 号文档 D4-①:Agent 内部协作模式收进 agent 自身(tc_agents.composition_mode)。"
        "原「从 tc_skill_compositions 按绑定集合相等回填 mode」的 UPDATE 已移除——"
        "0055 已删该表,保留该 UPDATE 会让全新库/清表重跑直接报表不存在。"
        "存量老库:该回填在 0055 之前已执行完毕,结果已固化在 tc_agents.composition_mode 列;"
        "全新库:create_all 按模型建列(默认 chain),agent 自身模式由管理端设置。"
        "幂等:IF NOT EXISTS",
        f"ALTER TABLE {P}tc_agents ADD COLUMN IF NOT EXISTS composition_mode VARCHAR(8) NOT NULL DEFAULT 'chain';",
    ),
    Migration(
        "0048_question_bank_param_template",
        "19 号文档 S7.5:问题库参数模板——固定问题命中后代码展开基准日期参数注入 slots,"
        "替代 Triager 的 LLM 日期推导(零 LLM、零延迟)。相对参数(对比期)复用 L3 x-default-rule "
        "规则引擎(基准已填时自动平移补全)。幂等:IF NOT EXISTS,新库 create_all 已含",
        f"ALTER TABLE {P}app_question_bank ADD COLUMN IF NOT EXISTS param_template JSON;",
    ),
    Migration(
        "0049_prewarm_cache_enrich_columns",
        "20 号文档:缓存表二次加工派生列——summary(卡片60字摘要,B方案轻量LLM)、"
        "overview_metrics(概览4条彩色指标,分色读 app_metric_def)、enrich_task_id"
        "(二次加工任务防重标记,收割后更新)。幂等:IF NOT EXISTS,新库 create_all 已含",
        f"""
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS summary VARCHAR(128);
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS overview_metrics JSON;
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS enrich_task_id VARCHAR(36);
        """,
    ),
    Migration(
        "0050_drop_master_skill_content_columns",
        "删除 tc_skills 主表内容列 description/tool_usage/suggested_mode/knowledge_sources/"
        "constraints:skill 内容键 v1.1.10 起纯快照化(同 agent description 0045 模式,适配器"
        "extra_keys)——create_skill 已改为创建即派生首草稿(内容落草稿快照),编辑=草稿快照、"
        "发布=冻结快照,运行时(13 号 Phase 3)一律从 published 快照解析,列表/详情/校验均"
        "以快照为准(上一版误读主表初值致发布质检误拦,见 _require_skill_quality 修订)。"
        "主表列彻底无读写点,删除以杜绝对其误读误写(同 0043 删 status / 0045 删 agent "
        "description 模式)。先回填后删:把「创建于版本化前、从未编辑/发布(无任何版本记录)」"
        "的存量 skill 主表内容物化为草稿(快照键与编辑保存同构,tool_ids 用旧键,发布时 "
        "normalize 为 tools pin),防删列丢数据;已发布/有草稿的 skill 内容本就在快照,不触碰。"
        "幂等:回填仅在无任何版本记录时插入;DROP 用 IF EXISTS,新库(create_all 按模型建表,"
        "模型已删列)无此列直接跳过。"
        "注:回填的 SELECT 需要读本迁移即将删除的 5 个列,而全新库按最终模型建表时这些列"
        "已不存在,直接执行会报 column does not exist——故在回填前先按历史形态"
        "ADD COLUMN IF NOT EXISTS 补齐(老库这些列本就在,IF NOT EXISTS 直接跳过,语义不变);"
        "全新库无存量行,回填自然 0 行,紧随其后的 DROP COLUMN 再把临时补的列一并删掉,"
        "最终结构与模型一致",
        f"""
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS description TEXT;
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS tool_usage JSON;
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS suggested_mode VARCHAR(8);
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS knowledge_sources JSON;
        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS constraints JSON;
        INSERT INTO {P}tc_resource_versions
          (id, resource_type, resource_id, version, status, snapshot, note, created_by,
           created_at, updated_at)
        SELECT 'ver_m0050_' || md5(s.id::text), 'skill', s.id, NULL, 'draft',
               jsonb_build_object(
                 'description', s.description,
                 'tool_usage', s.tool_usage,
                 'suggested_mode', COALESCE(s.suggested_mode, 'L2'),
                 'knowledge_sources', s.knowledge_sources,
                 'constraints', s.constraints,
                 'tool_ids', s.tool_ids),
               '0050 迁移:存量主表内容物化为草稿(防删列丢数据)',
               'system:migration_0050', now(), now()
        FROM {P}tc_skills s
        WHERE s.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM {P}tc_resource_versions v
                          WHERE v.resource_type = 'skill' AND v.resource_id = s.id)
          AND (s.description IS NOT NULL OR s.tool_usage IS NOT NULL
               OR s.knowledge_sources IS NOT NULL OR s.constraints IS NOT NULL
               OR s.suggested_mode IS NOT NULL OR s.tool_ids IS NOT NULL);
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS description;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS tool_usage;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS suggested_mode;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS knowledge_sources;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS constraints;
        """,
    ),
    Migration(
        "0051_drop_master_tool_content_columns",
        "删除 tc_tools 主表内容列 description/type/type_config/risk_level:Tool 与 "
        "Skill/Agent 同构(v1.2)——内容键纯快照化,适配器 fields 置空、extra_keys 承载,"
        "主表仅身份目录(name/owner/scenic/visibility/active/source_mcp_server_id)。"
        "配套改动:create_tool/MCP discover 导入改为创建即派生草稿并自动发布 v1(叶子工具"
        "保留「创建即生效」体验且让技能 refresh-pins 有 published 版本可钉),update_tool "
        "内容改动落草稿快照(visibility 直写主表,修复其被快照键过滤静默丢弃的 bug),运行时"
        "(resolver direct 工具执行 / invoke_tool 未注入兜底)一律从版本快照取内容。"
        "先回填后删:把「创建于版本化前、从未编辑/发布(无任何版本记录)」的存量工具主表"
        "内容物化为草稿(快照键与编辑保存同构),防删列丢数据;已有版本的工具内容本就在"
        "快照,不触碰。幂等:回填仅在无任何版本记录时插入;DROP 用 IF EXISTS,新库"
        "(create_all 按模型建表,模型已删列)无此列直接跳过。"
        "注:回填的 SELECT 需要读本迁移即将删除的 4 个列,而全新库按最终模型建表时这些列"
        "已不存在,直接执行会报 column does not exist——故在回填前先按历史形态"
        "ADD COLUMN IF NOT EXISTS 补齐(老库这些列本就在,IF NOT EXISTS 直接跳过,语义不变);"
        "全新库无存量行,回填自然 0 行,紧随其后的 DROP COLUMN 再把临时补的列一并删掉,"
        "最终结构与模型一致",
        f"""
        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS description TEXT;
        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS type VARCHAR(32);
        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS type_config JSON;
        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS risk_level VARCHAR(16);
        INSERT INTO {P}tc_resource_versions
          (id, resource_type, resource_id, version, status, snapshot, note, created_by,
           created_at, updated_at)
        SELECT 'ver_m0051_' || md5(s.id::text), 'tool', s.id, NULL, 'draft',
               jsonb_build_object(
                 'description', COALESCE(s.description, ''),
                 'type', COALESCE(s.type, ''),
                 -- 老库 tc_tools.type_config 是 json 类型,cast 成 jsonb 才能进 jsonb_build_object
                 'type_config', COALESCE(s.type_config::jsonb, '{{}}'::jsonb),
                 'risk_level', COALESCE(s.risk_level, 'low')),
               '0051 迁移:存量主表内容物化为草稿(防删列丢数据)',
               'system:migration_0051', now(), now()
        FROM {P}tc_tools s
        WHERE s.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM {P}tc_resource_versions v
                          WHERE v.resource_type = 'tool' AND v.resource_id = s.id)
          AND (s.description IS NOT NULL OR s.type IS NOT NULL
               OR s.type_config IS NOT NULL OR s.risk_level IS NOT NULL);
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS description;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS type;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS type_config;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS risk_level;
        """,
    ),
    Migration(
        "0052_app_question_bank_metric_name",
        "三级选择器第三级「指标」展示名:app_question_bank 新增 metric_name 列(方案 A:"
        "主题→维度→指标,一个指标=一个叶子问题=该行 question_id)。11 号口径第二级=指标、"
        "第三级=问题,19 号起第二级演化为维度(dimension_name),第三级叶子一直是问题行但"
        "APP 端期望看到「指标名」短标签而非整句问题——补该列供 home/items[].name 返回。"
        "与 app_metric_def.metric_name(全局定义阈值名)语义区分,运营维护即权威。幂等:IF NOT EXISTS",
        f"ALTER TABLE {P}app_question_bank ADD COLUMN IF NOT EXISTS metric_name VARCHAR(128);",
    ),
    Migration(
        "0053_app_question_bank_return_format",
        "问题级图表返回形态标注(22 号文档):app_question_bank 新增 return_format 列——"
        "auto=不干预 LLM 自由输出 cards/charts(默认);card/line/bar/pie/radar=该问题运营"
        "声明的图表形态。纯运营标注,运行时(graphs/网关/预热)不消费、不据此注入提示词——"
        "图表引导须内置于绑定技能自身提示词(create_p0_group_skills.PROMPT_TMPL 的"
        "PRD 3.2.2.4 段);标注唯一用途 = 自检确认「问题要的形态 ↔ 绑定技能是否教了怎么出」"
        "(question_bank 图表引导自检 + scripts/check_question_chart_guidance.py)。幂等:IF NOT EXISTS",
        f"ALTER TABLE {P}app_question_bank ADD COLUMN IF NOT EXISTS return_format VARCHAR(16) "
        f"NOT NULL DEFAULT 'auto';",
    ),
    Migration(
        "0054_prewarm_cache_biz_date",
        "22 号复盘(预热按业务日留存):app_prewarm_cache 加 biz_date 列(业务日=任务创建日),"
        "唯一键由 (question_id, scenic_id) 扩为 (question_id, scenic_id, biz_date)——每景区×问题"
        "每天一行,历史按日留存、消费只认当日行(今天未出=空态,不回退昨日);answer 放开 "
        "NOT NULL(初派报告任务即建当日占位行 answer=NULL,任务 COMPLETED 收割时 UPDATE 补全)。"
        "回填 biz_date=generated_at::date(老库每 (question_id, scenic_id) 至多一行 → 三元组自然"
        "唯一,建约束安全)。幂等:IF NOT EXISTS/DROP IF EXISTS;新库 create_all 按模型已含新列新约束。"
        "注:ADD CONSTRAINT 在 PostgreSQL 里没有 IF NOT EXISTS 语法,全新库 create_all 已按模型"
        "建好同名唯一约束,直接 ADD 会报 relation already exists——故先 DROP CONSTRAINT IF EXISTS "
        "同名约束再 ADD(老库为 no-op + 重建,新库为删除同名后重建,结果一致)",
        f"""
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS biz_date DATE;
        UPDATE {P}app_prewarm_cache SET biz_date = generated_at::date WHERE biz_date IS NULL;
        ALTER TABLE {P}app_prewarm_cache ALTER COLUMN biz_date SET NOT NULL;
        ALTER TABLE {P}app_prewarm_cache ALTER COLUMN answer DROP NOT NULL;
        ALTER TABLE {P}app_prewarm_cache DROP CONSTRAINT IF EXISTS uq_prewarm_question_scenic;
        ALTER TABLE {P}app_prewarm_cache DROP CONSTRAINT IF EXISTS uq_prewarm_q_scenic_date;
        ALTER TABLE {P}app_prewarm_cache ADD CONSTRAINT uq_prewarm_q_scenic_date
            UNIQUE (question_id, scenic_id, biz_date);
        """,
    ),
    Migration(
        "0055_drop_skill_compositions_and_question_routing",
        "23 号文档(APP 固定问题路由简化,去掉编排组合中间层):"
        "① app_question_bank 新增 skill_id 直接绑定执行技能,删除 composition_id 与 route_mode——"
        "原 composition_id 指向 tc_skill_compositions,而该表在 APP 链路里是恒等转发层(组合恒"
        "单技能,skill_ids_ordered[0] 恒等于直接绑技能),route_mode=auto/workflow 语义由 skill_id "
        "是否为空派生;"
        "② DROP tc_skill_compositions(整表删除;agent 路径编排走 Agent.composition_mode,不依赖此表)。"
        "迁移只做结构,不回填 skill_id——技能 id 各环境独立 UUID,回填一律走脚本按技能名锚定"
        "(scripts/backfill_question_skills.py --env local|dev)。"
        "幂等:IF NOT EXISTS/IF EXISTS;新库 create_all 按新模型建表,天然无此表/此二列",
        f"""
        ALTER TABLE {P}app_question_bank ADD COLUMN IF NOT EXISTS skill_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_{P}app_question_bank_skill_id
            ON {P}app_question_bank (skill_id);
        ALTER TABLE {P}app_question_bank DROP COLUMN IF EXISTS composition_id;
        ALTER TABLE {P}app_question_bank DROP COLUMN IF EXISTS route_mode;
        DROP TABLE IF EXISTS {P}tc_skill_compositions;
        """,
    ),
    Migration(
        "0056_drop_agent_related_questions",
        "删除 Agent 关联推荐追问(24 号定稿):APP 端问题全部由问题库配置,"
        "推荐入口走问题库,agent 预设追问不再使用;且管理端编辑无关闭入口、"
        "配置漂移无治理路径,整体下线。删除 0026 加的 related_questions_enabled/"
        "related_questions 两列(幂等:IF EXISTS;新库 create_all 按最终模型建表,"
        "链上 0026 加列→本条删列,结构收敛一致)。旧任务 result 与旧版本快照中的"
        "残留键为历史数据,代码已不读取,无需清理。",
        f"""
        ALTER TABLE {P}tc_agents DROP COLUMN IF EXISTS related_questions_enabled;
        ALTER TABLE {P}tc_agents DROP COLUMN IF EXISTS related_questions;
        """,
    ),
    Migration(
        "0057_prewarm_cache_ota_account",
        "OTA 数据按「景区×账号」维度拆分(25 号):不同商户账号(=ota_credentials."
        "platforms.*.username)取到的数据不同,缓存行唯一键从 (question_id, scenic_id,"
        " biz_date) 扩展到 (question_id, scenic_id, COALESCE(ota_account,''), biz_date)。"
        "NULL=账号无关行(客流/票务/舆情;概览基础版),非 NULL=该账号专属行"
        "(OTA 主题;概览账号版)。幂等:IF NOT EXISTS;旧唯一约束 DROP 后改表达式唯一索引"
        "(PG 唯一约束不支持 COALESCE 表达式);新库 create_all 按新模型建表天然含。",
        f"""
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS ota_account VARCHAR(128);
        ALTER TABLE {P}app_prewarm_cache DROP CONSTRAINT IF EXISTS uq_prewarm_q_scenic_date;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_prewarm_q_scenic_acct_date
            ON {P}app_prewarm_cache (question_id, scenic_id, COALESCE(ota_account, ''), biz_date);
        """,
    ),
    Migration(
        "0058_app_scenic_config",
        "景区配置表(25 号方案):景区维度配置,首期承载量(日最大人次)。"
        "挂景区不挂用户,核心列强校验 + extra JSONB 承载后期扩展配置"
        "(带 schema_version);未配置=消费侧维持现状,不猜默认值。"
        "幂等:IF NOT EXISTS;新库 create_all 按新模型建表天然含。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}app_scenic_config (
            id             VARCHAR(36) PRIMARY KEY,
            scenic_id      VARCHAR(64) NOT NULL,
            daily_capacity INTEGER,
            extra          JSON NOT NULL DEFAULT '{{}}',
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by     VARCHAR(64),
            CONSTRAINT uq_app_scenic_config_scenic UNIQUE (scenic_id),
            CONSTRAINT ck_app_scenic_daily_capacity_pos
                CHECK (daily_capacity IS NULL OR daily_capacity > 0)
        );
        """,
    ),
    Migration(
        "0059_prewarm_cache_skill_version",
        "固定问题的技能版本严格随用户指派(27 号:版本随指派,消费全部严格匹配,"
        "无指派=不可用,删除「最新 published」兜底)——预热缓存行增加 skill_version"
        "维度并进唯一键:同景区用户指派版本一致(常态)仍一行,版本分流则各版本"
        "一行;NULL=旧数据按 -1 桶,不匹配任何期望版本,随留存期自然淘汰。"
        "幂等:ADD COLUMN IF NOT EXISTS;旧唯一索引 DROP 后重建(含版本表达式)。",
        f"""
        ALTER TABLE {P}app_prewarm_cache ADD COLUMN IF NOT EXISTS skill_version INTEGER;
        DROP INDEX IF EXISTS uq_prewarm_q_scenic_acct_date;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_prewarm_q_scenic_acct_ver_date
            ON {P}app_prewarm_cache (question_id, scenic_id,
                COALESCE(ota_account, ''), COALESCE(skill_version, -1), biz_date);
        """,
    ),
    Migration(
        "0060_tc_context_params",
        "28 号:上下文参数 provider 资源化——装配器的数据源连接器从框架代码迁到 DB"
        "(与 agent/skill/tool 同构的配置化设计,新业务接入=配一行零框架代码)。"
        "内置 daily_capacity(查 app_scenic_config)与 supplier_ids(直调数据大脑接口)"
        "两个业务 provider 迁为配置行;ota_account 保留 builtin 执行(调用方显式传>"
        "用户首账号>Absent 的回退链是框架机制语义),但其缺失文案改由本行承载"
        "(单一来源)。supplier_ids 的 url/key 含凭证,迁移里不落值——由"
        "scripts/backfill_context_params.py 从 runtime 配置回填。幂等:IF NOT EXISTS/ON CONFLICT DO NOTHING。"
        "注:下面的种子行写了 scope 列,而 scope 原本要到 0061 才加 —— 全新库执行到本条时"
        "(该表的模型已随 0070 删除,create_all 不会建它)直接报 column does not exist。"
        "故建表时就带上 scope(与 0061 同定义);存量库已记录本条直接跳过,0061 是 IF NOT EXISTS,"
        "两条在新老库上的终态一致。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_context_params (
            id VARCHAR(36) PRIMARY KEY,
            param_key VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(128) NOT NULL DEFAULT '',
            description VARCHAR(512) NOT NULL DEFAULT '',
            type VARCHAR(16) NOT NULL,
            type_config JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            scope JSONB NOT NULL DEFAULT '["app"]'::jsonb,
            absent_reason VARCHAR(64),
            absent_answer VARCHAR(512),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ
        );
        INSERT INTO {P}tc_context_params (id, param_key, name, description, type, type_config,
            scope, absent_reason, absent_answer, enabled) VALUES
        ('seed-cp-ota-account', 'ota_account', 'OTA 商户账号',
         '当前用户绑定的第一个 OTA 商户账号(实时链路);预热按行账号走 explicit 显式传',
         'builtin', '{{}}', '["app"]',
         'user_unbound',
         '该景区尚未配置 OTA 账号,暂无数据。请在「我的 → OTA 账号配置」中完成配置后重试。',
         TRUE),
        ('seed-cp-daily-capacity', 'daily_capacity', '景区日最大承载量',
         '景区日最大承载量(app_scenic_config),提示词写报告/预警分析用',
         'db',
         '{{"table": "app_scenic_config", "column": "daily_capacity", '
         '"where": {{"scenic_id": "{{{{scenic_id}}}}"}}, "value_type": "int"}}',
         '["app"]', NULL, NULL, TRUE),
        ('seed-cp-supplier-ids', 'supplier_ids', '景区供应商 ID 列表',
         '数据大脑取数的供应商集合(url/凭证由 backfill_context_params.py 回填)',
         'http',
         '{{"method": "POST", "url": "", "headers": {{"xpftkey": ""}}, '
         '"body": {{"scenicId": "{{{{scenic_id}}}}"}}, '
         '"result_path": "data.0.supplierId", "timeout": 10}}',
         '["app"]', NULL, NULL, TRUE)
        ON CONFLICT (param_key) DO NOTHING;
        """,
    ),
    Migration(
        "0061_tc_context_params_scope",
        "28 号:上下文参数源按**端**匹配——tc_context_params 加 scope 列(JSONB 端码数组,"
        "默认 [\"app\"]),装配时只执行 scope 含当前端码或 \"*\" 的行。为什么按端不是"
        "按 agent/skill:装配发生在网关入口(端已知,agent 要到执行期才解析);"
        "上下文是「用户×景区×端」的事实而非 agent 属性;supplier_ids 这类被几十个"
        "技能共享的参数按 agent 绑=每建一个都要记得绑全套(漏配运行期才暴露)。"
        "各端同名 key 自然隔离。幂等:ADD COLUMN IF NOT EXISTS。",
        f"""
        ALTER TABLE {P}tc_context_params
            ADD COLUMN IF NOT EXISTS scope JSONB NOT NULL DEFAULT '["app"]'::jsonb;
        """,
    ),
    Migration(
        "0062_tc_context_params_callback",
        "28 号修正:框架零业务——ota_account 的取数逻辑从 runtime_service(框架)"
        "迁到 gateway_service.ota_accounts(业务侧),以 register_callback_provider "
        "注册;框架只保留扩展点(回调注册表+显式值优先机制 build_app_params("
        "explicit=...))。tc_context_params 的 type 由 builtin 改名 callback"
        "(语义:该行对应的取数函数由业务侧模块注册,行承载展示名/缺失文案/端/启停)。"
        "幂等:UPDATE 无副作用。",
        f"""
        UPDATE {P}tc_context_params SET type = 'callback'
        WHERE param_key = 'ota_account' AND type = 'builtin';
        """,
    ),
    Migration(
        "0064_question_model_instruction",
        "快捷问题新增「问题描述」(model_instruction):真正发给大模型的说明。"
        "与 question_text(前台按钮文案)分工不同——前者是给模型的口径与约束,"
        "后者是给用户看的一句话。共用一个字段会导致按钮变长或模型拿不到口径只能猜",
        f"ALTER TABLE {P}app_question_bank ADD COLUMN IF NOT EXISTS model_instruction TEXT;",
    ),
    Migration(
        "0065_tool_stats_and_context_memory",
        "工具调用按天汇总(tc_tool_daily_stats)+ 跨轮次上下文记忆(cp_context_memory)。"
        "前者用于工具列表按调用次数排序与 L4 耗时预估;后者保留「用户角色/关注周期/偏好」"
        "这类提炼事实,与 Checkpointer 不同——它很短、不会被上下文裁剪掉",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_tool_daily_stats (
            tool_id           VARCHAR(64) NOT NULL,
            stat_date         DATE        NOT NULL,
            scenic_id         VARCHAR(64),
            call_count        INTEGER     NOT NULL DEFAULT 0,
            success_count     INTEGER     NOT NULL DEFAULT 0,
            total_duration_ms INTEGER     NOT NULL DEFAULT 0,
            PRIMARY KEY (tool_id, stat_date)
        );
        CREATE INDEX IF NOT EXISTS ix_tool_stats_scenic ON {P}tc_tool_daily_stats (scenic_id);

        CREATE TABLE IF NOT EXISTS {P}cp_context_memory (
            id         VARCHAR(48) PRIMARY KEY,
            scope      VARCHAR(16) NOT NULL,
            owner_id   VARCHAR(64) NOT NULL,
            scenic_id  VARCHAR(64),
            label      VARCHAR(64) NOT NULL,
            value      TEXT        NOT NULL,
            source     VARCHAR(16) NOT NULL DEFAULT 'model',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ctx_mem ON {P}cp_context_memory (scope, owner_id, label);
        CREATE INDEX IF NOT EXISTS ix_ctx_mem_owner ON {P}cp_context_memory (owner_id);
        CREATE INDEX IF NOT EXISTS ix_ctx_mem_scenic ON {P}cp_context_memory (scenic_id);
        """,
    ),
    Migration(
        "0066_quickbi_integration",
        "工具中心 · Quick BI 智能小Q 集成模块:连接/资产/上传文件/固定问题/会话/运行记录 6 张表",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_connections (
            id                VARCHAR(36)  PRIMARY KEY,
            name              VARCHAR(64)  NOT NULL,
            server_domain     VARCHAR(255) NOT NULL DEFAULT 'https://bi.aliyun.com',
            api_key           VARCHAR(128) NOT NULL DEFAULT '',
            api_secret_cipher TEXT,
            user_token_cipher TEXT,
            use_trial         BOOLEAN      NOT NULL DEFAULT false,
            default_locale    VARCHAR(8)   NOT NULL DEFAULT 'zh_CN',
            is_default        BOOLEAN      NOT NULL DEFAULT false,
            api_enabled       BOOLEAN      NOT NULL DEFAULT true,
            active            BOOLEAN      NOT NULL DEFAULT true,
            status            VARCHAR(16)  NOT NULL DEFAULT 'pending',
            status_message    TEXT,
            checked_at        TIMESTAMPTZ,
            owner_user_id     VARCHAR(36)  NOT NULL,
            scenic_id         VARCHAR(64),
            visibility        VARCHAR(16)  NOT NULL DEFAULT 'group',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_assets (
            id            VARCHAR(36)   PRIMARY KEY,
            connection_id VARCHAR(36)   NOT NULL,
            kind          VARCHAR(16)   NOT NULL,
            name          VARCHAR(128)  NOT NULL DEFAULT '',
            ref_id        VARCHAR(128)  NOT NULL,
            url           VARCHAR(1024),
            meta          JSONB         NOT NULL DEFAULT '{{}}'::jsonb,
            owner_user_id VARCHAR(36)   NOT NULL,
            scenic_id     VARCHAR(64),
            created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_assets_conn_kind ON {P}tc_qbi_assets (connection_id, kind);
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_files (
            id            VARCHAR(36)  PRIMARY KEY,
            connection_id VARCHAR(36)  NOT NULL,
            file_name     VARCHAR(255) NOT NULL,
            size          INTEGER      NOT NULL DEFAULT 0,
            sha256        VARCHAR(64)  NOT NULL DEFAULT '',
            content       BYTEA,
            qa_file_id    VARCHAR(128),
            report_file   JSONB,
            markdown      TEXT,
            detail        JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            owner_user_id VARCHAR(64)  NOT NULL,
            scenic_id     VARCHAR(64),
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_files_connection_id ON {P}tc_qbi_files (connection_id);
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_fixed_questions (
            id              VARCHAR(36)  PRIMARY KEY,
            connection_id   VARCHAR(36)  NOT NULL,
            title           VARCHAR(128) NOT NULL,
            question        TEXT         NOT NULL,
            capability      VARCHAR(24)  NOT NULL,
            target          JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            locale          VARCHAR(8),
            category        VARCHAR(64),
            sort_order      INTEGER      NOT NULL DEFAULT 0,
            active          BOOLEAN      NOT NULL DEFAULT true,
            owner_user_id   VARCHAR(36)  NOT NULL,
            scenic_id       VARCHAR(64),
            visibility      VARCHAR(16)  NOT NULL DEFAULT 'group',
            last_run_id     VARCHAR(36),
            last_run_status VARCHAR(16),
            last_run_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_fixed_questions_connection_id
            ON {P}tc_qbi_fixed_questions (connection_id);
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_sessions (
            id            VARCHAR(36)  PRIMARY KEY,
            connection_id VARCHAR(36)  NOT NULL,
            title         VARCHAR(128) NOT NULL DEFAULT '',
            capability    VARCHAR(24)  NOT NULL DEFAULT 'auto',
            target        JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            user_id       VARCHAR(64)  NOT NULL,
            scenic_id     VARCHAR(64),
            source        VARCHAR(16)  NOT NULL DEFAULT 'console',
            api_key_id    VARCHAR(36),
            turn_count    INTEGER      NOT NULL DEFAULT 0,
            archived      BOOLEAN      NOT NULL DEFAULT false,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_sessions_user_id ON {P}tc_qbi_sessions (user_id);
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_runs (
            id                VARCHAR(36)  PRIMARY KEY,
            connection_id     VARCHAR(36)  NOT NULL,
            capability        VARCHAR(24)  NOT NULL,
            mode              VARCHAR(16)  NOT NULL DEFAULT 'direct',
            question          TEXT         NOT NULL DEFAULT '',
            target            JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            status            VARCHAR(16)  NOT NULL DEFAULT 'running',
            session_id        VARCHAR(36),
            fixed_question_id VARCHAR(64),
            chat_id           VARCHAR(64),
            report_url        VARCHAR(512),
            answer            TEXT,
            result            JSONB,
            error             TEXT,
            error_code        VARCHAR(32),
            trace_id          VARCHAR(128),
            event_count       INTEGER      NOT NULL DEFAULT 0,
            duration_ms       INTEGER,
            source            VARCHAR(16)  NOT NULL DEFAULT 'console',
            user_id           VARCHAR(64)  NOT NULL,
            scenic_id         VARCHAR(64),
            api_key_id        VARCHAR(36),
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            finished_at       TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_runs_created ON {P}tc_qbi_runs (created_at);
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_runs_session_id ON {P}tc_qbi_runs (session_id);
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_runs_fixed_question_id ON {P}tc_qbi_runs (fixed_question_id);
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_runs_chat_id ON {P}tc_qbi_runs (chat_id);
        """,
    ),
    Migration(
        "0067_missing_sys_and_app_tables",
        "补建六张「模型已声明、迁移链从未建过」的表:集团/景区主数据(sys_group、sys_scenics)、"
        "模型厂商与模型配置(sys_model_providers、sys_model_configs)、APP 关注问题与消息反馈"
        "(app_followed_questions、app_message_feedback)。这些表在 create_all 时代随模型自动建出,"
        "故全新库有、老库没有,老库启动时结构体检报「缺表」拒绝启动。DDL 按当前模型生成,"
        "与 create_all 结果逐列一致(建表顺序:先 providers 后 configs,满足外键)。幂等:IF NOT EXISTS",
        f"""
        CREATE TABLE IF NOT EXISTS {P}sys_group (
            id     VARCHAR(36)  NOT NULL,
            name   VARCHAR(128) NOT NULL,
            config JSON         NOT NULL,
            PRIMARY KEY (id)
        );
        CREATE TABLE IF NOT EXISTS {P}sys_scenics (
            scenic_id          VARCHAR(64)  NOT NULL,
            scenic_name        VARCHAR(255) NOT NULL,
            scenic_grade_level VARCHAR(16),
            is_enabled         BOOLEAN      NOT NULL,
            plan_config        JSON         NOT NULL,
            synced_at          TIMESTAMP WITHOUT TIME ZONE,
            PRIMARY KEY (scenic_id)
        );
        CREATE TABLE IF NOT EXISTS {P}sys_model_providers (
            id            VARCHAR(36)  NOT NULL,
            name          VARCHAR(128) NOT NULL,
            provider_type VARCHAR(32)  NOT NULL,
            base_url      VARCHAR(512) NOT NULL,
            api_key       VARCHAR(512) NOT NULL,
            description   VARCHAR(255) NOT NULL,
            is_enabled    BOOLEAN      NOT NULL,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        );
        CREATE TABLE IF NOT EXISTS {P}sys_model_configs (
            id           VARCHAR(36)  NOT NULL,
            name         VARCHAR(128) NOT NULL,
            provider_id  VARCHAR(36)  NOT NULL REFERENCES {P}sys_model_providers (id),
            model_name   VARCHAR(128) NOT NULL,
            description  VARCHAR(255) NOT NULL,
            is_enabled   BOOLEAN      NOT NULL,
            is_default   BOOLEAN      NOT NULL,
            extra_params JSON         NOT NULL,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        );
        CREATE TABLE IF NOT EXISTS {P}app_followed_questions (
            id                VARCHAR(36)   NOT NULL,
            user_id           VARCHAR(36)   NOT NULL,
            question_text     VARCHAR(1024) NOT NULL,
            source_message_id VARCHAR(36),
            deleted_at        TIMESTAMPTZ,
            created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        );
        CREATE INDEX IF NOT EXISTS ix_{P}app_followed_questions_user_id
            ON {P}app_followed_questions (user_id);
        CREATE TABLE IF NOT EXISTS {P}app_message_feedback (
            id            VARCHAR(36) NOT NULL,
            message_id    VARCHAR(36) NOT NULL,
            user_id       VARCHAR(36) NOT NULL,
            feedback_type VARCHAR(16) NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        );
        CREATE INDEX IF NOT EXISTS ix_{P}app_message_feedback_user_id
            ON {P}app_message_feedback (user_id);
        CREATE INDEX IF NOT EXISTS ix_{P}app_message_feedback_message_id
            ON {P}app_message_feedback (message_id);
        """,
    ),
    Migration(
        "0068_drop_legacy_tables_and_columns",
        "清理「模型已删、库里残留」的三张表与三个列:chat_attachments、turn_feedback、usr_departments;"
        "usr_users.department_id、tc_tools.category、tc_skills.input_variables。"
        "**数据不直接丢**:三张表改名为 legacy_*_0068(不带 numeri_core_ 前缀,因此不再被结构体检当作冗余表),"
        "三个列的非空值先抄进 legacy_dropped_columns 再删列;确认无用后由 DBA 手工 DROP 这些 legacy_* 即可。"
        "注:列备份前先按历史形态 ADD COLUMN IF NOT EXISTS 补齐(老库本就有,跳过;全新库补一个空列随后删掉),"
        "否则全新库执行到这里会因「列不存在」直接报错(同 0050/0051 的写法)。幂等:IF EXISTS/IF NOT EXISTS",
        f"""
        CREATE TABLE IF NOT EXISTS legacy_dropped_columns (
            id          BIGSERIAL   PRIMARY KEY,
            dropped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            table_name  TEXT        NOT NULL,
            row_id      TEXT,
            column_name TEXT        NOT NULL,
            value       TEXT
        );

        ALTER TABLE {P}usr_users ADD COLUMN IF NOT EXISTS department_id VARCHAR(36);
        INSERT INTO legacy_dropped_columns (table_name, row_id, column_name, value)
            SELECT '{P}usr_users', id, 'department_id', department_id
            FROM {P}usr_users WHERE department_id IS NOT NULL;
        ALTER TABLE {P}usr_users DROP COLUMN IF EXISTS department_id;

        ALTER TABLE {P}tc_tools ADD COLUMN IF NOT EXISTS category VARCHAR(64);
        INSERT INTO legacy_dropped_columns (table_name, row_id, column_name, value)
            SELECT '{P}tc_tools', id, 'category', category
            FROM {P}tc_tools WHERE category IS NOT NULL;
        ALTER TABLE {P}tc_tools DROP COLUMN IF EXISTS category;

        ALTER TABLE {P}tc_skills ADD COLUMN IF NOT EXISTS input_variables JSON;
        INSERT INTO legacy_dropped_columns (table_name, row_id, column_name, value)
            SELECT '{P}tc_skills', id, 'input_variables', input_variables::text
            FROM {P}tc_skills WHERE input_variables IS NOT NULL;
        ALTER TABLE {P}tc_skills DROP COLUMN IF EXISTS input_variables;

        ALTER TABLE IF EXISTS {P}chat_attachments RENAME TO legacy_chat_attachments_0068;
        ALTER TABLE IF EXISTS {P}turn_feedback    RENAME TO legacy_turn_feedback_0068;
        ALTER TABLE IF EXISTS {P}usr_departments  RENAME TO legacy_usr_departments_0068;
        """,
    ),
    Migration(
        "0069_column_type_drift_to_model",
        "收敛四处列类型漂移(库 ≠ 模型,均为 create_all 时代或历史模型留下的差异,模型为唯一事实源):"
        "app_config.config、app_competitor_requests.extra 由 jsonb 收敛为 json(同 0041 的处理,规范化后信息不丢失);"
        "gov_daily_usage.prompt_tokens/completion_tokens、tc_tool_daily_stats.total_duration_ms 由 bigint 收敛为 integer"
        "(按天汇总行:int4 上限 21 亿,token 日汇总与 24 天累计毫秒都远达不到;见 stats.py 注释)。"
        "幂等:类型已一致时 PostgreSQL 直接跳过,可重复执行",
        f"""
        ALTER TABLE {P}app_config
            ALTER COLUMN config TYPE JSON USING config::json;
        ALTER TABLE {P}app_competitor_requests
            ALTER COLUMN extra TYPE JSON USING extra::json;
        ALTER TABLE {P}gov_daily_usage
            ALTER COLUMN prompt_tokens TYPE INTEGER USING prompt_tokens::integer;
        ALTER TABLE {P}gov_daily_usage
            ALTER COLUMN completion_tokens TYPE INTEGER USING completion_tokens::integer;
        ALTER TABLE {P}tc_tool_daily_stats
            ALTER COLUMN total_duration_ms TYPE INTEGER USING total_duration_ms::integer;
        """,
    ),
    Migration(
        "0070_drop_context_params",
        "统一上下文参数机制整体下线:装配器、provider 资源与工具 schema 上的 x-context-param "
        "注入声明全部移除,原本被注入的业务参数(supplierId / ota_account / daily_capacity)回到"
        "「模型按 Schema 自填、缺失即走正常报错路径」。"
        "**数据不直接丢**:provider 配置行所在的 tc_context_params 改名 legacy_tc_context_params_0070"
        "(不带 numeri_core_ 前缀,因此不再被结构体检当作冗余表);工具版本快照里的 x-context-param "
        "声明先把整行 snapshot 抄进 legacy_dropped_columns 再原地摘除。"
        "只清 tc_resource_versions:0051 起工具正文只存在于版本快照,主表已无内容列。"
        "摘除只覆盖 params_schema.properties 这一层(声明本就只挂在参数属性上);"
        "更深层嵌套的历史残留会留在快照里,新代码不再消费它,只是 schema 里的死字段。"
        "snapshot 列是 json,jsonb 运算符需显式转换,写回时再转回 json;jsonb_each 与 `-` 运算符"
        "遇到非 object 会直接报错,而 AND 的求值顺序 PostgreSQL 不保证,故类型判定一律写成 CASE。"
        "幂等:改名 IF EXISTS;备份与摘除共用同一个「该行还有声明」判定,摘干净后再跑匹配不到行。",
        f"""
        INSERT INTO legacy_dropped_columns (table_name, row_id, column_name, value)
            SELECT '{P}tc_resource_versions', r.id, 'snapshot', r.snapshot::text
            FROM {P}tc_resource_versions r
            WHERE EXISTS (
                SELECT 1 FROM jsonb_each(
                    CASE WHEN jsonb_typeof(
                              r.snapshot::jsonb #> '{{type_config,params_schema,properties}}') = 'object'
                         THEN r.snapshot::jsonb #> '{{type_config,params_schema,properties}}'
                         ELSE '{{}}'::jsonb END) e
                WHERE CASE WHEN jsonb_typeof(e.value) = 'object'
                           THEN e.value - 'x-context-param' <> e.value ELSE false END);

        UPDATE {P}tc_resource_versions v
        SET snapshot = jsonb_set(
                v.snapshot::jsonb,
                '{{type_config,params_schema,properties}}',
                (SELECT jsonb_object_agg(
                            e.key,
                            CASE WHEN jsonb_typeof(e.value) = 'object'
                                 THEN e.value - 'x-context-param' ELSE e.value END)
                   FROM jsonb_each(
                       v.snapshot::jsonb #> '{{type_config,params_schema,properties}}') e)
            )::json
        WHERE EXISTS (
            SELECT 1 FROM jsonb_each(
                CASE WHEN jsonb_typeof(
                          v.snapshot::jsonb #> '{{type_config,params_schema,properties}}') = 'object'
                     THEN v.snapshot::jsonb #> '{{type_config,params_schema,properties}}'
                     ELSE '{{}}'::jsonb END) e
            WHERE CASE WHEN jsonb_typeof(e.value) = 'object'
                       THEN e.value - 'x-context-param' <> e.value ELSE false END);

        ALTER TABLE IF EXISTS {P}tc_context_params RENAME TO legacy_tc_context_params_0070;
        """,
    ),
    Migration(
        "0071_qbi_fixed_question_domain_and_ask_type",
        "固定问题加两个分类维度:domain(数据域:票务/财务/舆情/客群/核销/营销/流量/画像……,"
        "取值不固定,允许现场新增,故存自由文本并加索引而不是做成枚举);"
        "ask_type(请求类别:qa 问数 / insight 解读 / report 报告,比 capability 粗一档 —— "
        "调用方通常只关心「问数还是解读」,不关心数据集问数与仪表板问数的区别)。"
        "存量行按 capability 回填 ask_type:insight→insight,report→report,其余→qa。"
        "幂等:ADD COLUMN IF NOT EXISTS;回填只打 ask_type 为空的行。",
        f"""
        {_LEGACY_FIXED_QUESTIONS_STUB}

        ALTER TABLE {P}tc_qbi_fixed_questions
            ADD COLUMN IF NOT EXISTS domain   VARCHAR(32),
            ADD COLUMN IF NOT EXISTS ask_type VARCHAR(16);

        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_fixed_questions_domain
            ON {P}tc_qbi_fixed_questions (domain);

        UPDATE {P}tc_qbi_fixed_questions
        SET ask_type = CASE capability
                           WHEN 'insight' THEN 'insight'
                           WHEN 'report'  THEN 'report'
                           ELSE 'qa'
                       END
        WHERE ask_type IS NULL;

        ALTER TABLE {P}tc_qbi_fixed_questions
            ALTER COLUMN ask_type SET DEFAULT 'qa',
            ALTER COLUMN ask_type SET NOT NULL;
        """,
    ),
    Migration(
        "0072_qbi_ask_type_dashboard",
        "请求类别补上「报表」:仪表板问数(小Q报表)从 ask_type=qa 拆成独立的 dashboard。"
        "对调用方来说「随便问一个指标」与「针对某块已做好的看板提问」是两件事,"
        "合成一类会让按类别取问题时混在一起。"
        "回填只动 capability=dashboard_qa 且 ask_type='qa' 的行 —— 那正是 0071 自动推导出来的值;"
        "有人手工改成别的类别(比如把一条看板问题标成解读)则保持不动,不覆盖人的选择。"
        "幂等:改完就匹配不到行了。",
        f"""
        {_LEGACY_FIXED_QUESTIONS_STUB}

        UPDATE {P}tc_qbi_fixed_questions
        SET ask_type = 'dashboard'
        WHERE capability = 'dashboard_qa' AND ask_type = 'qa';
        """,
    ),
    Migration(
        "0073_qbi_run_debug_trace",
        "运行记录加 debug 列:存这一次的执行留痕(上游原样 SSE 事件 + 归一化后的事件,"
        "已脱敏并限长)。排障时一键导出给研发,不必靠复述现象 —— "
        "而现象往往正是「看起来不对但说不清哪里不对」。"
        "留痕默认开启,可用 quickbi.capture_debug_trace 关掉;单条上限 256KB,超了只记省略条数。"
        "幂等:ADD COLUMN IF NOT EXISTS。",
        f"""
        ALTER TABLE {P}tc_qbi_runs
            ADD COLUMN IF NOT EXISTS debug JSONB;
        """,
    ),
    Migration(
        "0074_assignment_resource_type",
        "分发指派扩展到 Skill:tc_agent_assignments 加 resource_type(agent/skill),"
        "存量行一律回填 'agent'(此前该表只记 Agent 指派,语义等价,不会改变任何人的现状)。"
        "agent_id 列沿用为「资源 id」——改名要动索引、外键式引用与全部读点,"
        "收益只是名字好看,风险却是停机迁移,不值当;读写一律经 versioning 的封装,"
        "调用方看到的是 resource_type + resource_id。"
        "唯一键随之从 (agent_id, scope_type, scope_id) 扩到带 resource_type ——"
        "否则同 id 的 agent 与 skill 会互相顶掉指派(现实中 id 不会撞,但约束不该指望这个)。"
        "幂等:ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS。",
        f"""
        ALTER TABLE {P}tc_agent_assignments
            ADD COLUMN IF NOT EXISTS resource_type VARCHAR(16) NOT NULL DEFAULT 'agent';
        UPDATE {P}tc_agent_assignments SET resource_type = 'agent'
            WHERE resource_type IS NULL OR resource_type = '';
        CREATE INDEX IF NOT EXISTS ix_agent_assign_res
            ON {P}tc_agent_assignments (resource_type, agent_id);
        DROP INDEX IF EXISTS uq_agent_assign_scope;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_assign_scope
            ON {P}tc_agent_assignments (resource_type, agent_id, scope_id);
        """,
    ),
    Migration(
        "0075_qbi_connection_grants",
        "Quick BI 连接分配:新表 tc_qbi_grants(connection_id, user_id)——"
        "「这个人能不能用这套 Quick BI 凭证」。不复用版本指派表是因为连接没有版本概念,"
        "硬塞进去就得编一个假版本号,之后每处读版本的地方都要特判。"
        "语义:某个连接一旦有了至少一条分配就转白名单制(未被分配的人看不到、用不了);"
        "一条都没有的连接维持原有景区 + 可见性判定 —— 升级当天不会有人突然用不了。"
        "幂等:CREATE TABLE / INDEX IF NOT EXISTS。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_grants (
            id            VARCHAR(48) PRIMARY KEY,
            connection_id VARCHAR(36) NOT NULL,
            user_id       VARCHAR(64) NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            created_by    VARCHAR(64) NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_grants_connection_id
            ON {P}tc_qbi_grants (connection_id);
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_grants_user_id
            ON {P}tc_qbi_grants (user_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_qbi_grants_conn_user
            ON {P}tc_qbi_grants (connection_id, user_id);
        """,
    ),
    Migration(
        "0076_issue_detail_and_dimension_label",
        "景区智能体问题体系(前后端改造 §1/§2):新增两张表 —— "
        "public_problem_dimension_label(主题→维度→指标 三级标签,启动即初始化 40 条) 与 "
        "scenic_agent_issue_detail(问题详情,一行=前台一个可点击的问题)。"
        "问题原先散在 app_question_bank(APP 问题库)与 tc_qbi_fixed_questions(Quick BI 固定问题)两处,"
        "字段、可见性、发起方式都不同,前台得自己合并、还要猜该调哪个接口 —— "
        "统一后由 business_class 说明怎么发起(quick_bi 直连 Quick BI / local_system 走对话引擎)。"
        "两张旧表**不动**(存量逻辑仍在用),这里把数据灌进新表并记 source_question_id 便于对账。"
        "标签的两道唯一约束:名称三元组重复会让下拉里出现两个一样的选项,"
        "编码三元组重复会让前台按编码分组时把两条路径混成一条。"
        "幂等:CREATE TABLE/INDEX IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}public_problem_dimension_label (
            id            BIGSERIAL PRIMARY KEY,
            topic         VARCHAR(64) NOT NULL,
            topic_id      VARCHAR(64) NOT NULL,
            dimension     VARCHAR(64) NOT NULL,
            dimension_id  VARCHAR(64) NOT NULL,
            metric        VARCHAR(64) NOT NULL,
            metric_id     VARCHAR(64) NOT NULL,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            active        BOOLEAN NOT NULL DEFAULT true,
            create_time   TIMESTAMPTZ NOT NULL DEFAULT now(),
            update_time   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_label_names
            ON {P}public_problem_dimension_label (topic, dimension, metric);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_label_codes
            ON {P}public_problem_dimension_label (topic_id, dimension_id, metric_id);
        CREATE INDEX IF NOT EXISTS idx_dim_label_topic
            ON {P}public_problem_dimension_label (topic_id, dimension_id);

        -- active 显式写出,因为模型侧 active 是 Python 默认值,新库用 create_all
        -- 建表时这一列在库里没有 DEFAULT,省略就会撞 NOT NULL
        INSERT INTO {P}public_problem_dimension_label
            (topic, topic_id, dimension, dimension_id, metric, metric_id, sort_order, active)
        VALUES
            ('客流','visitorFlow','在园人员','inParkVisitor','实时人数','liveVisitorCnt',0,true),
            ('客流','visitorFlow','在园人员','inParkVisitor','承载率','loadRate',10,true),
            ('客流','visitorFlow','在园人员','inParkVisitor','男女比例','genderRatio',20,true),
            ('客流','visitorFlow','在园人员','inParkVisitor','年龄结构','ageDistribution',30,true),
            ('客流','visitorFlow','入园趋势','checkinTrend','分时','hourly',40,true),
            ('客流','visitorFlow','入园趋势','checkinTrend','高峰','peak',50,true),
            ('客流','visitorFlow','入园趋势','checkinTrend','趋势','trend',60,true),
            ('客流','visitorFlow','入园趋势','checkinTrend','环比','mom',70,true),
            ('客流','visitorFlow','客流来源','visitorSource','省份','province',80,true),
            ('客流','visitorFlow','客流来源','visitorSource','城市','urban',90,true),
            ('客流','visitorFlow','区域分布','regionDistribution','密度','density',100,true),
            ('客流','visitorFlow','区域分布','regionDistribution','指数','indicator',110,true),
            ('票务','ticketing','售票概况','ticketingOverview','总量','totalVolume',120,true),
            ('票务','ticketing','售票概况','ticketingOverview','收入','income',130,true),
            ('票务','ticketing','售票概况','ticketingOverview','环比','mom',140,true),
            ('票务','ticketing','售票概况','ticketingOverview','趋势','trend',150,true),
            ('票务','ticketing','票种分析','ticketClassAnalysis','销量','salesVolume',160,true),
            ('票务','ticketing','票种分析','ticketClassAnalysis','畅销','hotSelling',170,true),
            ('票务','ticketing','票种分析','ticketClassAnalysis','占比','ratio',180,true),
            ('票务','ticketing','渠道分析','channelAnalysis','线上&线下','channelType',190,true),
            ('票务','ticketing','渠道分析','channelAnalysis','OTA渠道','otaChannel',200,true),
            ('票务','ticketing','渠道分析','channelAnalysis','自营渠道','ownChannel',210,true),
            ('票务','ticketing','退票预售','refundPresale','退票量','ticketRefundVolume',220,true),
            ('票务','ticketing','退票预售','refundPresale','退票率','refundRatio',230,true),
            ('票务','ticketing','退票预售','refundPresale','归因','attribution',240,true),
            ('票务','ticketing','退票预售','refundPresale','预售量','presaleVolume',250,true),
            ('舆情','opinion','情感分析','opinionAnalysis','情感倾向','emotionOrientation',260,true),
            ('舆情','opinion','情感分析','opinionAnalysis','负面关键词','negativeTerm',270,true),
            ('舆情','opinion','平台监控','platformMonitor','平台分布','platformDistribution',280,true),
            ('舆情','opinion','平台监控','platformMonitor','最多负面','topNegative',290,true),
            ('舆情','opinion','平台监控','platformMonitor','评分变化','ratingVariation',300,true),
            ('舆情','opinion','趋势对比','trendCompare','周趋势','weeklyTrend',310,true),
            ('舆情','opinion','趋势对比','trendCompare','周环比','weeklyMom',320,true),
            ('舆情','opinion','趋势对比','trendCompare','月趋势','monthlyTrend',330,true),
            ('OTA直销','otaDirectSale','订单/核销','orderRedemption','订单量','orderVolume',340,true),
            ('OTA直销','otaDirectSale','订单/核销','orderRedemption','出票量','ticketIssueVolume',350,true),
            ('OTA直销','otaDirectSale','订单/核销','orderRedemption','核销率','verificationRatio',360,true),
            ('OTA直销','otaDirectSale','订单/核销','orderRedemption','客单价','customerUnitPrice',370,true),
            ('OTA直销','otaDirectSale','历史对比','historicalComparison','环比','mom',380,true),
            ('OTA直销','otaDirectSale','历史对比','historicalComparison','波动','variation',390,true)
        ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS {P}scenic_agent_issue_detail (
            id                BIGSERIAL PRIMARY KEY,
            issue_id          VARCHAR(48) NOT NULL,
            scenic_id         VARCHAR(64),
            business_class    VARCHAR(16) NOT NULL DEFAULT 'local_system',
            connection_id     VARCHAR(36),
            ask_type          VARCHAR(16),
            capability        VARCHAR(24),
            target            JSONB,
            target_data_id    VARCHAR(128),
            target_data_name  VARCHAR(255),
            agent_id          VARCHAR(36),
            skill_id          VARCHAR(36),
            topic             VARCHAR(64),
            topic_id          VARCHAR(64),
            dimension         VARCHAR(64),
            dimension_id      VARCHAR(64),
            metric            VARCHAR(64),
            metric_id         VARCHAR(64),
            title             VARCHAR(255) NOT NULL,
            issue             TEXT NOT NULL,
            model_instruction TEXT,
            param_template    JSONB,
            time_period       VARCHAR(32),
            sort_order        INTEGER NOT NULL DEFAULT 0,
            active            BOOLEAN NOT NULL DEFAULT true,
            last_run_id       VARCHAR(64),
            last_run_status   VARCHAR(16),
            last_run_time     TIMESTAMPTZ,
            source_question_id VARCHAR(64),
            create_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
            update_time       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_detail_issue_id
            ON {P}scenic_agent_issue_detail (issue_id);
        CREATE INDEX IF NOT EXISTS idx_issue_scenic_active
            ON {P}scenic_agent_issue_detail (scenic_id, active);
        CREATE INDEX IF NOT EXISTS idx_issue_label
            ON {P}scenic_agent_issue_detail (topic_id, dimension_id, metric_id);
        CREATE INDEX IF NOT EXISTS idx_issue_business_class
            ON {P}scenic_agent_issue_detail (business_class);
        CREATE INDEX IF NOT EXISTS idx_issue_source
            ON {P}scenic_agent_issue_detail (source_question_id);
        CREATE INDEX IF NOT EXISTS idx_issue_connection
            ON {P}scenic_agent_issue_detail (connection_id);

        {_LEGACY_FIXED_QUESTIONS_STUB}

        -- 存量迁移 1:Quick BI 固定问题 -> business_class=quick_bi
        -- target_data_id 先从 target 里的 page_id/cube_id 取。asset_id 那种要连
        -- tc_qbi_assets 才知道 ref_id 与名称,所以顺带补上(取不到就留空,不阻断迁移)
        INSERT INTO {P}scenic_agent_issue_detail
            (issue_id, scenic_id, business_class, connection_id, ask_type, capability,
             target, target_data_id, target_data_name, title, issue, sort_order, active,
             last_run_id, last_run_status, last_run_time, source_question_id)
        SELECT
            'issue_' || replace(q.id::text, '-', ''),
            q.scenic_id, 'quick_bi', q.connection_id, q.ask_type, q.capability,
            q.target,
            COALESCE(q.target->>'page_id', q.target->>'cube_id', a.ref_id),
            COALESCE(a.name, q.target->>'cube_name'),
            q.title, q.question, q.sort_order, q.active,
            q.last_run_id, q.last_run_status, q.last_run_at, q.id
        FROM {P}tc_qbi_fixed_questions q
        LEFT JOIN {P}tc_qbi_assets a ON a.id = q.target->>'asset_id'
        WHERE NOT EXISTS (
            SELECT 1 FROM {P}scenic_agent_issue_detail d WHERE d.source_question_id = q.id
        );

        -- 存量迁移 2:APP 问题库 -> business_class=local_system
        -- theme_code/dimension_name/metric_name 是老的三级口径,先原样搬进
        -- topic/dimension/metric,编码列留空 —— 等运营在「维度标签」里把它们
        -- 挂到正式三级标签上。留空比瞎猜一个编码好,猜错了前台会分到错误的分组里
        INSERT INTO {P}scenic_agent_issue_detail
            (issue_id, scenic_id, business_class, agent_id, skill_id,
             topic, dimension, metric, title, issue, model_instruction, param_template,
             time_period, sort_order, active, source_question_id)
        SELECT
            'issue_' || replace(b.id::text, '-', ''),
            b.scenic_id, 'local_system', b.agent_id, b.skill_id,
            b.theme_code, b.dimension_name, b.metric_name,
            COALESCE(NULLIF(b.metric_name, ''), left(b.question_text, 60)),
            b.question_text, b.model_instruction, b.param_template,
            b.time_period, b.sort_order, b.active, b.id
        FROM {P}app_question_bank b
        WHERE NOT EXISTS (
            SELECT 1 FROM {P}scenic_agent_issue_detail d WHERE d.source_question_id = b.id
        );
        """,
    ),
    Migration(
        "0077_qbi_fixed_question_dimension_label",
        "Quick BI 固定问题挂三级标签:tc_qbi_fixed_questions 新增 "
        "topic/topic_id/dimension/dimension_id/metric/metric_id 六列。"
        "背景:0076 之后 APP 端的问题列表按 主题→维度→指标 组织,而后台建 Quick BI 固定问题时"
        "没地方选标签 —— 建完的问题落到问题表里三级全空,前台那棵树里一条都看不到"
        "(表现成「后台明明建了,APP 里没有」)。所以把标签存到固定问题这一行上,"
        "保存时同步写进 scenic_agent_issue_detail(按 source_question_id 认领同一行)。"
        "冗余中文名而不是只存编码,是因为标签改名后已发布的问题不该跟着漂,"
        "且列表接口不必为了显示一个名字去连表。"
        "幂等:ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS。",
        f"""
        {_LEGACY_FIXED_QUESTIONS_STUB}

        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS topic        VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS topic_id     VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS dimension    VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS dimension_id VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS metric       VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS metric_id    VARCHAR(64);
        CREATE INDEX IF NOT EXISTS idx_qbi_fixed_label
            ON {P}tc_qbi_fixed_questions (topic_id, dimension_id, metric_id);

        -- 已经迁到问题表里的老行:把标签回灌到固定问题上,保证两边一致
        -- (0076 建问题行时固定问题还没有这六列,只可能是问题表那边被人工补过)
        UPDATE {P}tc_qbi_fixed_questions q
           SET topic = d.topic, topic_id = d.topic_id,
               dimension = d.dimension, dimension_id = d.dimension_id,
               metric = d.metric, metric_id = d.metric_id
          FROM {P}scenic_agent_issue_detail d
         WHERE d.source_question_id = q.id
           AND d.topic_id IS NOT NULL
           AND q.topic_id IS NULL;
        """,
    ),
    Migration(
        "0078_scenic_master_data_convergence",
        "景区表收敛到主数据:删除 gov_scenic_orgs,其数据并入 sys_scenics。"
        "背景:景区信息原先散在三处 —— dim_prd_scenic_base_yr_f(数仓同步来的景区主数据)、"
        "sys_scenics(成员管理里给用户绑景区用)、gov_scenic_orgs(系统设置-组织管理的开关表)。"
        "最后一张只能在页面上手工一条条登记,所以组织管理里永远只有人工敲进去的那一两条,"
        "看起来像「景区没同步过来」。改造后:景区叫什么/几A/属地一律以主数据为准,"
        "sys_scenics 只存平台侧状态(是否开通、套餐),组织管理与成员管理共用这一份。"
        "同时把「有用户绑着、但没登记过」的景区补登记进来 —— 这类景区在成员管理里"
        "显示成一个光秃秃的 ID,排查时最费时间。"
        "幂等:INSERT ... ON CONFLICT DO NOTHING + DROP TABLE IF EXISTS。",
        f"""
        -- ① 老开关表并入 sys_scenics(service_enabled -> is_enabled)。
        -- 先 CREATE TABLE IF NOT EXISTS 再读:PostgreSQL 在解析阶段就要求表存在,
        -- 光靠 WHERE EXISTS(information_schema) 挡不住「表不在」的报错(全新库就是这种)。
        -- 建出来的空表在本迁移末尾一并 DROP,不留痕迹。
        CREATE TABLE IF NOT EXISTS {P}gov_scenic_orgs (
            scenic_id      VARCHAR(64) PRIMARY KEY,
            name           VARCHAR(128) NOT NULL DEFAULT '',
            service_enabled BOOLEAN NOT NULL DEFAULT true,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO {P}sys_scenics (scenic_id, scenic_name, is_enabled, plan_config)
        SELECT o.scenic_id, COALESCE(NULLIF(o.name, ''), o.scenic_id), o.service_enabled, '{{}}'::json
          FROM {P}gov_scenic_orgs o
        ON CONFLICT (scenic_id) DO NOTHING;

        -- ② 有用户绑着却没登记的景区:补一条登记记录(默认未开通,由管理员决定)。
        -- 这里**不连 dim_prd_scenic_base_yr_f 取名字**:名称在查询时由
        -- user_center_service/scenics.py 的 COALESCE(主数据, 平台侧) 现取,
        -- 迁移里再存一份只会多出一个会过期的副本,而且迁移不该假设数仓维表
        -- 此刻已经同步过数据(全新库里它可能还是空表)。
        -- DISTINCT 放在子查询里:json 类型没有等值运算符,直接 SELECT DISTINCT
        -- 带上 plan_config 会报 "could not identify an equality operator for type json"
        INSERT INTO {P}sys_scenics (scenic_id, scenic_name, is_enabled, plan_config)
        SELECT s.scenic_id, s.scenic_id, false, '{{}}'::json
          FROM (SELECT DISTINCT u.scenic_id
                  FROM {P}usr_users u
                 WHERE u.scenic_id IS NOT NULL AND u.scenic_id <> '') s
        ON CONFLICT (scenic_id) DO NOTHING;

        DROP TABLE IF EXISTS {P}gov_scenic_orgs;
        """,
    ),
    Migration(
        "0079_quickbi_fixed_questions_into_issue_detail",
        "Quick BI 固定问题整体并入统一问题表:scenic_agent_issue_detail 新增 "
        "owner_user_id / visibility / domain / category / locale 五列(原先长在 "
        "tc_qbi_fixed_questions 上),把老表剩余数据补灌进来,然后删除老表。"
        "0076 只是把数据复制过去、两张表并存,于是「在 Quick BI 管理端建的问题」和"
        "「APP 前台读的问题」仍是两份 —— 改了一边另一边不动,又回到当初那个"
        "「后台配了、前台看不到」的坑。这次把写入口也切过来:管理端的固定问题增删改"
        "直接落在统一表(business_class=quick_bi),老表退场。"
        "老 ID 不会失效:迁进来的行保留 source_question_id,按老 id 也能查到。"
        "幂等:ADD COLUMN IF NOT EXISTS + 只插 source_question_id 不存在的行 + DROP TABLE IF EXISTS。",
        f"""
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(64);
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS visibility VARCHAR(16) NOT NULL DEFAULT 'group';
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS domain VARCHAR(32);
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS category VARCHAR(64);
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS locale VARCHAR(8);
        CREATE INDEX IF NOT EXISTS idx_issue_domain ON {P}scenic_agent_issue_detail (domain);

        {_LEGACY_FIXED_QUESTIONS_STUB}

        -- 老表若是 0077 之前的形态(没有三级标签那六列),补上再读 ——
        -- 上面的 CREATE TABLE IF NOT EXISTS 对已存在的表是 no-op,补不了列
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS topic        VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS topic_id     VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS dimension    VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS dimension_id VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS metric       VARCHAR(64);
        ALTER TABLE {P}tc_qbi_fixed_questions ADD COLUMN IF NOT EXISTS metric_id    VARCHAR(64);

        -- ① 0076 之后新建的、还没进统一表的固定问题:补进来
        INSERT INTO {P}scenic_agent_issue_detail
            (issue_id, scenic_id, business_class, connection_id, ask_type, capability,
             target, target_data_id, target_data_name,
             topic, topic_id, dimension, dimension_id, metric, metric_id,
             title, issue, sort_order, active, owner_user_id, visibility, domain, category, locale,
             last_run_id, last_run_status, last_run_time, source_question_id)
        SELECT
            'issue_' || replace(q.id::text, '-', ''),
            q.scenic_id, 'quick_bi', q.connection_id, q.ask_type, q.capability,
            q.target,
            COALESCE(q.target->>'page_id', q.target->>'cube_id', a.ref_id),
            COALESCE(a.name, q.target->>'cube_name'),
            q.topic, q.topic_id, q.dimension, q.dimension_id, q.metric, q.metric_id,
            q.title, q.question, q.sort_order, q.active, q.owner_user_id, q.visibility,
            q.domain, q.category, q.locale,
            q.last_run_id, q.last_run_status, q.last_run_at, q.id
        FROM {P}tc_qbi_fixed_questions q
        LEFT JOIN {P}tc_qbi_assets a ON a.id = q.target->>'asset_id'
        WHERE NOT EXISTS (
            SELECT 1 FROM {P}scenic_agent_issue_detail d WHERE d.source_question_id = q.id
        );

        -- ② 0076 搬过来的行只带了问题本身,归属/可见性/数据域这几列当时还没有,补齐。
        -- 不补的话老问题会集体变成 visibility=group、owner 为空,景区私有问题会被别人看到
        UPDATE {P}scenic_agent_issue_detail d
           SET owner_user_id = COALESCE(d.owner_user_id, q.owner_user_id),
               visibility    = COALESCE(NULLIF(d.visibility, ''), q.visibility, 'group'),
               domain        = COALESCE(d.domain, q.domain),
               category      = COALESCE(d.category, q.category),
               locale        = COALESCE(d.locale, q.locale),
               topic         = COALESCE(d.topic, q.topic),
               topic_id      = COALESCE(d.topic_id, q.topic_id),
               dimension     = COALESCE(d.dimension, q.dimension),
               dimension_id  = COALESCE(d.dimension_id, q.dimension_id),
               metric        = COALESCE(d.metric, q.metric),
               metric_id     = COALESCE(d.metric_id, q.metric_id)
          FROM {P}tc_qbi_fixed_questions q
         WHERE d.source_question_id = q.id;

        -- ③ APP 问题库迁进来的那批没有 owner/可见性概念,统一按集团内可见
        UPDATE {P}scenic_agent_issue_detail
           SET visibility = 'group'
         WHERE visibility IS NULL OR visibility = '';

        DROP TABLE IF EXISTS {P}tc_qbi_fixed_questions;
        """,
    ),
    Migration(
        "0080_widen_qbi_run_fixed_question_id",
        "运行记录的 fixed_question_id 放宽到 64:0079 之后这一列存的是统一问题表的 "
        "issue_id,而它原先是 VARCHAR(36)(老表的 UUID 正好 36)。"
        "issue_id 形如 issue_<32位十六进制>(38 字符)或 issue_theme_<主题>_<8位>(最长 40 字符),"
        "**一律超长** —— 于是每次按固定问题发起都会在写运行记录时 "
        "value too long for type character varying(36) 而整个请求 500,"
        "现场表现是「主题解读点下去请求失败(500)」。列宽跟着 issue_id(48)放到 64 留余量",
        f"""
        ALTER TABLE {P}tc_qbi_runs ALTER COLUMN fixed_question_id TYPE VARCHAR(64);
        """,
    ),
    Migration(
        "0081_theme_report_dimension_labels",
        "三级标签补上「主题解读」这一档(概览/客群/票务/舆情/OTA直销 × 解读报告)。"
        "主题解读的问题行一直是 ensure_theme_issues 直接把三级写死在问题上的,"
        "标签表里并没有对应行 —— 于是管理端那个「主题 / 维度 / 指标」选择器里根本挑不到"
        "「主题解读」,运营想把一条自己写的解读挂到主题板块下也挂不上。"
        "编码与 scenic_binding.THEMES 一一对应,改那边的 code 要同步改这里",
        f"""
        INSERT INTO {P}public_problem_dimension_label
            (topic, topic_id, dimension, dimension_id, metric, metric_id, sort_order, active)
        VALUES
            ('主题解读','themeReport','概览','theme_overview',
             '解读报告','themeReport_overview',900,true),
            ('主题解读','themeReport','客群','theme_passenger_flow',
             '解读报告','themeReport_passenger_flow',910,true),
            ('主题解读','themeReport','票务','theme_ticket',
             '解读报告','themeReport_ticket',920,true),
            ('主题解读','themeReport','舆情','theme_public_opinion',
             '解读报告','themeReport_public_opinion',930,true),
            ('主题解读','themeReport','OTA直销','theme_ota_direct_sales',
             '解读报告','themeReport_ota_direct_sales',940,true)
        ON CONFLICT DO NOTHING;
        """,
    ),
    Migration(
        "0082_qbi_asset_file_active",
        "数据资产 / 上传文件加「是否可用」两列(active + disabled_reason)。"
        "起因:连接被删之后,名下资产只有两条路 —— 转到另一条连接,或者作废。"
        "而能不能转是有前提的:资产的 ref_id 是 Quick BI 那边的仪表板/数据集 id,"
        "只在同一个工作空间里有效;上传文件的远端 fileId 更是直接绑在 user_token 上。"
        "换一套凭证就打不开的东西,转过去只是把死引用挪了个地方 —— 该停用,不该转移。"
        "以前没有这一列,于是这类内容只能删(丢掉登记信息)或者留着当死引用(点了才报错);"
        "现在停用并写明原因,列表里仍看得到、知道为什么用不了,换回可用的连接还能启用回来。",
        f"""
        ALTER TABLE {P}tc_qbi_assets
            ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE {P}tc_qbi_assets
            ADD COLUMN IF NOT EXISTS disabled_reason VARCHAR(255);
        ALTER TABLE {P}tc_qbi_files
            ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE {P}tc_qbi_files
            ADD COLUMN IF NOT EXISTS disabled_reason VARCHAR(255);
        """,
    ),
    Migration(
        "0083_qbi_dataset_source_board",
        "数据集资产记下「从哪块仪表板拉出来登记的」(source_asset_id)。"
        "在此之前这层归属是**猜**出来的:拿仪表板 meta.cubes 里的 cube_id 去反查数据集的 ref_id。"
        "猜有两处会错 —— 两块板共用一个 cube 时归不了位;板刷新结构之后 meta.cubes 变了,"
        "原本属于它的集会突然变成「未归属」。而这层归属现在是有后果的:"
        "删仪表板要连带删它下面的数据集,归属算错就等于删错东西。"
        "登记时直接记下来,不再靠推断;历史数据留空,仍按 meta.cubes 反查(两种都认)。",
        f"""
        ALTER TABLE {P}tc_qbi_assets
            ADD COLUMN IF NOT EXISTS source_asset_id VARCHAR(36);
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_assets_source
            ON {P}tc_qbi_assets (source_asset_id);
        """,
    ),
    Migration(
        "0084_issue_prewarm_settings",
        "固定问题加「加速」(预热)三件套:prewarm_enabled / prewarm_interval_minutes / "
        "prewarm_ttl_minutes。"
        "解决的问题:Quick BI 的问数与解读慢的要几十秒、报告要几分钟,而前台/APP 上"
        "同一个景区的同一条固定问题,一天里会被很多人点很多次 —— 每次都现跑,"
        "既让人干等,也把上游额度和 token 白白烧掉。"
        "开了加速之后,后台按 interval 提前跑好,前台再点就直接回放那一次的结果。"
        "**刻意不建缓存表**:结果本来就整份存在 tc_qbi_runs 里,"
        "「缓存」= 这条问题在这个景区最近一次成功的运行且在 ttl 之内。"
        "多一份存储就多一处会不同步 —— 而缓存与源数据不同步,表现是「看到的是旧数,还没人察觉」。"
        "ttl 建议 >= interval,否则会出现「还没到下次刷新就已经过期」的空窗,那段时间照旧现跑。",
        f"""
        ALTER TABLE {P}scenic_agent_issue_detail
            ADD COLUMN IF NOT EXISTS prewarm_enabled BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE {P}scenic_agent_issue_detail
            ADD COLUMN IF NOT EXISTS prewarm_interval_minutes INTEGER;
        ALTER TABLE {P}scenic_agent_issue_detail
            ADD COLUMN IF NOT EXISTS prewarm_ttl_minutes INTEGER;
        CREATE INDEX IF NOT EXISTS ix_issue_prewarm
            ON {P}scenic_agent_issue_detail (prewarm_enabled)
            WHERE prewarm_enabled = TRUE;
        """,
    ),
    Migration(
        "0085_agent_files",
        "智能体可读的上传文件表 tc_agent_files。"
        "解决的问题:原先文件只能存 Quick BI 那张表(tc_qbi_files),整份内容放在 bytea 列里,"
        "上限 5MB —— 那个 5MB 是 Quick BI OpenAPI 的限制,却把整套上传都框住了,"
        "十兆的报表、扫描件 PDF 根本传不进来。而 minio 的配置(bucket zhilv-artifacts)"
        "在 config.yaml 里躺了很久,代码里一次都没被 import 过。"
        "这张表把「上传给智能体读」独立出来,内容走对象存储(storage=minio,只存 object_key),"
        "对象存储不可用时落 content 列兜底(storage=db,受 5MB 限制)—— "
        "**storage 必须记清楚存在哪**,靠猜的话会出现「库里没有、桶里也没有,但记录还在」,"
        "那种文件读不了也删不掉。"
        "parsed_kind 记住解析结果是表格还是文档:表格类读进来变成数据集句柄交给 sys_table_*,"
        "文档类才返回文本 —— 一份三千行的 Excel 转成文本是四十多万字,塞不进上下文。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_agent_files (
            id              VARCHAR(36) PRIMARY KEY,
            file_name       VARCHAR(255) NOT NULL,
            size            INTEGER NOT NULL DEFAULT 0,
            sha256          VARCHAR(64) NOT NULL DEFAULT '',
            storage         VARCHAR(16) NOT NULL DEFAULT 'db',
            object_key      VARCHAR(512),
            content         BYTEA,
            parsed_kind     VARCHAR(16),
            parse_error     TEXT,
            detail          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            owner_user_id   VARCHAR(64) NOT NULL DEFAULT '',
            scenic_id       VARCHAR(64),
            conversation_id VARCHAR(64),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_agent_files_owner
            ON {P}tc_agent_files (owner_user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_agent_files_conv
            ON {P}tc_agent_files (conversation_id);
        CREATE INDEX IF NOT EXISTS ix_agent_files_scenic
            ON {P}tc_agent_files (scenic_id, created_at DESC);
        """,
    ),
    Migration(
        "0086_platform_secrets",
        "平台级共用密钥表 gov_platform_secrets(目前只有数据平台 xpftkey)。"
        "解决的问题:这把 key 以前只能写在服务器环境变量 DATA_PLATFORM_XPFTKEY 里,"
        "要改部署、要重启,运营碰不到;没配的表现是票务 / 客群 / 舆情数据全部查不了,"
        "现场问「在哪里设置」没有一个页面可指。现在在「系统设置」里配,加密落库"
        "(与模型 API Key 同一套 vault),接口永不回明文;取值顺序 后台 > 环境变量。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}gov_platform_secrets (
            name         VARCHAR(64) PRIMARY KEY,
            value_cipher VARCHAR(2048) NOT NULL,
            updated_by   VARCHAR(36) NOT NULL DEFAULT '',
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    Migration(
        "0087_resource_version_updated_by",
        "tc_resource_versions 加 updated_by(最近修改人 user_id)。"
        "解决的问题:Tool / Skill / Agent 列表卡片要显示「最近修改时间、修改人、归属」,"
        "原表只有 created_by(草稿派生人),之后谁保存、谁发布、谁下线都不留名,"
        "列表上只能显示创建者,和「最近是谁改的」对不上。存量行用 created_by 回填。",
        f"""
        ALTER TABLE {P}tc_resource_versions ADD COLUMN IF NOT EXISTS updated_by VARCHAR(64) NOT NULL DEFAULT '';
        UPDATE {P}tc_resource_versions SET updated_by = COALESCE(created_by, '') WHERE updated_by = '';
        """,
    ),
    Migration(
        "0088_qbi_asset_category",
        "tc_qbi_assets 加 category(数据资产的自定义分类)。"
        "解决的问题:一个景区挂着几十个数据集(九龙潭 28 个,票务 / 客群 / 舆情 / OTA 混在一起),"
        "只能按「来自哪块仪表板」分组,找数据集全靠名字前缀;编辑问题时也没法「把票务的都选上」。"
        "现在运营在数据集上输入分类回车即存(不需要先建分类),列表按分类分组,"
        "编辑问题时按分类多选 / 全选数据集(多选走 multipleCubeIds 多表问数)。",
        f"""
        ALTER TABLE {P}tc_qbi_assets ADD COLUMN IF NOT EXISTS category VARCHAR(64);
        CREATE INDEX IF NOT EXISTS ix_tc_qbi_assets_category ON {P}tc_qbi_assets (category);
        """,
    ),
    Migration(
        "0089_app_scenic_param",
        "新建 app_scenic_param(景区级接口参数)。"
        "解决的问题:同一个工具不同景区要传不同的值(OTA 商户账号 account 等),以前只能让用户"
        "在对话里自己报一遍(任务 5877db72:「OTA商户ID我补充一下:vbk_569202」,多花三分钟重跑)。"
        "现在按景区配一次:执行工具时,参数名命中 arg_names 且模型没填 → 自动填入(mode=force 时"
        "连模型填的也覆盖);非密文值同时写进提示词,模型不会再向用户索要。一行 = 一个景区的一个参数。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}app_scenic_param (
            id           VARCHAR(36) PRIMARY KEY,
            scenic_id    VARCHAR(64) NOT NULL,
            param_key    VARCHAR(64) NOT NULL,
            label        VARCHAR(128) NOT NULL DEFAULT '',
            value        TEXT NOT NULL DEFAULT '',
            arg_names    JSON NOT NULL DEFAULT '[]',
            mode         VARCHAR(16) NOT NULL DEFAULT 'fill',
            is_secret    BOOLEAN NOT NULL DEFAULT false,
            enabled      BOOLEAN NOT NULL DEFAULT true,
            description  TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by   VARCHAR(64),
            CONSTRAINT uq_app_scenic_param_key UNIQUE (scenic_id, param_key),
            CONSTRAINT ck_app_scenic_param_mode CHECK (mode IN ('fill', 'force'))
        );
        CREATE INDEX IF NOT EXISTS ix_app_scenic_param_scenic ON {P}app_scenic_param (scenic_id);
        """,
    ),
    Migration(
        "0090_system_params",
        "参数改成「系统定义 + 景区覆盖」两层,取代 0089 的 app_scenic_param(一个景区一张平表)。"
        "解决的问题:① 同一个参数(OTA 商户账号)在每个景区都得重新定义一遍名字、说明、工具参数名;"
        "② 一个景区可能有多个商户账号,平表一个参数只能存一个值;③ Tool / MCP / Skill 里没法直接写 {{参数}} 引用。"
        "新表:sys_param_def 在系统设置里定义参数(标识、说明、单值/多值、多值怎么用、默认值),"
        "sys_param_value 按景区覆盖取值(可多个,各带备注)。0089 的数据原样迁过来后删表。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}sys_param_def (
            id             VARCHAR(36) PRIMARY KEY,
            param_key      VARCHAR(64) NOT NULL,
            label          VARCHAR(128) NOT NULL DEFAULT '',
            description    TEXT NOT NULL DEFAULT '',
            multi_mode     VARCHAR(16) NOT NULL DEFAULT 'single',
            default_values JSON NOT NULL DEFAULT '[]',
            arg_names      JSON NOT NULL DEFAULT '[]',
            fill_mode      VARCHAR(16) NOT NULL DEFAULT 'fill',
            is_secret      BOOLEAN NOT NULL DEFAULT false,
            enabled        BOOLEAN NOT NULL DEFAULT true,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by     VARCHAR(64),
            CONSTRAINT uq_sys_param_def_key UNIQUE (param_key),
            CONSTRAINT ck_sys_param_def_multi CHECK (multi_mode IN ('single', 'fanout', 'list', 'join')),
            CONSTRAINT ck_sys_param_def_fill CHECK (fill_mode IN ('fill', 'force'))
        );
        CREATE TABLE IF NOT EXISTS {P}sys_param_value (
            id          VARCHAR(36) PRIMARY KEY,
            param_key   VARCHAR(64) NOT NULL,
            scenic_id   VARCHAR(64) NOT NULL,
            "values"    JSON NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by  VARCHAR(64),
            CONSTRAINT uq_sys_param_value UNIQUE (param_key, scenic_id)
        );
        CREATE INDEX IF NOT EXISTS ix_sys_param_value_scenic ON {P}sys_param_value (scenic_id);
        DO $$
        BEGIN
          IF to_regclass('{P}app_scenic_param') IS NOT NULL THEN
            INSERT INTO {P}sys_param_def (id, param_key, label, description, arg_names, fill_mode, is_secret, enabled)
            SELECT DISTINCT ON (param_key) md5(random()::text || param_key)::uuid::text, param_key,
                   COALESCE(NULLIF(label, ''), param_key), description, arg_names, mode, is_secret, true
              FROM {P}app_scenic_param ORDER BY param_key, updated_at DESC
            ON CONFLICT (param_key) DO NOTHING;
            INSERT INTO {P}sys_param_value (id, param_key, scenic_id, "values")
            SELECT md5(random()::text || param_key || scenic_id)::uuid::text, param_key, scenic_id,
                   json_build_array(json_build_object('value', value, 'label', ''))
              FROM {P}app_scenic_param WHERE enabled AND value <> ''
            ON CONFLICT (param_key, scenic_id) DO NOTHING;
            DROP TABLE {P}app_scenic_param;
          END IF;
        END $$;
        """,
    ),
    Migration(
        "0091_qbi_prompts_scenic_datasets_run_task",
        "Quick BI 对齐 1.2.4 的三处补齐:① tc_qbi_prompts 口径说明库(可复用的口径片段,导入 = 抄进"
        "固定问题的 model_instruction,不做外键引用 —— 改库不静默改写正在跑的问题);"
        "② tc_qbi_scenic_datasets 景区数据集映射(一景区一行:连接 + 数据集 + 景区级预热开关,"
        "显式配置,问题没指定目标数据 / 绑的连接失效时按它取);"
        "③ tc_qbi_runs.task_id(触发这次运行的任务,运行记录 ↔ 任务的溯源线索)。"
        "幂等:IF NOT EXISTS;新库 create_all 已含,本条跳过。",
        f"""
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_prompts (
            id            VARCHAR(36)  PRIMARY KEY,
            name          VARCHAR(128) NOT NULL,
            content       TEXT         NOT NULL,
            description   VARCHAR(255),
            owner_user_id VARCHAR(64)  NOT NULL,
            scenic_id     VARCHAR(64),
            visibility    VARCHAR(16)  NOT NULL DEFAULT 'group',
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS {P}tc_qbi_scenic_datasets (
            id              VARCHAR(36)  PRIMARY KEY,
            scenic_id       VARCHAR(64)  NOT NULL UNIQUE,
            connection_id   VARCHAR(36)  NOT NULL,
            cube_id         VARCHAR(128) NOT NULL,
            cube_name       VARCHAR(255) NOT NULL DEFAULT '',
            prewarm_enabled BOOLEAN      NOT NULL DEFAULT true,
            meta            JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
            updated_by      VARCHAR(64),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_scenic_datasets_connection_id
            ON {P}tc_qbi_scenic_datasets (connection_id);
        ALTER TABLE {P}tc_qbi_runs ADD COLUMN IF NOT EXISTS task_id VARCHAR(64);
        CREATE INDEX IF NOT EXISTS ix_{P}tc_qbi_runs_task_id ON {P}tc_qbi_runs (task_id);
        """,
    ),
    Migration(
        "0092_issue_qbi_cache_cleared_at",
        "固定问题加速缓存的作废时刻:scenic_agent_issue_detail.qbi_cache_cleared_at。"
        "只有在它之后跑成功的运行才能回放;改问题/口径/目标数据、管理台重新问、点「清除缓存」时刷新。"
        "幂等:IF NOT EXISTS。",
        f"""
        ALTER TABLE {P}scenic_agent_issue_detail ADD COLUMN IF NOT EXISTS qbi_cache_cleared_at TIMESTAMPTZ;
        """,
    ),
]


# ══════════════════════════════════════════════════════════════════════════
# DDL 注释:让人直接在数据库客户端里就能看懂每张表/每个字段是干什么的。
# 每次运行都会重新应用(COMMENT ON 本身幂等),所以改注释不需要写迁移。
# ══════════════════════════════════════════════════════════════════════════
TABLE_COMMENTS: dict[str, str] = {
    "gov_platform_secrets": "平台级共用密钥(系统设置里配):目前只有 data_platform_key(数据平台 xpftkey,数据指标接口 / sys_supplier_ids / sys_quickbi_link 共用)。值加密存放,接口只回「配没配 / 来源 / 末四位」;取值顺序 本表 > 环境变量(tool_center.secrets)",
    "public_problem_dimension_label": "公共问题维度标签(前后端改造 §2):主题→维度→指标 三级路径,一行一条完整路径。前台选择器与后台问题维护的下拉都取自这里;名称三元组与编码三元组各有一道唯一约束(名称重复=下拉出现两个一样的选项,编码重复=前台分组会把两条路径混成一条)。启动初始化 40 条,可在「知识中心-维度标签」增删改",
    "scenic_agent_issue_detail": "景区智能体问题详情(前后端改造 §1):一行=前台一个可点击的问题,统一承载两类问题。business_class 决定怎么发起:quick_bi 直连 Quick BI(POST /api/app/quickbi/ask,需要 connection_id+capability+target),local_system 走对话引擎(POST /api/app/chat,用 agent_id/skill_id 或走意图路由)。数据由 0076 从 app_question_bank 与 tc_qbi_fixed_questions 迁入,source_question_id 记来源行便于对账;两张旧表保留不动",
    "tc_agent_files": "智能体可读的上传文件。内容走对象存储(storage=minio,只存 object_key),对象存储不可用时落 content 列兜底(storage=db,5MB 上限)——**storage 记的是真实存放位置,不能靠猜**,猜错会出现「两边都没有但记录还在」的死文件。表格类(xlsx/csv)读进来变成数据集句柄交给 sys_table_* 统计,不转成文本:三千行的表转文本是四十多万字,塞不进上下文",
    "usr_users": "用户。role 六值:super_admin/group_admin/group_developer/group_viewer/scenic_admin/scenic_operator;scenic_id 为空=集团级账号",
    "sys_param_def": "系统参数定义(0090,系统设置 · 参数配置):一行 = 一个可在 Tool / MCP / Skill 里用 {{参数标识}} 引用的参数(如 OTA 商户账号 {{ota_account}})。定义名字、说明、单值或多值、多值怎么用(每个值调用一次 / 作为列表一次传入 / 逗号拼接)、系统默认值;景区可在 sys_param_value 覆盖。工具入参名命中 arg_names 时也会自动填入",
    "sys_param_value": "参数的景区取值(0090,景区配置 · 接口参数):一行 = 一个景区对一个参数的覆盖值,values 可有多个(各带备注,如「携程主账号」)。没有这一行 = 该景区用系统默认值",
    "app_scenic_config": "景区配置(25 号):日最大承载量等景区级参数,APP 运营维护。核心列强校验,extra JSONB 承载后期扩展(带 schema_version);未配置=消费侧维持现状不猜默认值",
    "usr_login_devices": "登录设备记录,用于识别新设备并触发短信二次验证",
    "usr_sms_codes": "短信验证码,5 分钟有效",
    "task_queue": "任务队列 + 状态机。PG 为唯一事实源,靠 SKIP LOCKED 竞争认领、租约续期防僵死",
    "conversations": "多轮会话。conversation.id 直接作为 LangGraph Checkpointer 的 thread_id,消息历史由 Checkpointer 持久化,本表只存元信息",
    "tc_tools": "工具注册表。type: http/db/python/mcp;工具名在租户范围内唯一(见 uq_tc_tools_scope_name,部分唯一索引仅约束未删除行,软删归档后同名可重建)",
    "tc_credentials": "凭证密文库。三维归属(tool_id,user_id,scope):user_id 必填=归属用户(无共享凭证),scope 缺省 default 场景;调用时解密注入,前端与模型均不可见明文",
    "tc_grants": "资源定向授权。在四级可见范围之外,额外把某资源授权给指定用户/景区",
    "tc_skills": "Skill=工具×Prompt×约束。scenic_id 为空=集团级资源",
    "tc_prompts": "Skill 的提示词(遗留表,已停用:prompt 内嵌 skill 版本快照 extra_keys,0042 已删 version/is_current 列)",
    "tc_agents": "Agent=Skill 集合 + 人格设定,是对外的对话入口。intent_config=Agent 级意图识别配置(JSON,enabled/mode/model/prompt/history_rounds,未配置时回退框架默认);关联推荐追问已删(0056,推荐入口走问题库)",
    "tc_qbi_grants": "Quick BI 连接分配(0075):哪个用户能用哪个连接。有分配的连接=白名单制(未分配的人不可见/不可用),零分配的连接维持景区+可见性判定;超管豁免。API Key 走连接自身的 api_enabled + 景区,不受本表约束",
    "tc_agent_assignments": "分发指派(15 号文档:收敛为按用户维度,无默认版本概念):把用户(user)钉到 Agent 的某个 published 版本;解析仅查 user 指派命中即用,无记录=agent_not_distributed 报错,绝不回落默认;唯一 (agent_id,scope_id),切换=更新 version_id",
    "tc_mcp_servers": "外部 MCP Server 接入记录。发现导入的工具写入 tc_tools 并标记 source_mcp_server_id",
    "kb_db_datasources": "数据库知识:业务库连接配置。Text2SQL 生成的 SQL 只允许在这些数据源上只读执行",
    "kb_db_catalog": "数据库知识:已登记的库表元数据(DDL+字段释义),作为 Text2SQL 的上下文",
    "kb_db_custom_sources": "数据库知识:自定义虚拟数据源(基于已有数据源的只读 SQL 视图)",
    "kb_web_search_config": "网络知识:搜索引擎配置(博查/SearXNG/Bing)。密钥以 Fernet 加密存 api_key_cipher,页面可维护,优先级高于 config.yaml",
    "kb_web_whitelist": "网络知识:域名白名单,只有命中白名单的检索结果才会进入上下文",
    "kb_web_site_archive": "网络知识:常驻站点档案,按周期抓取",
    "kb_vector_storage_configs": "向量知识:对象存储配置(原始文件存放位置)",
    "kb_vector_db_configs": "向量知识:向量库后端配置(默认 pgvector)",
    "kb_knowledge_bases": "向量知识:知识库",
    "kb_documents": "向量知识:文档。status: processing/indexed/failed",
    "kb_chunks": "向量知识:分块与向量。embedding 维度需与 knowledge.embedding_model 对齐",
    "evt_outbox": "事件发件箱。业务写入与事件入库在同一事务,派发器异步投递,保证不丢事件",
    "gov_audit": "敏感事件审计流水(越权访问/高风险调用/审批处理等),按月分区保留 ≥180 天",
    "gov_llm_usage": "LLM 用量与成本明细,按 scenic_id 聚合出账",
    "gov_approvals": "审批记录。高风险工具调用会先落一条 pending,通过后任务从检查点恢复执行",
    "gov_eval_cases": "评测用例。运行时会真实调用执行引擎,按关键词命中率判定通过",
    "gov_eval_runs": "评测运行结果。同一 batch_id 的多条构成一份运行报告",
    "gov_llm_model_configs": "运行时模型配置。优先级高于 config.yaml,改完立即生效无需重启",
    "gov_llm_model_prices": "模型单价配置(元/百万 token)。由系统设置页面统一管理,替代 litellm 实时拉取 GitHub 计费表;cost.py 按 model_id 精确匹配,优先级:单价表>模型手填>兜底",
    "gov_budget_thresholds": "Token 预算护栏。超硬上限熔断任务,超告警阈值仅记录",
    "tc_resource_versions": "Tool/Skill/Agent 的统一版本表(15 号文档:无默认版本概念,is_default 已删除)。同一资源允许多个 published 并行(供不同指派用户);快照=运行时唯一真实数据源,不再物化回主表;draft 行即编辑区,不参与运行时;published 不可直接编辑,须派生新版本",
    "sys_group": "集团信息(单行,id=default)。集团名与集团级配置",
    "sys_scenics": "已登记到平台的景区(0078 起是 dim_prd_scenic_base_yr_f 的子集)。景区名称/等级/属地以主数据维表为准,本表只存平台侧状态:is_enabled 是否开通服务、plan_config 套餐配置、synced_at 最近一次与主数据对齐的时间。组织管理与成员管理共用这一份",
    "sys_model_providers": "模型厂商接入配置(base_url/api_key)。与 gov_llm_model_configs 的区别:此表是厂商维度,后者是运行时模型维度",
    "sys_model_configs": "模型配置(厂商 × 模型名)。is_default 标记默认模型;extra_params 承载厂商自定义参数",
    "app_followed_questions": "APP 端用户关注的问题(对某条回答里的问题点关注以便跟踪),软删用 deleted_at",
    "app_message_feedback": "APP 端消息反馈(like/dislike),按 message_id × user_id 记录",
    "tc_qbi_connections": "Quick BI 智能小Q 连接:server_domain/api_key + Fernet 加密的 api_secret、user_token(决定可访问数据范围);api_enabled 控制是否允许开放平台 /v1/quickbi 调用",
    "tc_qbi_assets": "Quick BI 数据资产:kind=dashboard(仪表板,meta 存结构摘要/引用数据集/候选问题)或 dataset(数据集)",
    "tc_qbi_files": "Quick BI 上传数据文件(≤5MB 原文件 bytea);qa_file_id=问数 fileId,report_file=报告引用文件,markdown=解读数据,均按需懒上传后缓存",
    "tc_qbi_sessions": "Quick BI 对话会话:粘住目标数据(数据集/文件/仪表板),多轮追问;capability=auto 时按意图路由",
    "tc_qbi_prompts": "Quick BI 口径说明库(0091):可复用的口径 / 约束片段。导入 = 抄进固定问题的 model_instruction(不是引用),改库不改写已配的问题",
    "tc_qbi_scenic_datasets": "Quick BI 景区数据集映射(0091):一景区一行 = 用哪条连接 + 哪个数据集取数 + 景区级预热开关。显式配置:问题没指定目标数据、或绑的连接失效时按它取",
    "tc_qbi_runs": "Quick BI 运行记录:每次问数/解读/报告一行,result 存结构化结果快照(图表行数截断到 1000);报告任务 status=running 期间可按 chat_id 刷新",
    "schema_migrations": "数据库结构迁移记录。由 cogni_agent/common/schema.py 维护,请勿手工改动",
    "llm_prompt_logs": "LLM 提示词日志:每次大模型调用 LLM 真正收到的最终提示词(system_prompt 完整、history 截断)。step=intent_check/generation/drift_check;供会话信息技能做提示词质量分析(业务噪音/内部信息泄漏排查),v1.1.6 起写入",
}

COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "sys_param_def": {
        "param_key": "参数标识,全局唯一,小写字母开头的字母/数字/下划线(如 ota_account)。在 Tool / MCP / Skill 里写 {{ota_account}} 引用;不能和内置变量(scenic_id / today …)或平台密钥同名",
        "label": "显示名(如「OTA 商户账号」),界面与提示词里用它说明这是什么",
        "description": "说明:干什么用、值找谁要、哪些工具用",
        "multi_mode": "取值个数与多值用法:single=单值;fanout=多值,每个值各调用一次工具再合并结果;list=多值,作为一个数组一次传入;join=多值,逗号拼成一个字符串一次传入",
        "default_values": "系统默认值(JSON 数组,元素 {value, label});景区没覆盖时用它,为空 = 景区不配就没有值",
        "arg_names": "JSON 数组:工具入参名,命中任一且模型没填时自动填入(如 [\"account\"])。为空 = 只能用 {{参数标识}} 显式引用",
        "fill_mode": "按入参名自动填入时:fill=模型没填才填;force=一律用配置值覆盖模型填的",
        "is_secret": "是否敏感:true 时不写进提示词(Skill 里的 {{参数}} 渲染成「已配置」),界面只显示末四位",
        "enabled": "是否启用;停用 = 保留定义但不注入、不渲染",
        "created_at": "创建时间",
        "updated_at": "最后修改时间",
        "updated_by": "最后修改人的用户 ID",
    },
    "sys_param_value": {
        "param_key": "参数标识(sys_param_def.param_key)",
        "scenic_id": "景区 ID",
        "values": "该景区的取值(JSON 数组,元素 {value, label});多值参数可有多个,label 是备注(如「携程主账号」)",
        "created_at": "创建时间",
        "updated_at": "最后修改时间",
        "updated_by": "最后修改人的用户 ID",
    },
    "gov_platform_secrets": {
        "name": "密钥名,白名单见 platform_secrets.KNOWN(不接受任意名字)",
        "value_cipher": "加密后的值(vault,与模型 API Key 同一套);明文不落库、不回前端",
        "updated_by": "最后修改人的用户 ID",
        "updated_at": "最后修改时间",
    },
    "tc_qbi_assets": {
        "connection_id": "属于哪条 Quick BI 连接(= 哪一套凭证)。资产能不能打开由它决定:ref_id 是 Quick BI 那边的 id,只在同一个工作空间里有效",
        "kind": "dashboard(仪表板)或 dataset(数据集)",
        "ref_id": "Quick BI 侧的仪表板 pageId / 数据集 cubeId",
        "scenic_id": "归属景区;为空 = 集团级(所有景区都能用,但只有集团角色能改)",
        "active": "是否可用。连接被删且无法转移到等效连接时置 false —— 与其留着当死引用(点了才报错),不如停用并写明原因;换回可用连接后可重新启用",
        "disabled_reason": "停用原因(给人看的一句话,如「原连接已删除,新连接不是同一个 Quick BI 工作空间」)",
        "source_asset_id": "数据集专用:它是从哪块仪表板拉出来登记的。删那块板时要连带删它,所以这层归属直接记下来,不靠 meta.cubes 反查(共用 cube、板刷新结构这两种情况都会把反查算错)",
        "category": "自定义分类(0088):运营在数据集上输入文字回车即存,不需要先建分类;数据集列表按它分组展示,编辑问题时按分类多选 / 全选数据集。空 = 未分类",
    },
    "tc_qbi_files": {
        "connection_id": "属于哪条 Quick BI 连接。远端 fileId 直接绑在该连接的 user_token 上,换一套凭证必然失效",
        "active": "是否可用。连接被删时一律置 false —— 上传文件是强绑定,没有「转移到另一条连接」这种说法",
        "disabled_reason": "停用原因(给人看的一句话)",
    },
    "public_problem_dimension_label": {
        "id": "自增主键",
        "topic": "主题中文名,如 客流 / 票务 / 舆情 / OTA直销",
        "topic_id": "主题编码,如 visitorFlow / ticketing / opinion / otaDirectSale",
        "dimension": "维度中文名(主题下的第二级),如 在园人员 / 售票概况",
        "dimension_id": "维度编码,如 inParkVisitor / ticketingOverview",
        "metric": "指标中文名(第三级,问题挂在它下面),如 实时人数 / 承载率",
        "metric_id": "指标编码,如 liveVisitorCnt / loadRate",
        "sort_order": "同维度下的展示顺序,小的在前;相同则按 id",
        "active": "是否启用。停用的标签不进前台选择器,但已挂它的问题不会丢",
        "create_time": "创建时间",
        "update_time": "更新时间",
    },
    "tc_agent_files": {
        "file_name": "原始文件名(带后缀)。解析按后缀分流:表格/文档/图片三类走完全不同的路",
        "size": "字节数",
        "sha256": "内容摘要,用于去重与校验",
        "storage": "内容存在哪:minio=对象存储(看 object_key)/ db=数据库(看 content)。取的时候按它走,不要两边试",
        "object_key": "对象存储里的键,形如 agent-files/<景区>/<年/月/日>/<uuid>_<文件名>。按景区与日期分目录,便于按批清理",
        "content": "对象存储不可用时的兜底内容(≤5MB)。兜底比主路小是故意的:它是为了 MinIO 挂掉时服务不断,不是为了长期扛大文件",
        "parsed_kind": "解析出来是什么:table 表格 / doc 文档 / image 图片。表格给句柄,文档与图片给文本",
        "parse_error": "解析失败的原因(给模型看的那句话),失败也留痕,便于回答「为什么读不了这个文件」",
        "detail": "解析附带信息:表格的行数/列名/工作表清单,文档的字数等",
        "conversation_id": "在哪个会话里传的。同一轮对话里模型才认得出「我刚传的那份」",
        "scenic_id": "归属景区,空=集团级。按它做可见范围隔离",
    },
    "scenic_agent_issue_detail": {
        "qbi_cache_cleared_at": "加速缓存作废时刻(0092):只回放在它之后跑成功的运行。改问题/口径说明/目标数据、管理台重新问、点「清除缓存」时刷新;空 = 没清过",
        "prewarm_enabled": "加速(预热)开关。开了之后后台定时提前跑好,前台/APP 再点这条问题直接回放,不必现跑",
        "prewarm_interval_minutes": "刷新时间(分钟):后台多久重跑一次。留空取 runtime 默认",
        "prewarm_ttl_minutes": "存储时间(分钟):跑出来的结果多久之内算新鲜、可直接回放。建议 >= 刷新时间,否则会出现「还没到下次刷新就已过期」的空窗",
        "id": "自增主键(物理主键,不进接口)",
        "issue_id": "问题业务 ID,对外一律用它。形如 issue_3f2a…",
        "scenic_id": "归属景区;为空=全景区通用模板(所有景区都能看到)",
        "business_class": "业务渠道类别:quick_bi=直连 Quick BI 提问;local_system=走平台对话引擎。前台据此决定调哪个接口",
        "connection_id": "Quick BI 连接 ID(tc_qbi_connections.id)——就是操练台上那个「Quick BI 链接」下拉选中的那一项,一套访问凭证。文档里的 quickbiLinkId 与它是同一个东西,表里只存一列,接口两个字段名都返回。仅 quick_bi 有值",
        "ask_type": "Quick BI 请求类别:qa 问数 / insight 解读 / dashboard 报表 / report 报告。仅 quick_bi 有值",
        "capability": "Quick BI 能力:dataset_qa 数据集问数 / dashboard_qa 仪表板问数 / file_qa 文件问数 / insight 解读 / report 报告。比 ask_type 细一档。仅 quick_bi 有值",
        "target": "Quick BI 目标数据(JSONB,沿用 tc_qbi_fixed_questions.target 结构),键可选 asset_id/page_id/cube_id/cube_name/file_id/dashboard",
        "target_data_id": "目标数据在 Quick BI 侧的编码(仪表板 pageId / 数据集 cubeId),从 target 里提取冗余,供前台直接展示与回传",
        "target_data_name": "目标数据名称(数据集/仪表板名),冗余存一份:名称改了不影响已发出的问题,也免得每次展示都去 Quick BI 查一次",
        "agent_id": "指定执行 Agent(仅 local_system);为空=用 APP 默认 Agent",
        "skill_id": "指定执行 Skill(仅 local_system);非空=命中后直跳该技能,跳过意图识别;为空=走意图路由",
        "topic": "所属主题中文名(冗余自维度标签表,避免列表接口每次连表)",
        "topic_id": "所属主题编码",
        "dimension": "所属维度中文名",
        "dimension_id": "所属维度编码",
        "metric": "所属指标中文名",
        "metric_id": "所属指标编码",
        "title": "问题标题(列表上显示的短文案)",
        "issue": "问题原文(真正发给 Quick BI / 大模型的那句话)",
        "model_instruction": "给模型的口径说明(不是给人看的介绍):取哪个时间范围、用哪个工具、什么算异常、答多长。与 issue 分开是因为按钮要短,而模型需要的恰恰是那些啰嗦的约束",
        "param_template": "参数模板 JSONB,{参数名: 带占位符的值},发起前由代码展开({{today}}/{{yesterday}} 等),省掉让模型推日期(推错很难发现)",
        "time_period": "取数窗口:today/yesterday/week_to_date/month_to_date…,供展示与预热判断",
        "sort_order": "同指标下的展示顺序,小的在前",
        "active": "是否上架。下架的问题不进前台列表",
        "last_run_id": "最后一次执行的会话/运行 ID(Quick BI 为 run_id,本地为 task_id)",
        "last_run_status": "最后一次执行状态:success/failed/running",
        "last_run_time": "最后一次执行时间",
        "owner_user_id": "创建者用户 ID(0079 从 tc_qbi_fixed_questions 迁入)。只有创建者与集团管理员/开发者可以改这条问题",
        "visibility": "可见范围:private 仅自己 / scenic 本景区 / group 集团内 / public(景区 API Key 也可调用)。0079 从 tc_qbi_fixed_questions 迁入",
        "domain": "数据域:票务/财务/舆情/客群/核销/营销/流量/画像……自由文本,允许现场新增(业务分类总会长出新的一类,加一类不该改表)",
        "category": "管理端自定义分组(与三级标签无关,纯粹后台好找)",
        "locale": "语种(zh_CN/en_US),为空则跟随 Quick BI 连接的默认语种",
        "source_question_id": "迁移溯源:来自 tc_qbi_fixed_questions.id(该表已于 0079 删除)或 app_question_bank.id。老 ID 仍可按它查到这一行,历史工具配置与运行记录不会失效",
        "create_time": "创建时间",
        "update_time": "更新时间",
    },
    "task_queue": {
        "status": "CREATED/QUEUED/RUNNING/WAITING_HUMAN/PAUSED/COMPLETED/FAILED/CANCELLED",
        "conversation_id": "所属多轮会话;为空表示一次性任务(无对话记忆)",
        "version": "乐观锁版本号,状态流转时用于并发控制",
        "lease_until": "租约到期时间。worker 心跳续租,过期后由巡检回收重新入队",
        "payload": "任务入参快照(message/user_id/role/overrides 等)",
        "result": "执行结果(answer/usage/timeline)",
        "executor_type": "agent / skill / tool / mcp",
    },
    "conversations": {
        "id": "同时作为 LangGraph Checkpointer 的 thread_id",
        "turn_count": "成功完成的轮次数(失败/取消的轮次不计)",
        "title": "取首条用户消息前 40 字",
        "archived": "归档后不再出现在会话列表,也不允许继续追加对话",
    },
    "tc_tools": {
        "type": "http=发 HTTP 请求 / db=只读 SQL / python=沙箱插件 / mcp=远端 MCP 工具(走 JSON-RPC tools/call)",
        "type_config": "按 type 不同结构不同:http={method,url,params_schema,headers};db={datasource_id,sql_template};python={script,input_schema};mcp={server_id,remote_tool_name,input_schema}",
        "risk_level": "low/medium/high。high 在执行前强制触发人工审批(HITL)",
        "visibility": "private(仅创建者)/scenic(本景区)/group(集团内)/public(全平台)",
        "scenic_id": "为空=集团级资源",
        "locked": "锁定后不可编辑/删除,防止被引用中的工具被误改",
        "source_mcp_server_id": "由 MCP 发现导入时标记来源 Server",
    },
    "tc_skills": {
        "description": "能力一句话概述(人写,供意图识别路由器/能力清单展示)。留空时运行时回退 system_prompt 前120字",
        "suggested_mode": "L0=直接调用 / L1=固定编排 / L2=智能体循环。单 Skill 执行时以此为准",
        "constraints": "护栏与扩展配置:max_steps/max_tool_calls/token_budget(单轮 prompt+completion 累计上限,超出熔断;0/留空=不限制,消耗仍记录)/model/response_format/knowledge_mode/strict_tool_execution/route_hit_keywords(路由强挂关键词:goal 命中即强制并入 target_skills,防 Triager 漏挂裁剪,如客流分析配 [客流,人流量,拥挤])",
        "knowledge_sources": "启用的知识源:vector/database/websearch",
        "scenic_id": "为空=集团级资源",
    },
    "tc_agents": {
        "bound_skill_ids": "绑定的 Skill;多个时按 agent 自身 composition_mode 的编排方式执行",
    },
    "tc_resource_versions": {
        "resource_type": "tool / skill / agent",
        "version": "版本号,同一资源内自增,派生时定死(max+1),保存只改内容不递增",
        "status": "draft=编辑区(不参与运行时)/ published=已发布快照 / offline=已下线",
        "base_version_id": "草稿来源版本(仅 draft 有意义):派生时记录,UI 可展示「基于 vN 编辑中」",
        "snapshot": "该版本的完整定义快照(含 pin:bound_skills/tools 带版本),运行时唯一真实数据源(15 号文档:无默认版本概念,快照不再物化回主表)",
        "note": "这个版本改了什么(发布/回滚时由操作者填写)",
        "created_by": "派生该版本(打开草稿)的用户 id",
        "updated_by": "最近修改人 user_id:保存草稿 / 发布 / 下线 / 重新发布时写入;工具中心列表的「修改人」取自这里(0087)",
        "updated_at": "最近修改时间:列表「最近修改」取该资源所有版本里的最大值",
    },
    "tc_mcp_servers": {
        "connect_status": "pending/connected/failed,由发现或连通性自检写入",
        "auth_type": "none/apikey/bearer/basic;凭证密文存 tc_credentials",
    },
    "gov_approvals": {
        "status": "pending/approved/rejected",
        "context": "待执行动作的上下文快照(工具名与实参),供审批人判断",
        "related_task_id": "关联任务。通过后该任务从 LangGraph 检查点恢复执行",
    },
    "kb_chunks": {
        "chunk_type": "text/image",
        "content_ref": "text 类型直接存文本;image 类型存对象存储路径",
    },
    "gov_llm_model_configs": {
        "id": "UUID 主键,模型身份。业务记录(model_override / usage.model)持久化 model_id 而非 name,改名不影响归因",
        "name": "展示名(唯一字段,可改名)。给人看的别名,运行时解析以它为 key、按 model_id 调用",
        "is_default": "是否为默认模型。未指定模型的任务用它;全局最多一个 true(有唯一索引兜底)",
        "api_key_cipher": "Fernet 加密后的 API Key,不回显明文",
        "active": "停用后模型解析会跳过它,退回 config.yaml 静态清单",
    },
    "llm_prompt_logs": {
        "task_id": "所属任务 ID(task_queue.id)。同一任务多次 LLM 调用各记一行",
        "conversation_id": "所属会话 ID(conversations.id)。按它聚合即还原某会话每轮 LLM 看到什么",
        "scenic_id": "强隔离字段,空=集团级任务",
        "step": "调用类型:intent_check=Triager 分诊 / generation=主体生成(含 DAG 子任务与汇总) / drift_check=目标漂移自检",
        "model": "实际调用模型 key(model_id)",
        "prompt_tokens": "本次调用输入 token 数(调用后回填)",
        "system_prompt": "完整 system 层提示词(主 prompt + extra_systems 拼接),分析「系统提示词有无业务噪音」的核心字段",
        "history": "user/assistant/tool 消息 JSON 数组(每条截断 300 字符,最多保留 40 条),用于分析上下文注入",
        "created_at": "调用时间",
    },
}


def _split_statements(sql: str) -> list[str]:
    """把一段多语句 SQL 拆成单条。

    **不能直接 sql.split(";")** —— 分号完全可能出现在字符串字面量里,比如
    `COMMENT ON TABLE t IS '这是说明;还有后半句'`,裸切会把语句从中间劈开,
    报一个指向莫名其妙位置的「unterminated quoted string」。
    这里做最小限度的引号感知:单引号内的分号不算分隔符,连续两个单引号是转义;
    `$$ … $$`(DO 块 / 函数体)内的分号也不算。
    """
    out: list[str] = []
    buf: list[str] = []
    in_str = False
    in_dollar = False     # DO $$ … $$ 块体内的分号也不是分隔符(0090 迁移数据时用了 DO 块)
    i = 0
    while i < len(sql):
        ch = sql[i]
        if not in_str and sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if in_dollar:
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")      # SQL 里 '' 表示一个转义的单引号
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif ch == ";" and not in_str:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


async def _ensure_migration_table(conn) -> None:
    await conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {P}schema_migrations (
            id          VARCHAR(64) PRIMARY KEY,
            note        TEXT,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))


async def _applied_ids(conn) -> set[str]:
    rows = await conn.execute(text(f"SELECT id FROM {P}schema_migrations"))
    return {r[0] for r in rows}


async def apply_migrations(conn, *, verbose: bool = True) -> list[str]:
    await _ensure_migration_table(conn)
    done = await _applied_ids(conn)
    applied: list[str] = []
    for m in MIGRATIONS:
        if m.id in done:
            continue
        for stmt in _split_statements(m.sql):
            # 用 exec_driver_sql 而不是 execute(text(...)):
            # text() 会把 `:xxx` 解析成绑定参数,而 DDL 注释里出现「枚举:自然风光」
            # 这种冒号跟中文的写法非常自然 —— 中文在正则里也算词字符,于是被当成
            # 一个没有传值的绑定参数,报 "A value is required for bind parameter '自然风光'"。
            # 迁移 SQL 是本文件里的静态常量,不含用户输入,直接走驱动层执行最省事也最安全。
            await conn.exec_driver_sql(stmt)
        await conn.execute(
            text(f"INSERT INTO {P}schema_migrations (id, note) VALUES (:i, :n) ON CONFLICT (id) DO NOTHING"),
            {"i": m.id, "n": m.note},
        )
        applied.append(m.id)
        if verbose:
            print(f"    ↳ 迁移 {m.id}:{m.note}")
    return applied


def _quote(literal: str) -> str:
    """把注释内容转成 SQL 字符串字面量。

    COMMENT ON **不接受绑定参数**($1 会被当成语法错误),只能内联。所以这里必须
    自己处理转义:单引号翻倍。注释内容全部来自本文件里的常量、不含用户输入,
    没有注入面,但转义仍然要做对——否则注释里出现一个撇号就会把语句劈开。
    """
    return "'" + literal.replace("'", "''") + "'"


async def apply_comments(conn) -> int:
    """应用表/字段注释。COMMENT ON 幂等,每次都重跑,改注释不需要写迁移。"""
    existing = {r[0] for r in await conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ))}
    n = 0
    for table, comment in TABLE_COMMENTS.items():
        full = f"{P}{table}"
        if full not in existing:
            continue
        await conn.exec_driver_sql(f"COMMENT ON TABLE {full} IS {_quote(comment)}")
        n += 1
    for table, cols in COLUMN_COMMENTS.items():
        full = f"{P}{table}"
        if full not in existing:
            continue
        present = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"), {"t": full})}
        for col, comment in cols.items():
            if col in present:
                await conn.exec_driver_sql(f"COMMENT ON COLUMN {full}.{col} IS {_quote(comment)}")
                n += 1
    return n


async def _model_column_drift(conn) -> list[str]:
    """只读:找出「模型已声明、库里却缺失」的列(形如 表.列)。"""
    rows = await conn.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public'"))
    actual: dict[str, set[str]] = {}
    for r in rows:
        actual.setdefault(r[0], set()).add(r[1])

    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        cols = actual.get(table.name)
        if cols is None:
            continue  # 表不存在 => 缺表由 sync_schema 单独报(不再 create_all 兜底)
        for col in table.columns:
            if col.name not in cols:
                drift.append(f"{table.name}.{col.name}")
    return drift


async def _model_column_type_drift(conn) -> list[str]:
    """只读:找出「模型已声明、库列存在但类型不一致」的列(形如 表.列 库=X ≠ 模型=Y)。

    **类型漂移只能靠 MIGRATIONS 里追加 ALTER TYPE 收敛**(见 0040)——比如模型把
    String(255) 放宽成 Text,dev 老库列却仍是 varchar(255),迁移 Agent 时 15 个
    description>255 的工具 INSERT 直接 500。

    自动补列已下线。这里在启动同步时做**只读体检**:迁移全跑完后模型与库仍对不上,
    一律由 sync_schema raise 拒绝启动(没有迁移文件记录 = 不放行)。
    """
    from sqlalchemy import BigInteger, Boolean, CHAR, Date, DateTime, Float, Integer, JSON
    from sqlalchemy import LargeBinary, Numeric, SmallInteger, String, Text, Time, Unicode
    from sqlalchemy.dialects.postgresql import JSONB

    rows = await conn.execute(text(
        "SELECT table_name, column_name, data_type, udt_name, "
        "character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema='public'"))
    actual: dict[str, dict[str, tuple]] = {}
    for r in rows:
        actual.setdefault(r[0], {})[r[1]] = (r[2], r[3], r[4], r[5], r[6])

    def model_sig(col_type):
        """模型列 -> (类型, 附加信息)。不认得的类型返回 None,跳过该列(宁缺勿误报)。"""
        t = col_type
        if isinstance(t, Text):
            return ("text", None)
        if isinstance(t, (String, Unicode)):
            return ("character varying", getattr(t, "length", None))
        if isinstance(t, CHAR):
            return ("character", getattr(t, "length", None))
        if isinstance(t, Boolean):
            return ("boolean", None)
        # BigInteger / SmallInteger 都是 Integer 的子类,必须排在它前面判断 ——
        # 顺序反了的话 bigint 列会被当成 integer,体检天天报一条假漂移
        # (而真漂移就淹没在假的里面了)。
        if isinstance(t, BigInteger):
            return ("bigint", None)
        if isinstance(t, SmallInteger):
            return ("smallint", None)
        if isinstance(t, Integer):
            return ("integer", None)
        if isinstance(t, Float):
            return ("double precision", None)
        if isinstance(t, Numeric):
            return ("numeric", (t.precision, t.scale))
        if isinstance(t, DateTime):
            return ("timestamp with time zone" if t.timezone
                    else "timestamp without time zone", None)
        if isinstance(t, Date):
            return ("date", None)
        if isinstance(t, Time):
            return ("time with time zone" if getattr(t, "timezone", False)
                    else "time without time zone", None)
        if isinstance(t, LargeBinary):
            return ("bytea", None)
        if isinstance(t, JSONB):
            return ("jsonb", None)
        if isinstance(t, JSON):
            return ("json", None)
        if type(t).__name__ in ("UUID", "Uuid"):
            return ("uuid", None)
        if type(t).__module__.startswith("pgvector.sqlalchemy"):
            return ("vector", None)
        return None

    def db_sig(dt, udt, length, num_prec, num_scale):
        if dt == "ARRAY":
            return None  # 数组列类型宽度杂,暂不体检
        if dt == "USER-DEFINED":
            return ("vector", None) if udt == "vector" else None
        if dt in ("character varying", "character"):
            return (dt, length)
        if dt == "numeric":
            return ("numeric", (num_prec, num_scale))
        return (dt, None)

    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        cols = actual.get(table.name)
        if cols is None:
            continue  # 表不存在 => 缺表由 sync_schema 单独报(不再 create_all 兜底)
        for col in table.columns:
            got_db = cols.get(col.name)
            if got_db is None:
                continue  # 缺列 => 由 _model_column_drift 报(自动补列已下线)
            want = model_sig(col.type)
            if want is None:
                continue
            got = db_sig(*got_db)
            if got is None:
                continue
            if got != want:
                drift.append(
                    f"{table.name}.{col.name}  库={got[0]}{got[1] or ''}  "
                    f"模型={want[0]}{want[1] or ''}"
                )
    return drift


async def _model_extra_column_drift(conn) -> list[str]:
    """只读:找出「库里有、模型却已删」的冗余列(形如 表.列)。

    模型 = 唯一事实源。库里多出的列 = 曾声明、后来删字段但没写 DROP 迁移的历史遗留
    (多为 create_all 时代建过、迁移链从未收敛)。这类列不触发缺列/类型漂移体检的盲区,
    但若 NOT NULL 无默认(如 tc_prompts.version),任何对该表的 INSERT 都会 500——
    dev 跨环境迁移 Agent/Skill 时被 IntegrityError 当场炸出。

    自动补列/建表已下线。这里同样做只读体检:冗余列存在即由 sync_schema raise,
    需到 MIGRATIONS 末尾补 DROP COLUMN 迁移收敛(参照 0042)。

    created_at/updated_at 是约定维护列(DB server_default 自动填充、模型可不声明),
    不视为冗余。
    """
    rows = await conn.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public'"))
    actual: dict[str, set[str]] = {}
    for r in rows:
        actual.setdefault(r[0], set()).add(r[1])

    model_cols: dict[str, set[str]] = {
        table.name: {c.name for c in table.columns}
        for table in Base.metadata.sorted_tables
    }
    MAINT = {"created_at", "updated_at"}
    extra: list[str] = []
    for table_name, db_cols in actual.items():
        mcols = model_cols.get(table_name)
        if mcols is None:
            continue  # 库多出的整表不在此拦,由 _model_extra_table_drift 负责
        for col in sorted(db_cols - mcols - MAINT):
            extra.append(f"{table_name}.{col}")
    return extra


async def _model_extra_table_drift(conn) -> list[str]:
    """只读:找出「库里有、模型却已删」的冗余业务表(形如 表名)。

    与冗余列同源:曾声明建模、后来把整个模型删了但没写 DROP TABLE 迁移的残留表
    (create_all 时代建过、迁移链从未收敛)。模型 = 唯一事实源,库里本项目命名空间
    (P 前缀)下多出的表 = 漏迁移,须由 sync_schema raise、到 MIGRATIONS 补
    DROP TABLE IF EXISTS 收敛(与冗余列 0042 同思路)。

    只查 P 前缀表:非 P 前缀表(LangGraph checkpointer 自建等)不在本项目结构治理
    范围;{P}schema_migrations 是迁移记录表(_ensure_migration_table 手建、非模型),
    天然存在,显式排除。
    """
    rows = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'"))
    model_tables = {t.name for t in Base.metadata.sorted_tables}
    infra = {f"{P}schema_migrations"}
    db_p_tables = {r[0] for r in rows if r[0].startswith(P)}
    return sorted(db_p_tables - model_tables - infra)





class DatabaseUnreachable(RuntimeError):
    """数据库连不上。

    单独一个类型,是为了让调用方能**只打印一句人话**,而不是把 200 行
    SQLAlchemy/asyncpg 内部堆栈糊到屏幕上 —— 那里面真正有用的只有最后一行
    「Connect call failed ('127.0.0.1', 5432)」,其余全是噪音。
    """


async def check_connection() -> None:
    """连库自检。连不上时抛出一句能直接照着做的说明。

    单独做这一步,是因为后面 create_all / 迁移的堆栈都很深,连接失败混在里面
    极难一眼看出根因。先用一次轻量连接把「能不能连上」单独判定掉。
    """
    from cogni_agent.config import get_settings

    cfg = get_settings().get("database", default={}) or {}
    host, port = cfg.get("host", "?"), cfg.get("port", "?")
    db, user = cfg.get("db", "?"), cfg.get("user", "?")

    try:
        async with get_engine().connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        low = text.lower()

        if "refused" in low or "10061" in text or "1225" in text or "connect call failed" in low:
            raise DatabaseUnreachable(
                f"连不上数据库 {host}:{port} —— 端口拒绝连接,通常是 PostgreSQL 没有启动。\n\n"
                f"请依次检查:\n"
                f"  1. PostgreSQL 服务是否已启动\n"
                f"     Windows:服务管理器里找 postgresql-x64-16,或 `net start postgresql-x64-16`\n"
                f"     Docker :`docker compose up -d postgres`\n"
                f"     Linux  :`sudo systemctl start postgresql`\n"
                f"  2. 端口是否为 {port}(默认 5432)。改过端口就同步改 .env 的 POSTGRES_PORT\n"
                f"  3. 手工连一次确认:psql -h {host} -p {port} -U {user} -d {db}\n\n"
                f"当前配置来自 .env:POSTGRES_HOST={host} POSTGRES_PORT={port} "
                f"POSTGRES_DB={db} POSTGRES_USER={user}"
            ) from exc

        if "password authentication failed" in low:
            raise DatabaseUnreachable(
                f"数据库 {host}:{port} 拒绝了账号 {user!r} —— 密码不对。\n"
                f"检查 .env 里的 POSTGRES_USER / POSTGRES_PASSWORD。"
            ) from exc

        if "does not exist" in low and db in text:
            raise DatabaseUnreachable(
                f"数据库 {db!r} 不存在。先建库再运行本脚本:\n"
                f"  createdb -h {host} -p {port} -U {user} {db}\n"
                f"  psql -h {host} -p {port} -U {user} -d {db} "
                f"-c \"CREATE EXTENSION IF NOT EXISTS vector;\""
            ) from exc

        # 认不出来的错误原样抛出:猜错了反而会把人引到错误方向
        raise


async def sync_schema(*, verbose: bool = True) -> dict:
    """建表基线 + 增量迁移 + 结构体检,全部幂等。应用启动前会自动调用一次。

    **模型 = 结构声明源,迁移 = 唯一增量通道**。结构演进铁律:

      1. 全新库(库里还没有任何本项目表)=> create_all 一次性建全量基线(仅首次
         部署),随后 apply_migrations 把整条迁移链记录为已应用——新库天然带
         完整迁移记录,可追溯;
      2. 老库=> **不再 create_all、不再自动补列**。加表/加列/改类型只能通过
         MIGRATIONS 增量演进;本地改了模型却忘了写迁移,迁移跑完后只读体检会
         当场发现并 **raise 拒绝启动**(没有迁移文件记录 = 不放行)。

    为什么砍掉自动补列:历史版本曾在启动时自动 ADD COLUMN 兜底(git 历史里的
    _backfill_model_columns),「改了模型不写迁移」在本地/dev 都能静默通过——
    零迁移记录、不可追溯,dev 一重置就打回原形。现在改成硬约束:
    **任何结构改动必须有迁移文件,没有迁移文件记录的一律不放行。**
    """
    # 先单独判定「能不能连上」。放在建表/迁移之前 —— 那些操作的堆栈很深,
    # 连接失败混在里面根本看不出根因,报出来是 200 行 asyncpg 内部调用。
    await check_connection()

    engine = get_engine()
    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "无法启用 pgvector 扩展。需要先在 Postgres **服务器**上安装 pgvector 扩展文件:\n"
                "  - Windows 官方安装包:用 StackBuilder 勾选安装\n"
                "  - Docker:改用 pgvector/pgvector:pg16 镜像(docker-compose.yml 已是)\n"
                "  - Linux:apt install postgresql-16-pgvector\n"
                f"原始错误:{exc}"
            ) from exc

        async def _db_table_names() -> set[str]:
            rows = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"))
            return {r[0] for r in rows}

        db_before = await _db_table_names()
        is_fresh = not any(n.startswith(P) for n in db_before)

        created: list[str] = []
        if is_fresh:
            # 全新库:create_all 建全量基线(仅此一次,后续演进全走迁移)
            await conn.run_sync(Base.metadata.create_all)
            db_after_create = await _db_table_names()
            created = sorted(db_after_create - db_before)
            if verbose and created:
                print(f"    ↳ 全新库建基线 {len(created)} 张表:"
                      f"{', '.join(t.replace(P, '') for t in created)}")
        elif verbose:
            print("    ↳ 非全新库:跳过 create_all/自动补列(结构改动只能追加迁移)")

        applied = await apply_migrations(conn, verbose=verbose)

        # ── 只读体检:迁移全跑完后,模型与库仍对不上 = 改了没写迁移 ──────────
        db_final = await _db_table_names()  # 迁移可能建表,以迁移后的库为准
        missing_tables = sorted({t.name for t in Base.metadata.sorted_tables} - db_final)
        extra_tables = await _model_extra_table_drift(conn)
        missing_columns = await _model_column_drift(conn)
        type_drift = await _model_column_type_drift(conn)
        extra_columns = await _model_extra_column_drift(conn)
        problems = (
            [f"缺表 {t}(模型声明、库里没有)" for t in missing_tables]
            + [f"冗余表 {t}(库残留废弃表,模型已删)" for t in extra_tables]
            + [f"缺列 {c}" for c in missing_columns]
            + [f"冗余列 {c}(库残留废弃列,模型已删)" for c in extra_columns]
            + [f"类型漂移 {d}" for d in type_drift]
        )
        if problems:
            detail = "\n      - ".join(problems)
            raise RuntimeError(
                "数据库结构体检失败:模型与库不一致,且这些改动没有迁移文件覆盖。\n"
                "铁律:任何结构改动(加表/删表/加列/删列/改类型)都必须有迁移文件,"
                "没有迁移记录的一律不放行。\n"
                "请到 cogni_agent/common/schema.py 的 MIGRATIONS 末尾追加幂等迁移"
                "(参照 0040/0042 的写法):\n"
                "      - 缺表   => CREATE TABLE IF NOT EXISTS ...(按模型定义写全)\n"
                "      - 冗余表 => DROP TABLE IF EXISTS ...(模型已删整表,库残留)\n"
                "      - 缺列   => ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...\n"
                "      - 冗余列 => ALTER TABLE ... DROP COLUMN IF EXISTS ...(模型已删字段,"
                "库残留;NOT NULL 无默认会卡写入)\n"
                "      - 类型漂移 => ALTER TABLE ... ALTER COLUMN ... TYPE ...\n"
                f"  发现:\n      - {detail}\n"
                "(本次同步已在事务内整体回滚,补好迁移重跑即可。)"
            )
        commented = await apply_comments(conn)

    if verbose:
        print(f"    ↳ 结构体检通过:模型与库一致(共 {len(db_final)} 张表)")
    return {"fresh": is_fresh, "created_tables": created,
            "applied_migrations": applied,
            "comments_applied": commented, "total_tables": len(db_final)}
