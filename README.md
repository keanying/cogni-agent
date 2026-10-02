# cogni_agent（数字大脑智能体系统）

模块化单体智能体平台：为景区/集团提供 Agent 化的任务编排、工具调用、知识问答与审批协同能力。后端 Python（FastAPI + LangGraph + PostgreSQL，包名 `cogni_agent`），前端 React + Vite + TypeScript（`web/`），包管理统一使用 uv，配置集中在 `config/`。一个包一键启动，扩容靠多起几台实例，模块边界预埋了未来拆分为微服务的路径。

## 功能特性

- **执行引擎**：L0（单次直调）/ L1（固定编排链）/ L2（单 Skill ReAct 或多 Skill 动态池）三种模式共用同一张 LangGraph 图；支持固定 DAG 编排、输入/输出双侧内容审核、LLM 超时重试、上下文窗口裁剪、工具结果截断、多工具并发执行、目标漂移自检。
- **人机协同（HITL）**：`risk_level=high` 的工具执行前暂停整张图，审批通过/驳回后从 Postgres 检查点精确恢复执行，支持跨进程恢复。
- **工具中心**：Tool / MCP / Skill / Agent 全生命周期管理（含版本化），工具沙箱隔离执行（HTTP / 数据库只读查询 / Python 子进程资源限额），凭证与模型 API Key 加密存储。
- **Quick BI 智能小Q**（工具中心独立模块）：对接小Q问数 / 小Q报表 / 小Q解读 / 小Q报告，数据源支持已有仪表板、上传数据、数据集；覆盖固定问题、对话、报告生成三个场景；管理台操练台验证、开放 API `/v1/quickbi/*`、`quickbi` 类型工具供 Agent 调用。详见下文「Quick BI 智能小Q」。
- **知识中心**：数据库知识（数据源管理 + 真实表内省）、网络知识（多搜索引擎 + 域名白名单）、向量知识（文本切分嵌入入库 + 余弦检索）。
- **评测与门禁**：评测用例真实通过执行引擎执行并判定，Agent 发布前设置最低通过率门禁。
- **治理与审计**：事件总线（Outbox）+ 审计流水 + LLM 计费 + 预算护栏（超限熔断/告警）+ 统一审批记录。
- **多租户与权限**：集团/景区两级组织模型，资源四级可见范围（private → scenic → group → public），任务/审计/成本按 `scenic_id` 强隔离。
- **对外能力**：管理后台 API、APP 端 API（`/api/app/*`）、开放平台协议（`/v1/*`，兼容 OpenAI SDK base_url 约定）、真实 SSE 流式输出。
- **前端管理后台**：工作台 / 操练台（三栏布局，真实 SSE 流式 + 执行时间线 + Token 用量） / 工具中心 / 知识中心 / 审计中心 / 系统中心，全部对接真实接口。

## 目录结构

