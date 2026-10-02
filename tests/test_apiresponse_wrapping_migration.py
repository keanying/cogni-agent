"""
ApiResponse 包裹统一化改造 · 回归测试

目标：验证 routes_app.py 和 routes_api_keys.py 去掉手动 ApiResponse 包裹、
加上中间件自动包裹后，对外响应结构与改造前完全一致。

用法：
  # 改造前:对现有接口做快照验证
  pytest tests/test_apiresponse_wrapping_migration.py -v -k "test_snapshot"

  # 改造后:重新运行，确保响应结构不变
  pytest tests/test_apiresponse_wrapping_migration.py -v

前置条件：需要服务已在运行（仅 local 环境手动运行，dev/prod 不执行）。
账号密码不硬编码，通过环境变量注入（未配置时自动跳过）：
  COGNI_AGENT_BASE_URL=http://localhost:8000    # 默认
  COGNI_AGENT_ACCOUNT=<账号>
  COGNI_AGENT_PASSWORD=<密码>
"""
from __future__ import annotations

import json, os, time
from dataclasses import dataclass, field
from typing import Any

import pytest
import httpx

# ── 配置 ──────────────────────────────────────────────────
BASE = os.environ.get("COGNI_AGENT_BASE_URL", "http://localhost:8000")
ACCOUNT = os.environ.get("COGNI_AGENT_ACCOUNT", "")
PASSWORD = os.environ.get("COGNI_AGENT_PASSWORD", "")

if not ACCOUNT or not PASSWORD:
    pytest.skip(
        "未配置 COGNI_AGENT_ACCOUNT / COGNI_AGENT_PASSWORD 环境变量，"
        "本测试仅本地手动运行，跳过。",
        allow_module_level=True,
    )


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

