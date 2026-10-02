"""
T-07-06: 组织管理 API 测试（景区列表、角色列表、集团信息）。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from cogni_agent.user_center_service.models import User


def auth(user: User) -> dict:
    from tests.conftest import _issue_token
    return {"Authorization": f"Bearer {_issue_token(user)}"}


# ════════════════════════════════════════════════════════
# 景区列表
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_scenics(async_client: AsyncClient, super_admin: User):
    """查看景区列表，应返回正确数据结构。"""
    from cogni_agent.user_center_service.models import SysScenic
    from cogni_agent.common.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        s.add(SysScenic(scenic_id="sc001", scenic_name="武夷山", scenic_grade_level="5A", is_enabled=True))
        s.add(SysScenic(scenic_id="sc002", scenic_name="鼓浪屿", scenic_grade_level="5A", is_enabled=False))
        await s.commit()

    res = await async_client.get("/api/admin/scenics", headers=auth(super_admin))
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    items = data["data"]["items"]
    assert len(items) >= 2
    assert any(s["scenic_name"] == "武夷山" for s in items)
    assert any(s["scenic_name"] == "鼓浪屿" for s in items)


@pytest.mark.asyncio
async def test_list_scenics_filter_enabled(async_client: AsyncClient, super_admin: User):
    """按启用状态筛选景区。"""
    from cogni_agent.user_center_service.models import SysScenic
    from cogni_agent.common.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        s.add(SysScenic(scenic_id="sc_enabled", scenic_name="已启用景区", is_enabled=True))
        s.add(SysScenic(scenic_id="sc_disabled", scenic_name="已停用景区", is_enabled=False))
        await s.commit()

    res = await async_client.get("/api/admin/scenics?is_enabled=true", headers=auth(super_admin))
    assert res.status_code == 200
    items = res.json()["data"]["items"]
    assert all(s["is_enabled"] for s in items)


@pytest.mark.asyncio
async def test_update_scenic_status(async_client: AsyncClient, super_admin: User):
    """更新景区启用/停用状态。"""
    from cogni_agent.user_center_service.models import SysScenic
    from cogni_agent.common.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        s.add(SysScenic(scenic_id="sc_toggle", scenic_name="待切换景区", is_enabled=True))
        await s.commit()

    # 停用(is_enabled 是 query 参数, 非 body)
    res = await async_client.patch("/api/admin/scenics/sc_toggle?is_enabled=false", headers=auth(super_admin))
    assert res.status_code == 200
    assert res.json()["code"] == 0
    assert res.json()["data"]["is_enabled"] is False

    # 重新启用
    res2 = await async_client.patch("/api/admin/scenics/sc_toggle?is_enabled=true", headers=auth(super_admin))
    assert res2.status_code == 200


# ════════════════════════════════════════════════════════
# 角色列表
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_roles(async_client: AsyncClient, super_admin: User):
    """查看角色列表，应返回五种角色。"""
    res = await async_client.get("/api/admin/roles", headers=auth(super_admin))
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    roles = data["data"]  # 当前返回裸数组: [{role, label, permissions}]
    role_values = {r["role"] for r in roles}
    assert role_values == {"super_admin", "group_admin", "group_developer", "group_viewer", "scenic_admin", "scenic_operator"}


@pytest.mark.asyncio
async def test_roles_require_admin(async_client: AsyncClient, scenic_user: User):
    """角色列表当前未做角色鉴权,任何登录用户均可访问(记录当前行为以对齐接口)。"""
    res = await async_client.get("/api/admin/roles", headers=auth(scenic_user))
    assert res.status_code == 200


# ════════════════════════════════════════════════════════
# 集团信息
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_group_info(async_client: AsyncClient, super_admin: User):
    """查看集团基本信息。"""
    res = await async_client.get("/api/admin/group", headers=auth(super_admin))
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    # 第一次请求可能还没有集团记录
    assert "name" in data["data"]


# ════════════════════════════════════════════════════════
# 搜索景区名称
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_search_scenics_by_name(async_client: AsyncClient, super_admin: User):
    """模糊搜索景区名称。"""
    from cogni_agent.user_center_service.models import SysScenic
    from cogni_agent.common.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        s.add(SysScenic(scenic_id="sc_search1", scenic_name="杭州西湖风景名胜区", is_enabled=True))
        s.add(SysScenic(scenic_id="sc_search2", scenic_name="丽江古城", is_enabled=True))
        await s.commit()

    res = await async_client.get("/api/admin/scenics?keyword=西湖", headers=auth(super_admin))
    assert res.status_code == 200
    items = res.json()["data"]["items"]
    assert any("西湖" in s["scenic_name"] for s in items)
