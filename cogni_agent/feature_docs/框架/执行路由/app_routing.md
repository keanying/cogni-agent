---
title: APP 端两条执行路线(Agent 动态与固定问题直跳)
category: 执行路由
summary: app_chat 的分流全貌——固定问题路线(命中判定/预热直出/编排直出/L1 技能直跳)与 Agent 动态路线(Triager 意图路由/L0-L2 模式判定),含入口参数(pre_params)、上下文策略(固定问题 thread 隔离+纯净上下文=工作流语义;非固定按轮对话文本 exec_history_rounds 默认 2)、技能级 strict_tool_execution 协议强制取数、槽位优先级、版本解析档位、建图优先级、工具参数两机制与注入值类型强制转换
related: cogni_agent/gateway_service/routes_app.py, cogni_agent/gateway_service/question_bank.py, cogni_agent/gateway_service/pre_params/app_pre_params.py, cogni_agent/runtime_service/engine/executor.py, cogni_agent/runtime_service/engine/intent.py, cogni_agent/runtime_service/engine/resolver.py, cogni_agent/runtime_service/engine/modes.py, cogni_agent/runtime_service/engine/graphs.py, cogni_agent/runtime_service/engine/tools_adapter.py
---

# APP 端两条执行路线(Agent 动态与固定问题直跳)

## 定位

`POST /api/app/chat` 是 APP 端唯一任务入口,按「固定问题命中」分流成两条
路线——**固定问题直跳**(确定性:L1 技能直达,参数模板+预热缓存,零路由
开销)与 **Agent 动态**(自主性:Triager 意图路由 + L2 ReAct)。两条路线
共享同一套执行引擎(executor)与工具机制,分叉只发生在「谁决定执行目标」。

## 总分流

```
POST /api/app/chat
  ├─ 会话:get_or_create(executor=agent, 会话景区=用户绑定景区,续聊沿用)
  ├─ assert_no_active_turn(单轮互斥)
  ├─ pre_params(请求结构层):JWT身份 → scenic_id/user_id/role +
  │   req.date(问数日期,可缺——缺时日期主力来自问题行 param_template
  │   展开;见上下文参数文档「日期三层优先级」)/req.question_id
  │   (固定问题入口)
  │
  ├─ 固定问题命中判定 match_question ──────┐
  │   question_id = 唯一契约(卡片/选择器/关注问题必带;校验 active+景区,  │
  │   脏 id 即未命中,不降级文本匹配——防静默改道)                    │
  │                                        │
  │   命中且 skill_id 非空 ──→ 固定问题路线 │
  │   命中但 skill_id 空 ────→ 不干预,仅记命中 → Agent 路线
  │   未命中(含手打自由文本)→ Agent 路线
```

端码(source)由路由端点决定(服务端常量,客户端无此参数)——详见上下文
参数文档「端匹配」。

## 路线一:固定问题(确定性优先)

实时执行有两种形态:**技能绑了工具**走编排直出(并行取数 + 一次成文,本节 3),未绑工具
或其余情形走 L1 技能直跳 ReAct(本节 2)。

### 1. 预热直出(0 LLM,最快路径)

命中 `prewarm_enabled` 问题 → 查当日缓存行
`(question_id, scenic_id, ota_account=当前用户账号, skill_version=指派版本,
biz_date=今天)`:

- **命中**(answer 非空)→ 直接返回预热结果 + 补一条终态 COMPLETED task
  (历史会话以 task 为轮次数据源,不补则该轮在回放里消失);
  固定问题带 OTA 维度且当前用户未绑账号 → 直出「未配置账号」说明
  (no_data=true,不进 LLM 零 token);
- **未命中**(占位/无行)→ 空态回落实时执行,**绝不回退昨日缓存**。

数据供给 = 预热体系(派单/收割/摘要,见 `业务/APP数据预热/prewarm.md`)。

### 2. L1 技能直跳(实时执行)

- `executor_type=skill, executor_id=question.skill_id`,版本 = 用户对该问题
  锚 agent 的**指派快照 pin**(见版本解析档位节;`question_authorized`
  授权穿透——否则 APP 景区用户对集团工具失权,技能降级无工具);
- **参数模板**(param_template)装配期全量展开:基准日期与对比窗口
  (`{{today}}`/`{{yesterday_start}}` 等)都由代码生成——派单前所有参数
  已生成完整,无任何运行期默认补全,缺 = 由 L2 required_param_gaps 拦截暴露;
