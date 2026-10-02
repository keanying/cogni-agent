"""
本地单元测试：验证 ApiResponseMiddleware 包裹逻辑和 exception_handler 行为。
用 FastAPI + TestClient，每个用例打印实际响应 JSON。
运行方式: pytest tests/test_middleware_unit.py -v -s
"""
import json
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient

from cogni_agent.common.schemas import ApiResponse, ErrorCode
from cogni_agent.common.middleware import ApiResponseMiddleware
from cogni_agent.common.errors import NotFound, ValidationError, ZhilvError


# ── helpers ──────────────────────────────────────────────

def _make_client(route_path, handler_fn):
    """创建一个带 ApiResponseMiddleware + exception_handler 的 FastAPI TestClient。"""
    app = FastAPI()

    @app.get(route_path)
    async def _h():
        return handler_fn()

    @app.exception_handler(ZhilvError)
    async def zhilv_h(request, exc: ZhilvError):
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


def _pretty(body, label=""):
    """打印带标签的美化 JSON。"""
    prefix = f"[{label}] " if label else ""
    # 尝试提取 trace_id 用于参考
    tid_info = ""
    if isinstance(body, dict) and "trace_id" in body:
        tid_info = f"  trace_id={body.get('trace_id', '')}"
    print(f"\n{prefix}response{tid_info}:")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print("-" * 60)


# ═══════════════════════════════════════════════
# 1. 正常返回 → 中间件包裹
# ═══════════════════════════════════════════════

class TestWrapping:
    """handler 返回 dict/list/None/str → 中间件包成 ApiResponse 信封。"""

    def test_01_plain_dict(self):
        """返回普通 dict → {code:0, data:{...}}"""
        client = _make_client("/test", lambda: {"items": [1, 2, 3], "total": 3})
        res = client.get("/test")
        body = res.json()
        _pretty(body, "普通dict返回")
        assert res.status_code == 200
        assert body["code"] == 0
        assert body["message"] == "success"
        assert body["data"] == {"items": [1, 2, 3], "total": 3}

    def test_02_plain_list(self):
        """返回 list → {code:0, data:[...]}"""
        client = _make_client("/test", lambda: [{"id": 1}, {"id": 2}])
        res = client.get("/test")
        body = res.json()
        _pretty(body, "返回list")
        assert body["code"] == 0
        assert body["data"] == [{"id": 1}, {"id": 2}]

    def test_03_none(self):
        """返回 None → {code:0, data:null}"""
        client = _make_client("/test", lambda: None)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "返回None")
        assert body["code"] == 0
        assert body["data"] is None

    def test_04_empty_dict(self):
        """返回 {} → {code:0, data:{}}"""
        client = _make_client("/test", lambda: {})
        res = client.get("/test")
        body = res.json()
        _pretty(body, "返回空dict")
        assert body["code"] == 0
        assert body["data"] == {}

    def test_05_string(self):
        """返回字符串 → {code:0, data:"hello"}"""
        client = _make_client("/test", lambda: "hello")
        res = client.get("/test")
        body = res.json()
        _pretty(body, "返回字符串")
        assert body["code"] == 0
        assert body["data"] == "hello"

    def test_06_change_password_success(self):
        """模拟改密码成功 → {ok,message} 被放入 data"""
        client = _make_client("/test", lambda: {"ok": True, "message": "密码修改成功"})
        res = client.get("/test")
        body = res.json()
        _pretty(body, "改密码成功(嵌套结构)")
        assert body["code"] == 0
        assert body["data"] == {"ok": True, "message": "密码修改成功"}

    def test_07_login_response(self):
        """模拟登录返回的复杂结构。"""
        def login():
            return {
                "access_token": "at_xxx",
                "refresh_token": "rt_xxx",
                "must_change_password": False,
                "user": {"id": "u1", "name": "admin", "account": "admin",
                         "role": "super_admin", "scenic_id": "default"},
            }
        client = _make_client("/test", login)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "登录返回(复杂嵌套)")
        assert body["code"] == 0
        assert body["data"]["user"]["role"] == "super_admin"


# ═══════════════════════════════════════════════
# 2. 跳过包裹
# ═══════════════════════════════════════════════

class TestSkipping:
    """这些场景中间件不应包裹。"""

    def test_08_sse_streaming(self):
        """SSE 流式 → 原样透传，不碰 body"""
        client = _make_client("/stream", lambda: StreamingResponse(
            iter([b"data: hello\n\n"]), media_type="text/event-stream"
        ))
        res = client.get("/stream")
        print(f"\n[SSE流式] response:\n{res.text}")
        print("-" * 60)
        assert res.text.startswith("data: hello")

    def test_09_404_from_handler(self):
        """handler 返回 status=404 → 原样，不包裹"""
        def h():
            return JSONResponse({"detail": "not found"}, status_code=404)
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "404响应(不包裹)")
        assert res.status_code == 404
        assert body == {"detail": "not found"}


# ═══════════════════════════════════════════════
# 3. 异常路径
# ═══════════════════════════════════════════════

