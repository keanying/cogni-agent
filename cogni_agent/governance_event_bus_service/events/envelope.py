"""G-API-01/M7-03:CloudEvents 1.0 信封构造。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from cogni_agent.common.logging import get_trace_id


def make_cloudevent(event_type: str, source: str, scenic_id: str, data: dict, dataschema: str | None = None) -> dict:
    """
    统一事件信封,字段含义见 README「事件模型」:
    - id: 幂等键(消费端用它去重)
    - traceparent: 复用当前请求/任务的 trace_id,贯穿全链路
    - tenantid: 实际存 scenic_id(即 scenic_id)
    """
    return {
        "specversion": "1.0",
        "id": f"evt_{uuid.uuid4().hex}",
        "source": source,
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        "traceparent": get_trace_id(),
        "tenantid": scenic_id,
        "dataschema": dataschema,
        "data": data,
    }