- `question_params` 非空 → **跳过 Triager**(路由已定、参数已有,这一次
  LLM 调用纯属浪费);**合成 intent 事件**(`intent=fixed_question_hit`,
  detail 带 question_id/template_slots)供前端「意图识别」节点展示——
  不补发的话该节点永远收不到事件,表现为「卡在意图识别」:
  - **必须显式 `_publish` 推 SSE**:只把事件塞进 `graph_input["timeline"]`
    的话仅随 checkpoint 落库(管理后台链路可视化/会话信息可见),**实时流
    里没有**,实时执行链照样空白;
  - **审批恢复(is_resume)路径同样补发**:恢复后前端是新的 SSE 连接,
    不补发同样空白;
  - 分支写法铁律:跳过 Triager 的 `else` 必须与「是否走 Triager」的 `if`
    同层级。曾误写成外层 `if not is_resume` 的层级,语义变成「恢复执行才
    跳过」——固定问题既不走 Triager 也不补发事件(任务 4dfda734 即此症状);
  - 跳过 Triager 的两种情形(模板已装配 / 未启用意图识别)共用同一槽位
    合并口径:会话历史 < 模板 < APP date。
- **新问题上线的检查项**:param_template 必须把该问题需要的日期/对比窗口
  参数 key 配全(key 缺失 = 配置错误,报错暴露;与版本锚 agent_id 同为
  上线门禁)。

### 3. 编排直出(工具全集并行 + 一次成文)

技能绑了工具的固定问题走这条路径,是**预热未命中时实时执行的默认形态**。

触发由网关与 executor 各判一半,共用同一份版本快照:

| 环节 | 判定 |
|---|---|
| 网关 `routes_app.py` | 固定问题命中 → 取到该技能的**指派 pin 版本**快照 → 快照 `tools` 非空 → 置 `payload["direct_dispatch"]` |
| executor | 该标记成立 且 `direct_tool is None` 且 `skill_chain[0].tools` 非空 → 建编排图 |

网关只在版本快照的原始 `tools` 列表上做存在性判断;真正调用用的 `ToolSpec` 仍由 resolver
按同一个版本快照解析产出 —— 一处真相,不是两处各解析一遍后各自漂移。

**建图优先级**(executor,自上而下首个命中):

| 顺序 | 条件 | 图 |
|---|---|---|
| 1 | `direct_tool` 非空 | L0 单工具直调 |
| 2 | L1 + `composition_hint=dag` + 技能数 > 1 | 固定 DAG |
| 3 | 编排直出标记成立 | 编排直出 |
| 4 | 其余 | ReAct(`build_agent_graph`) |

技能没绑工具时(快照 `tools` 为空)落到第 4 档 ReAct —— 没有工具就无从并行。

图是 `plan → invoke_all → synthesize` 三段,**LLM 全程只出现 1 次**:

- **plan**:产出 `orchestration_plan` 时间线事件(skill / tools / `predicted_gaps`)。
  `predicted_gaps` 取 `json_schema.required` 与已装配参数的差集,**纯诊断、不决定是否调用**
  —— 补齐实际发生在 `execute_single_tool_call` 内部(forced_params / x-param-source),
  而模型可见 Schema 已把这些参数剥离,差集必然与实际结果不符;代价仅限一条时间线日志。
- **invoke_all**:工具全集 `asyncio.gather` 并行。每个工具开**独立 AsyncSession**(共享主
  session 会并发冲突);每次真实调用都要过 `check_and_increment_tool_call`(resilience 的
  重复调用检测依赖这个计数,不因为「不是模型调的」就跳过)。
- 结果不论成败都进 `skill_outputs`(成功数据 / 缺参报错 / 空结果三类内容混在一起);失败的
  工具额外把 `kind:返回原文前 300 字` 留在时间线 `error` 字段 —— 缺参排查的主入口,
  executor 收尾 `_fallback_answer` 的归因文案也读它。
- **synthesize**:一次 LLM 把 N 份真实返回整理成答案;模型取 `skill.model` 优先于编排级
  覆盖(与 DAG 图同口径)。

#### 参数同源

参数从 executor 装配好的同一批 slots 里挑(`_dispatch_args`:取该工具 `json_schema.properties`
声明的那些 key),与 ReAct 路径下**模型看到的参数完全同源**,不存在第二套参数口径。挑不出值
就让它缺着,由真实调用结果回答缺不缺,不在这里预判。

#### 成文的「如实」硬约束

喂进成文环节的是①成功数据 ②缺参/报错文本 ③空结果 的混合体,而模型惯性是用合理的数据把
缺口补上,补完没人看得出缺过参数。所以 `_DIRECT_DISPATCH_CONSTRAINTS` 是这条路径**能不能
开的开关前提**:

