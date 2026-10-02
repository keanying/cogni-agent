"""Casbin 策略测试——断言的是 README 第三章那张角色矩阵。"""
from __future__ import annotations

import pytest

from cogni_agent.governance_event_bus_service import policy


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    """策略存在数据库里,所以这组测试需要真实 Postgres。

    其余测试都刻意设计成不依赖 DB(conftest 里灌的是 dummy 连接串),所以这里
    连不上时**跳过而不是失败**——否则在没起数据库的机器上跑 `pytest tests/`
    会红一片,让人误以为代码坏了。真实校验由 CI / 手动集成校验覆盖。
    """
    try:
        policy.seed_policies()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"需要真实 Postgres 才能验证 Casbin 策略(当前不可用:{type(exc).__name__})")


def test_super_admin_can_everything():
    assert policy._sync_can("super_admin", "system:settings", "write")
    assert policy._sync_can("super_admin", "tool:anything", "delete")


def test_group_developer_has_no_system_center():
    """README 第三章明确:group_developer「1~5模块全权限,系统中心不可见」。"""
    assert policy._sync_can("group_developer", "tool:x", "write")
    assert policy._sync_can("group_developer", "agent:x", "publish")
    assert not policy._sync_can("group_developer", "system:settings", "read")
    assert not policy._sync_can("group_developer", "system:settings", "write")


def test_group_viewer_is_read_only():
    assert policy._sync_can("group_viewer", "tool:x", "read")
    assert not policy._sync_can("group_viewer", "tool:x", "write")
    assert not policy._sync_can("group_viewer", "tool:x", "delete")


def test_scenic_operator_cannot_approve():
    """审批是景区管理员及以上的权限,操作员只能发起。"""
    assert policy._sync_can("scenic_operator", "console", "execute")
    assert not policy._sync_can("scenic_operator", "audit:approval", "approve")
    assert policy._sync_can("scenic_admin", "audit:approval", "approve")


def test_role_inheritance_works():
    """继承链:super_admin > group_admin > group_developer > group_viewer。
    只给最低角色加权限,上层应自动获得——否则加权限时容易漏给上层角色。"""
    assert policy._sync_can("group_viewer", "dashboard", "read")
    for role in ("group_developer", "group_admin", "super_admin"):
        assert policy._sync_can(role, "dashboard", "read"), f"{role} 应继承 group_viewer 的权限"


def test_unknown_role_denied():
    assert not policy._sync_can("no_such_role", "tool:x", "read")
    assert not policy._sync_can("", "tool:x", "read")


@pytest.mark.asyncio
async def test_permissions_payload_shape():
    p = await policy.permissions_for("group_developer")
    assert p["role"] == "group_developer"
    assert p["menus"]["tools"] is True
    assert p["menus"]["system"] is False
    assert p["capabilities"]["tool.write"] is True
    assert p["capabilities"]["system.write"] is False


def test_seed_is_idempotent():
    """启动时会调用,重复灌入不能产生重复策略。"""
    before = policy.seed_policies()["total_policies"]
    after = policy.seed_policies()
    assert after["policies_added"] == 0
    assert after["total_policies"] == before
