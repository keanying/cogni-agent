"""
Phase 4.2 引擎集成测试

验证引擎核心链路：流生命周期、状态机转换、异常恢复、全链路冒烟。
所有流测试使用内存后端（mock Redis），避免外部依赖。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogni_agent.runtime_service.streaming import (
    publish, subscribe, close_stream, TERMINAL_EVENTS, _mem_streams,
)


# ═══════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _force_memory_backend():
    """每个测试清空内存流，强制禁用 Redis。"""
    _mem_streams.clear()
    with patch("cogni_agent.runtime_service.streaming._redis_enabled", return_value=False):
        yield
    _mem_streams.clear()


def _mock_task(task_id: str | None = None, **overrides):
    """快速构造 mock Task 对象。"""
    tid = task_id or str(uuid.uuid4())
    task = MagicMock()
    task.id = tid
    task.title = overrides.get("title", "test-task")
    task.goal = overrides.get("goal", "hello, agent")
    task.status = overrides.get("status", "PENDING")
    task.executor_type = overrides.get("executor_type", "agent")
    task.executor_id = overrides.get("executor_id", "agent-1")
    task.scenic_id = overrides.get("scenic_id", "scenic-001")
    task.user_id = overrides.get("user_id", "user-001")
    task.conversation_id = overrides.get("conversation_id", "conv-001")
    task.model = overrides.get("model", "test-model")
    task.context = overrides.get("context", {})
    task.result_summary = overrides.get("result_summary", "")
    task.error = overrides.get("error", None)
    task.retry_count = overrides.get("retry_count", 0)
    task.replica_count = overrides.get("replica_count", 0)
    task.created_at = overrides.get("created_at", datetime.now(timezone.utc))
    task.updated_at = overrides.get("updated_at", datetime.now(timezone.utc))
    return task


def _mock_session(task: MagicMock | None = None):
    """构造 mock DB session（async context manager）。"""
    sess = AsyncMock()
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    sess.get = AsyncMock(return_value=task)
    sess.commit = AsyncMock()
    sess.refresh = AsyncMock()
    sess.add = MagicMock()
    return sess


# ═══════════════════════════════════════════════════════════
#  4.2.1  流生命周期集成
# ═══════════════════════════════════════════════════════════

class TestStreamingLifecycle:
    """publish → subscribe 完整生命周期。"""

    @pytest.mark.asyncio
    async def test_publish_subscribe_full_chain(self):
        """发布 3 个事件 → 订阅收到完整序列。"""
        task_id = "sse-lifecycle-1"

        await publish(task_id, "start", {"msg": "begin"})
        await publish(task_id, "stage", {"stage": "running"})
        await publish(task_id, "done", {"status": "COMPLETED"})
        await close_stream(task_id)

        events = []
        async for _eid, event, _data in subscribe(task_id):
            events.append(event)

        assert events == ["start", "stage", "done"], f"事件序列: {events}"

    @pytest.mark.asyncio
    async def test_last_event_id_replay(self):
        """Last-Event-ID 断线重连：只重放之后的事件。"""
        task_id = "sse-replay-1"

        await publish(task_id, "start", {"seq": 1})
        mid_seq = await publish(task_id, "stage", {"seq": 2})
        await publish(task_id, "done", {"seq": 3})
        await close_stream(task_id)
        await publish(task_id, "done", {"seq": 3})
        await close_stream(task_id)

        events = []
        async for _eid, event, _data in subscribe(task_id, last_id=mid_seq):
            events.append(event)

        assert events == ["done"], f"应只重放 done, 实际: {events}"

    @pytest.mark.asyncio
    async def test_concurrent_isolation(self):
        """两个 task 并发写入 → 各自收到正确事件。"""
        t1, t2 = "iso-a", "iso-b"

        await publish(t1, "start", {"task": t1})
        await publish(t2, "start", {"task": t2})
        await publish(t1, "done", {"task": t1})
        await publish(t2, "done", {"task": t2})
        await close_stream(t1)
        await close_stream(t2)

        e1 = [d["task"] async for _, e, d in subscribe(t1) if e == "start"]
        e2 = [d["task"] async for _, e, d in subscribe(t2) if e == "start"]
        assert e1 == [t1], f"task1 隔离: {e1}"
        assert e2 == [t2], f"task2 隔离: {e2}"

    @pytest.mark.asyncio
    async def test_high_volume_no_loss(self):
        """100 个事件无丢失。"""
        task_id = "sse-volume-1"
        N = 100
        for i in range(N):
            await publish(task_id, "stage", {"step": i})
        await publish(task_id, "done", {"status": "OK"})
        await close_stream(task_id)

        count = 0
        async for _ in subscribe(task_id):
            count += 1
        assert count == N + 1

    @pytest.mark.asyncio
    async def test_terminal_event_ends_subscription(self):
        """done 后订阅立即结束。"""
        task_id = "sse-done-1"
        await publish(task_id, "done", {"status": "OK"})
        await close_stream(task_id)

        events = []
        async for _eid, event, _data in subscribe(task_id):
            events.append(event)
        assert events == ["done"]


# ═══════════════════════════════════════════════════════════
#  4.2.2  异常恢复集成
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  4.2.3  全链路冒烟测试
# ═══════════════════════════════════════════════════════════

class TestSmokeFullPipeline:
    """冒烟测试：验证 execute_task 的关键不变量（不崩溃，流最终关闭）。"""

    @pytest.mark.asyncio
    async def test_smoke_task_not_found(self):
        """不存在的任务 → 返回 True，不崩溃。"""
        sess = _mock_session(task=None)

        from cogni_agent.runtime_service.engine import executor as ex
        with (
            patch.object(ex, "get_sessionmaker") as m_sm,
            patch.object(ex, "streaming") as m_stream,
        ):
            m_sm.return_value.return_value = sess
            m_stream.publish = AsyncMock()
            m_stream.close_stream = AsyncMock()

            result = await ex.execute_task("nonexistent-123")
            assert result is True

    @pytest.mark.asyncio
    async def test_smoke_resolve_error_handled(self):
        """resolve 失败 → execute_task 返回 True，流关闭。"""
        task_id = str(uuid.uuid4())
        task = _mock_task(task_id, executor_id=None, status="PENDING")
        sess = _mock_session(task)

        from cogni_agent.runtime_service.engine import executor as ex
        with (
            patch.object(ex, "get_sessionmaker") as m_sm,
            patch.object(ex, "streaming") as m_stream,
        ):
            m_sm.return_value.return_value = sess
            m_stream.publish = AsyncMock()
            m_stream.close_stream = AsyncMock()
            m_stream.STAGE_UNDERSTANDING = "understanding"

            result = await ex.execute_task(task_id)
            assert result is True
            assert m_stream.close_stream.call_count >= 1 or m_stream.close_stream.await_count >= 1

    @pytest.mark.asyncio
    async def test_smoke_inner_exception_wrapped(self):
        """_execute_task_inner 异常 → execute_task 包装后仍返回 True，流关闭。"""
        task_id = str(uuid.uuid4())

        from cogni_agent.runtime_service.engine import executor as ex
        with (
            patch.object(ex, "_execute_task_inner") as m_inner,
            patch.object(ex, "streaming") as m_stream,
        ):
            m_inner.side_effect = RuntimeError("模拟内部崩溃")
            m_stream.publish = AsyncMock()
            m_stream.close_stream = AsyncMock()

            result = await ex.execute_task(task_id)
            assert result is True
            # 验证 error 事件已发布
            error_events = [
                c for c in m_stream.publish.call_args_list
                if len(c.args) >= 2 and "error" in str(c.args[1])
            ]
            assert len(error_events) >= 1
            # 验证流已关闭
            assert m_stream.close_stream.call_count >= 1 or m_stream.close_stream.await_count >= 1
