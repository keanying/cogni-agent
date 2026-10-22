---
title: Quick BI 集成(工具中心独立模块)
category: 工具中心
summary: 对接 Quick BI 小Q问数/小Q报表/小Q解读/小Q报告;数据源=已有仪表板/上传文件/数据集;场景=固定问题/对话/报告生成;入口=操练台(Quick BI 执行目标)、开放 API /v1/quickbi、quickbi 类型工具
related: cogni_agent/tool_center_service/quickbi/signing.py, cogni_agent/tool_center_service/quickbi/client.py, cogni_agent/tool_center_service/quickbi/events.py, cogni_agent/tool_center_service/quickbi/capabilities.py, cogni_agent/tool_center_service/quickbi/dashboard.py, cogni_agent/tool_center_service/quickbi/filesupport.py, cogni_agent/tool_center_service/quickbi/models.py, cogni_agent/tool_center_service/quickbi/service.py, cogni_agent/tool_center_service/quickbi/tool_type.py, cogni_agent/tool_center_service/quickbi/routes_admin.py, cogni_agent/tool_center_service/quickbi/routes_open.py, cogni_agent/tool_center_service/mcp/protocol.py, cogni_agent/runtime_service/engine/resolver.py, cogni_agent/common/schema.py, config/config.yaml, web/src/pages/Tools/QuickBI/index.tsx, web/src/lib/api.ts
---

# Quick BI 集成

## 一句话

`cogni_agent/tool_center_service/quickbi/` 是工具中心下的独立模块:把 Quick BI 的智能小Q
OpenAPI(与阿里云官方 skill `alibabacloud-quickbi-smartq` 同一套协议)封装成平台能力,
管理台(操练台选 Quick BI 执行目标 / 工具中心 → Quick BI 配置)、开放平台(`/v1/quickbi/*`)、执行引擎(`type=quickbi` 工具)
三个入口共用同一个执行函数 `service.run_stream()`,每次调用落一条 `tc_qbi_runs`。

## 能力(capability)

| capability | 产品 | OpenAPI | 目标数据 |
|---|---|---|---|
| `dataset_qa` | 小Q问数 | 单表:`POST /openapi/v2/smartq/queryByQuestionStream`(SSE);多表:`POST /openapi/v2/smartq/queryByQuestion` + `multipleCubeIds`(官方 SmartqQueryAbility,同步) | `cube_id` 单表;`cube_ids`(≥2 个)= **多表问数**,小Q在所选数据集里选一张或多张表回答(超过 30 个按与问题的名称相关度截断;`cube_mode=pick_one` 保留老的「候选里挑一份」);都为空时 `llmCubeWithThemeList` + `tableSearch` 智能选表(批次 30→10 降级,失败按名称相关性兜底);`cube_name` 精确匹配时跳过选表 |
| `file_qa` | 小Q问数 | `POST /openapi/v2/copilot/parse`(上传得 fileId)+ `queryByQuestionStreamByFile` | 平台文件 `file_id` |
| `dashboard_qa` | 小Q报表 | `skills/dashboard/handle` 预校验 + `dashboard/query` + `dataset/batchGetSchema`,再问数 | 仪表板 `asset_id` / `page_id` / `dashboard`(URL);对齐官方「小Q报表技能」:问题点到某张图表的名字 → 用该图的数据集单表问;否则看板(或 `chart_ids` 所选组件)用到的全部数据集走 `multipleCubeIds` 多表问数。`qa_scope=dataset` 是 v3.56 存量配置的「候选里挑一份」老跑法,原样保留 |
| `insight` | 小Q解读 | `POST /openapi/v2/snapshot/calling/shot`(轮询至 SUCCESS)+ `dataInterpretationStream` | 仪表板(可带 `chart_ids` 限定组件,不传 = 全部):快照只导出当前页签,快照没覆盖到的组件用到的数据集**按用户的问题一次多表取数**(`multipleCubeIds`)后与快照一起解读;数据集(单个或多选,先按问题取数);上传文件(xls/xlsx/csv 本地转 Markdown 表);`data` 文本。超过字符上限先逐级压每张表的行数,仍超才拒绝 |
| `report` | 小Q报告 | `qreport/uploadReferenceFile` + `createQreportChat`(async)+ 轮询 `qreportChatData` | `file_ids`(doc/docx/xls/xlsx/csv);仪表板时把快照 Markdown 的表格拆成 ≤5 个 CSV 作为引用文件;`web_search` 默认 true |

