"""
多账号绑定同一景区:任务与成本按账号归因(P0+P1+P2)回归测试。

背景:create_task 原本只收 scenic_id,账号信息靠 payload 携带,且 4 个调用方
传参不一(APP 端甚至不传 user_id,私有资源在 APP 端退化为全景区可见)。
同一景区被多个账号绑定时,任务/成本无法按账号归因。
改动:Task / LlmUsage 新增 user_id 列;create_task 增加 user_id 参数
(未显式传则从 payload.user_id 兜底);usage 事件带 user_id;cost_subscriber 落库。
"""
from __future__ import annotations

import pytest


# ── 结构:两张表都有 user_id 列且可空(兼容历史数据) ──
def test_task_queue_has_user_id():
    from cogni_agent.control_plane_service.models import Task

    col = Task.__table__.columns["user_id"]
    assert col.nullable, "task_queue.user_id 必须可空,历史任务没有账号信息"


def test_llm_usage_has_user_id():
    from cogni_agent.governance_event_bus_service.cost import LlmUsage

    col = LlmUsage.__table__.columns["user_id"]
    assert col.nullable, "gov_llm_usage.user_id 必须可空,历史用量没有账号信息"


def test_schema_migration_0018_registered():
    """建表/加列走 common.schema.MIGRATIONS,0018 必须注册且幂等。"""
    from cogni_agent.common.schema import MIGRATIONS

    assert "0018_task_and_usage_user_id" in [m.id for m in MIGRATIONS]


# ── 行为:create_task 账号兜底优先级 ──
class _FakeDB:
    """只实现 create_task / cost_subscriber 用到的 db 接口,不真连库。"""

    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        # 订阅方新增的日汇总 UPSERT(pg_insert)与单价表加载在 fake 会话里不真正执行;
        # 返回 None 即可,调用方要么不接收、要么被内部 try/except 兜底。
        return None

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_sessionmaker(monkeypatch):
    """把 create_task 里的 get_sessionmaker 换成假会话,捕获 Task 实例。

    queue.py 是模块级 import,monkeypatch queue.get_sessionmaker 即可;
    cost.py 是函数内 import(调用时才解析),要 monkeypatch common.db.get_sessionmaker。
    """
    captured = {"db": None}

    def _factory():
        if captured["db"] is None:
            captured["db"] = _FakeDB()
        return lambda: captured["db"]  # get_sessionmaker()() 双重调用

    monkeypatch.setattr("cogni_agent.control_plane_service.queue.get_sessionmaker", _factory)
    monkeypatch.setattr("cogni_agent.common.db.get_sessionmaker", _factory)
    return captured


async def test_create_task_stores_explicit_user_id(fake_sessionmaker):
    from cogni_agent.control_plane_service.queue import create_task

    await create_task("scenic_a", "internal", {"message": "hi"}, user_id="u_abc")
    task = fake_sessionmaker["db"].added[0]
    assert task.user_id == "u_abc"
    # 同步回 payload,executor 执行时从 payload 取账号做权限校验
    assert task.payload["user_id"] == "u_abc"


async def test_create_task_falls_back_to_payload_user_id(fake_sessionmaker):
    from cogni_agent.control_plane_service.queue import create_task

    await create_task("scenic_a", "internal", {"message": "hi", "user_id": "u_payload"})
    task = fake_sessionmaker["db"].added[0]
    assert task.user_id == "u_payload"


async def test_create_task_no_user_id_stays_none(fake_sessionmaker):
    from cogni_agent.control_plane_service.queue import create_task

    await create_task("scenic_a", "internal", {"message": "hi"})
    task = fake_sessionmaker["db"].added[0]
    assert task.user_id is None
    assert "user_id" not in task.payload


async def test_create_task_does_not_mutate_caller_payload(fake_sessionmaker):
    """显式传 user_id 时 payload 被浅拷贝,调用方的 dict 不被回写。"""
    from cogni_agent.control_plane_service.queue import create_task

    payload = {"message": "hi"}
    await create_task("scenic_a", "internal", payload, user_id="u_x")
    assert "user_id" not in payload


# ── 行为:usage 事件 → LlmUsage 落库带 user_id ──
async def test_cost_subscriber_persists_user_id(fake_sessionmaker):
    from cogni_agent.governance_event_bus_service import cost as cost_mod

    await cost_mod.cost_subscriber({
        "type": "com.zhilv.llm.usage",
        "data": {"scenic_id": "scenic_a", "user_id": "u_abc", "task_id": "t_1",
                 "model": "test", "prompt_tokens": 10, "completion_tokens": 5},
    })
    usage = fake_sessionmaker["db"].added[0]
    assert usage.user_id == "u_abc"
    assert usage.scenic_id == "scenic_a"
    assert usage.task_id == "t_1"
