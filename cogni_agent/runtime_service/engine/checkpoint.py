"""
M4:LangGraph Checkpointer——HITL 中断/恢复、断线续跑靠的就是这一份持久化状态。

演进路径与 governance_event_bus_service 的 Outbox 一致:P1 单体阶段用 Postgres
(复用主库,新增 checkpoints/checkpoint_writes/checkpoint_blobs 三张表,首次启动
调用一次 setup() 建表),多进程/多 Worker 部署也天然安全(状态落库,不依赖进程内存)。
本地没有配置真实 Postgres 时(比如单元测试/离线演示),自动降级为进程内 InMemorySaver
并打印明显的告警日志,避免团队误以为已经具备跨进程恢复能力。
"""
from __future__ import annotations

import asyncio

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.checkpoint")

_saver = None
_saver_cm = None  # AsyncPostgresSaver.from_conn_string(...) 返回的 async context manager,需要一直持有
_lock = asyncio.Lock()


def _build_psycopg_dsn() -> str:
    """AsyncPostgresSaver 走 psycopg,用 URL.create 自动处理密码特殊字符转义。"""
    from sqlalchemy.engine import URL

    db = get_settings().get("database", default={})
    return URL.create(
        drivername="postgresql",
        username=db["user"],
        password=db["password"],
        host=db["host"],
        port=int(db["port"]),
        database=db["db"],
    ).render_as_string(hide_password=False)


async def get_checkpointer():
    """全局单例,应用启动时(main.py lifespan)调用一次完成初始化;
    graphs.py compile() 时直接拿这个单例用,不重复建连接。"""
    global _saver, _saver_cm
    if _saver is not None:
        return _saver
    async with _lock:
        if _saver is not None:
            return _saver
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            dsn = _build_psycopg_dsn()
            cm = AsyncPostgresSaver.from_conn_string(dsn)
            saver = await cm.__aenter__()
            await saver.setup()
            _saver_cm = cm
            _saver = saver
            log.info("checkpointer: 使用 AsyncPostgresSaver(持久化,支持跨进程/跨重启恢复)")
        except Exception:  # noqa: BLE001 - 任何原因连不上都优雅降级,不阻断服务启动
            log.exception("checkpointer: 无法初始化 Postgres 检查点存储,降级为 InMemorySaver(仅限单进程开发/演示,重启后 HITL 断点会丢失)")
            from langgraph.checkpoint.memory import InMemorySaver

            _saver = InMemorySaver()
        return _saver


async def close_checkpointer() -> None:
    global _saver, _saver_cm
    if _saver_cm is not None:
        try:
            await _saver_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            log.exception("checkpointer: 关闭连接时出错")
    _saver = None
    _saver_cm = None
