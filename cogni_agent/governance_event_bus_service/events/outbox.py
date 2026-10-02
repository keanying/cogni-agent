"""
M7-01/02:事务性发件箱(Outbox)——单体版事件总线核心。

emit_event() 必须在调用方已有的数据库事务内被 await(不要自己开新 session/commit),
这样业务写入与事件写入天然同一事务,不会出现"业务改了但事件丢了"的不一致。
真正的对外派发由 dispatcher.py 里的后台协程轮询本表完成(见 worker/loops.py)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name
from cogni_agent.governance_event_bus_service.events.envelope import make_cloudevent


class OutboxEvent(Base):
    __tablename__ = table_name("evt_outbox")

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 复用 cloudevent.id 作为主键,天然幂等
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    # 可空:集团级任务(超管/集团账号发起)没有所属景区。
    # 早期这里是 NOT NULL,导致**所有集团级账号发起对话直接 500** ——
    # 而全库其它表的 scenic_id 早就统一成"None = 集团级"了,只有这几张漏了。
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def emit_event(db: AsyncSession, event_type: str, scenic_id: str, data: dict, source: str = "app") -> None:
    """在业务事务内调用,不 commit(由调用方统一提交)。"""
    envelope = make_cloudevent(event_type, source=source, scenic_id=scenic_id, data=data)
    db.add(OutboxEvent(id=envelope["id"], event_type=event_type, scenic_id=scenic_id, payload=envelope))


async def fetch_undispatched(db: AsyncSession, limit: int = 100) -> list[OutboxEvent]:
    stmt = select(OutboxEvent).where(OutboxEvent.dispatched.is_(False)).order_by(OutboxEvent.created_at).limit(limit)
    return list((await db.execute(stmt)).scalars())