`capability` 为空或 `auto` 时 `capabilities.route_capability()` 路由:问题含报告类关键词 → report;
含解读/趋势/异常且有文件或仪表板 → insight;有文件 → file_qa;只有仪表板 → dashboard_qa;否则 dataset_qa。
`locale` 未指定时按问题句式判断(去掉引号内实体名后含中文 = zh_CN)。

## 协议要点(client.py / signing.py)

- 签名头 `X-Gw-AccessId / X-Gw-Nonce / X-Gw-Timestamp / X-Gw-Signature`:
  `StringToSign = METHOD\nURI[\nk=v&...]\nX-Gw-AccessId:..\nX-Gw-Nonce:..\nX-Gw-Timestamp:..`,
  整串 `quote(safe="")` 后 HMAC-SHA256(api_secret) 再 Base64。GET query 与 urlencoded 表单参与签名,JSON body 与 multipart 不参与。
- 信封 `{success, code, message, data}`:`success=false` 抛 `QuickBIError(kind=business)`,已知错误码
  (`errors.KNOWN_ERROR_CODES`,如 AE0510200000 无权限、AE0579100004 试用到期)翻译为中文。
- HTTP 401/403 → `kind=auth`;5xx/超时/代理拒绝 → `kind=network`;配置缺失 → `kind=config`。
  路由层映射:config/input 400、auth 424、business 422、network 504(`http_helpers.QuickBIHttpError`,业务码 4100)。
- SSE 以空行分帧,`data:` 行 JSON;遇到归一化器的终止事件(问数 `finish`,文件问数 `finish/error/check`)即停止读取。

## 统一事件(events.py)

`run`(首个,含 run_id)→ `status / trace / reasoning / text{section} / sql / code / chart / step / usage / report / error`
→ `done`(最后一个,含 status/answer/report_url/duration_ms)。`chart` 结构:
`{title, chart_type, fields[{name,type,role}], rows[{...}], row_count, sql, conclusion}`;
数据集问数的字段名取自 `param` 事件 DSL(如 `销售额(SUM)`),数值列转为数字。
报告接口每次返回全量累积事件,`summarize_report(since=n)` 只输出第 n 个之后的新事件,
`qreport/qreportUsedToken` 视为完成,出现 `error` 视为失败。

## 连接与凭证(service.py)

- 表 `tc_qbi_connections`:`api_key` 明文(AccessId)、`api_secret_cipher` / `user_token_cipher`
  用凭证库 Fernet(`CREDENTIAL_FERNET_KEY`)加密;接口只返回脱敏值。
- `use_trial=true` 且 api_key/secret 为空时,用 `quickbi.trial_api_key/trial_api_secret`(环境变量
  `QUICKBI_TRIAL_API_KEY/SECRET`)签名。
- 环境变量连接:`quickbi.default_connection.user_token` 非空时出现只读虚拟连接 `env-default`
  (`QUICKBI_*` 环境变量;`cogni_agent/config.py` 额外加载项目根目录 `.env.quickbi`,不覆盖已有变量)。
  仅集团角色、集团级 API Key、执行引擎可用。
- 可见范围:owner / public / group(集团角色)/ scenic(同景区)。编辑删除:创建者或
  super_admin/group_admin/group_developer;group_viewer 只读;景区角色只能设 private/scenic。
- `server_domain` 白名单:默认 `*.aliyun.com`、`*.aliyuncs.com`,私有化域名加到 `quickbi.allowed_domains`;
  只接受「协议 + 域名」,不允许内网 IP。修改已有连接的域名时,未在同一请求里重新填写的 api_secret / user_token 会被清空。
- 默认连接是按人的偏好:普通用户设默认只清自己名下的默认,集团管理员/开发者清全部;
  选择顺序:显式 id > 自己的默认 > 他人共享的默认 > 第一个可见 > `env-default`。
- `POST /admin/quickbi/connections/paste` 解析「一键复制 skill 配置」文本(`key: value` / `key：value` / `key=value`),
  api_key/secret 缺失时自动置 `use_trial=true`。

## 数据资产与文件

