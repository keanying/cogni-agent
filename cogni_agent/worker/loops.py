"""
worker/ 后台协程:任务认领循环 · Outbox 派发 · 租约巡检(README「一键启动」的核心)。
main.py 启动时用 asyncio.create_task() 把这三个循环和 FastAPI 一起跑在同一进程里,
这就是"模块化单体"的关键——不需要单独部署 Worker 服务,一个包全包了。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.control_plane_service.queue import (
    claim_one,
    heartbeat_lease,
    promote_aged_tasks,
    reap_expired_waiting_human,
    requeue_expired_leases,
)
from cogni_agent.governance_event_bus_service.events.dispatcher import dispatch_once
from cogni_agent.runtime_service import streaming
from cogni_agent.runtime_service.engine.executor import execute_task, get_last_progress

log = get_logger("worker.loops")
_WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"


async def claim_loop(poll_interval: float = 1.0) -> None:
    """
    M3-03:Pull 模式认领循环。

    **并发执行,不是串行。** 早期实现是 `claimed = await claim_one(); await execute_task()`
    —— 认领一个就一直等它跑完再认领下一个,于是整个进程同时只能跑一个任务。表现出来
    就是:开了新会话提问,却要排在上一个会话的长任务后面,前端一直转圈。对一个要给多个
    景区、多个用户同时提供对话的平台来说这是不可接受的。

    现在改为:认领后**丢进后台任务**立即返回继续认领,并发度由信号量控制
    (runtime.max_concurrent_tasks)。信号量是必要的——不限并发的话,几十个用户同时
    提问会瞬间打满数据库连接池和模型配额,反而所有人都跑不动。

    并发度用满时不再认领新任务:让任务留在队列里由别的实例领走(水平扩容的前提),
    比领回来堆在自己进程里排队更好。
    """
    max_concurrent = int(get_settings().get("runtime", "max_concurrent_tasks", default=8))
    sem = asyncio.Semaphore(max_concurrent)
    running: set[asyncio.Task] = set()
    log.info(f"claim_loop 启动,最大并发任务数 {max_concurrent}")

    async def _run_one(task_id: str) -> None:
        """执行单个任务:租约心跳 + 执行 + 释放信号量。"""
        heartbeat_task = asyncio.create_task(_heartbeat_until_done(task_id))
        try:
            # execute_task 自己按 task_id 从 task_queue 读 scenic_id/executor_type/payload,
            # 认领循环不重复传——避免这里的副本与 DB 实际值不一致。
            await execute_task(task_id)
        except Exception:  # noqa: BLE001 - 单个任务失败不能影响其它并发任务
            log.exception(f"task {task_id} 执行失败")
        finally:
            heartbeat_task.cancel()
            sem.release()

    while True:
        try:
            # 先拿并发额度再认领:反过来的话会出现"认领了却没额度执行",
            # 任务被占住租约又不干活,只能等租约过期回收。
            await sem.acquire()
            try:
                claimed = await claim_one(_WORKER_ID)
            except Exception:
                sem.release()
                raise
            if not claimed:
                sem.release()
                await asyncio.sleep(poll_interval)
                continue

            task = asyncio.create_task(_run_one(claimed["id"]))
            # 持有强引用,否则任务可能被 GC 回收(asyncio 只保留弱引用)
            running.add(task)
            task.add_done_callback(running.discard)
        except Exception:  # noqa: BLE001 - 单次循环异常不应打断整个 worker
            log.exception("claim_loop iteration failed")
            await asyncio.sleep(poll_interval)


async def _heartbeat_until_done(task_id: str, interval: float = 30.0, stale_after: float = 90.0) -> None:
    """M3-03:租约心跳——但只在任务「有进展」时续租。

    以前是无条件续租:execute_task 哪怕挂在某个 await 上(收尾路径的 DB/Redis 调用
    不返回),心跳也照常续租,lease_until 永远在未来 → requeue_expired_leases 永远
    回收不了它 → 任务永久 RUNNING → 会话被锁死(前端光标闪、消息发不出去)。

    现在续租前检查 executor 登记的最近活跃时间(每次 publish 事件/关键步骤都会更新):
    超过 stale_after 无任何进展即判定任务卡死,停止续租让租约在 lease_seconds 内过期,
    由 lease_patrol_loop 回收为 FAILED 并广播 error,前端立刻解锁。
    """
    while True:
        await asyncio.sleep(interval)
        last = get_last_progress(task_id)
        if last is not None and (time.monotonic() - last) > stale_after:
            log.warning(f"task {task_id} 已 {stale_after:.0f}s 无执行进展,停止续租,等待租约过期回收")
            return
        try:
            await heartbeat_lease(task_id)
        except Exception:  # noqa: BLE001 - 单次续租失败(DB 抖动)不退出,下个周期重试
            log.warning(f"task {task_id} 心跳续租失败,稍后重试", exc_info=True)


async def dispatcher_loop(poll_interval: float = 2.0) -> None:
    """M7-02:Outbox 事件派发循环。"""
    while True:
        try:
            n = await dispatch_once()
            if n:
                log.debug(f"dispatched {n} events")
        except Exception:
            log.exception("dispatcher_loop iteration failed")
        await asyncio.sleep(poll_interval)


async def lease_patrol_loop(poll_interval: float = 15.0) -> None:
    """M3-05:租约巡检——回收超时租约 + 老化提升 + WAITING_HUMAN 超时兜底。"""
    while True:
        try:
            reclaimed = await requeue_expired_leases()
            promoted = await promote_aged_tasks()
            expired_human = await reap_expired_waiting_human()
            stale_streams = streaming.cleanup_idle_mem_streams()
            if reclaimed or promoted or expired_human or stale_streams:
                log.info(f"lease patrol: reclaimed={reclaimed} promoted={promoted} "
                         f"waiting_human_expired={expired_human} stale_streams_gc={stale_streams}")
        except Exception:
            log.exception("lease_patrol_loop iteration failed")
        await asyncio.sleep(poll_interval)


def start_background_loops() -> list[asyncio.Task]:
    return [
        asyncio.create_task(claim_loop()),
        asyncio.create_task(dispatcher_loop()),
        asyncio.create_task(lease_patrol_loop()),
    ]