```text
cogni_agent/
├── pyproject.toml          # uv 依赖声明（PEP 621 标准格式），运行 `uv sync` 生成 uv.lock
├── config/config.yaml       # 唯一固定配置文件，密钥类用 ${ENV_VAR} 占位
├── .env.example              # 复制为 .env，填入数据库密码/JWT密钥/模型Key/凭证加密Key等真实值
├── docker-compose.yml         # 一键起 PostgreSQL(pgvector) + Redis + MinIO
├── tests/                       # pytest：执行引擎行为的无外部依赖单元测试
├── scripts/
│   ├── restart_local.sh         # 本地环境快速重启
│   ├── init_db.py              # 建表脚本（自动启用 pgvector 扩展，开发用）
│   └── manual_e2e_check.py       # 手动集成校验：真实 Postgres/Redis + 假模型服务器，
│                                   跑通"建任务→真实工具调用→真实SSE流式→HITL中断→
│                                   审批通过→真实Postgres检查点跨进程恢复"全链路
├── cogni_agent/                 # ── 后端 7 模块 ──
│   ├── main.py                    # 应用入口：FastAPI + 后台 worker 协程 + 挂载 web/dist
│   ├── config.py                   # 配置加载器
│   ├── common/                      # DB引擎、日志、异常、中间件
│   ├── gateway_service/              # ① 接入层：路由、鉴权、SSE(/chat、/v1/*、/tasks)
│   ├── user_center_service/           # ② 用户中心：登录、短信验证、用户CRUD
│   ├── control_plane_service/          # ③ 任务生命周期：队列、租约、状态机
│   ├── runtime_service/                 # ④ 执行引擎：engine(解析/判定/护栏/HITL/图/
│   │                                       Checkpointer)+ llm(模型路由+真实流式)+ streaming.py(SSE)
│   ├── tool_center_service/              # ⑤ 工具中心：tool/skill/mcp/sandbox/agent + routes.py
│   ├── knowledge_service/                 # ⑥ 知识中心：database/websearch/multi2vec_clip
│   │                                        三类知识源 + embeddings.py + routes.py
│   ├── governance_event_bus_service/       # ⑦ 治理及事件总线：events(Outbox)/audit/cost/
│   │                                          evals/policy/approvals/settings + routes.py
│   └── worker/loops.py                      # 后台协程：任务认领·事件派发·租约巡检
└── web/                          # ── 前端 ──
    ├── package.json / vite.config.ts
    └── src/
        ├── main.tsx / App.tsx      # 入口 + 应用外壳（登录态切换 + 侧边栏路由）
        ├── theme.ts                 # 设计令牌
        ├── ui.tsx                    # 通用组件(Card/Btn/Modal/Table/Toast...)
        ├── Sidebar.tsx                # 侧边栏导航（工作台/操练台/工具中心.../系统中心）
        ├── lib/api.ts                  # 统一后端 API 客户端（真实接口 + 手写 SSE 解析）
        └── pages/
            ├── Login.tsx / Dashboard.tsx / Console.tsx(操练台,核心页面)
            ├── ToolCenter.tsx        # Tool / MCP / Skill / Agent
            ├── KnowledgeCenter.tsx    # 数据库知识 / 网络知识 / 向量知识
            ├── AuditCenter.tsx         # 任务中心 / 审批中心 / 评测中心
            └── SystemCenter.tsx         # 系统日志 / 系统设置
```

## 技术栈 / 架构

| 层 | 技术 |
|---|---|
| 后端框架 | Python 3 · FastAPI · SQLAlchemy 2.0 · LangGraph · loguru |
| 数据与中间件 | PostgreSQL（pgvector） · Redis · MinIO · 启动自动建表/迁移/补列（common/schema.py） |
| 模型接入 | LiteLLM 统一路由（OpenAI 协议 / 自建 OpenAI 兼容服务 / 多厂商），模型清单运行时热更新 |
| 前端 | React · Vite · TypeScript · Tailwind CSS · zustand · framer-motion · SSE |
| 包管理与构建 | uv（依赖 + 虚拟环境） · Dockerfile 多阶段构建（前端产物由后端挂载） |
| 部署 | docker-compose（本地基础设施） · 多环境配置（local / dev / prod，经 `${ENV_VAR}` 注入） |
| 架构形态 | 模块化单体：7 大 service 模块 + 网关接入层 + 后台 worker 协程；事件总线（Outbox）解耦模块间通信；任务队列（`SKIP LOCKED`）承载分布式租约；模块边界已预埋微服务拆分路径 |


## 初始化与维护(新环境 / 老库都用这一套)

```bash
# 0) 依赖
uv sync

# 1) 老库(表已经存在):体检 + 只补增量迁移 + 保证超管可登录
uv run python scripts/bootstrap.py --admin-password pftroot123 --no-force-change

# 1') 全新环境:顺带建库 + 生成缺失密钥(JWT / 凭证主密钥 / 登录用 RSA 密钥对)
uv run python scripts/bootstrap.py --create-db --fix-env --admin-password pftroot123

# 2) 随时体检(只读,不改任何东西)
uv run python scripts/bootstrap.py --check

# 3) 启动
uv run uvicorn cogni_agent.main:app --reload
```

单步执行(bootstrap 只是把它们串起来):

