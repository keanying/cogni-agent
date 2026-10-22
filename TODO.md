# 待办

## Quick BI:移植 1.2.4 的 APP 报告链路(「报告类型」「取数方式」两列)

**背景**:2026-10-22 对齐 Quick BI 到 1.2.4 时,这两列没做 —— 它们依赖的整条链路我们都没有,
只加列会是没人读取的空字段。

**1.2.4 里这两列各自依赖什么**:

- **报告类型**(固定问题 `report_period`:日报 / 周报 / 月报,外加 `report_prompt` 报告加工提示词)
  - `gateway_service/report_builder.py` + `cronjobs/app_report_prewarm.py`:按景区排期生成日报 / 周报 / 月报;
  - `app_messages` 系列表(迁移 0098–0104):报告产物物化进 APP 消息,保留 365 天;
  - 管理端「报告结果」查看(`/fixed-questions/{id}/report-rows`)与「报告重跑」(`/reports/rebuild`);
  - 约束:同一报告周期全平台只能有一条在用的固定问题。
- **取数方式**(固定问题 `external_source`,如 `quickbi`)
  - 执行引擎的「外部问答源」分支:`runtime_service/engine/external_qa.py`、`graphs.invoke_all_node`
    里的 `_invoke_external_qa`、executor 放行零工具技能链;
  - `tool_center_service/quickbi/engine_adapter.py`(把 `run_fixed` 包成外部问答源);
  - `gateway_service/routes_app.py` 命中后在任务 payload 里带 `external_source` / `direct_dispatch`。
  - 我们这边对应的是 `gateway_service/qbi_bridge.py`(APP 直连 Quick BI + 无数据时转交技能 / Agent),
    移植时要决定二者谁替代谁,还是并存。

**移植时注意**:

- 数据模型用我们的统一问题表 `scenic_agent_issue_detail`(1.2.4 是独立的 `tc_qbi_fixed_questions`),
  新列走我们迁移链的下一个编号,不能照搬 1.2.4 的迁移号(两边从 0063 起编号含义不同);
- 1.2.4 的预热是按期的缓存表 `tc_qbi_prewarm_cache` + `cronjobs/prewarm_v2.py`,我们是用运行记录当缓存
  (`cronjobs/quickbi_prewarm.py`),报告链路不要顺手把预热口径换掉;
- 参考代码:1.2.4 压缩包(`cogni-agent-1.2.4.zip`)。