class ApiTestClient:
    """轻量 httpx 测试客户端，带 auth 状态缓存。"""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._token: str | None = None
        self._token_data: dict = {}

    async def login(self, account: str, password: str) -> bool:
        res = await self._raw("POST", "/api/app/auth/login", json={
            "account": account,
            "password": password,
        })
        data = res.json()
        if data.get("code") != 0:
            print(f"[login] 失败: {data}")
            return False
        self._token = data["data"]["access_token"]
        self._token_data = data["data"]
        return True

    @property
    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self._raw("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self._raw("POST", path, **kw)

    async def patch(self, path: str, **kw) -> httpx.Response:
        return await self._raw("PATCH", path, **kw)

    async def _raw(self, method: str, path: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        headers.update(self.auth_headers)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
            return await client.request(method, f"{self.base}{path}", headers=headers, **kw)


@dataclass
class EndpointTest:
    path: str
    method: str
    description: str
    # 需要 auth 的接口特殊配置
    needs_auth: bool = True
    body: dict | None = None               # POST 请求体
    query: dict | None = None               # GET 查询参数
    expect_status: int = 200               # 预期 HTTP 状态码
    expect_code_zero: bool = True          # 预期业务 code == 0
    # 对 data 字段的结构约束
    data_keys: list[str] | None = None     # data 中必须包含的 key
    data_types: dict[str, str] | None = None  # 数据类型检查
    skip: bool = False                     # 是否需要特殊前置条件（如先创建 API Key）
    extra_setup: str = ""                   # 前置条件说明


# ═══════════════════════════════════════════════════════════
# routes_app.py 端点（16 个手动包裹端点）
# ═══════════════════════════════════════════════════════════

APP_ENDPOINTS: list[EndpointTest] = [
    # 无需认证
    EndpointTest("/api/app/auth/public-key", "GET", "获取RSA公钥", needs_auth=False,
                 data_keys=["public_key", "algorithm", "key_size"]),
    EndpointTest("/api/app/login-policy", "GET", "登录策略查询", needs_auth=False,
                 data_keys=["require_sms_on_first_login", "allow_self_register"]),
    EndpointTest("/api/app/auth/sms/send", "POST", "发送验证码", needs_auth=False,
                 query={"account": ACCOUNT},
                 data_keys=["ok"]),
    # 登录类（不用 auth header，走 login 返回的 token；refresh 需要先 login 拿 refresh_token，
    # 由独立的 test_app_refresh_structure 覆盖，此处跳过）
    EndpointTest("/api/app/auth/refresh", "POST", "Token刷新", skip=True,
                 extra_setup="先用 login 拿到 refresh_token"),
    # 认证类
    EndpointTest("/api/app/auth/logout", "POST", "登出",
                 data_keys=["ok", "note"]),
    EndpointTest("/api/app/permissions", "GET", "APP权限清单"),
    EndpointTest("/api/app/me", "GET", "个人资料查询",
                 data_keys=["id", "name", "account", "phone", "role",
                            "scenic_id", "position", "must_change_password", "last_login_at"]),
    EndpointTest("/api/app/me", "POST", "修改个人资料",
                 body={"name": f"test_{int(time.time()) % 10000}"},
                 data_keys=["id", "name", "account"]),
    EndpointTest("/api/app/config", "GET", "获取用户配置",
                 query={"type": "workbench"},
                 data_keys=["config_type", "config", "updated_at"]),
    EndpointTest("/api/app/config", "POST", "保存用户配置",
                 body={"key": "value_test"},
                 query={"type": "workbench"},
                 data_keys=["ok", "updated_at"]),
    EndpointTest("/api/app/config/types", "GET", "列出配置类型",
                 data_keys=["types"]),
    # 竞品需求
    EndpointTest("/api/app/competitor-requests", "POST", "提交竞品需求",
                 body={"schema_version": "1.0", "extra": {"name": "test", "url": "http://example.com"}},
                 data_keys=["id", "schema_version", "status", "created_at"]),
    EndpointTest("/api/app/competitor-requests", "GET", "竞品需求列表分页",
                 query={"page": 1, "page_size": 5},
                 data_keys=["items", "total", "page", "page_size"]),
]


# ═══════════════════════════════════════════════════════════
# routes_api_keys.py 端点（3 个手动包裹端点）
# ═══════════════════════════════════════════════════════════

APIKEY_ENDPOINTS: list[EndpointTest] = [
    EndpointTest("/api/admin/api-keys", "GET", "API Key列表",
                 data_types={"data": "list"}),
    EndpointTest("/api/admin/api-keys", "POST", "创建API Key",
                 body={"agent_allowed_ids": [], "expires_in_days": 30},
                 data_keys=["id", "key", "prefix_display", "agent_allowed_ids", "expires_at", "created_at"]),
]


# ═══════════════════════════════════════════════════════════
# 通用校验
# ═══════════════════════════════════════════════════════════

def validate_api_response_envelope(data: dict, expect_zero: bool = True, ctx: str = ""):
    """校验 ApiResponse 信封结构: {code, message, data, trace_id}。"""
    prefix = f"[{ctx}] " if ctx else ""

    # 1. 四个必需字段
    for key in ("code", "message", "data", "trace_id"):
        assert key in data, f"{prefix}缺少字段 {key}, 实际字段: {list(data.keys())}"

    # 2. code 正确性
    if expect_zero:
        assert data["code"] == 0, f"{prefix}code 应为 0，实际: {data['code']}, message: {data.get('message')}"
    else:
        assert data["code"] != 0, f"{prefix}code 应为非 0（错误），实际: {data['code']}"

    # 3. trace_id 非空
    assert data.get("trace_id"), f"{prefix}trace_id 不应为空"


def validate_data_fields(data: dict, keys: list[str] | None, ctx: str = ""):
    """校验 data 对象中的必需字段。"""
    if not keys:
        return
    inner = data.get("data", {})
    if inner is None:
        return  # data 为 null 是合法的
    prefix = f"[{ctx}] " if ctx else ""
    for k in keys:
        assert k in inner, f"{prefix}data 中缺少字段 {k}, 实际: {list(inner.keys()) if isinstance(inner, dict) else type(inner).__name__}"


# ═══════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════

class TestAppEndpoints:
    """routes_app.py 下的所有手动包裹端点快照测试。"""

    @pytest.fixture(scope="class")
    async def client(self):
        c = ApiTestClient(BASE)
        ok = await c.login(ACCOUNT, PASSWORD)
        assert ok, "登录失败，请检查账号密码和服务是否运行"
        return c

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ep", [
        e for e in APP_ENDPOINTS if not e.skip
    ], ids=lambda e: f"{e.method} {e.path} [{e.description}]")
    async def test_app_endpoint_snapshot(self, client, ep: EndpointTest):
        """逐个端点验证：响应信封结构 + data 内部字段。"""
        # 特殊处理:不需要 auth 的用裸请求
        client_to_use = client
        if ep.needs_auth is False:
            client_to_use = ApiTestClient(BASE)

        # 发送请求
        if ep.method == "GET":
            res = await client_to_use.get(ep.path, params=ep.query or {})
        elif ep.method == "POST":
            res = await client_to_use.post(ep.path, json=ep.body or {}, params=ep.query or {})
        elif ep.method == "PATCH":
            res = await client_to_use.patch(ep.path, json=ep.body or {})
        else:
            pytest.skip(f"不支持的方法: {ep.method}")

        ctx = f"{ep.method} {ep.path}"
        assert res.status_code == ep.expect_status, \
            f"[{ctx}] HTTP状态码 {res.status_code} != {ep.expect_status}, body: {res.text[:500]}"

        body = res.json()
        validate_api_response_envelope(body, expect_zero=ep.expect_code_zero, ctx=ctx)
        validate_data_fields(body, ep.data_keys, ctx=ctx)

        # 额外:有 data_types 约束时做类型检查
        if ep.data_types:
            inner = body.get("data")
            for key, expected_type in ep.data_types.items():
                if expected_type == "list":
                    assert isinstance(inner, list), f"[{ctx}] data 应为 list, 实际: {type(inner).__name__}"

    # ── 登录单独测（特殊路径，不放在 parametrize 里）──
    @pytest.mark.asyncio
    async def test_app_login_structure(self, client):
        """登录端点响应结构。"""
        res = await client._raw("POST", "/api/app/auth/login", json={
            "account": ACCOUNT, "password": PASSWORD,
        })
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx="POST /api/app/auth/login")
        validate_data_fields(body, ["access_token", "refresh_token", "must_change_password", "user"],
                             ctx="POST /api/app/auth/login")
        assert body["data"]["user"] is not None
        for uk in ("id", "name", "account", "role", "scenic_id"):
            assert uk in body["data"]["user"], f"user 缺少字段 {uk}"

    # ── 改密错误分支 ──
    @pytest.mark.asyncio
    async def test_app_change_password_wrong_old(self, client):
        """修改密码-旧密码错误应返回非 0 code。"""
        res = await client.post("/api/app/me/change-password", json={
            "old_password": "wrong_old_password_123",
            "new_password": "NewPass123!",
        })
        assert res.status_code == 400 or res.status_code == 200, \
            f"Unexpected status: {res.status_code}"
        body = res.json()
        validate_api_response_envelope(body, expect_zero=False, ctx="POST /api/app/me/change-password (错误分支)")
        # 旧密码错，code 应非 0
        assert body["code"] != 0, f"应为错误 code, 实际: {body['code']}"

    # ── Refresh（需要先拿到 refresh_token）──
    @pytest.mark.asyncio
    async def test_app_refresh_structure(self, client):
        """Token 刷新端点响应结构。"""
        # 先登录拿 refresh_token
        login_res = await client._raw("POST", "/api/app/auth/login", json={
            "account": ACCOUNT, "password": PASSWORD,
        })
        refresh_token = login_res.json()["data"]["refresh_token"]

        res = await client.post("/api/app/auth/refresh", json={
            "refresh_token": refresh_token,
        })
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx="POST /api/app/auth/refresh")
        validate_data_fields(body, ["access_token", "refresh_token", "must_change_password"],
                             ctx="POST /api/app/auth/refresh")

    # ── 竞品需求详情（需要先创建一个）──
    @pytest.mark.asyncio
    async def test_app_competitor_request_detail(self, client):
        """竞品需求详情端点。先创建一个，再查详情。"""
        create_res = await client.post("/api/app/competitor-requests", json={
            "schema_version": "1.0",
            "extra": {"name": "test_detail", "url": "http://example.com"},
        })
        assert create_res.status_code == 200, f"创建竞品需求失败: {create_res.text}"
        req_id = create_res.json()["data"]["id"]

        res = await client.get(f"/api/app/competitor-requests/{req_id}")
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx=f"GET /api/app/competitor-requests/{req_id}")
        assert body["data"]["id"] == req_id


