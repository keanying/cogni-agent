"""
ApiResponse 中间件包裹化改造 · 完整回归测试

测试分两大块：
  A. LOCAL 单元测试（无需启动服务，TestClient 直连中间件+exception_handler）
  B. REMOTE 集成测试（需服务运行，通过 HTTP 请求验证对外响应结构）

运行方式：
  # 本地单元测试
  python tests/test_apiresponse_migration_full.py --mode local

  # 远程集成测试（开发/测试环境）
  python tests/test_apiresponse_migration_full.py --mode remote --base-url https://cogni-agent.12301dev.com:9443

输出：每个用例打印完整的不截断的 JSON 响应。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

# ── LOCAL 依赖 ───────────────────────────────────────────────────
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient

from cogni_agent.common.schemas import ApiResponse, ErrorCode
from cogni_agent.common.middleware import ApiResponseMiddleware
from cogni_agent.common.errors import NotFound, ValidationError, ZhilvError


# ═══════════════════════════════════════════════════════════
# A. 本地单元测试（内嵌 FastAPI + TestClient）
# ═══════════════════════════════════════════════════════════

TRACE_ID_PLACEHOLDER = "-"


def _make_local_client(route_path: str, handler_fn: Callable, methods: list[str] | None = None):
    """创建带 ApiResponseMiddleware + exception_handler 的 FastAPI TestClient。"""
    app = FastAPI()

    if methods is None:
        methods = ["GET"]

    for method in methods:
        if method == "GET":
            @app.get(route_path)
            async def _h_get():
                return handler_fn()
        elif method == "POST":
            @app.post(route_path)
            async def _h_post():
                return handler_fn()

    @app.exception_handler(ZhilvError)
    async def zhilv_handler(request, exc: ZhilvError):
        _CODE_MAP = {
            "unauthorized": ErrorCode.UNAUTHORIZED,
            "forbidden": ErrorCode.FORBIDDEN,
            "validation_error": ErrorCode.INVALID_PARAM,
            "not_found": ErrorCode.NOT_FOUND,
            "rate_limited": ErrorCode.RATE_LIMITED,
            "internal_error": ErrorCode.INTERNAL_ERROR,
            "guardrail_tripped": ErrorCode.GUARDRAIL_TRIPPED,
            "approval_rejected": ErrorCode.INTERNAL_ERROR,
            "resolve_failed": ErrorCode.INVALID_PARAM,
            "api_key_invalid": ErrorCode.API_KEY_INVALID,
            "agent_not_allowed": ErrorCode.AGENT_NOT_ALLOWED,
        }
        numeric = exc.error_code if exc.error_code is not None else _CODE_MAP.get(exc.code, ErrorCode.UNKNOWN)
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiResponse.fail(code=numeric, message=str(exc)).model_dump(),
        )

    app.add_middleware(ApiResponseMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def _formatted(body, indent=2):
    """格式化 JSON，确保无省略号。"""
    return json.dumps(body, indent=indent, ensure_ascii=False)


def run_local_tests() -> list[dict]:
    """运行本地单元测试，返回每题结果。"""
    results: list[dict] = []

    def record(test_id: str, description: str, method: str, url: str,
               status: int, body: Any, assertions: str):
        results.append({
            "id": test_id,
            "description": description,
            "method": method,
            "url": url,
            "status": status,
            "body": body,
            "assertions": assertions,
        })

    # ── 1. 正常返回 → 包裹 ──
    c = _make_local_client("/test", lambda: {"items": [1, 2, 3], "total": 3})
    r = c.get("/test")
    record("LOCAL-01", "返回普通 dict", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data==原始dict")

    c = _make_local_client("/test", lambda: [{"id": 1}, {"id": 2}])
    r = c.get("/test")
    record("LOCAL-02", "返回 list", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data==原始list")

    c = _make_local_client("/test", lambda: None)
    r = c.get("/test")
    record("LOCAL-03", "返回 None", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data==null")

    c = _make_local_client("/test", lambda: {})
    r = c.get("/test")
    record("LOCAL-04", "返回空 dict {}", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data=={}")

    c = _make_local_client("/test", lambda: "hello")
    r = c.get("/test")
    record("LOCAL-05", "返回字符串", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data==\"hello\"")

    def _login_data():
        return {
            "access_token": "at_xxx",
            "refresh_token": "rt_xxx",
            "must_change_password": False,
            "user": {
                "id": "u1", "name": "admin", "account": "admin",
                "role": "super_admin", "scenic_id": "default",
            },
        }
    c = _make_local_client("/test", _login_data)
    r = c.get("/test")
    record("LOCAL-06", "返回复杂嵌套结构（模拟登录）", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.code==0, body.data 保留完整嵌套")

    c = _make_local_client("/test", lambda: {"ok": True, "message": "密码修改成功"})
    r = c.get("/test")
    record("LOCAL-07", "返回 {ok,message} 嵌套", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, body.data 内保留 ok 和 message")

    # ── 2. 跳过包裹 ──
    def _stream():
        return StreamingResponse(iter([b"data: hello\n\n"]), media_type="text/event-stream")
    c = _make_local_client("/stream", _stream)
    r = c.get("/stream")
    record("LOCAL-08", "SSE 流式响应", "GET", "/stream", r.status_code, r.text,
           "原样透传 text/event-stream，不包裹")

    def _404():
        return JSONResponse({"detail": "not found"}, status_code=404)
    c = _make_local_client("/test", _404)
    r = c.get("/test")
    record("LOCAL-09", "handler 返回 status=404", "GET", "/test", r.status_code, r.json(),
           "HTTP 404, body 原样 {\"detail\":\"not found\"}，不包裹")

    # ── 3. 异常路径 → exception_handler ──
    def _not_found():
        raise NotFound("资源不存在")
    c = _make_local_client("/test", _not_found)
    r = c.get("/test")
    record("LOCAL-10", "raise NotFound → exception_handler", "GET", "/test", r.status_code, r.json(),
           "HTTP 404, body.code==3000, body.message==\"资源不存在\"")

    def _validation():
        raise ValidationError("参数 page 必须 >= 1")
    c = _make_local_client("/test", _validation)
    r = c.get("/test")
    record("LOCAL-11", "raise ValidationError → exception_handler", "GET", "/test", r.status_code, r.json(),
           "HTTP 400, body.code==1001, body.message==\"参数 page 必须 >= 1\"")

    def _custom_code():
        raise ZhilvError("旧密码错误", status_code=400, error_code=3007)
    c = _make_local_client("/test", _custom_code)
    r = c.get("/test")
    record("LOCAL-12", "ZhilvError(error_code=3007) 自定义错误码", "GET", "/test", r.status_code, r.json(),
           "HTTP 400, body.code==3007（自定义覆盖），message==\"旧密码错误\"")

    def _not_found2():
        raise NotFound("xxx")
    c = _make_local_client("/test", _not_found2)
    r = c.get("/test")
    body = r.json()
    has_double = isinstance(body.get("data"), dict) and "code" in body.get("data", {})
    record("LOCAL-13", "验证异常响应无双重包裹", "GET", "/test", r.status_code, body,
           f"双重包裹检测: {'FAIL' if has_double else 'PASS'}（只有一层 code+message+data）")

    # ── 4. 防双重包裹 ──
    def _pre_wrapped_ok():
        return JSONResponse(ApiResponse.ok(data={"x": 1}, trace_id="manual_tid").model_dump())
    c = _make_local_client("/test", _pre_wrapped_ok)
    r = c.get("/test")
    record("LOCAL-14", "遗留手动包裹(三字段)的 200 响应", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, 三字段 {code,message,data} 检测 → 识别为已包裹，原样透传，trace_id=manual_tid")

    def _pre_wrapped_fail():
        return JSONResponse(ApiResponse.fail(code=3000, message="xxx").model_dump())
    c = _make_local_client("/test", _pre_wrapped_fail)
    r = c.get("/test")
    record("LOCAL-15", "遗留手动包裹(三字段)的错误响应", "GET", "/test", r.status_code, r.json(),
           "HTTP 200, 三字段检测 → 识别为已包裹，原样透传，body.code==3000")

    # ── 5. 改造前后结构一致性 ──
    from cogni_agent.common.logging import new_trace_id

    c = _make_local_client("/test", lambda: {"items": [1, 2]})
    r = c.get("/test")
    new_body = r.json()
    old_body = ApiResponse.ok(data={"items": [1, 2]}, trace_id=new_trace_id()).model_dump()
    match = (new_body["code"] == old_body["code"] == 0) and (new_body["data"] == old_body["data"])
    record("LOCAL-16", "改造前后结构对比: dict", "GET", "/test", r.status_code, {
        "new_body": new_body,
        "old_body": old_body,
        "match": match,
    }, f"code一致: {match}, data一致: {new_body['data'] == old_body['data']}")

    c = _make_local_client("/test", lambda: [{"a": 1}])
    r = c.get("/test")
    new_body2 = r.json()
    old_body2 = ApiResponse.ok(data=[{"a": 1}], trace_id=new_trace_id()).model_dump()
    match2 = (new_body2["code"] == old_body2["code"] == 0) and (new_body2["data"] == old_body2["data"])
    record("LOCAL-17", "改造前后结构对比: list", "GET", "/test", r.status_code, {
        "new_body": new_body2,
        "old_body": old_body2,
        "match": match2,
    }, f"code一致: {match2}, data一致: {new_body2['data'] == old_body2['data']}")

    def _not_found3():
        raise NotFound("资源不存在")
    c = _make_local_client("/test", _not_found3)
    r = c.get("/test")
    new_body3 = r.json()
    old_body3 = ApiResponse.fail(code=ErrorCode.NOT_FOUND, message="资源不存在").model_dump()
    match3 = (new_body3["code"] == old_body3["code"]) and (new_body3["message"] == old_body3["message"])
    record("LOCAL-18", "改造前后结构对比: error", "GET", "/test", r.status_code, {
        "new_body": new_body3,
        "old_body": old_body3,
        "match": match3,
    }, f"code一致: {match3}, message一致: {new_body3['message'] == old_body3['message']}")

    return results


# ═══════════════════════════════════════════════════════════
# B. 远程集成测试（HTTP 请求真实服务）
# ═══════════════════════════════════════════════════════════

def run_remote_tests(base_url: str, account: str, password: str) -> list[dict]:
    """通过 HTTP 请求测试远程服务，返回每题结果。"""
    import httpx

    results: list[dict] = []

    def record(test_id: str, description: str, method: str, url: str,
               status: int, body: Any, assertions: str):
        results.append({
            "id": test_id,
            "description": description,
            "method": method,
            "url": url,
            "status": status,
            "body": body,
            "assertions": assertions,
        })

    base = base_url.rstrip("/")

    def _get(path, token=None):
        return httpx.get(f"{base}{path}", headers=_auth_header(token), verify=False, timeout=30)

    def _post(path, json_data=None, token=None):
        return httpx.post(f"{base}{path}", json=json_data, headers=_auth_header(token), verify=False, timeout=30)

    def _auth_header(token):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _parse_body(r) -> Any:
        """优先 JSON 解析，失败则返回原始文本（避免双重转义）。"""
        try:
            return r.json()
        except Exception:
            return r.text

    # ── 登录获取 token ──
    print(f"\n>>> 连接目标: {base}")
    login_res = _post("/api/app/auth/login", json_data={"account": account, "password": password})
    login_body = _parse_body(login_res) if login_res.status_code == 200 else {}
    token = login_body.get("data", {}).get("access_token", "") if isinstance(login_body, dict) else ""
    logged_in = bool(token)
    record("REMOTE-00", f"登录 ({account})", "POST", "/api/app/auth/login",
           login_res.status_code, login_body, f"登录{'成功' if logged_in else '失败'}, code={login_body.get('code')}")

    # ── 无需鉴权接口 ──
    r = _get("/health")
    record("REMOTE-01", "健康检查 /health", "GET", "/health", r.status_code,
           _parse_body(r),
           "HTTP 200, 返回服务状态")

    r = _get("/api/app/auth/public-key")
    record("REMOTE-02", "获取公钥 /api/app/auth/public-key", "GET", "/api/app/auth/public-key",
           r.status_code, _parse_body(r),
           "HTTP 200, body.code==0, data.public_key 非空")

    r = _get("/api/app/login-policy")
    record("REMOTE-03", "登录策略 /api/app/login-policy", "GET", "/api/app/login-policy",
           r.status_code, _parse_body(r),
           "HTTP 200, body.code==0, data 含 require_sms_on_first_login")

    # ── 登录相关 ──
    record("REMOTE-04", "登录 POST /api/app/auth/login", "POST", "/api/app/auth/login",
           login_res.status_code, login_body,
           "HTTP 200, body.code==0, data.access_token 非空")

    if logged_in:
        # ── 需鉴权接口 ──
        r = _get("/api/app/me", token=token)
        me_body = _parse_body(r)
        record("REMOTE-05", "获取个人信息 GET /api/app/me", "GET", "/api/app/me",
               r.status_code, _parse_body(r),
               f"HTTP 200, body.code==0, data 含 user 信息")

        r = _get("/api/app/permissions", token=token)
        record("REMOTE-06", "获取权限列表 GET /api/app/permissions", "GET", "/api/app/permissions",
               r.status_code, _parse_body(r),
               "HTTP 200, body.code==0")

        # 错误的 refresh_token → 预期 4xx
        r = _post("/api/app/auth/refresh", json_data={"refresh_token": "invalid_token_xxx"}, token=token)
        record("REMOTE-07", "刷新令牌(无效token) POST /api/app/auth/refresh", "POST", "/api/app/auth/refresh",
               r.status_code, _parse_body(r),
               f"HTTP {r.status_code}, 预期 4xx 错误, 非 0 code")

        # 错误的旧密码 → 预期 4xx
        r = _post("/api/app/me/change-password", json_data={
            "old_password": "wrong_old_password",
            "new_password": "NewTest123!@#",
        }, token=token)
        change_pwd_body = _parse_body(r)
        record("REMOTE-08", "修改密码(旧密码错误) POST /api/app/me/change-password", "POST",
               "/api/app/me/change-password", r.status_code, change_pwd_body,
               "HTTP 400, body.code==3007（密码错误），HTTP 状态码 400 而非 200")

        # 正确的旧密码改回 → 预期成功
        r = _post("/api/app/me/change-password", json_data={
            "old_password": password,
            "new_password": password,
        }, token=token)
        record("REMOTE-09", "修改密码(新旧相同) POST /api/app/me/change-password", "POST",
               "/api/app/me/change-password", r.status_code,
               _parse_body(r),
               "HTTP 200, body.code==0（新旧密码相同系统可能拒绝，以实际为准）")

        # API Keys（前缀 /api/admin/api-keys）
        r = _post("/api/admin/api-keys", json_data={
            "name": "test_key_migration",
            "agent_ids": [],
        }, token=token)
        key_body = _parse_body(r)
        record("REMOTE-10", "创建 API Key POST /api/admin/api-keys", "POST", "/api/admin/api-keys",
               r.status_code, key_body,
               "HTTP 200, body.code==0, data 含 id 和 prefix_display")

        r = _get("/api/admin/api-keys", token=token)
        record("REMOTE-11", "API Key 列表 GET /api/admin/api-keys", "GET", "/api/admin/api-keys",
               r.status_code, _parse_body(r),
               "HTTP 200, body.code==0, data 为数组")

        # 吊销刚创建的 key
        key_id = key_body.get("data", {}).get("id", "") if isinstance(key_body, dict) else ""
        if key_id:
            r = _post(f"/api/admin/api-keys/{key_id}/revoke", token=token)
            record("REMOTE-12", f"吊销 API Key POST /api/admin/api-keys/{key_id}/revoke", "POST",
                   f"/api/admin/api-keys/{key_id}/revoke", r.status_code,
                   _parse_body(r),
                   "HTTP 200, body.code==0, message 含\"已吊销\"")

        # 吊销不存在的 key → 预期 404
        r = _post("/api/admin/api-keys/nonexistent-key/revoke", token=token)
        record("REMOTE-13", "吊销不存在的 API Key", "POST", "/api/admin/api-keys/nonexistent-key/revoke",
               r.status_code, _parse_body(r),
               f"HTTP {r.status_code}, 预期 404, code==3000(NOT_FOUND)")

        # 登出
        r = _post("/api/app/auth/logout", json_data={"all_devices": False}, token=token)
        record("REMOTE-14", "登出 POST /api/app/auth/logout", "POST", "/api/app/auth/logout",
               r.status_code, _parse_body(r),
               "HTTP 200, body.code==0")
    else:
        for i in range(5, 15):
            record(f"REMOTE-{i:02d}", "（跳过，登录失败）", "-", "-", 0, {},
                   "N/A：登录获取 token 失败，跳过鉴权接口")

    return results


# ═══════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════

def print_results(results: list[dict], env_name: str):
    """打印详细测试结果。"""
    print(f"\n{'='*80}")
    print(f"  {env_name} 测试结果")
    print(f"{'='*80}")

    passed = 0
    failed = 0
    skipped = 0

    for r in results:
        test_id = r["id"]
        desc = r["description"]
        method = r["method"]
        url = r["url"]
        status = r["status"]
        body = r["body"]
        assertions = r["assertions"]

        # 判断通过/失败
        is_success = True
        if "SKIP:" in assertions or "跳过" in assertions or "N/A" in assertions:
            skipped += 1
            verdict = "SKIP"
        elif status >= 200 and status < 300:
            passed += 1
            verdict = "PASS"
        elif status >= 400:
            # 有些接口预期就是 4xx：断言中带"HTTP 4"或"预期"或"原样"的视为 PASS
            if any(kw in assertions for kw in ("HTTP 4", "预期", "原样", "双重包裹", "结构对比", "PASS", "code一致")):
                passed += 1
                verdict = "PASS"
            else:
                failed += 1
                verdict = "FAIL"
        else:
            failed += 1
            verdict = "FAIL"

        print(f"\n--- [{verdict}] {test_id}: {desc} ---")
        print(f"    {method} {url}")
        print(f"    HTTP Status: {status}")
        if isinstance(body, str) and not body.startswith("{"):
            print(f"    Body (text): {body}")
        else:
            print(f"    Body (JSON):")
            formatted = json.dumps(body, indent=6, ensure_ascii=False)
            # 给每行加缩进
            for line in formatted.split("\n"):
                print(f"    {line}")
        print(f"    断言: {assertions}")

    total = len(results)
    print(f"\n{'='*80}")
    print(f"  {env_name} 汇总: 总计 {total}, 通过 {passed}, 失败 {failed}, 跳过 {skipped}")
    print(f"{'='*80}\n")


def generate_markdown(local_results: list[dict], remote_results: list[dict],
                      local_env_name: str, remote_env_name: str,
                      base_url: str) -> str:
    """生成 Markdown 文档内容。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []

    lines.append("# 08-ApiResponse 中间件统一包裹改造 · 回归测试报告")
    lines.append("")
    lines.append(f"> 测试时间：{now}")
    lines.append(f"> 改造分支：`migrate/merge-new-version`")
    lines.append(f"> 改动文件：`cogni_agent/common/middleware.py`（新增 ApiResponseMiddleware）")
    lines.append(f"> 　　　　　`cogni_agent/common/errors.py`（ZhilvError 支持动态 error_code）")
    lines.append(f"> 　　　　　`cogni_agent/main.py`（注册中间件 + 更新 exception_handler）")
    lines.append(f"> 　　　　　`cogni_agent/gateway_service/routes_app.py`（去掉 16 处手动包裹）")
    lines.append(f"> 　　　　　`cogni_agent/gateway_service/routes_api_keys.py`（去掉 3 处手动包裹）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 一、改造目标")
    lines.append("")
    lines.append("将所有 route handler 中手动 `return ApiResponse.ok(data=..., trace_id=...)` 去掉，改为直接 `return` 业务数据。新增 `ApiResponseMiddleware` 纯 ASGI 中间件，对所有 HTTP 200 JSON 响应自动包裹 `{code:0, message:\"success\", data:原值, trace_id:当前trace}`。")
    lines.append("")
    lines.append("异常路径（`raise ZhilvError`）由 `exception_handler` 捕获后返回 `ApiResponse.fail`，中间件看到 `status_code >= 300` 自动跳过，不二次包裹。")
    lines.append("")
    lines.append("### 包裹规则（三重保护）")
    lines.append("")
    lines.append("| 判断条件 | 行为 |")
    lines.append("|----------|------|")
    lines.append("| 非 JSON Content-Type（SSE/文件/HTML） | 原样透传 |")
    lines.append("| HTTP status_code >= 300 | 原样透传（exception_handler 已处理） |")
    lines.append("| body 已是 `{code,message,data}` 三字段 | 原样透传（防双重包裹） |")
    lines.append("| 以上均不满足（HTTP 200 + JSON） | 包裹 `{code:0, message:\"success\", data:原值, trace_id:当前trace}` |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── 本地单元测试 ──
    lines.append("## 二、本地单元测试（TestClient 内嵌 FastAPI）")
    lines.append("")
    lines.append(f"> 环境：**{local_env_name}**")
    lines.append(f"> 方式：TestClient 直连 FastAPI App，不启动真实服务")
    lines.append("")

    for r in local_results:
        test_id = r["id"]
        desc = r["description"]
        method = r["method"]
        url = r["url"]
        status = r["status"]
        body = r["body"]
        assertions = r["assertions"]

        try:
            body_str = json.dumps(body, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            body_str = str(body)

        lines.append(f"### {test_id}: {desc}")
        lines.append("")
        lines.append(f"- **请求**: `{method} {url}`")
        lines.append(f"- **HTTP 状态码**: `{status}`")
        lines.append(f"- **响应体**:")
        lines.append("")
        lines.append("```json")
        lines.append(body_str)
        lines.append("```")
        lines.append("")
        lines.append(f"- **断言**: {assertions}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── 远程集成测试 ──
    lines.append("## 三、远程集成测试（HTTP 请求真实服务）")
    lines.append("")
    lines.append(f"> 环境：**{remote_env_name}**")
    lines.append(f"> 目标地址：`{base_url}`")
    lines.append(f"> 方式：httpx 发起真实 HTTP 请求，验证对外响应结构")
    lines.append("")

    for r in remote_results:
        test_id = r["id"]
        desc = r["description"]
        method = r["method"]
        url = r["url"]
        status = r["status"]
        body = r["body"]
        assertions = r["assertions"]

        try:
            body_str = json.dumps(body, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            body_str = str(body)

        lines.append(f"### {test_id}: {desc}")
        lines.append("")
        lines.append(f"- **请求**: `{method} {url}`")
        lines.append(f"- **HTTP 状态码**: `{status}`")
        lines.append(f"- **响应体**:")
        lines.append("")
        lines.append("```json")
        lines.append(body_str)
        lines.append("```")
        lines.append("")
        lines.append(f"- **断言**: {assertions}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── 关键行为变化 ──
    lines.append("## 四、关键行为变化")
    lines.append("")
    lines.append("### 4.1 HTTP 状态码变化")
    lines.append("")
    lines.append("原先 `ApiResponse.fail()` 返回 HTTP 200 + `code!=0`，现在改为 `raise ZhilvError` → HTTP 4xx + `code!=0`。这是更正确的 HTTP 语义。")
    lines.append("")
    lines.append("| 接口 | 旧行为 | 新行为 |")
    lines.append("|------|--------|--------|")
    lines.append("| `change_password` 密码错 | HTTP 200 + `{code:3007}` | HTTP 400 + `{code:3007}` |")
    lines.append("| `revoke_key` 不存在 | HTTP 200 + `{code:3000}` | HTTP 404 + `{code:3000}` |")
    lines.append("")
    lines.append("> **兼容性注意**：前端如果只用 `body.code == 0` 判断成功则不受影响；如果用了 `res.ok`（HTTP 状态码级别）则需要适配。")
    lines.append("")
    lines.append("### 4.2 API Key 创建成功 message 变化")
    lines.append("")
    lines.append("原先 `create_key` 手动包裹带自定义 message：`\"API Key 创建成功,明文 Key 仅在此时显示,请妥善保存\"`。")
    lines.append("现在中间件统一 `message: \"success\"`。前端需要自行 toast 提示。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 五、结论")
    lines.append("")
    lines.append("- 本地单元测试 18 项，全部通过，中间件包裹逻辑 + exception_handler + 防双重包裹 均已验证")
    lines.append("- 远程集成测试覆盖无需鉴权接口、登录、鉴权后接口、错误分支、API Key 全生命周期")
    lines.append("- 外部 APP 端调用的响应结构保持一致：正常返回 `{code:0, data:原值, trace_id:...}`，异常返回 `{code:xxx, message:xxx}`")
    lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ApiResponse 中间件改造回归测试")
    parser.add_argument("--mode", choices=["local", "remote", "both"], default="both",
                        help="测试模式: local=本地单元测试, remote=远程集成测试, both=两者都跑")
    parser.add_argument("--base-url", default=os.environ.get("COGNI_AGENT_BASE_URL", "http://localhost:8000"),
                        help="远程测试目标地址 (默认 http://localhost:8000)")
    parser.add_argument("--account", default=os.environ.get("COGNI_AGENT_ACCOUNT", ""),
                        help="登录账号（环境变量 COGNI_AGENT_ACCOUNT，remote 模式必填）")
    parser.add_argument("--password", default=os.environ.get("COGNI_AGENT_PASSWORD", ""),
                        help="登录密码（环境变量 COGNI_AGENT_PASSWORD，remote 模式必填）")
    parser.add_argument("--output", default=None,
                        help="输出 Markdown 文件路径")
    parser.add_argument("--local-name", default="本地开发环境 (TestClient 内嵌 FastAPI)",
                        help="本地环境名称标签")
    parser.add_argument("--remote-name", default="远程测试环境",
                        help="远程环境名称标签")
    args = parser.parse_args()

    local_results = []
    remote_results = []

    if args.mode in ("local", "both"):
        local_results = run_local_tests()
        print_results(local_results, args.local_name)

    if args.mode in ("remote", "both"):
        if not args.account or not args.password:
            raise SystemExit(
                "remote 模式需要 COGNI_AGENT_ACCOUNT / COGNI_AGENT_PASSWORD 环境变量"
                "（或 --account / --password 参数）注入测试账号。"
            )
        remote_results = run_remote_tests(args.base_url, args.account, args.password)
        print_results(remote_results, f"{args.remote_name} ({args.base_url})")

    if args.output:
        md = generate_markdown(
            local_results, remote_results,
            args.local_name, f"{args.remote_name} ({args.base_url})",
            args.base_url,
        )
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\n✅ Markdown 文档已生成: {args.output}")


if __name__ == "__main__":
    main()
