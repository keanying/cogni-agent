"""cronjobs 调度注册表:HTTP 心跳 tick 驱动的定时任务调度(2026-09-11)。

架构(K8s 侧与业务镜像彻底解耦):
- K8s CronJob 用静态 curl 镜像,每 5 分钟 POST 一次 /api/internal/cron/tick
  (集群内 Service 直连,带 X-Cron-Token)——只是一个心跳,不含任何业务逻辑,
  **一次性配置,发版永不改**;
- 「哪些定时任务、什么周期、怎么跑」全部在本注册表(应用代码)里声明,
  随正常发版自动迭代——不再依赖 CronJob 换镜像 tag(旧模式断档根因:
  CronJob 镜像停在 09-08,09-10 起每轮失败没人发现)。

语义:
- tick 频率 ≠ 任务频率:CronJob 心跳 */5,每个 job 自己声明 interval_seconds;
- 到点判定 = 距上次触发 ≥ interval(进程内存态,重启后视为从未跑过→
  首个 tick 立即触发,天然补拍);
- 防重叠:每 job 一把 asyncio.Lock,上一轮没跑完本 tick 跳过
  (等价 K8s concurrencyPolicy: Forbid,但作用在 job 粒度);
- 漏拍容忍:所有 job 必须幂等(漏几个 tick 无害,下轮兜底)——prewarm
  的扫描式无等待状态机本来就满足;
- 任务以 fire-and-forget 后台协程执行,tick 立即返回(HTTP 快进快出,
  不占 CronJob 的 activeDeadline)。

边界:
- 单副本假设:锁是进程内的,Deployment 多副本时 tick 会负载均衡到不同 pod,
  同一 job 理论上可并行——但 prewarm 等任务自身幂等+DB 唯一约束兜底,
  无正确性风险,只有少量重复扫描的浪费;若将来多副本常态化,再引入分布式锁。
- 新增定时任务:在 _BUILTIN_JOBS 处注册(或运行期调 register()),无需动 K8s。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from cogni_agent.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class _Job:
    name: str
    interval_seconds: float
    func: Callable[[], Awaitable[None]]
    desc: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_run: float | None = None          # epoch 秒;None=从未跑过(重启后)

    def due(self, now: float) -> bool:
        return self.last_run is None or (now - self.last_run) >= self.interval_seconds


# name → job(注册表;tick 按注册顺序判定)
_JOBS: dict[str, _Job] = {}

# 后台任务强引用(防 GC 回收运行中的协程)
_RUNNING: set[asyncio.Task] = set()


def register(name: str, interval_seconds: float,
             func: Callable[[], Awaitable[None]], desc: str = "") -> None:
    """注册/覆盖一个定时任务(interval 秒,进程内存态,重启即重置)。"""
    _JOBS[name] = _Job(name=name, interval_seconds=interval_seconds,
                       func=func, desc=desc)


async def _run_job(job: _Job) -> None:
    async with job.lock:
        job.last_run = time.time()
        try:
            await job.func()
            log.info(f"cronjob[{job.name}] 执行完成")
        except Exception:
            log.exception(f"cronjob[{job.name}] 执行失败(任务自身幂等,等下个周期重试)")


def _builtin_jobs() -> None:
    """内置任务注册(import 期一次性执行)。

    新增定时任务在这里加:register(name, 周期秒, func)。
    func 内部再 import 具体实现,避免拖慢应用启动。
    """

    async def _prewarm() -> None:
        from cogni_agent.cronjobs.prewarm import run
        await run()

    register("prewarm", interval_seconds=30 * 60, func=_prewarm,
             desc="固定问题预热扫描(派单/收割/补派/摘要,幂等)")

    async def _quickbi_reports() -> None:
        from cogni_agent.tool_center_service.quickbi.report_watcher import sweep_once
        await sweep_once()

    # 主力是应用内的常驻轮询(main.lifespan 里起的 run_forever),这里是兜底:
    # 进程内循环万一被取消/异常退出,心跳还能把没收完的报告捞回来。两者都幂等。
    register("quickbi_report_sweep", interval_seconds=5 * 60, func=_quickbi_reports,
             desc="小Q报告收尾兜底扫描(应用内常驻轮询的第二道保险,幂等)")

    async def _quickbi_prewarm() -> None:
        from cogni_agent.cronjobs.quickbi_prewarm import run
        await run()

    # Quick BI 固定问题的「加速」。周期 5 分钟 = 与心跳同频,**不是**刷新频率 ——
    # 刷新间隔是每条问题自己配的(默认 30 分钟),这一轮只是「看看有没有到点的」。
    # 把这里设成 30 分钟的话,一条配了 5 分钟刷新的问题最快也只能 30 分钟刷一次。
    register("quickbi_prewarm", interval_seconds=5 * 60, func=_quickbi_prewarm,
             desc="Quick BI 固定问题加速预热(按问题×景区跑,各自刷新间隔内跑过的跳过,幂等)")

    async def _qbi_runs_retention() -> None:
        from cogni_agent.cronjobs.qbi_runs_retention import run
        await run()

    # 与心跳同频,但**不是**每拍都删:「每天凌晨 1 点后跑一次」由 func 内部判
    # (北京时间已过 1 点 + 今天没跑过)。不用 interval=86400:那个时刻会随进程重启漂移。
    register("qbi_runs_retention", interval_seconds=5 * 60, func=_qbi_runs_retention,
             desc="tc_qbi_runs 保留:每天凌晨 1 点(北京时间)后删保留天数之前的运行记录(幂等)")


_builtin_jobs()


async def tick(job_name: str | None = None, force: bool = False) -> dict[str, str]:
    """一次心跳:到点的 job 触发执行,未到点/运行中跳过。

    - job_name: 只处理指定 job(运维定点触发用);None=全部;
    - force: 忽略 interval 立即触发(仍受运行锁约束);
    - 返回 {job名: 状态} 供调用方/日志观测。
    """
    now = time.time()
    result: dict[str, str] = {}
    for name, job in _JOBS.items():
        if job_name and name != job_name:
            continue
        if job.lock.locked():
            result[name] = "skipped_running"        # 上一轮还没跑完
            continue
        if not force and not job.due(now):
            result[name] = "skipped_not_due"
            continue
        t = asyncio.create_task(_run_job(job))
        _RUNNING.add(t)
        t.add_done_callback(_RUNNING.discard)
        result[name] = "triggered"
        log.info(f"cronjob[{name}] tick 触发(force={force})")
    return result
