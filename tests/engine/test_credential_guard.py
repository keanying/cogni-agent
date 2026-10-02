"""§10.1/§10.2/§10.3 M0 单元测试:凭证脱敏守卫、保存凭证工具、注入工具构造。

不连真实 DB/模型(见 tests/conftest.py):纯逻辑 + monkeypatch。覆盖:
- credential_guard:明文暂存/一次性消费/任务清理/脱敏标记消费;
- graphs._sanitize_credential_tool_calls:§10.3 动作1(剥离子调用明文 + 明文暂存缓存;
  v1.1.1 起不再原地擦除 HumanMessage,改由 mark_handled 驱动 tools_node 消费);
- tools_adapter 保存凭证分支:审批通过后加密入库(§10.2)、缓存丢失安全降级;
- resolver:{{credential}} 占位符判定与内建工具构造(§10.2)。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from cogni_agent.runtime_service.engine import credential_guard
from cogni_agent.runtime_service.engine.graphs import _sanitize_credential_tool_calls
from cogni_agent.runtime_service.engine.resolver import (
    _build_credential_tool,
    _uses_credential_placeholder,
    ToolSpec,
)
from cogni_agent.runtime_service.engine.tools_adapter import execute_single_tool_call


def _credential_spec() -> ToolSpec:
    return _build_credential_tool("tool_123", "PFT12301下单")


def _normal_spec(name: str = "query_order") -> ToolSpec:
    return ToolSpec(
        id="tool_123", name=name, description="d", type="http",
        json_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
        risk_level="low", type_config={},
        allow_override=False, forced_params={},
    )


# ── credential_guard ──────────────────────────────────────────────────────────

class TestCredentialGuard:
    def test_stash_pop_roundtrip(self):
        key = credential_guard.stash_key("t1", "c1")
        credential_guard.stash_credential_args(key, {"username": "u", "password": "p"})
        assert credential_guard.pop_credential_args(key) == {"username": "u", "password": "p"}
        # 一次性消费:第二次取不到
        assert credential_guard.pop_credential_args(key) is None

    def test_mark_handled_and_pop_flags(self):
        """mark_handled 同时置位两个脱敏标记,各自一次性消费、互不抢占。"""
        credential_guard.mark_handled("t1")
        assert credential_guard.pop_msg_erase("t1") is True
        assert credential_guard.pop_msg_erase("t1") is False
        assert credential_guard.pop_payload_mask("t1") is True
        assert credential_guard.pop_payload_mask("t1") is False

    def test_cleanup_task_only_own(self):
        credential_guard.stash_credential_args("t1:c1", {"username": "u"})
        credential_guard.stash_credential_args("t2:c1", {"username": "v"})
        credential_guard.mark_handled("t1")
        credential_guard.cleanup_task("t1")
        assert credential_guard.pop_credential_args("t1:c1") is None
        assert credential_guard.pop_credential_args("t2:c1") == {"username": "v"}
        assert credential_guard.pop_msg_erase("t1") is False
        assert credential_guard.pop_payload_mask("t1") is False


# ── graphs._sanitize_credential_tool_calls(§10.3 动作1+2) ────────────────────

class TestSanitizeCredentialToolCalls:
    def test_strips_plaintext_keeps_human_message(self):
        msgs = [HumanMessage(content="账号是 zhangsan 密码是 p@ss123,帮我查一下订单")]
        calls = [
            {"id": "call_1", "name": "保存PFT12301下单账号凭证",
             "arguments": json.dumps({"username": "zhangsan", "password": "p@ss123"})},
            {"id": "call_2", "name": "query_order", "arguments": json.dumps({"order_id": "123"})},
        ]
        tools_by_name = {
            "保存PFT12301下单账号凭证": _credential_spec(),
            "query_order": _normal_spec(),
        }
        out = _sanitize_credential_tool_calls("t1", calls, tools_by_name)

        # 保存凭证调用:username/password 已被剥离
        safe_args = json.loads(out[0]["arguments"])
        assert "username" not in safe_args and "password" not in safe_args
        # 普通调用原样保留
        assert json.loads(out[1]["arguments"]) == {"order_id": "123"}
        # v1.1.1 起 sanitize 不再原地擦除 HumanMessage —— 擦除改由 mark_handled
        # 置位、tools_node 消费,因此本轮消息原文保留
        assert msgs[0].content == "账号是 zhangsan 密码是 p@ss123,帮我查一下订单"
        # 明文暂存缓存,审批通过后可取回
        assert credential_guard.pop_credential_args("t1:call_1") == {
            "username": "zhangsan", "password": "p@ss123",
            "scope": None, "target_tool_id": "tool_123",
        }
        # 脱敏标记不再由 sanitize 置位(v1.1.1:由 mark_handled 驱动)
        assert credential_guard.pop_msg_erase("t1") is False
        assert credential_guard.pop_payload_mask("t1") is False

    def test_non_credential_call_untouched(self):
        calls = [{"id": "c", "name": "query_order", "arguments": '{"a": 1}'}]
        out = _sanitize_credential_tool_calls("t1", calls, {"query_order": _normal_spec()})
        assert out[0]["arguments"] == '{"a": 1}'
        assert credential_guard.pop_msg_erase("t1") is False

    def test_unknown_tool_untouched(self):
        """模型调用了 tools_by_name 里不存在的工具(幻觉)——不剥离不擦除。"""
        calls = [{"id": "c", "name": "ghost", "arguments": '{"username": "u"}'}]
        out = _sanitize_credential_tool_calls("t1", calls, {})
        assert out[0]["arguments"] == '{"username": "u"}'


# ── tools_adapter 保存凭证分支(§10.2) ────────────────────────────────────────

class TestCredentialSaveTool:
    @pytest.mark.asyncio
    async def test_approved_saves_and_returns_ok(self):
        spec = _credential_spec()
        credential_guard.stash_credential_args(
            credential_guard.stash_key("t1", "call_1"),
            {"username": "u", "password": "p", "scope": "prod", "target_tool_id": "tool_123"},
        )
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt", return_value=True),
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()) as sv,
            patch("cogni_agent.runtime_service.engine.tools_adapter.write_audit", new=AsyncMock()) as wau,
        ):
            outcome = await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p", "scope": "prod"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="u1", call_id="call_1",
            )
        assert outcome.kind == "ok"
        assert "已安全保存" in str(outcome.content)
        sv.assert_awaited_once()
        args, kwargs = sv.await_args
        assert args[0] == "tool_123"       # tool_id(位置参数)
        assert args[1] == "u:p"            # secret(位置参数,组合明文)
        assert kwargs["user_id"] == "u1"
        assert kwargs["scope"] == "prod"
        # 审计留痕:event_type 与 detail 不含明文
        assert wau.await_args.args[0] == "credential.saved"
        assert "username" not in str(wau.await_args.args[3]) and "password" not in str(wau.await_args.args[3])

    @pytest.mark.asyncio
    async def test_approval_payload_has_no_plaintext(self):
        """§10.3 动作1:HITL 审批 payload 里不能有 username/password。"""
        spec = _credential_spec()
        credential_guard.stash_credential_args(
            credential_guard.stash_key("t1", "call_1"),
            {"username": "u", "password": "p", "scope": None, "target_tool_id": "tool_123"},
        )
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt") as m_interrupt,
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()) as sv,
        ):
            m_interrupt.return_value = True
            sv.return_value = None
            await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="u1", call_id="call_1",
            )
        payload = m_interrupt.call_args.args[0]
        assert "username" not in payload["params"]
        assert "password" not in payload["params"]

    @pytest.mark.asyncio
    async def test_save_without_user_identity_rejected(self):
        """v0.1.4:凭证必属具体用户。无用户身份(user_id 为空)时拒绝保存,绝不落成共享凭证。"""
        spec = _credential_spec()
        credential_guard.stash_credential_args(
            credential_guard.stash_key("t1", "call_1"),
            {"username": "u", "password": "p", "scope": None, "target_tool_id": "tool_123"},
        )
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt", return_value=True),
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()) as sv,
        ):
            outcome = await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="", call_id="call_1",
            )
        assert outcome.kind == "format_error"
        assert "用户身份" in str(outcome.content)
        sv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pending_degrades_safely(self):
        """缓存丢失(如审批期间服务重启)→ 安全降级,不保存、不回显明文。"""
        spec = _credential_spec()
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt", return_value=True),
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()) as sv,
        ):
            outcome = await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="u1", call_id="no_such_call",
            )
        assert outcome.kind == "error"
        assert "过期" in str(outcome.content)
        sv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignored_skips_save_and_clears_plaintext(self):
        """E 方案(v0.1.5):用户「忽略」保存凭证——不抛 ApprovalRejected、不取消任务,
        跳过保存并清理明文缓存,返回 ok 让模型感知后继续对话。"""
        spec = _credential_spec()
        key = credential_guard.stash_key("t1", "call_1")
        credential_guard.stash_credential_args(
            key, {"username": "u", "password": "p", "scope": None, "target_tool_id": "tool_123"},
        )
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt", return_value=False),
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()) as sv,
        ):
            outcome = await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="u1", call_id="call_1",
            )
        assert outcome.kind == "ok"
        assert "未确认保存" in str(outcome.content)
        sv.assert_not_awaited()
        # 明文缓存已被清理,不会滞留内存
        assert credential_guard.pop_credential_args(key) is None

    @pytest.mark.asyncio
    async def test_interrupt_payload_carries_target_tool_id(self):
        """E 方案(v0.1.5):确认卡片要展示"给哪个工具保存凭证"——interrupt payload
        的 params 里补充 target_tool_id,且不含 username/password 明文。"""
        spec = _credential_spec()
        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt") as m_interrupt,
            patch("cogni_agent.runtime_service.engine.tools_adapter.save_credential", new=AsyncMock()),
        ):
            m_interrupt.return_value = False
            await execute_single_tool_call(
                AsyncMock(), task_id="t1", tool_name="保存PFT12301下单账号凭证",
                raw_arguments='{"username": "u", "password": "p"}',
                bound_tools={"保存PFT12301下单账号凭证": spec},
                scenic_id="s1", role="user", user_id="u1", call_id="call_1",
            )
        payload = m_interrupt.call_args.args[0]
        assert payload["params"].get("target_tool_id") == "tool_123"
        assert "username" not in payload["params"]
        assert "password" not in payload["params"]


# ── resolver 注入构造(§10.2) ─────────────────────────────────────────────────

class TestCredentialToolBuild:
    def test_placeholder_detection(self):
        assert _uses_credential_placeholder({"headers": {"Authorization": "Bearer {{credential}}"}})
        assert _uses_credential_placeholder({"url": "https://x?token={{credential}}"})
        assert not _uses_credential_placeholder({"url": "https://x"})
        assert not _uses_credential_placeholder({"nested": {"a": [1, 2]}})
        assert not _uses_credential_placeholder(None)

    def test_build_credential_tool(self):
        spec = _credential_spec()
        assert spec.type == "credential_save"
        assert spec.risk_level == "high"  # HITL 兜底
        assert spec.type_config == {"target_tool_id": "tool_123"}
        assert "username" in spec.json_schema["required"]
        assert "password" in spec.json_schema["required"]
