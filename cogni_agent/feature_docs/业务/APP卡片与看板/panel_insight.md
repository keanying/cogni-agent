---
title: APP 卡片与看板数据链路
category: APP 交互
summary: APP 端卡片/看板的消费口径——首页聚合(卡片壳静态+代表问题取「主题解读」行)、试试问我/关注问题(问题库过滤分页,空则回退静态)、看板 AI 解读(/panel/{theme}/insight 纯读当日预热缓存);统一用 question_id 锁定问题、skill_version 指派 pin 严格匹配,未命中即空态,不回退昨日、不实时生成
related: cogni_agent/gateway_service/routes_app.py, cogni_agent/gateway_service/schemas_app.py, cogni_agent/gateway_service/guide_data.py, cogni_agent/gateway_service/insight_service.py, cogni_agent/gateway_service/models.py, cogni_agent/gateway_service/question_bank.py, cogni_agent/gateway_service/ota_accounts.py, cogni_agent/cronjobs/prewarm.py, cogni_agent/tool_center_service/versioning.py
---

# APP 卡片与看板数据链路

## 一句话

APP 端所有卡片/看板的内容都**不实时生成**:问题文案来自问题库
(`AppQuestionBank`,空则整体回退静态常量),数据来自**当日预热缓存**
(`AppPrewarmCache`)。两端靠 `question_id` 锁问题、`skill_version`(指派 pin)
锁版本严格对齐——**版本不符或当日未预热就是空态**,不回退昨日、不实时兜底。

## 一、接口面(前缀 `/api/app`)

| 接口 | 作用 |
|---|---|
| `GET /agent/home` | 首页聚合:agent 信息 + 引导卡片 + 关注问题 + 三级选择器 + 概览指标 |
| `GET /guide-questions` | 「试试问我」问题列表(支持主题过滤 + 分页) |
| `POST`/`GET`/`DELETE /followed-questions` | 关注问题的加/查/删 |
| `GET /panel/{theme}/insight` | 看板 AI 解读卡片(概要 + 一句话 + 异常分色 + 四维条) |
| `POST /chat` | 卡片/看板点击后的统一入口(带 `question_id` 走固定问题直跳) |

运营侧问题库维护走管理端路由(非 `/api/app` 前缀)。

## 二、内容的三段来源

1. **问题库 `AppQuestionBank`**(`gateway_service/models.py:24`)——唯一可运营编辑的源:
   落 `question_text`、`theme_code`、`skill_id`、`agent_id`、`sort_order`、`active`、
   `scenic_id`(NULL = 全景区通用)。问题库里 `【主题解读】` 前缀的行是**预热派单锚点**
   (与缓存行一一对应)。
2. **静态兜底 `gateway_service/guide_data.py`**——问题库**无行**时整体回退:
   卡片壳 `GUIDE_CARDS`(:20)、问题 `GUIDE_QUESTIONS`(:76)、三级选择器
   `SELECTOR_DATA`(:115)、主题中文名 `THEME_NAMES`(:64);
   主题编码固定为 `overview / passenger_flow / ticket / public_opinion / ota_direct_sales`(:8)。
3. **预热缓存 `AppPrewarmCache`**——数据正文(解读报告 / 一句话摘要 / 四维指标 /
   异常分色)。写入侧与严格版本口径见 `业务/APP数据预热/prewarm.md`。

## 三、首页聚合 `GET /agent/home`

- **卡片壳是静态的**(`routes_app.py:443` 的 5 张默认卡片:标题/图标/主题),
  **问题与副标题是动态的**:每主题取一条代表问题(优先
  `question_text.startswith("【主题解读】")` 的行,否则取该主题 `sort_order` 首条,:654),
  同主题只出一张(:656)。
- 卡片副标题 = 缓存 `summary`,无缓存则用静态副标题(:679);`generated_at` 取缓存生成时间
  (:680);`is_today = 缓存行是否存在`(:681)。
- `question_id` 随卡片返回(:678),APP 点击后由 `/chat` 携带,用于锁定该问题
  (不允许模型自行换问题)。
