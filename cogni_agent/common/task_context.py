"""当前正在执行的任务 id(ContextVar)。

为什么不一路传参:任务 id 要从执行引擎(executor)穿过 LangGraph 图、工具适配器、MCP 协议层,
一直传到工具实现(如 Quick BI 的运行记录)—— 中间每一层都不关心它,逐层加参数只会把签名
弄脏,漏传一层就静默变成空。ContextVar 跟着 asyncio 任务走:executor 设一次,同一个任务里
(包括 create_task 派生出去的协程)都读得到;不同任务互不串。
"""
from __future__ import annotations

from contextvars import ContextVar

_current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)


def set_current_task_id(task_id: str | None):
    """设置当前任务 id,返回 token(需要还原时传给 reset_current_task_id)。"""
    return _current_task_id.set(task_id)


def reset_current_task_id(token) -> None:
    _current_task_id.reset(token)


def current_task_id() -> str | None:
    return _current_task_id.get()