class TestApiKeyEndpoints:
    """routes_api_keys.py 下的手动包裹端点快照测试。"""

    @pytest.fixture(scope="class")
    async def client(self):
        c = ApiTestClient(BASE)
        ok = await c.login(ACCOUNT, PASSWORD)
        assert ok, "登录失败"
        return c

    @pytest.mark.asyncio
    async def test_list_api_keys(self, client):
        """GET /api/admin/api-keys 列表。"""
        res = await client.get("/api/admin/api-keys")
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx="GET /api/admin/api-keys")
        assert isinstance(body["data"], list), \
            f"data 应为 list, 实际: {type(body['data']).__name__}"

    @pytest.mark.asyncio
    async def test_create_api_key(self, client):
        """POST /api/admin/api-keys 创建。"""
        res = await client.post("/api/admin/api-keys", json={
            "agent_allowed_ids": [],
            "expires_in_days": 30,
        })
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx="POST /api/admin/api-keys")
        validate_data_fields(body, ["id", "key", "prefix_display", "agent_allowed_ids",
                                     "expires_at", "created_at"],
                             ctx="POST /api/admin/api-keys")

    @pytest.mark.asyncio
    async def test_revoke_api_key(self, client):
        """POST /api/admin/api-keys/{id}/revoke 吊销。"""
        # 先创建一个
        create_res = await client.post("/api/admin/api-keys", json={
            "agent_allowed_ids": [],
            "expires_in_days": 30,
        })
        key_id = create_res.json()["data"]["id"]

        res = await client.post(f"/api/admin/api-keys/{key_id}/revoke")
        assert res.status_code == 200
        body = res.json()
        validate_api_response_envelope(body, ctx=f"POST /api/admin/api-keys/{key_id}/revoke")
        assert body["code"] == 0

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_api_key(self, client):
        """吊销不存在的 API Key（错误分支）。"""
        res = await client.post("/api/admin/api-keys/non-existent-id-xxx/revoke")
        body = res.json()
        validate_api_response_envelope(body, expect_zero=False,
                                        ctx="POST /api/admin/api-keys/xxx/revoke (不存在)")


