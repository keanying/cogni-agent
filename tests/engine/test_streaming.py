"""
§1.5 streaming 集成测试。

覆盖: SSE 事件发布/订阅、时间线条目、事件顺序、
终端事件、断线续传(last_id)。

所有测试强制使用内存后端。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cogni_agent.runtime_service.streaming import (
    publish, subscribe, close_stream, TERMINAL_EVENTS, cleanup_idle_mem_streams,
    _mem_streams, _subscribe_redis,
)


@pytest.fixture(autouse=True)
def _clean_and_force_mem():
    """每个测试前清空内存流缓冲区,强制禁用 Redis。"""
    _mem_streams.clear()
    with patch("cogni_agent.runtime_service.streaming._redis_enabled", return_value=False):
        yield
    _mem_streams.clear()


# ── SSE 事件发布/订阅 ─────────────────────────────────────────────────────────

class TestSSEPublishSubscribe:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe_basic(self):
        await publish("task-1", "token", {"text": "你好"})
        await close_stream("task-1")

        events = []
        async for event_id, event_name, data in subscribe("task-1"):
            events.append((event_id, event_name, data))

        assert len(events) >= 1
        assert events[0][1] == "token"
        assert events[0][2]["text"] == "你好"

    @pytest.mark.asyncio
    async def test_done_event_terminates_subscription(self):
        await publish("task-2", "timeline", {"step": "agent", "status": "started"})
        await publish("task-2", "done", {"result": "ok"})

        events = []
        async for eid, ename, data in subscribe("task-2"):
            events.append((eid, ename, data))

        assert len(events) == 2
        assert events[0][1] == "timeline"
        assert events[1][1] == "done"

    @pytest.mark.asyncio
    async def test_error_event_terminates(self):
        await publish("task-3", "error", {"message": "LLM timeout"})
        events = []
        async for eid, ename, _data in subscribe("task-3"):
            events.append((eid, ename))
        assert len(events) == 1
        assert events[0][1] == "error"

    @pytest.mark.asyncio
    async def test_multiple_events_ordered_by_id(self):
        for i in range(5):
            await publish("task-4", "token", {"index": i})
        await publish("task-4", "done", {})

        ids = []
        async for eid, _, _data in subscribe("task-4"):
            ids.append(eid)
        assert len(ids) == 6
        assert int(ids[0]) < int(ids[1]) < int(ids[-1])


# ── 时间线条目 ────────────────────────────────────────────────────────────────

class TestTimelineEntries:
    @pytest.mark.asyncio
    async def test_timeline_preserves_structure(self):
        await publish("task-tl", "timeline", {"step": "resolve", "skill": "客流查询", "duration_ms": 45})
        await publish("task-tl", "done", {})

        events = []
        async for _eid, ename, data in subscribe("task-tl"):
            events.append((ename, data))

        tl = events[0]
        assert tl[0] == "timeline"
        assert tl[1]["step"] == "resolve"
        assert tl[1]["skill"] == "客流查询"


# ── usage 事件 ────────────────────────────────────────────────────────────────

class TestUsageEvents:
    @pytest.mark.asyncio
    async def test_usage_event_contains_tokens(self):
        await publish("task-usage", "usage", {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        await publish("task-usage", "done", {})

        events = []
        async for _eid, ename, data in subscribe("task-usage"):
            events.append((ename, data))
        assert events[0][0] == "usage"
        assert events[0][1]["prompt_tokens"] == 100


# ── HITL 事件 ─────────────────────────────────────────────────────────────────

class TestHITLEvent:
    @pytest.mark.asyncio
    async def test_waiting_human_event(self):
        await publish("task-hitl", "waiting_human", {
            "interrupt_id": "i1", "tool": "高危发布", "message": "是否确认发布?",
        })

        events = []
        async for eid, ename, data in subscribe("task-hitl"):
            events.append((eid, ename, data))

        assert len(events) == 1
        assert events[0][1] == "waiting_human"
        assert events[0][2]["tool"] == "高危发布"


# ── 断线续传 ──────────────────────────────────────────────────────────────────

class TestReconnection:
    @pytest.mark.asyncio
    async def test_last_id_resumes_from_after(self):
        await publish("task-rc", "token", {"text": "一"})
        await publish("task-rc", "token", {"text": "二"})
        await publish("task-rc", "done", {})

        events = []
        async for eid, ename, data in subscribe("task-rc", last_id="1"):
            events.append((eid, ename, data.get("text")))

        assert len(events) == 2
        assert events[0][1] == "token"
        assert events[0][2] == "二"

    @pytest.mark.asyncio
    async def test_last_id_none_from_start(self):
        await publish("task-rc2", "token", {"text": "first"})
        await publish("task-rc2", "done", {})

        events = []
        async for eid, _, data in subscribe("task-rc2", last_id=None):
            events.append((eid, data.get("text")))
        assert len(events) == 2


# ── close_stream 兜底 ─────────────────────────────────────────────────────────

class TestCloseStream:
    @pytest.mark.asyncio
    async def test_close_stream_wakes_subscriber(self):
        await publish("task-cs", "token", {"text": "hei"})

        events = []
        task = asyncio.create_task(_collect_events("task-cs", events))
        await asyncio.sleep(0.2)
        await close_stream("task-cs")
        await asyncio.wait_for(task, timeout=5)
        assert len(events) >= 1


async def _collect_events(task_id: str, out: list):
    async for eid, ename, data in subscribe(task_id, timeout_seconds=3):
        out.append((eid, ename))


# ── Redis xread 返回格式兼容(redis-py 4.x list vs 5.x+/8.x dict) ─────────────

class TestRedisXreadCompat:
    @pytest.mark.asyncio
    async def test_redis8_dict_format(self):
        """redis-py 5.x+/8.x:单 stream dict 入参返回 {stream: [[(id, fields)]]}。"""
        fake = type("FakeRedis", (), {"xread": AsyncMock(side_effect=[
            {"zhilv:stream:task:t8": [[("100-0", {"event": "token", "data": '{"text":"hi"}'})]]},
            {"zhilv:stream:task:t8": [[("101-0", {"event": "done", "data": "{}"})]]},
        ])})()
        with patch("cogni_agent.runtime_service.streaming._get_redis", return_value=fake):
            events = []
            async for eid, ename, data in _subscribe_redis("t8", None, 5):
                events.append((eid, ename, data))
        assert events == [("100-0", "token", {"text": "hi"}), ("101-0", "done", {})]

    @pytest.mark.asyncio
    async def test_redis4_list_format(self):
        """redis-py 4.x:返回 [(stream, [(id, fields)])] list 格式,仍兼容。"""
        fake = type("FakeRedis", (), {"xread": AsyncMock(side_effect=[
            [("zhilv:stream:task:t4", [("100-0", {"event": "token", "data": '{"text":"yo"}'})])],
            [("zhilv:stream:task:t4", [("101-0", {"event": "done", "data": "{}"})])],
        ])})()
        with patch("cogni_agent.runtime_service.streaming._get_redis", return_value=fake):
            events = []
            async for eid, ename, data in _subscribe_redis("t4", None, 5):
                events.append((eid, ename, data))
        assert events == [("100-0", "token", {"text": "yo"}), ("101-0", "done", {})]