- `tc_qbi_assets`:`kind=dashboard` 时 `meta` 存 `dashboard.parse_dashboard()` 结果(图表、维度/度量、
  查询控件、引用数据集、`suggested_questions`);运行时直接用缓存的数据集列表,不重复拉仪表板结构。
  支持 `/dashboard/view/pc.htm?pageId=`、`/token3rd/...?pageId=&accessTicket=`、数据门户
  `/product/view.htm?productId=&menuId=`(菜单为空时取首页下第一个非空叶子;非 report 类型菜单报错)。
- 远端 fileId、仪表板权限都绑定在连接上:资产/文件与本次使用的连接不一致时直接报错(`_same_connection`)。
- `tc_qbi_files`:原文件 bytea(≤5MB,列表查询 defer 掉 content/markdown);上传时 xls/xlsx/csv 立即 `copilot/parse` 缓存 `qa_file_id`、
  本地转 Markdown 缓存 `markdown`(xlsx 依赖 openpyxl,xls 依赖可选 xlrd);报告引用在首次使用时上传并缓存 `report_file`。

## 三个场景

1. **固定问题** `scenic_agent_issue_detail`(business_class=`quick_bi`;0079 起,老表 `tc_qbi_fixed_questions` 已删除):
   问题支持 `{{变量}}`,运行时 `params` 填充,缺参数返回 400。
   问题挂三级标签(主题→维度→指标,取自 `public_problem_dimension_label`),APP 端的问题树按它组织 ——
   **没挂标签的问题前台看不到**,管理端列表会标「未挂标签 · APP 不展示」并可单独筛选(`unlabeled=true`)。
   对外 id 形如 `issue_xxxx`;历史配置里的老表 UUID 仍可用(按 `source_question_id` 命中同一行)。
   `run_fixed()` 以 `mode=fixed` 执行并回写 `last_run_*`;`batch_run()` 串行执行(报告类只创建不等待,最多 50 条);
   `adopt_suggestions()` 把仪表板候选问题批量存为固定问题。
2. **对话** `tc_qbi_sessions`:目标数据存会话上,每轮可追加;capability=auto 时每轮按意图路由。
   首轮智能选表选中的 `cube_id` 回写会话(`sticky_cube` 默认开),后续不再选表。
   问数类追问(≤24 字,以「那/再/换成/按/只看/同比/环比…」开头或以「呢/吗」结尾)自动拼接上一轮
   成功问题:`上一轮问题;追问:本轮问题`(`carry_context=false` 关闭)。
3. **报告** `start_report()`:创建任务后立即返回(`status=running`,记 `chat_id`);
   `run_stream(report_async=...)` 在路由结果为报告时决定是否等待:管理端/开放平台的非流式调用、批量执行、
   `report_wait=false` 的工具都传 `True`(创建即返回),避免自动路由落到报告时阻塞;流式调用才等待完成。
   跟踪超时(`REPORT_TIMEOUT`)且已有 chat_id 时保持 `running`;
   `refresh_report()` 拉一次进度并在完成时落库(无状态,多实例安全);`stream_report()` SSE 跟踪到结束。
   同一用户已有运行中的报告时,Quick BI 返回提示文本,平台解析出旧 chatId 继续跟踪。

## 运行记录收尾

`run_stream()` 的收尾(`finalize`)放在 `finally` 中并 `asyncio.shield`:SSE 客户端断开(GeneratorExit)或任务被取消
(CancelledError)时,记录置为 `failed / error_code=cancelled`;报告已拿到 chat_id 的保持 `running`。
问数/解读流在没有终止事件、且没有任何内容事件时判为协议错误(`capabilities._guarded`)。
对话的轮次计数与粘性数据集回写同样在 `finally` 中完成。

`done` 事件带 `content`(回答 + 图表转 Markdown,`ResultAccumulator.as_llm_text()`):纯图表类回答
`answer` 为空,给模型/外部调用方用 `content`。

**运行记录保留**(`cronjobs/qbi_runs_retention.py`,心跳注册名 `qbi_runs_retention`):每天北京时间过了
凌晨 1 点后删一次 `quickbi.run_retention_days`(默认 7,0 = 不清理)天前的行,分批小事务。
**不能小于 7 天**(小于按 7 算):这张表同时是加速缓存,存储时间上限 7 天。