1. **缺失必报**:哪个工具没取到数据,必须在答案里明确指出是哪一块没取到、原因是什么(可引用工具返回原话);
2. **禁止填补**:不得用推断值、历史值、行业合理值去填补没取到的部分,也不得含糊带过;
3. **允许不完整**:宁可答案缺一块,也不允许看起来完整但数据是编的。

#### 全部失败的降级(零 token)

失败数 == 工具总数(`dispatch_all_failed`)时跳过成文 LLM:`final_answer` 留空,时间线留
`generation / skipped=all_tools_failed`,由 executor 收尾的 `_fallback_answer` 按时间线里的
失败原因给出可读归因 —— 全都没数据时不该再花一次 LLM。这个信号放在 state(`tool_tracker`)
而不是闭包变量:要能随 checkpoint 恢复。

#### 已知风险(链路内无法根治)

缺参时接口**未必报错**,可能返回默认值或全量聚合 —— 那不是「缺参告知」而是静默的错口径数据。
根治手段在工具契约侧:**必填缺失必须报错**。

### 4. 上下文策略(固定问题 = 工作流语义)

**固定问题(question_id 命中)是确定性执行,不信任会话上下文**:

- **thread 隔离**:执行用独立 checkpoint(thread_id=task_id),不继承会话
  messages——旧轮的工具结果/系统提示不会进入本轮 LLM 上下文(多轮
  工具结果是上下文干扰源,会让模型凭旧结果作答而不重新取数);
- 参数已由模板装配期全量生成,跨轮槽位零损失;会话回放不受影响
  (展示层读 tasks 聚合,不读 checkpoint);
- **执行层历史强制 0 轮**(纯净上下文:本轮问题+技能提示词+装配参数)。

**非固定问题(Agent 路线)按轮注入对话文本**:

- LLM 上下文 = 最近 N 轮的**用户/助手原话**(conversations 层聚合),
  **不含旧轮工具结果原文**——多轮追问(「那同比呢」)需要「说了什么」,
  不需要「上次查到什么」;
- 轮数 = Agent `intent_config.exec_history_rounds`(0-5,**默认 2**;
  0 = 纯净上下文;与 Triager 的 `history_rounds`(默认 0)是两个配置);
- 注入失败静默降级为纯净上下文,不阻断执行。

**技能级严格取数(strict_tool_execution)**:技能 constraints 配置(管理端
「护栏约束」开关)。开启后,该技能在本轮**尚无任何工具调用**时,首轮
generation 以 `tool_choice="required"` 协议级强制模型发起工具调用(模型
没有「要不要调」的决策权);首次调用后恢复自主 ReAct。数据查询类技能
建议开启,防止模型凭上下文直接作答。

## 路线二:Agent 动态(自主性兜底)

### 1. Triager(L1 意图层,一次轻量 LLM 调用)

意图路由 + 技能裁剪(target_skills 按能力清单白名单校验)+ 槽位抽取 +
问题改写(多轮省略补全)+ 分步计划(plan)。要点:

- **槽位优先级**:APP date(用户显式选)> 问题语义 > 会话 slot >
  语义默认;会话绑定景区 ID 以全部字段别名注入(scenic_id/scenicId/…,
  历史工具驼峰口径),Triager 据此判「景区参数已填」不反问;
- **多轮历史注入**:判定前注入最近 N 轮对话文本(跨轮话题延续);
- **clarify 不走 interrupt**:意图是「本轮结束、把球踢回用户」,配合会话
  记忆,用户回答后自然接着走(不占租约、不进审批中心);
- 缓存键含会话 ID + history + 日期,跨会话/跨天不传染。

### 2. 模式判定与 L2 执行

模式判定链(`modes.py`,确定性优先——用能完成任务的最低档):

| 场景 | 模式 |
|---|---|
| 单 Tool/MCP 直调 | L0(不经 LLM 推理层) |
| Agent 未绑任何 Skill | L0(纯人格对话,单次 LLM) |
| 单 Skill | 该 Skill 登记的 suggested_mode;未登记按有无工具退化(无→L0,有→L2) |
| Agent 绑多 Skill | agent.composition_mode:chain→L1(固定顺序链)/pool→L2(动态可选池);未登记默认按绑定顺序视为链 L1 |
| 调用方显式 override_mode | 最高优先 |

L2 ReAct 循环里模型自主调工具;工具参数两个机制并存:

| 机制 | 语义 |
|---|---|
| `x-param-source` | 执行期经前置工具解析注入,模型侧剥离 |
| `forced_params` | 运行时强制覆盖(管理端调试 override 等) |

