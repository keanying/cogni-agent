"""M7-03:事件 Schema 注册表(占位)。真实实现应维护 event_type -> JSON Schema 映射,
新增/修改事件结构时做向后兼容校验,校验逻辑此处留 TODO。"""
from __future__ import annotations

_REGISTRY: dict[str, dict] = {}


def register_schema(event_type: str, schema: dict, version: str = "v1") -> None:
    _REGISTRY[f"{event_type}@{version}"] = schema


def check_backward_compatible(event_type: str, new_schema: dict) -> bool:
    # TODO: 实现真正的 JSON Schema 兼容性比对(新增可选字段允许,删除/改类型不允许)
    return True