## 入口

- 管理台 `/api/admin/quickbi/*`(routes_admin.py):overview、connections(含 paste/check/cubes/default)、
  dashboards/parse、assets、files、run、fixed-questions(含 run/batch-run)、sessions、reports、runs、tools。
  执行类接口 `stream=true` 返回 SSE,`false` 返回运行记录;执行前先取首事件,入参/权限错误以 HTTP 错误返回。
- 开放平台 `/v1/quickbi/*`(routes_open.py):`Authorization: Bearer sk-zhilv-*`,`validate_api_key()` 真实校验;
  可用连接 = `api_enabled` 且非 private;集团级 Key(scenic_id 为空)可用全部,景区 Key 仅本景区或 public;
  限定了 `agent_allowed_ids` 的 Key 一律拒绝(`AgentNotAllowed`);运行记录/会话按 `api_key_id` 隔离。
  可选 `X-End-User` 头写入 user_id。
- 执行引擎:工具类型 `quickbi`(tool_type.py)。`type_config = {connection_id, capability, target,
  fixed_question_id, report_wait, max_rows}`;resolver 用 `input_schema(cfg)` 生成模型可见参数
  (`question` 必填;目标未固定时开放 `cube_name/dashboard/file_id`;绑定固定问题时只开放 `params`),
  `allow_override=False`;创建/编辑(`tool_center_service/routes.py`)时按编辑者身份校验配置引用
  (`tool_type.validate_config` → `service.validate_tool_refs`);执行时再按**工具所有者**(用户表当前角色/景区)
  校验一次,通过的连接/资产/文件/固定问题 id 写入 `Actor.trusted_refs`,模型运行时传入的 `file_id` 等仍按调用者校验;
  配置留空的连接固定为所有者视角的默认连接。`mcp/protocol.invoke_tool()` 对 `quickbi` 分支调用 `tool_type.invoke()`
  (不走沙箱),以 `source=agent, mode=tool` 落运行记录,返回 `content`(回答 + 每表前 max_rows 行 Markdown)。
  `POST /admin/quickbi/tools` 复用 `create_tool()` 生成并发布 v1。

## 配置(config/config.yaml → quickbi)

| 键 | 默认 | 说明 |
|---|---|---|
| default_connection.* | 环境变量 | server_domain / api_key / api_secret / user_token / locale / api_enabled |
| trial_api_key / trial_api_secret | 环境变量 | 试用凭证 |
| http_timeout_seconds | 60 | 普通接口超时 |
| stream_timeout_seconds | 600 | SSE 超时 |
| snapshot_poll_seconds | 3 | 仪表板快照轮询间隔(最多 60 次) |
| report_poll_seconds | 10 | 报告轮询间隔 |
| report_max_wait_seconds | 1800 | 报告跟踪最长等待 |
| allowed_domains | [] | server_domain 白名单追加项(默认已含阿里云域名) |
| verify_ssl | true | TLS 校验 |

## 数据表(迁移 0066_quickbi_integration)

`tc_qbi_connections`、`tc_qbi_assets`、`tc_qbi_files`、`tc_qbi_sessions`、`tc_qbi_runs`、`tc_qbi_grants`(0075 连接分配)。
固定问题不在这里 —— 它已并入统一问题表 `scenic_agent_issue_detail`(0079),与 APP 问题同表同结构,
前台读的就是管理端写的那一行(并表前两边各存一份,是「后台配了、APP 看不到」的主要来源)。
`tc_qbi_runs.result` 为结构化结果快照,每张图表最多保留 1000 行(`truncated=true`),reasoning/code 截断到 5 万字。

## 前端

`web/src/pages/Tools/QuickBI/`:操练台(单次问答 / 对话 / 报告生成)、固定问题、数据资产、连接配置、
运行记录、API 接入(含 API Key 管理)。流式读取用 `api.qbiStream()`(fetch + ReadableStream),
事件经 `ResultView.reduceQbi()` 累积渲染;图表用 recharts(bar/line/pie/ranking),可切换为表格。

## 测试

`tests/quickbi/mock_quickbi_server.py` 是按官方协议实现的模拟服务(独立实现的签名校验);
`test_quickbi_core.py` 覆盖签名/SSE/归一化/各能力编排,`test_quickbi_api.py` 覆盖接口、权限、开放平台、
工具发布并经 resolver + invoke_tool 执行。`client.default_transport_factory` 为测试钩子,生产保持 None。

