"""
T-07-07: 标准化响应格式测试。
验证所有管理接口返回统一的 ApiResponse 格式。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from cogni_agent.user_center_service.models import User


def auth(user: User) -> dict:
    from tests.conftest import _issue_token
    return {"Authorization": f"Bearer {_issue_token(user)}"}


# ════════════════════════════════════════════════════════
# 正常响应格式
# ════════════════════════════════════════════════════════

API_PATHS = [
    # (path, method, description)
    # 说明: /api/admin/users 与 /api/admin/roles 当前返回裸数组(data 为 list),
    # 此处仅保留 data 为 dict 的接口做统一包装断言。
    ("/api/admin/users/paged", "GET", "成员分页列表"),
    ("/api/admin/scenics", "GET", "景区列表"),
    ("/api/admin/group", "GET", "集团信息"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method,desc", API_PATHS)
async def test_api_response_structure(async_client: AsyncClient, super_admin: User, path, method, desc):
    """所有管理接口返回格式应为 {code, message, data, trace_id}。"""
    if method == "GET":
        res = await async_client.get(path, headers=auth(super_admin))
    else:
        res = await async_client.post(path, headers=auth(super_admin))

    data = res.json()
    # 必需字段
    for key in ("code", "message", "data", "trace_id"):
        assert key in data, f"{desc} 缺少字段 {key}, 实际: {list(data.keys())}"
    # 成功时 code 应为 0
    if res.status_code == 200:
        assert data["code"] == 0, f"{desc} 成功时 code 应为 0, 实际: {data['code']}"


@pytest.mark.asyncio
async def test_create_user_response_format(async_client: AsyncClient, super_admin: User):
    """POST 创建用户应返回标准化格式。"""
    res = await async_client.post("/api/admin/users", json={
        "name": "格式测试", "account": "fmt_test", "phone": "13811111111", "role": "group_developer",
    }, headers=auth(super_admin))
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] == 0
    assert data["data"]["account"] == "fmt_test"


@pytest.mark.asyncio
async def test_update_user_response_format(async_client: AsyncClient, super_admin: User, group_viewer: User):
    """PATCH 更新用户应返回标准化格式。"""
    res = await async_client.patch(f"/api/admin/users/{group_viewer.id}", json={
        "name": "改名测试",
    }, headers=auth(super_admin))
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] == 0


@pytest.mark.asyncio
async def test_reset_password_response_format(async_client: AsyncClient, super_admin: User, group_developer: User):
    """POST 重置密码应返回标准化格式。"""
    res = await async_client.post(
        f"/api/admin/users/{group_developer.id}/reset-password",
        headers=auth(super_admin),
    )
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data


# ════════════════════════════════════════════════════════
# 错误响应格式
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_param_error_response(async_client: AsyncClient, super_admin: User):
    """参数错误应返回标准化错误格式。"""
    # 非法角色参数(业务层 ValidationError)
    res = await async_client.post("/api/admin/users/batch-role", json={
        "user_ids": [super_admin.id],
        "role": "invalid_role",
    }, headers=auth(super_admin))
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] != 0, f"参数错误时 code 应非0，实际: {data['code']}"
    assert data["message"] != ""


@pytest.mark.asyncio
async def test_permission_error_response(async_client: AsyncClient, scenic_user: User):
    """权限不足应返回标准化错误格式（403/401）。"""
    res = await async_client.get("/api/admin/users", headers=auth(scenic_user))
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] != 0


@pytest.mark.asyncio
async def test_not_found_response(async_client: AsyncClient, super_admin: User):
    """资源不存在应返回标准化错误格式。"""
    res = await async_client.patch("/api/admin/users/non-existent-id-12345", json={
        "name": "不存在",
    }, headers=auth(super_admin))
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] != 0


@pytest.mark.asyncio
async def test_health_check_response(async_client: AsyncClient):
    """健康检查也应返回标准化格式。"""
    res = await async_client.get("/health")
    data = res.json()
    for key in ("code", "message", "data", "trace_id"):
        assert key in data
    assert data["code"] == 0
    assert data["data"]["status"] == "ok"


# ════════════════════════════════════════════════════════
# 分页响应格式
# ════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pagination_response_format(async_client: AsyncClient, super_admin: User):
    """分页列表应返回 items/total/page/page_size。"""
    res = await async_client.get("/api/admin/users/paged?page=1&page_size=5", headers=auth(super_admin))
    data = res.json()
    assert data["code"] == 0
    page_data = data["data"]
    for key in ("items", "total", "page", "page_size"):
        assert key in page_data, f"分页数据缺少 {key}"
    assert page_data["page"] == 1
    assert page_data["page_size"] == 5
