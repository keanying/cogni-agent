"""
数据库引擎与会话(SQLAlchemy 2.0 异步)。

所有模型统一继承 Base;表命名统一带 numeri_core_ 前缀(见 app/config.TABLE_PREFIX),
通过 `table_name()` 辅助函数生成,避免各模块手写前缀导致遗漏。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from cogni_agent.config import TABLE_PREFIX, get_settings


def utcnow() -> datetime:
    """返回当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def table_name(short_name: str) -> str:
    """例如 table_name("task_queue") -> "numeri_core_task_queue"。"""
    return f"{TABLE_PREFIX}{short_name}"


class Base(DeclarativeBase):
    pass


def _build_dsn():
    """使用 URL.create 构造 DSN,自动处理密码中的特殊字符(%/@等)转义。"""
    from sqlalchemy.engine import URL

    db = get_settings().get("database", default={})
    return URL.create(
        drivername="postgresql+asyncpg",
        username=db["user"],
        password=db["password"],
        host=db["host"],
        port=int(db["port"]),
        database=db["db"],
    )


_engine = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        db = get_settings().get("database", default={})
        _engine = create_async_engine(
            _build_dsn(),
            pool_size=db.get("pool_size", 10),
            max_overflow=db.get("max_overflow", 20),
            pool_pre_ping=True,
            connect_args={
                "timeout": db.get("connect_timeout_seconds", 10),
                "command_timeout": db.get("command_timeout_seconds", 30),
            },
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _SessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入:`db: AsyncSession = Depends(get_db)`。"""
    async with get_sessionmaker()() as session:
        yield session