## 景区维度(scenic_id)

资产登记时绑景区(`POST /assets/dashboards` 的 `scenic_id`,留空=集团级,所有景区都能用);
提问时传 `scenic_id` 用于计量/权限/筛选,**不指定目标数据就不补** —— 由能力层报「缺目标数据」
(`dataset_qa` 例外:它本来就是让 Quick BI 自己智能选表,不指定才是对的)。

**没有「按景区自动挑一个已登记资产」的兜底**(对齐 1.2.4 时去掉)。它会盖掉用户**已经选定的目标数据**:
判定清单只认单数 `cube_id`,多选存的 `cube_ids` 认不出来(被判成没选),兜底于是挂上一块景区仪表板,
跑出来的是那块看板的快照 —— 数值看着正常,实际不是用户选的数据。
同一原因,解读时 target 里同时有残留的看板 `asset_id` 和 `cube_ids`,以数据集为准(先取数再解读);
数据集资产也不再把 `cube_id` 并进已有的 `cube_ids`。

连接同理:问题上绑的连接失效后按景区重挑(本景区 → 集团级,同档默认连接优先),
**挑不到返回空并报「去哪配」,不拿「当前账号看得见的第一条」凑数** —— 集团账号看得见所有景区的连接,
凑到的多半是别的景区的那条,用别人的 user_token 问出来的数照样像模像样。

问句与口径里的 `{{截止时间}}` / `{{统计日期}}` / `{{昨日}}` 由 `run_stream` 按本次运行时刻(东八区)替换,
不算「缺参数」;管理台、APP、预热、开放平台同一处替换,口径不会分叉。

越界防护:景区角色只能绑/问自己的景区(`_bind_scenic`),否则 403。
一个例外是公开的固定问题可能挂在别的景区名下,这时它只是问题模板,景区跟着提问的人走。

## 按固定问题发起

`POST /run`(管理台)与 `POST /v1/quickbi/query`(开放平台)都接受 `fixed_question_id`:
只给 id 即可,问题文本 / 能力 / 连接 / 目标数据从那条固定问题带出来,
请求里显式传的字段优先(例如同一条问题换个景区问)。

## 思考过程与正式数据分离

每个统一事件出厂时带 `channel`:

- `data` —— 结论正文(section ∈ answer/report/conclusion/summary)、图表、报告入口、错误;
- `thinking` —— 推理、阶段提示、SQL、分析代码、执行步骤、trace、token 用量。

非流式结果 `result` 里同样分成 `data` / `thinking` 两块(顶层扁平字段保留给既有调用方)。
前端据此分区渲染:过程进「执行过程」卡,数据进回答气泡 —— 推理文字再长也不会把结论顶出视野。

## 数据域与请求类别

固定问题上有两个分类维度,给**外部调用方**用:

- `domain` 数据域:票务 / 财务 / 舆情 / 客群 / 核销 / 营销 / 流量 / 画像 / OTA。
  预置这 8 个只是选择器里的候选,**不是白名单** —— 现场可以直接填新的值(自由文本 + 索引),
  业务分类总会长出新的一类,加一类不该改表也不该改代码。
- `ask_type` 请求类别:`qa` 问数 / `dashboard` 报表 / `insight` 解读 / `report` 报告,
  与 Quick BI 的四个产品叫法一一对应。比 capability 粗一档 —— capability 还要区分
  数据集问数与上传文件问数,而调用方不关心那层。
  「问数」与「报表」分开是刻意的:前者是随便问一个指标,后者是针对某块已做好的看板提问,
  合成一类会让按类别取问题时混在一起(迁移 0072 把 0071 自动推成 qa 的仪表板问题改回 dashboard,
  人工改过类别的行不动)。不显式指定时按 capability 推导。

## 目录接口(对外集成的第一步)

`GET /v1/quickbi/catalog?scenic_id=&ask_type=&domain=`(管理端同名接口用于预览)返回:
该景区可用的固定问题(含数据域与类别)+ 全部数据集/仪表板,**每条都带可直接发起提问的 id**
(仪表板 `page_id`、数据集 `cube_id`,都附 `asset_id`)。拿到后两种发起方式:

