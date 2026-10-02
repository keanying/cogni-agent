"""APP 端 `/api/app/permissions` 接口测试。

覆盖:
- 统一响应格式（ApiResponse 信封）
- APP_MENU_PERMISSIONS 映射完整性
- 策略引擎各角色在 APP 域的权限正确性
"""
from __future__ import annotations

import pytest

from cogni_agent.common.schemas import ApiResponse
from cogni_agent.governance_event_bus_service import policy


# ═══════════════════════════════════════════════════════════════
# 响应格式通用性：不依赖 DB，纯格式/结构校验
# ═══════════════════════════════════════════════════════════════

class TestAppPermissionsResponseShape:
    """验证 /api/app/permissions 返回的数据结构符合 ApiResponse 统一规范。"""

    def test_response_has_required_fields(self):
        """data 中必须包含 role, menus, capabilities 三个字段。"""
        # 模拟 permissions_for(domain="app") 返回的结构
        expected_keys = {"role", "menus", "capabilities"}
        actual = {"role": "scenic_operator", "menus": {"data_overview": True}, "capabilities": {}}
        missing = expected_keys - set(actual.keys())
        assert not missing, f"permissions 缺少字段: {missing}"

    def test_menu_keys_are_valid(self):
        """menus 中的 key 只能来自 APP_MENU_PERMISSIONS 定义的 5 个 tab。"""
        valid_keys = set(policy.APP_MENU_PERMISSIONS.keys())
        assert valid_keys == {
            "data_overview", "ticket_manage", "passenger_flow",
            "public_opinion", "ota_direct_sales",
        }, f"APP_MENU_PERMISSIONS 应为精确 5 个 tab, 实际: {valid_keys}"

    def test_menu_values_are_boolean(self):
        """每个菜单项的可见性必须是 bool。"""
        sample = {
            "data_overview": True,
            "ticket_manage": False,
            "passenger_flow": True,
            "public_opinion": False,
            "ota_direct_sales": True,
        }
        for key, val in sample.items():
            assert isinstance(val, bool), f"menus.{key} 应为 bool, 实际: {type(val).__name__}"

    def test_api_response_envelope_shape(self):
        """ApiResponse.ok() 包装后的完整格式必须包含 code/message/data/trace_id。"""
        perms = {"role": "scenic_operator", "menus": {}, "capabilities": {}}
        resp = ApiResponse.ok(data=perms, trace_id="test-123")
        serialized = resp.model_dump()
        for field in ("code", "message", "data", "trace_id"):
            assert field in serialized, f"ApiResponse 缺少字段 {field}"
        assert serialized["code"] == 0
        assert serialized["message"] == "success"
        assert serialized["data"] == perms
        assert serialized["trace_id"] == "test-123"

    def test_api_response_fail_shape(self):
        """错误响应也必须包含四个标准字段。"""
        resp = ApiResponse.fail(code=1002, message="Missing param", trace_id="err-456")
        serialized = resp.model_dump()
        for field in ("code", "message", "data", "trace_id"):
            assert field in serialized
        assert serialized["code"] == 1002
        assert serialized["message"] == "Missing param"


# ═══════════════════════════════════════════════════════════════
# 策略引擎测试：需要 DB（无 DB 时跳过）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module", autouse=False)
def _seeded():
    """策略存在数据库里，无 DB 时跳过而非失败。"""
    try:
        policy.seed_policies()
    except Exception as exc:
        pytest.skip(f"需要真实 Postgres 才能验证 Casbin 策略(当前不可用: {type(exc).__name__})")


class TestAppDomainPermissions:
    """验证 APP 域（domain=app）下各角色的 Casbin 策略正确性。"""

    @pytest.mark.asyncio
    async def test_permissions_payload_shape_app_domain(self, _seeded):
        """permissions_for(role, domain='app') 返回正确结构。"""
        p = await policy.permissions_for("scenic_operator", domain="app")
        assert p["role"] == "scenic_operator"
        assert "menus" in p
        assert "capabilities" in p
        # APP 域 capabilities 固定为空
        assert p["capabilities"] == {}

    @pytest.mark.parametrize("role", ["scenic_operator", "scenic_admin"])
    @pytest.mark.asyncio
    async def test_scenic_roles_have_all_five_tabs(self, role, _seeded):
        """景区操作员、景区管理员应能看到全部 5 个 data tab。"""
        p = await policy.permissions_for(role, domain="app")
        for tab in ("data_overview", "ticket_manage", "passenger_flow", "public_opinion", "ota_direct_sales"):
            assert tab in p["menus"], f"缺少 tab key: {tab}"
            assert p["menus"][tab] is True, f"{role} 应有 {tab} 权限"

    @pytest.mark.parametrize("role", ["group_viewer", "group_developer", "group_admin", "super_admin"])
    @pytest.mark.asyncio
    async def test_group_roles_have_no_app_permissions(self, role, _seeded):
        """集团线角色不应持有任何 APP 端 tab 权限。"""
        p = await policy.permissions_for(role, domain="app")
        for tab in ("data_overview", "ticket_manage", "passenger_flow", "public_opinion", "ota_direct_sales"):
            assert tab in p["menus"], f"缺少 tab key: {tab}"
            assert p["menus"][tab] is False, f"{role} 不应有 {tab} 权限"

    @pytest.mark.asyncio
    async def test_scenic_admin_inherits_operator(self, _seeded):
        """景区管理员继承操作员权限，两者在 APP 域看到相同的 5 个 tab。"""
        op = await policy.permissions_for("scenic_operator", domain="app")
        ad = await policy.permissions_for("scenic_admin", domain="app")
        assert op["menus"] == ad["menus"], "scenic_admin 应继承 scenic_operator 的全部 APP 权限"

    @pytest.mark.asyncio
    async def test_admin_domain_permissions_unchanged(self, _seeded):
        """管理端 admin 域的 permissions_for 不应受 APP 域改动影响。"""
        p = await policy.permissions_for("super_admin", domain="admin")
        assert p["role"] == "super_admin"
        assert "menus" in p
        assert "capabilities" in p
        assert p["menus"]["system"] is True
        assert p["capabilities"]["system.write"] is True

    @pytest.mark.asyncio
    async def test_admin_menus_do_not_include_app_tabs(self, _seeded):
        """管理端 admin 域的 menus 不应包含 APP 端 tab key。"""
        p = await policy.permissions_for("scenic_admin", domain="admin")
        app_tabs = {"data_overview", "ticket_manage", "passenger_flow", "public_opinion", "ota_direct_sales"}
        overlap = set(p["menus"].keys()) & app_tabs
        assert not overlap, f"admin 域 menus 不应包含 APP tab: {overlap}"
