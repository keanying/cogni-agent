---
title: APP端数据预热体系
category: APP数据预热
summary: APP 固定问题预热的派单-占位-收割-补派-摘要-消费全链路——扫描式无等待状态机、占位行防重、OTA 按景区×账号拆行、版本随指派严格匹配、四维指标真数据管道、K8s 心跳调度入口
related: cogni_agent/cronjobs/prewarm.py, cogni_agent/cronjobs/registry.py, cogni_agent/gateway_service/routes_internal.py, cogni_agent/gateway_service/routes_app.py, cogni_agent/gateway_service/insight_service.py, cogni_agent/worker/loops.py, cogni_agent/gateway_service/models.py, cogni_agent/gateway_service/ota_accounts.py
---

# 预热定时任务体系

## 定位

APP 固定问题(问题库 `prewarm_enabled` 的行)每天离线预生成完整回答,用户
点击**秒开直出**(0 LLM)。核心是「扫描式无等待状态机」——每次扫描秒级
退出,任务执行发生在各轮扫描**之间**,靠下一轮收割接力。

## 时间语义

| 概念 | 含义 | 决定什么 |
|---|---|---|
| `biz_date` | 行归属的**业务日**(=任务创建日,东八区) | 缓存唯一键的一维 |
| `time_period` | **取数窗口参数**(today/yesterday/week_to_date…) | 只进 `param_template` 展开成取数参数,与行归属无关 |
| `ota_account` | **OTA 账号维度**(platforms.*.username) | 行唯一键的一维:OTA 主题/概览按景区×账号拆行 |
| `skill_version` | **技能版本维度**(指派 pin) | 行唯一键的一维:版本随用户指派,消费严格匹配 |
| CronJob 周期 | 扫描频率(每 5 分钟心跳×应用侧 30 分钟) | 纯机械,何时派/派什么全由扫描逻辑判定 |

**派单与 period 无关**:每轮对所有到点问题一视同仁,判定唯一依据 =
**当日占位行是否存在**。消费端只认当日行(`biz_date=今天` 且 `answer`
非空),今日未出 = 空态,**绝不回退昨日**。

## 参数装配期全量生成

所有取数参数在 **派单之前全部生成完毕**,供整条后续链路
(报告任务/摘要/直出)直接使用:

- **来源只有两个**:① 请求显式带的(APP 端用户选的问数日期,最高优先);
  ② 问题行 `param_template` 按当日展开(基准日期 + 对比窗口都由 time_period
  语义在模板里配全,`{{today}}`/`{{yesterday_start}}` 等占位符代码替换);
- **无运行期默认补全**:模板没配的参数就是缺,由执行层 required 校验拦截
  并明确报错,不隐藏;
- **新问题上线的检查项**:param_template 必须把该问题需要的日期/对比窗口
  参数 key 配全(key 缺失 = 配置错误,报错暴露;与版本锚 agent_id 同为
  上线门禁)。

## 行维度与唯一键

缓存行唯一键 = **`(question_id, scenic_id, COALESCE(ota_account,''),
COALESCE(skill_version,-1), biz_date)`**(表达式唯一索引,DB 层硬防双派,
CronJob 与手动触发并发也插不进第二行)。

**拆行规则**:

- OTA 主题 + 概览(取数含 OTA 指标,数据随账号变)→ 按「景区×账号」拆行;
  客流/票务/舆情数据与账号无关,一景区一行(ota_account=NULL);
- 概览额外保留一行 NULL 基础版——未绑账号用户回退看三域概览;
- 景区一个有效账号都没有:OTA 主题整体跳过(不派 LLM、不落行,零 token),
  概览只剩 NULL 基础行,其余不变。

**版本维度(fail-closed)**:问题行 `agent_id` 是版本锚(必配);景区 active
用户对该锚 agent 的指派快照里,该问题技能 pin 的版本去重集合 × 账号维度
× 业务日各派一行——同景区指派一致(常态)单版本不膨胀,版本分流各版本
一行;**无锚/无指派/技能未绑 → 该景区该问题不派(不可用)**,绝不回退
「技能最新 published」。

**消费(全部严格)**:chat 直出/首页卡片/四维条全部按「用户期望版本
(=其对锚 agent 的指派 pin)」匹配行,无匹配版本行 = 空态回落实时(实时
直跳同样按指派链解析版本,无指派降级 agent 动态池——agent 侧自身
fail-closed),绝不拿别的版本结果冒充。

**消费匹配(用户级)**:当前登录用户自己的账号(`user_ota_accounts`)匹配
行;账号行未命中回退 NULL 行;未绑账号 = OTA 主题「未配置账号」说明、概览
用基础行。改绑自愈:新账号当日缺行 → 下一轮派单自动补。空壳 OTA 配置
(platforms 平台 dict 为空)不算已配置(以 username 非空为准)。

