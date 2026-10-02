---
title: 定时任务心跳调度体系
category: 调度
summary: K8s CronJob 只当闹钟(静态镜像每 5 分钟打 HTTP 心跳),定时任务的注册/周期/防重/补拍全在常驻服务进程内——发版零 K8s 操作,新增任务只改一处注册;token 鉴权、运行锁、心跳与任务频率两层解耦
related: cogni_agent/cronjobs/registry.py, cogni_agent/cronjobs/prewarm.py, cogni_agent/gateway_service/routes_internal.py, cogni_agent/main.py, config/config.yaml
---

# 定时任务心跳调度体系

## 一句话

K8s CronJob 退化成**每 5 分钟一次的哑心跳**,「哪些定时任务、什么周期、怎么跑」
全部由常驻服务里的 `cronjobs/registry.py` 决定——**调度逻辑随发版自动演进,
K8s 侧一次性配置永不改**(不再依赖 CronJob 换业务镜像 tag)。

## 架构

```
K8s CronJob cogni-agent-prewarm(静态配置,*/5 心跳,一次性交付)
   │  python urllib 发 POST(3 秒生命的哑 pod)
   ▼
http://cogni-agent-svc:8000/api/internal/cron/tick   ← 集群内 Service 直连
   │  X-Cron-Token 鉴权
   ▼
gateway_service/routes_internal.py → cronjobs/registry.tick()
   │  逐 job 判定:到点? 运行锁空闲? → 触发
   ▼
常驻 Deployment 进程内后台协程执行(prewarm 每 30 分钟一轮,幂等)
```

## 设计:心跳与业务镜像解耦

CronJob 只做一件事:按固定周期发心跳,不承载任何业务逻辑——与业务镜像
零耦合,发版不需要动 K8s:

| 层 | 载体 | 变更频率 |
|---|---|---|
| 心跳 CronJob(curl/python 哑 pod) | K8s | **一次性,永不改** |
| 调度逻辑(注册表/周期/防重) | 应用代码 | 随发版自动演进 |
| 任务本体(prewarm 等) | 应用代码 | 同上 |

## 语义(两层频率解耦)

- **心跳频率 ≠ 任务频率**:CronJob `*/5` 只是打点,每个 job 自己声明
  `interval_seconds`(prewarm=30 分钟;未来任务可每天 02:00、每小时……全在代码里)
- **到点判定** = 距上次触发 ≥ interval(进程内存态);**重启后视为从未跑过,
  首拍立即触发**——发布/故障窗口漏掉的拍自动补上
- **防重叠**:每 job 一把 `asyncio.Lock`,上一轮没跑完本拍跳过
  (等价 K8s `concurrencyPolicy: Forbid`,但作用在 job 粒度)+ CronJob 侧 Forbid 双保险
- **漏拍容忍**:所有 job 必须幂等(漏几个心跳无害,下轮兜底)
- **执行模型**:fire-and-forget 后台协程,tick 立即返回(HTTP 快进快出)

## 接口与安全

- `POST /api/internal/cron/tick` — 常规心跳,按各 job 周期判定,返回
  `{jobs: {name: triggered|skipped_running|skipped_not_due}}`
- `POST .../tick?job=prewarm&force=true` — 定点触发(force 忽略周期,仍受运行锁
  约束,可安全重复调用)
- 鉴权 = 共享 token:请求头 `X-Cron-Token`,服务端从
  `config.yaml cron.tick_token`(`${CRON_TICK_TOKEN}` 环境变量,K8s 注入,
  与 Deployment 同值)校验;**未配置 token = 接口一律 403**(安全默认)
- 9443 公网网关会透传该路径,token 是唯一防线,勿泄露

## 新增一个定时任务(唯一要改的地方)

`cogni_agent/cronjobs/registry.py` 的 `_builtin_jobs()`:

```python
async def _my_job() -> None:
    from ... import run_xxx          # 实现内部再 import,避免拖慢启动
    await run_xxx()

register("my_job", interval_seconds=3600, func=_my_job, desc="每小时干一次")
```

K8s、接口、鉴权全部零改动。任务实现放 `cronjobs/<name>.py`,
独立进程调试可用 `python -m cogni_agent.cronjobs.<name>`(须幂等,可与 tick 并行)。

## 运维

- CronJob YAML 在运维侧维护,不进本仓库
  (业务镜像只是「发 HTTP 的壳」,**tag 旧了也能用**,不用随发版换)
- 心跳目标地址:`http://cogni-agent-svc:8000`(实测确认;Service 改名只改 URL)
- 健康判断:Job 状态 `Complete` + pod 日志 `tick 200`;一次性 pod 跑完即退,
  `Ready: False` 是完成后的正常长相,不是故障
- 人工触发:`curl -XPOST -H "X-Cron-Token: ..." ".../api/internal/cron/tick?job=prewarm&force=true"`

## 边界说明

- **单副本假设**:锁是进程内的,Deployment 多副本时 tick 负载均衡到不同 pod,
  同一 job 理论可并行——任务自身幂等 + DB 唯一约束兜底,无正确性风险,
  常态多副本时再引入分布式锁
- 调度精度受心跳频率限制(*/5 = 最多 5 分钟误差),对分钟级以上周期足够
- 常驻循环型后台(任务认领/续租巡检)不走本体系,在 `worker/loops.py`
