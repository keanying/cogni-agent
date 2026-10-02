---
title: 景区配置(承载量与景区级参数)
category: 景区配置
summary: 景区维度配置管理:日最大承载量首期落库,extra JSONB 承载后期扩展;列表前置条件=被用户绑定过的在用景区(主数据万级不全量浏览),配置挂景区不挂用户与用户模型演进解耦;未配置=消费侧维持现状不猜默认值
related: cogni_agent/gateway_service/routes_app_ops.py, cogni_agent/gateway_service/models.py, cogni_agent/common/schema.py, web/src/pages/AppOps/ScenicConfig.tsx, web/src/pages/AppOps/ScenicConfigDetail.tsx
---

# 景区配置(承载量与景区级参数)

## 一句话

APP 运营后台维护的景区级参数(首期:日最大承载量),挂**景区维度**不挂用户,
与用户模型演进解耦;未配置时消费侧维持现状,不猜默认值。

## 数据模型

`numeri_core_app_scenic_config`(0058 迁移):`scenic_id` 唯一键,
`daily_capacity` 核心列(>0 整数 CHECK),`extra` JSONB(带 schema_version)
承载后期扩展配置——核心列强类型、扩展走 extra,与竞品需求表同一模式。

## 关键设计

- **列表前置条件 = 在用景区**:被 active 用户绑定过的景区(usr_users.
  scenic_id 去重,与预热派单同口径);主数据 `sys_scenics` 万级(1.7 万行)
  绝不全量浏览,只按景区 ID/名称在子集内搜索——主数据字段(名称/等级/
  启用状态/同步时间)只读展示不落配置表
- **进详情编辑**:列表显基础信息(名称/等级/绑定用户数/配置状态/承载量/
  更新时间),行点击进 `/app-ops/scenic-config/{scenic_id}` 编辑
- **未配置语义**:承载量为空 = 消费侧维持现状(报告不注入参数、分色
  value=null 仅展示文本)——**绝不猜默认值,宁缺勿错**
- **upsert**:`ON CONFLICT (scenic_id) DO UPDATE`,写 `updated_by` 溯源

## API(APP 运营 /admin/app-ops/scenic-configs)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 在用景区列表 × 主数据展示 × 配置状态;`?kw=`(ID/名称)、`?state=configured/unconfigured` |
| GET | `/{scenic_id}` | 详情(无配置行返回空壳 `configured:false`) |
| PUT | `/{scenic_id}` | 保存:`{daily_capacity: int|null}`(>0;null=清空);group_viewer 只读 |

## 消费(后续批次,当前未接)

1. 预热报告参数注入(cronjobs/prewarm 派单时给客流/概览主题注入承载量);
2. insight 分色兜底(在园人数已知时服务端补算承载比);
3. 实时问答槽位注入、景区级阈值覆盖(extra)。

## 用户模型附注(25 号定稿)

统一身份多端通行是目标架构(一张用户表 + user_type,各端独立 profile);
过渡规约:C 端语义以 `scenic_id IS NOT NULL` 圈定,管理端以 `IS NULL` 圈定,
后期拆分按此边界切割。本配置挂景区维度,与该演进完全解耦。