class TestExceptionPath:
    """raise ZhilvError → exception_handler 捕获 → 返回 ApiResponse.fail → 中间件跳过。"""

    def test_10_not_found(self):
        """raise NotFound → HTTP 404 + ApiResponse.fail"""
        def h():
            raise NotFound("资源不存在")
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "NotFound异常(404)")
        assert res.status_code == 404
        assert body["code"] == ErrorCode.NOT_FOUND
        assert body["message"] == "资源不存在"

    def test_11_validation_error(self):
        """raise ValidationError → HTTP 400 + fail"""
        def h():
            raise ValidationError("参数 page 必须 >= 1")
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "ValidationError异常(400)")
        assert res.status_code == 400
        assert body["code"] == ErrorCode.INVALID_PARAM

    def test_12_custom_error_code(self):
        """change_password 场景:运行时携带自定义 numeric 错误码"""
        def h():
            raise ZhilvError("旧密码错误", status_code=400, error_code=3007)
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "自定义error_code=3007(400)")
        assert res.status_code == 400
        assert body["code"] == 3007
        assert body["message"] == "旧密码错误"

    def test_13_no_double_wrapping(self):
        """确保异常响应不被中间件二次包裹（只一层 {code,message,data}）。"""
        def h():
            raise NotFound("xxx")
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "异常响应(验证无双重包裹)")
        assert "code" in body and "message" in body
        if isinstance(body.get("data"), dict):
            assert "code" not in body["data"], f"检测到双重包裹！{body}"


# ═══════════════════════════════════════════════
# 4. 防双重包裹：三字段检测
# ═══════════════════════════════════════════════

class TestNoDoubleWrapping:
    """遗留的手动 ApiResponse 格式不会被二次包裹。"""

    def test_14_legacy_wrapped_success(self):
        """已手动包好的 200 成功响应 → 中间件识别三字段后跳过"""
        def h():
            return JSONResponse(ApiResponse.ok(data={"x": 1}, trace_id="manual_tid").model_dump())
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "遗留手动包裹(三字段跳过)")
        assert body["code"] == 0
        assert body["data"] == {"x": 1}
        assert body["trace_id"] == "manual_tid"

    def test_15_legacy_wrapped_error(self):
        """已手动包好的错误响应 → 同样跳过"""
        def h():
            return JSONResponse(ApiResponse.fail(code=3000, message="xxx").model_dump())
        client = _make_client("/test", h)
        res = client.get("/test")
        body = res.json()
        _pretty(body, "遗留手动fail(三字段跳过)")
        assert body["code"] == 3000
        assert body["message"] == "xxx"


# ═══════════════════════════════════════════════
# 5. 改造前后结构对比
# ═══════════════════════════════════════════════

class TestStructuralConsistency:
    """改造后(中间件包裹) vs 旧方式(手动 ApiResponse.ok) 结构一致。"""

    def test_16_structure_matches_dict(self):
        """dict 返回值：新中间件包裹 = 旧手动包裹"""
        client = _make_client("/test", lambda: {"items": [1, 2]})
        res = client.get("/test")
        new_body = res.json()

        from cogni_agent.common.logging import new_trace_id
        old_body = ApiResponse.ok(data={"items": [1, 2]}, trace_id=new_trace_id()).model_dump()

        print("\n[结构对比-dict]")
        print(f"  新(中间件): {json.dumps(new_body, ensure_ascii=False)}")
        print(f"  旧(手动):   {json.dumps(old_body, ensure_ascii=False)}")
        assert new_body["code"] == old_body["code"] == 0
        assert new_body["data"] == old_body["data"]
        assert isinstance(new_body["trace_id"], str) and len(new_body["trace_id"]) > 0
        print("  ✅ 结构一致 (trace_id 不同是正常的)")
        print("-" * 60)

    def test_17_structure_matches_list(self):
        """list 返回值同理"""
        client = _make_client("/test", lambda: [{"a": 1}])
        res = client.get("/test")
        new_body = res.json()

        from cogni_agent.common.logging import new_trace_id
        old_body = ApiResponse.ok(data=[{"a": 1}], trace_id=new_trace_id()).model_dump()

        print("\n[结构对比-list]")
        print(f"  新(中间件): {json.dumps(new_body, ensure_ascii=False)}")
        print(f"  旧(手动):   {json.dumps(old_body, ensure_ascii=False)}")
        assert new_body["code"] == old_body["code"] == 0
        assert new_body["data"] == old_body["data"]
        print("  ✅ 结构一致")
        print("-" * 60)

    def test_18_structure_matches_error(self):
        """异常响应：exception_handler 产出 == 旧 ApiResponse.fail"""
        def h():
            raise NotFound("资源不存在")
        client = _make_client("/test", h)
        res = client.get("/test")
        new_body = res.json()

        old_body = ApiResponse.fail(code=ErrorCode.NOT_FOUND, message="资源不存在").model_dump()

        print("\n[结构对比-error]")
        print(f"  新(exception_handler): {json.dumps(new_body, ensure_ascii=False)}")
        print(f"  旧(ApiResponse.fail):   {json.dumps(old_body, ensure_ascii=False)}")
        assert new_body["code"] == old_body["code"]
        assert new_body["message"] == old_body["message"]
        print("  ✅ 结构一致")
        print("-" * 60)