## 单轮扫描的六个步骤(cronjobs/prewarm.py)

每轮按序执行后秒退,任意一步失败不影响其余,下一轮天然续跑:

```
⑥ 裁剪    清 biz_date < 今天-7 的历史行(防表膨胀)
⑤ 补派    今日失败的 redispatch 报告任务 → create_task 新任务(沿用原 payload)
① 收割    当日创建的 COMPLETED 任务 → 回填缓存行(见状态机)
② 派单    prewarm_enabled 且到点的问题 × 每个在用景区
          → create_task + 建当日占位行;OTA 主题无账号景区整体跳过(零 token)
③ 派摘要  主题解读行 answer 有/summary 无/无 enrich 占位 → 派摘要任务
④ 登记失败 今日 FAILED → 计数(补派留下一轮)
```

## 任务状态机(报告任务)

```
初派: create_task + 建占位行(task_id=本任务, answer=NULL 锁位)
  ├─ 没跑完(CREATED/QUEUED/RUNNING)
  │    什么都不做;卡死(90s 无进展停续租,600s 租约过期)→ 巡检回收 FAILED → 失败分支
  ├─ 跑完成(COMPLETED)
  │    收割 UPDATE 占位行补 answer/structured(行 task_id 不变)
  │    下一轮 ③:主题解读行 → 派摘要;非主题解读行 → 收官
  │    例外: COMPLETED 但 answer 空 → 不回填不重试(模型判定无内容)
  └─ 跑失败(FAILED)
       登记第 N/5 次 → 下一轮补派新任务(旧任务标 replaced_by)
       → 新任务完成走收割(行不变,UPDATE 补全) → 达 5 次 ERROR 停止,当日空态
```

摘要任务同构:占位字段 `enrich_task_id`、失败/产出不合法同口径计 5 次、
计数挂初派报告任务 payload(`enrich_attempt`)。

## 占位行——防重的唯一依据

**初派即建行**(answer=NULL 锁位),防重判定只看「当日占位行是否存在」:

- 报告任务「行等任务」:任务 payload 自带坐标(`question_id`/`biz_date`/
  `scenic_id`),收割按坐标找行回填;同组合一天多个 COMPLETED 时按
  created_at 序后者覆盖;行上 `task_id` 永远是初派任务(摘要失败计数的
  挂载点,溯源链不漂移);
- 摘要任务「行记任务」:派单时写 `enrich_task_id` 到行上,收割按它认领。

## 摘要环节

- **只对主题解读行派摘要**(消费方 = home 主题卡副标题 + insight.sentence,
  均只读主题解读行;其余预热行无摘要消费方,不派);
- **概览行三件套**:answer + summary + `overview_metrics`(四维彩色指标,
  见下);其余四主题只要 answer + summary;
- 摘要技能(`数字大脑-预热摘要`)输出 json:`{"summary":...}`(单主题)或
  `{"summary":..., "metric":..., "metrics":[{theme,value,display}]}`
  (概览四维)。

## 四维彩色指标真数据管道

```
预热报告(工具真数据) → 概览解读报告 → 摘要技能提取 metrics 数组
→ 收割写入行 overview_metrics.items → panel/insight 与 agent/home 读取
```

缺行/缺维度 → 显示 `--`、level normal(纯读缓存不回退实时)。阈值分色:
在园承载比 ≥75% 警告/≥90% 严重;退票率 >5%/>10%;负面占比 >10%/>30%;
核销率仅展示。

## 并发与执行

- 任务队列 Pull 模式:worker `FOR UPDATE SKIP LOCKED` 认领,认领即丢后台
  协程,并发度 = `runtime.max_concurrent_tasks`(缺省 8),租约 600s +
  90s 无进展停续租防僵死;
- 预热任务 priority=8,链路 30 分钟收官。

## 运维

- 调度入口:K8s CronJob(`cogni-agent-prewarm`)用**静态 curl 镜像每 5 分钟
  打心跳** `POST /api/internal/cron/tick`(集群内 Service 直连,`X-Cron-Token`
  共享鉴权,token 经 `CRON_TICK_TOKEN` 环境变量注入 Deployment)→
  `cronjobs/registry` 判定 prewarm 到点(30 分钟周期)并在常驻服务进程内执行。
  **CronJob 与业务镜像彻底解耦,发版零运维**(CronJob YAML 在运维侧,不进仓库);
- 新增定时任务:`cogni_agent/cronjobs/registry.py` 的 `_builtin_jobs()` 里
  `register(name, 周期秒, func)` 即可,不动 K8s;
- 手动补跑:`ZHILV_ENV=<env> python -m cogni_agent.cronjobs.prewarm`(幂等,
  可并行);`--dry-run` 只读观察不写库;在线触发:
  `POST /api/internal/cron/tick?job=prewarm&force=true`(需 X-Cron-Token 头,
  立即执行仍受运行锁约束)。
