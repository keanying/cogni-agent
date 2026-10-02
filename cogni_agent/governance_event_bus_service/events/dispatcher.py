"""
M7-02:派发器——轮询未派发事件,调用订阅者(审计/成本/评测回流),
失败重试 3 次后进入死信(此脚手架简化为标记 dispatched=True 并记录失败原因到日志,
生产实现应有独立 evt_dlq 表)。

演进路径:P1 订阅者是进程内函数列表;P2 把这里换成往 JetStream publish,
订阅者搬到独立进程消费,emit_event() 调用方无需任何改动。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.governance_event_bus_service.events.outbox import OutboxEvent, fetch_undispatched

log = get_logger("governance_event_bus_service.dispatcher")

Subscriber = Callable[[dict], Awaitable[None]]
_subscribers: list[Subscriber] = []


def register_subscriber(fn: Subscriber) -> None:
    """M7-04/05/06:审计、成本、评测回流等订阅者在各自模块启动时调用本函数注册。"""
    _subscribers.append(fn)


async def dispatch_once() -> int:
    async with get_sessionmaker()() as db:
        events = await fetch_undispatched(db)
        for evt in events:
            for sub in _subscribers:
                try:
                    await sub(evt.payload)
                except Exception:  # noqa: BLE001 - 派发失败不应影响其它订阅者/事件
                    log.exception(f"subscriber failed for event {evt.id}")
            evt.dispatched = True
        await db.commit()
        return len(events)