1. `POST /v1/quickbi/query {"fixed_question_id": "..."}` —— 原样问这条预置问题;
2. `POST /v1/quickbi/query {"question": "...", "target": {"page_id": "..."}}` —— 自己组问题打在同一份数据上。

这样集成方不必硬编码 pageId/cubeId —— 后台换看板时目录会跟着变,硬编码则会悄悄失效。

## 上游自己标了「这是模型在想」(首选判据)

2026-09-18 拿到现场导出后才看清:小Q解读的流里,**推理和回答用的是同一个 type**
(`summary` / `text`),靠 `subType` 区分:

```json
{"type": "summary", "subType": "model_reasoning", "data": "用户要求解读"}
{"type": "text",                                  "data": "【用户问题解答】\n"}
```

以前没读 `subType`,于是整条思维链被当成回答灌进气泡 —— 这就是「思考过程和结果粘在一起」
的真正根因。`events.is_reasoning_event()` 现在认 `model_reasoning` / `reasoning` /
`thinking` / `cot` 等写法(含 `*_reasoning` 后缀),数据集问数、文件问数、解读三条链路都接上了。
小Q报告本来就把非 mainText/check 的内容归到 reasoning,不受影响。

**有标记时一切都不用猜**;下面的文本切分器只是没有标记时的兜底。

## 思考过程与正式回答的文本级切分(兜底)

事件级的 `channel` 解决不了一种情况:小Q(尤其解读/报告)会把整条思维链和最终回答
放在**同一个 text 事件流**里 ——
「思考过程: 1. 第一步…… 完美,开始输出。### 用户问题解答 ……」。
`events.AnswerSplitter` 在文本层再切一刀:开头命中思维链特征就进入 thinking 态,
直到遇到分界标志(`开始输出`/`最终输出`/`### 用户问题解答`/`查询的逻辑SQL如下` 等)才切回 answer。

开头特征分强弱两类,决定「没等到分界标志时怎么收尾」:

- **弱**(`思考过程`/`<think>`/`推理过程`…):没等到分界就按回答原样吐出 —— 拿不准时不吞内容;
- **强**(数据集问数的 NL2SQL 前言:`问题选中数据集`/`提及字段及维值`/`判定N:`):
  即便没等到分界也按推理收尾。这类开头特征很硬(普通回答不会以「判定0」起头),
  而整段都是「怎么把这句话翻成 SQL」的过程说明,放进回答气泡就是噪音 ——
  真正的结论在图表和结论段里。
流式友好:标志被切碎在多个 delta 里也能识别(尾部保留 24 字符待定),
整段没等到分界时按回答原样吐出 —— 宁可少切一刀,也不能把内容吞掉。

## 开发者文档

`/developer` 是**免登录**的对外 API 文档(静态页,`cogni_agent/static/developer/index.html`),
覆盖鉴权、LLM 调用、Quick BI 全部对外接口与示例。对接方通常没有后台账号,
把文档藏在登录后的菜单里等于没有文档。

## 报告后台续跑(报告不靠客户端收结果)

报告要几分钟到几十分钟,发起它的那条 HTTP 连接活不了那么久:页面刷新、切走、网络抖一下,
跟踪就断了。断了之后运行记录停在 running —— 远端其实早生成完了,库里却永远「生成中」。

所以收结果不挂在客户端身上:`quickbi/report_watcher.py` 是服务端常驻轮询,
随应用启动(`main.lifespan`),每 30 秒扫一遍还没有结论的报告并拉取进度,直到成功/失败。
`cronjobs` 里另注册了一个 5 分钟的兜底扫描,防的是进程内循环被取消却没人发现。
两者都走同一个幂等的 `refresh_report`,多副本同时跑也不会写坏数据。

**什么算失败,什么只是「这次没拉到」** —— 这是这块最要紧的判断:

- 超时 / 连不上 / 返回结构一时对不上(`kind=network|protocol`)→ **不判死**,下一轮接着拉。
  现场症状是 `请求 Quick BI 超时: GET /openapi/v2/smartq/qreportChatData`:
  这个接口返回的是报告到目前为止的**全量事件**,报告越长响应越大,通用 60 秒超时不够,
  于是 `fetch_report_raw` 单独放宽到 180 秒并自带 3 次退避重试。