# ═══════════════════════════════════════════════════════════
# 合规性综合检查:确保没有双重包裹
# ═══════════════════════════════════════════════════════════

class TestNoDoubleWrapping:
    """改造后专用:确保中间件 + 手动包裹不会产生双重包裹。"""

    @pytest.fixture(scope="class")
    async def client(self):
        c = ApiTestClient(BASE)
        ok = await c.login(ACCOUNT, PASSWORD)
        assert ok, "登录失败"
        return c

    @pytest.mark.asyncio
    async def test_no_nested_api_response(self, client):
        """所有端点返回的 data 内部不应再有嵌套的 code/message/data。"""
        paths = [
            ("GET", "/api/app/me"),
            ("GET", "/api/app/permissions"),
            ("GET", "/api/app/auth/public-key"),
            ("GET", "/api/admin/api-keys"),
        ]
        for method, path in paths:
            if method == "GET":
                res = await client.get(path)
            else:
                res = await client.post(path)
            body = res.json()
            inner = body.get("data", {})
            if isinstance(inner, dict):
                # 内层 data 不应再有 code+message+data 三字段（双重包裹特征）
                if {"code", "message", "data"}.issubset(inner.keys()):
                    pytest.fail(
                        f"[{method} {path}] 检测到双重包裹! "
                        f"外层: code={body['code']} data={inner.keys()}, "
                        f"内层: code={inner['code']} data={inner.get('data')}"
                    )


# ═══════════════════════════════════════════════════════════
# 快速健康检查（改造前后都可用）
# ═══════════════════════════════════════════════════════════

class TestQuickHealth:
    """快速验证核心端点均可访问且格式正确。"""

    @pytest.fixture(scope="class")
    async def client(self):
        c = ApiTestClient(BASE)
        ok = await c.login(ACCOUNT, PASSWORD)
        assert ok, "登录失败"
        return c

    @pytest.mark.asyncio
    async def test_core_app_endpoints(self, client):
        """批量快速检查 app 端点。"""
        checks = [
            ("GET", "/api/app/me", 200, True),
            ("GET", "/api/app/permissions", 200, True),
            ("GET", "/api/app/auth/public-key", 200, True),
            ("POST", "/api/app/auth/logout", 200, True),
            ("GET", "/api/app/config?type=workbench", 200, True),
            ("GET", "/api/admin/api-keys", 200, True),
        ]
        for method, path, expected_status, expect_zero in checks:
            if method == "GET":
                res = await client.get(path)
            else:
                res = await client.post(path)
            assert res.status_code == expected_status, \
                f"[{method} {path}] HTTP {res.status_code} != {expected_status}"
            body = res.json()
            validate_api_response_envelope(body, expect_zero=expect_zero,
                                           ctx=f"{method} {path}")
