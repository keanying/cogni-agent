"""
统一日志与链路追踪(G-LOG-01~05)。

- 全局唯一使用 loguru,禁止其它模块直接 `import logging` 打印。
- trace_id 通过 contextvars 在同一请求/任务的整个调用链中透传,
  包括跨 asyncio 协程、跨 Outbox 异步派发边界,都需显式带上 trace_id 调用
  bind_trace(trace_id) 或使用 get_logger() 自动附着当前上下文的 trace_id。
- 落盘为结构化 JSON(config.yaml: logging.serialize_json),供「系统日志」
  页面(M7-11~14)按 trace_id / 模块 / 级别过滤查询。
"""
from __future__ import annotations

import sys
import uuid
from contextvars import ContextVar

from loguru import logger as _logger

from cogni_agent.config import get_settings

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
_scenic_id_var: ContextVar[str] = ContextVar("scenic_id", default="-")


def new_trace_id() -> str:
    """网关层为每个入口请求生成一个 trace_id(W3C traceparent 兼容的 32 位 hex)。"""
    return uuid.uuid4().hex


def set_trace_context(trace_id: str, scenic_id: str | None = None) -> None:
    _trace_id_var.set(trace_id)
    if scenic_id:
        _scenic_id_var.set(scenic_id)


def get_trace_id() -> str:
    return _trace_id_var.get()


def _patch_record(record):
    record["extra"].setdefault("trace_id", _trace_id_var.get())
    record["extra"].setdefault("scenic_id", _scenic_id_var.get())
    return record


def configure_logging() -> None:
    """应用启动时调用一次(见 app/main.py)。日志级别取自 config.yaml 的 logging.level。"""
    settings = get_settings()
    log_cfg = settings.get("logging", default={}) or {}
    level = log_cfg.get("level", "INFO")
    sink_dir = log_cfg.get("sink_dir", "./logs")
    rotation = log_cfg.get("rotation", "00:00")
    retention = log_cfg.get("retention", "30 days")
    serialize = log_cfg.get("serialize_json", True)

    _logger.remove()
    _logger.configure(patcher=_patch_record)

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        "trace_id={extra[trace_id]} scenic_id={extra[scenic_id]} | "
        "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
    )
    _logger.add(sys.stdout, level=level, format=fmt, colorize=True, serialize=False)
    _logger.add(
        f"{sink_dir}/zhilv-{{time:YYYY-MM-DD}}.log",
        level=level,
        rotation=rotation,
        retention=retention,
        serialize=serialize,  # JSON 结构化,供系统日志页面索引服务消费
        enqueue=True,          # 多协程/多进程安全写入
    )


def get_logger(module: str):
    """各模块入口调用:`log = get_logger("runtime_service.engine")`。"""
    return _logger.bind(module=module)