- Quick BI 明确报错(额度用尽、无权限、配置失效等 `kind=business|auth|config`)→ 收尾为 failed,
  一直重试没有意义。
- 超过 `report_give_up_hours`(默认 24 小时)仍无结论 → 标记失败并写明原因与 chatId,
  永远 running 的记录比失败更难排查。

同理,`run_stream` 里报告只要已经拿到 chat_id,跟踪这一程失败也不再把记录判死,
只发一条 `report_tracking_interrupted` 状态事件,告诉用户「仍在后台获取」。

## 操练台里的 Quick BI 会话

Quick BI 的每一问都落在 `tc_qbi_sessions` 的一个会话里,操练台左侧列表把它与任务引擎的
`conversations` 合并展示(按更新时间排序,条目上标明类型)。两张表不合并的原因是它们
存的东西本就不同(一个挂 LangGraph thread,一个粘目标数据与选表结果),
但对用户来说都是「我之前问过的那段对话」,分两个列表放只会让人来回找。

- 自由提问 → `POST /sessions/{id}/messages`(目标数据与选表结果粘在会话上,支持追问);
- 固定问题 → `POST /run` 带 `session_id`,这一轮同样归到当前会话名下;
- 打开历史会话 → `GET /sessions/{id}` 回放各轮问答;其中还没跑完的报告会挂上前端轮询,
  把后台已经收到的结果刷出来。

## 能力名就是四个字

`CAPABILITIES[*].label` 统一成 **问数 / 报表 / 解读 / 报告**,不带「小Q」前缀,也不在名字里
写数据来源。用户要知道的是「这次干了什么」;走数据集还是上传文件,目标数据那一栏已经写着了,
名字里再说一遍就是噪音。来源放在 `source` 字段(徽标旁边淡色显示),上游产品名保留在
`product` 字段 —— 对账时还要能跟 Quick BI 的叫法对上。

能力**选择器**是例外:同为「问数」的两项要能分清打在哪儿,所以那里是「问数 · 数据集」
「问数 · 上传文件」「报表 · 仪表板」。

## 执行留痕与一键导出

排障时最缺的不是我们的日志,而是**上游到底发了什么**。归一化之后的事件已经掺进了我们的
理解 —— 理解错了,光看它是看不出来的(「思考过程混在回答里」就是这么发现的:
归一化后只是一串 text,看上游原文才知道那是一段 NL2SQL 前言)。

所以每次运行都留一份 `tc_qbi_runs.debug`(迁移 0073):

- `http` —— **非流式接口的原始一问一答**(方法/URI/状态码/请求参数/响应原文)。
  选表、仪表板结构、快照、报告进度都走这条,问题常出在这一步;只留 SSE 的话,
  这些接口到底返回了什么无从查起;
- `raw` —— 上游原样 SSE 事件,带是哪个接口发的;
- `normalized` —— 我们归一化之后的事件,带 channel 标记(对照「我们怎么理解的」);
- **连续增量会合并**:流式是逐字吐的,一句话能拆成上百条事件。同 type/subType 的
  连续增量并成一条并标上 `_merged_deltas`,信息一点不少 —— 否则一次导出里
  400 条名额全被「用户」「要求解读」「提供的」这种碎片占满,既撑爆限额也没人读得下去;
- 三条纪律:**脱敏**(三道:键名命中敏感词、文本里 `token=xxx` 这类裸露写法、
  **已知凭证的字面值** —— 第三道不能省,Quick BI 把 user_token 放在 `userId` /
  `oapiUserId` 这种看不出是凭证的字段里,只靠键名会漏)、
  **限长**(单事件 2000 字、每路 400 条、整包 256KB,超了只记省略条数)、
  **坏了就不留**(旁路异常绝不影响这次调用)。

导出:`GET /api/admin/quickbi/runs/{id}/debug`(JSON)或 `?format=md`(可直接发给人读的
Markdown:元信息 → 正式数据 → 思考过程 → 归一化事件 → 上游原文)。
前端在操练台右侧与运行记录详情各有一个「导出执行详情」按钮,存成 .md 文件。
开关 `quickbi.capture_debug_trace`,默认开 —— 等出了问题再去开它,现场早就过去了。
