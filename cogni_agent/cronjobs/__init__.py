"""cronjobs:定时任务包(2026-09-11 升级为 HTTP 心跳调度)。

定位(与常驻服务的区别):
- 本包只放「定时触发的任务」——不再各自起独立进程,而是由
  cronjobs/registry.py 统一注册(声明周期 + run),由 K8s CronJob(静态 curl
  镜像,每 5 分钟)POST /api/internal/cron/tick 心跳触发,在常驻服务进程内
  执行——调度逻辑随发版自动演进,K8s 侧一次性配置永不改;
- 常驻后台循环(claim/dispatcher/lease_patrol)在 cogni_agent/worker/loops.py,
  API 服务在 gateway_service 等,不放本包。

命名约定:脚本名 = 调度单元名(prewarm = 固定问题预热),
独立进程运行方式 `python -m cogni_agent.cronjobs.<name>`(可用 --dry-run 只读
观察;幂等,可与在线 tick 并行)。

脚本间幂等可并行:任意一轮失败由下个周期天然兜底。
"""