- **OTA 直连卡**(`theme_code == "ota_direct_sales"`)仅当该用户已绑 OTA 账号才返回(:666)。
- **三级选择器**(:583):`theme_code → dimension_name → 问题行`,叶子节点带
  `question_id`;问题库无行时整套回退静态 `SELECTOR_DATA`(:683)。

## 四、试试问我 `GET /guide-questions`

- 过滤:`active = true` 且 `scenic_id IS NULL OR scenic_id = 当前用户景区`(:1292);
  带 `theme_code` 时再加主题条件(:1298)。
- 排序 `sort_order, created_at`(:1296);返回前分页切片(:1302)。
- 问题库查无结果 → 回退静态 `list_guide_questions`(:1318)。
- 返回 `id`(问题库行 id,APP 回传为 `question_id`)、`theme`(中文名)、`question`。

## 五、看板 AI 解读 `GET /panel/{theme}/insight`

`theme` 即 `AppQuestionBank.theme_code`,精确匹配(:1368);**只有存在 `【主题解读】%`
行时才有数据**(取 `.first()`,:1369)。

缓存行匹配条件(:1381):

```
question_id = 主题解读行.id
scenic_id   = user.scenic_id or ""
ota_account = 该用户的 OTA 账号
skill_version = 该技能对该用户的指派 pin
biz_date   = 今天
answer is not None
```

- **`skill_version` 是严格相等匹配**(:1386)。pin 由
  `assigned_skill_version(db, 主题行.agent_id 或 请求 agent_id, user.user_id, 主题行.skill_id)`
  解析(:1377);主题行未绑 `skill_id` 时 pin 为 `None`。
- **账号无关行兜底**:账号维度行取不到时,回退 `ota_account IS NULL` 的行
  (版本仍按同一 pin 严格匹配,:1392)。
- **空态**(:1434):行不存在 → `summary=""`、`sentence=None`、`level="normal"`、
  `metrics=[]`。**不回退昨日行、不触发实时生成**。
- 返回结构(`schemas_app.py:169`):`summary` = 缓存 `answer`(完整解读报告);
  `sentence` = 缓存 `summary`(一句话,写入侧截断 128 字);`level` 优先取缓存
  `overview_metrics["level"]`,缺失则取本主题四维项的 level(:1414);
  `metrics` = 概览四维彩色条,**仅 `theme == "overview"` 时非空**(:1424);
  `updated_at` = 缓存 `generated_at`。
- 未绑 OTA 账号时,四维条里过滤掉 `ota_direct_sales` 项(:1410)。
- `insight_service` 只负责四维指标条(供 `/agent/home` 与 panel 复用),不产出
  summary/sentence。

## 六、版本对齐口径(三处消费端同一套)

| 消费点 | 位置 |
|---|---|
| chat 预热直出 | `routes_app.py:880` |
| 看板 insight | `routes_app.py:1386` |
| 概览四维条 | `gateway_service/insight_service.py:102` |

三处都要求 `AppPrewarmCache.skill_version == 指派 pin`,与写入侧唯一键
`uq_prewarm_q_scenic_acct_ver_date`(`question_id, scenic_id, coalesce(ota_account,''),
coalesce(skill_version,-1), biz_date`,见 `cronjobs/prewarm.py:120`)对齐。

历史行 `skill_version IS NULL` 按 `-1` 入唯一键(`models.py:136`)——
**不匹配任何期望 pin,自然淘汰**,不做跨版本兜底。

## 七、边界与例外(明确「不兜底」清单)

- 今日未预热 / 版本不匹配 / 缓存行 `answer` 为空 → **空态**(卡片显示静态副标题,
  看板返回空结构),不回退昨日、不实时 LLM、不降级到其它版本。
- 问题库无行 → 全套回退静态常量(卡片、问题、选择器);**静态层不含数据**,只给文案。
- 未绑 OTA 账号 → 不返回 OTA 直连卡,四维条去掉 `ota_direct_sales`。
- 消费只认 `biz_date == 今天` 且 `answer is not None`(:641,`models.py:109`)。