| 目的 | 命令 |
|---|---|
| 环境密钥体检 / 生成 | `uv run python scripts/init_env.py [--write] [--env dev]` |
| 结构检查(五项:缺表/冗余表/缺列/冗余列/类型漂移) | `uv run python scripts/init_db.py --check` |
| 结构迁移(老库只增量,全新库建基线) | `uv run python scripts/init_db.py [--create-db] [--with-seed]` |
| 超管创建 / 重置密码 | `uv run python scripts/seed_superadmin.py --password pftroot123 --no-force-change` |
| 全新库迁移链门禁(CI 用) | `uv run python scripts/check_migration_chain_fresh_db.py` |

结构演进铁律不变:**模型 = 唯一事实源,任何结构改动必须有迁移文件**;老库不会自动补列,
体检不过就拒绝启动(清单会直接打出来,照着在 `cogni_agent/common/schema.py` 的 MIGRATIONS 末尾补幂等迁移)。
登录页一直 401 / 点登录没反应,通常是 `RSA_PUBLIC_KEY`、`RSA_PRIVATE_KEY` 没配 —— `init_env.py --write` 生成后**重启服务**即可。
写入的文件必须是服务真正读的那个:`ZHILV_ENV` 没设置时服务读 `.env`,设了才读 `.env.{ZHILV_ENV}`;`init_env.py` 默认跟随同一口径,并会打印「服务读取」一行,不一致时告警。
生成完可用 `uv run python scripts/init_env.py --verify` 走服务自身链路验证(公钥下发 + 加解密往返),验证通过登录页就不会再 401。
详见系统功能说明「框架 / 数据库 / 初始化与维护」。

## Quick BI 智能小Q

工具中心下的独立模块(`cogni_agent/tool_center_service/quickbi/`,页面:工具中心 → Quick BI),
协议与阿里云官方 skill `alibabacloud-quickbi-smartq` 一致。完整说明见系统功能说明
`业务/QuickBI智能小Q/quickbi`。

**1. 配置连接**(三选一)

- 页面:连接配置 →「粘贴 skill 配置」(Quick BI 控制台右上角头像 →「一键复制 skill 配置」);
- 环境变量 / 项目根目录 `.env.quickbi`(已被 `.gitignore` 排除):
  `QUICKBI_SERVER_DOMAIN` `QUICKBI_API_KEY` `QUICKBI_API_SECRET` `QUICKBI_USER_TOKEN`
  → 出现只读连接 `env-default`;
- 脚本入库(加密保存、设为默认、自检、登记仪表板并生成固定问题):

```bash
uv run python scripts/seed_quickbi.py --from-env --check --adopt \
  --dashboard "https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=...&accessTicket=..."
```

只有 user_token、没有 api_key/api_secret 时,需要配置试用凭证 `QUICKBI_TRIAL_API_KEY / QUICKBI_TRIAL_API_SECRET`。
服务器出网需放行 `bi.aliyun.com`(或私有化部署的 server_domain)。xlsx 解读依赖 `openpyxl`(已加入依赖,需 `uv lock`)。

**2. 验证**:工具中心 → Quick BI → 操练台(单次问答 / 对话 / 报告生成)。

**3. 对外提供**

| 场景 | 开放 API(`Authorization: Bearer sk-zhilv-*`) |
|---|---|
| 固定问题 | `GET /v1/quickbi/fixed-questions` · `POST /v1/quickbi/fixed-questions/{id}/run` |
| 对话 | `POST /v1/quickbi/sessions` · `POST /v1/quickbi/sessions/{id}/messages`(`stream`) |
| 报告 | `POST /v1/quickbi/reports` · `GET /v1/quickbi/reports/{id}` · `GET .../stream` |
| 一次性提问 | `POST /v1/quickbi/query` |
| 上传 / 元数据 | `POST /v1/quickbi/files` · `GET /v1/quickbi/datasets` · `GET /v1/quickbi/assets` |

Agent 侧:页面「生成工具」创建 `type=quickbi` 工具,绑定到 Skill 即可。

**4. 测试**:`uv run pytest tests/quickbi -q`(模拟 Quick BI 服务,不访问外网)。