### 参数注入铁律(33 号事故后补)

**注入以「工具 schema 是否声明」为准,不以参数名判断**:参数名叫
`supplierId`/`account` 不等于它会被注入——只有属性上真写了
`x-param-source` 才会剥离+注入;没声明的参数就是普通 required,
**暴露给模型**。

- 实测事故:36 个工具(supplierId ×30、account ×6)从未声明,而审计的排除名单
  按**名字**排除 supplierId/account,断线从未报过缺口;结果模型要么直接反问
  (任务 f400994c,44s 收敛),要么编个值取到空数据后反复重试,直到工具被摘除、
  步数耗尽、**输出空回答**(249488c8:4 轮 generation 273s、零输出、成本照扣);
- 现判定改为「按 schema 声明」,未声明的注入键单独报**工具声明断线**
  (`scripts/audit_param_coverage.py`);
- 收尾兜底:模型最终没产出内容时由 executor 按时间线生成「没取到 + 原因」文案
  (`_fallback_answer`),**不留空回答**——任务显示已完成却一片空白,比报错更糟。

### 注入值类型强制转换

注入类参数(slots 模板值 / x-param-source)来自 JSON 装配,类型未必与目标
Schema 一致 —— `supplierIds` 由前置工具取到的是数字 `[1757026]`,而工具契约是
`array<string>`;此前这类不匹配直接撞 Schema 校验报错,表现为工具明明有数据却取不到(同一个
参数由模型自己传 `["1757026"]` 是成功的,说明契约没错、是注入值没转换)。

`coerce_args_to_schema` 的位置在 `execute_single_tool_call` 里**所有系统补齐完成之后、
Schema 校验之前**,一次覆盖模型实参 / forced_params / x-param-source
全部来源,两条路线同一套:

- **按目标 Schema 声明的类型**转换(string / integer / number / boolean,array 按其 `items`),
  而不是硬编码「一律转字符串」—— 日后某工具声明 integer 契约时自动兼容;
- `bool` 是 `int` 的子类要先把布尔排除,`True` 不该被静默转成 1;
- 转不动就原样返回交给 Schema 校验报错,不吞异常;
- 方向改的是注入值、**不是工具 Schema** —— 外部接口契约不接受我们改,那是反的。

### 参数设计不变式

声明了来源的参数**由代码解析,LLM 不填、不猜、运行前不知道值**:

```
                 解析时机        解析方式          LLM 何时不知情
x-param-source   工具调用期      经前置工具        调用前(模型侧 Schema 剥离)
```

没声明来源的参数一律是普通 required,由模型按 Schema 自填;取不到值时走
工具本身的缺参/空结果报错路径,不做任何隐式补齐。另有一条**显式可见通道**
prompt_vars:提示词显式写了 `{{var}}` 的才渲染进 system prompt
(运营决定 LLM 合法需要知道的上下文)。

## 公共执行层(executor)

- 模式判定 → 建图执行;`mode_decision`/`intent_check` 事件随 checkpoint
  落库(管理后台链路可视化/会话信息的数据源);
- 参数级景区越权校验:景区级角色只能操作本景区(模型可控参数参与校验,
  系统注入值不参与);
- 重复调用拦截/护栏计数/凭证确认(HITL)两条路线同一套。

## 版本解析档位(resolver)

档位 0:调用方显式钉版本(`executor_version_id` / Console 显式指定,预热派单用它
锁定当日口径)
> 档位 1:用户维度的分发指派(分配中心,按人;无 scenic/global 维度)
> 档位 2:**无第三档**——未命中即 `agent_not_distributed` fail-closed。

`agent`/`skill`/`tool` 三类实体**没有「默认版本」概念**,版本生效的唯一途径是
指派;资源主表不再参与版本兜底。(模型配置的 `is_default` 是另一件事,不在此列)

**固定问题直跳(fail-closed)**:技能版本 = 用户对该问题锚 agent(问题行
`agent_id`,缺省会话 agent)的**指派版本快照里该技能的 pin**;无指派/未绑
该技能/pin 版本已下线 → 降级 agent 动态池(agent 侧同样 fail-closed)。
**没有「技能最新 published」兜底**——版本体系的意义就是可指定,与预热行/
消费端的严格版本匹配口径一致(见 `业务/APP数据预热/prewarm.md`)。

## 消息与执行的表口径

task_queue 一表两用(轮次数据源 + 执行队列):预热缓存命中也产生用户可见
轮次,因此直出时补终态 COMPLETED task 行,`prewarm_hit` 标记区分统计口径。
