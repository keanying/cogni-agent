---
title: 初始化与维护(环境密钥 / 数据库结构 / 超级管理员)
category: 数据库
summary: 一条命令跑完环境体检→结构迁移→种子数据;老库只走「检查 + 增量迁移」,缺表/冗余表/冗余列/类型漂移的收敛写法(0067~0069);RSA 密钥缺失导致登录 401 的处理;超管密码初始化与重置
related: scripts/bootstrap.py, scripts/init_env.py, scripts/init_db.py, scripts/seed_superadmin.py, cogni_agent/common/schema.py, cogni_agent/config.py, cogni_agent/user_center_service/auth.py, tests/test_legacy_db_convergence.py
---

# 初始化与维护

## 一条命令

```bash
# 老库(表已存在):体检 + 补迁移 + 保证超管可登录
uv run python scripts/bootstrap.py --admin-password pftroot123 --no-force-change

# 全新环境:顺带建库、生成缺失密钥
uv run python scripts/bootstrap.py --create-db --fix-env --admin-password pftroot123

# 只体检,不改任何东西
uv run python scripts/bootstrap.py --check
```

`bootstrap.py` 只做编排,三步分别对应 `init_env.py` / `sync_schema()` / 种子数据,每一步都能单独跑:

| 步骤 | 单独执行 | 作用 |
|---|---|---|
| 环境密钥 | `uv run python scripts/init_env.py [--write]` | 体检必填项,生成缺失的 JWT/凭证主密钥/RSA 密钥对/心跳 token |
| 数据库结构 | `uv run python scripts/init_db.py [--check\|--create-db]` | 全新库建基线;老库只按 MIGRATIONS 增量演进,收尾做只读体检 |
| 种子数据 | `uv run python scripts/seed_superadmin.py …` | 权限策略 + 超级管理员账号 |

## 结构演进:老库只「检查 / 增量」

- 全新库(库里没有任何 `numeri_core_` 表):`create_all` 建全量基线,并把整条迁移链记为已应用;
- 老库:**不 create_all、不自动补列**,只顺序执行未应用的 MIGRATIONS;
- 收尾统一做五项只读体检(缺表 / 冗余表 / 缺列 / 冗余列 / 类型漂移),任一不过 `sync_schema` 直接 raise,
  且整个同步在一个事务里,失败全部回滚 —— 不会留下半截结构。
- `init_db.py --check` 与体检同一套判定(2026-09 之前只查「缺列」,缺表/冗余/类型漂移是盲区,
  出现过「--check 说没问题、真跑被拦下」)。

## 0067 ~ 0069:create_all 时代遗留差异的收敛

现场老库(建于 create_all 时代)与当前模型的差异,迁移链里没有对应条目,表现为启动/初始化被体检拦下:

| 迁移 | 处理 | 写法要点 |
|---|---|---|
| `0067_missing_sys_and_app_tables` | 补建 6 张缺表:`sys_group`、`sys_scenics`、`sys_model_providers`、`sys_model_configs`、`app_followed_questions`、`app_message_feedback` | DDL 按模型生成,与 `create_all` 逐列一致;先 providers 后 configs(外键);`IF NOT EXISTS` |
| `0068_drop_legacy_tables_and_columns` | 清理 3 张冗余表(`chat_attachments`、`turn_feedback`、`usr_departments`)与 3 个冗余列(`usr_users.department_id`、`tc_tools.category`、`tc_skills.input_variables`) | **不直接丢数据**:表 `RENAME TO legacy_*_0068`(不带 `numeri_core_` 前缀 → 不再被冗余表体检扫到);列的非空值先 `INSERT INTO legacy_dropped_columns` 再 `DROP COLUMN`;删列前按历史形态 `ADD COLUMN IF NOT EXISTS` 补齐(否则全新库执行到这里会报列不存在,同 0050/0051) |
| `0069_column_type_drift_to_model` | 收敛 5 处类型漂移:`app_config.config`、`app_competitor_requests.extra` jsonb→json;`gov_daily_usage.prompt_tokens/completion_tokens`、`tc_tool_daily_stats.total_duration_ms` bigint→integer | 模型是唯一事实源(同 0041);按天汇总行的取值远在 int4 内(见 `tool/stats.py` 注释) |

确认 `legacy_*` 里的数据不再需要后,由 DBA 手工 `DROP TABLE legacy_…` 即可,平台不再引用它们。
回归测试 `tests/test_legacy_db_convergence.py` 会把现场那台库的形态原样造出来,断言迁移后五项体检清零、
归档数据仍在、补建的表与 `create_all` 逐列一致、迁移可重复执行。

## 登录相关的两个初始化

- **RSA 密钥对**:前端登录前调 `GET /api/admin/auth/public-key` 取公钥加密密码。`RSA_PUBLIC_KEY` /
  `RSA_PRIVATE_KEY`(值是 base64(PEM),单行)没配时该接口抛 `Unauthorized`,表现为登录页一直 401、
  点登录没反应。`scripts/init_env.py --write` 生成并写回服务实际读取的 env 文件(`ZHILV_ENV` 未设置即 `.env`),
  **改完要重启服务**;`--verify` 会按服务自身的 config+auth 链路做一次加解密往返,确认不是「文件里有值但服务没读到」。
  各环境独立密钥对,轮换后前端需刷新页面重新取公钥。
- **超级管理员**:
  ```bash
  uv run python scripts/seed_superadmin.py                                   # 不存在则创建(配置里的初始密码)
  uv run python scripts/seed_superadmin.py --reset-password                  # 重置回配置里的初始密码
  uv run python scripts/seed_superadmin.py --password pftroot123 --no-force-change
  ```
  重置会同时解禁账号、清空 `last_login_at`、更新 `credentials_changed_at`(旧 JWT 立即失效)。
  账号默认 `pftroot`,`scenic_id` 置空(平台级);密码以哈希存储,平台读不出明文,忘记只能重置。

## 常见失败与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| `数据库结构体检失败:… 缺表/冗余表/冗余列/类型漂移` | 改了模型没写迁移,或老库是 create_all 时代建的 | 按清单到 `MIGRATIONS` 末尾追加幂等迁移(参照 0067/0068/0069)后重跑 |
| `GET /api/admin/auth/public-key → 401` | RSA 密钥对未配置 | `scripts/init_env.py --write` 后重启服务;用 `--verify` 确认服务真读到了 |
| `--write` 说已生成,登录页仍 401 | 密钥写进了服务不读的 `.env.{env}` | `ZHILV_ENV` 未设置时服务读 `.env`;不传 `--env` 即与服务同口径,或启动时 `export ZHILV_ENV=<环境名>` |
| `无法启用 pgvector 扩展` | 目标库没装 pgvector | 让 DBA 在该库执行 `CREATE EXTENSION IF NOT EXISTS vector;`(或用 pgvector/pgvector 镜像) |
| 登录报「账号/手机号或密码错误」但确认密码没错 | 库里压根没有账号,或密码哈希被手工改坏 | `scripts/seed_superadmin.py --password <新密码>` 重置 |
| `CREDENTIAL_FERNET_KEY` 变更后凭证解不开 | 凭证库主密钥轮换 | 重新录入工具凭证 / 模型 Key / Quick BI 密钥;主密钥不要随意轮换 |
