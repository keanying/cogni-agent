"""会话链路优化(景区上下文注入/租户隔离/耗时打点/token 裁剪)的回归测试。

对应真实会话 conv_04f6701ff0324ceda82b47bf113c6786 审查后落地的优化项:
- 景区上下文无条件注入(_scenic_context_hint)
- 参数级景区越权校验(tools_adapter 景区级角色)
- timeline 每步耗时打点(_with_step_durations)
- 上下文 token 级二次裁剪(trim_messages + estimate_tokens)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cogni_agent.runtime_service.engine import context as ctx
from cogni_agent.runtime_service.engine.executor import _scenic_slots, _with_step_durations
from cogni_agent.runtime_service.engine.graphs import _scenic_context_hint
from cogni_agent.runtime_service.engine.resolver import ToolSpec
from cogni_agent.runtime_service.engine.tools_adapter import execute_single_tool_call


class TestScenicContextHint:
    def test_injects_binding_scenic_id(self):
        hint = _scenic_context_hint({"scenic_id": "PFTSCX00012325"})
        assert "PFTSCX00012325" in hint
        assert "不要向用户反问景区 ID" in hint

    def test_empty_when_no_scenic(self):
        assert _scenic_context_hint({}) == ""
        assert _scenic_context_hint({"scenic_id": "  "}) == ""


class TestScenicSlots:
    """方案2:Triager 槽位注入景区 ID 全部别名(scenicId 等)。

    平台统一 snake_case scenic_id,但数字大脑(data.12301dev.com)接口参数是驼峰
    scenicId;若槽位里只有 scenic_id,LLM 可能把 scenicId 误判为 missing 反问用户。
    _scenic_slots 把所有别名(值相同)一并注入,保证 Triager 无论面对哪种字段名
    都判定景区参数已填。与 tools_adapter 参数级越权校验共用同一份配置。
    """

    def test_injects_all_aliases(self):
        slots = _scenic_slots("PFTSCX00012325")
        assert slots["scenic_id"] == "PFTSCX00012325"
        # 数字大脑接口驼峰字段,必须注入避免 Triager 误判
        assert slots["scenicId"] == "PFTSCX00012325"
        assert slots["scene_id"] == "PFTSCX00012325"
        assert slots["spot_id"] == "PFTSCX00012325"

    def test_empty_when_no_scenic(self):
        assert _scenic_slots(None) == {}
        assert _scenic_slots("") == {}
        assert _scenic_slots("   ") == {}

    def test_config_override_applies(self):
        with patch("cogni_agent.runtime_service.engine.executor.get_settings") as m_gs:
            m_gs.return_value.get.return_value = ["scenic_id", "scenicId"]
            slots = _scenic_slots("PFTSCX00012325")
        assert set(slots) == {"scenic_id", "scenicId"}


class TestEstimateTokens:
    def test_positive(self):
        assert ctx.estimate_tokens("今天景区客流 5000 人次") > 0

    def test_empty(self):
        assert ctx.estimate_tokens("") == 0


class TestTrimByTokens:
    def _build(self):
        return [
            SystemMessage(content="人格"),
            HumanMessage(content="最初目标"),
            HumanMessage(content="历史问题1"),
            AIMessage(content="", additional_kwargs={"tool_calls_raw": [{"id": "c1", "name": "t", "arguments": "{}"}]}),
            ToolMessage(content="景" * 20000, tool_call_id="c1", name="t"),  # 约 2 万 token
            HumanMessage(content="最新问题"),
        ]

    def test_trims_when_over_budget(self):
        msgs = self._build()
        trimmed = ctx.trim_messages(msgs, keep_recent=10, max_tokens=5000)
        total = sum(ctx.estimate_tokens(str(m.content)) for m in trimmed)
        assert total <= 5000 + 2000
        # 目标锚点与人格保留
        assert any(isinstance(m, HumanMessage) and m.content == "最初目标" for m in trimmed)
        assert any(isinstance(m, SystemMessage) and m.content == "人格" for m in trimmed)

    def test_no_orphan_tool_messages(self):
        """裁剪后不能有孤立的 ToolMessage / 孤立的 tool_calls AIMessage(协议约束)。"""
        msgs = self._build()
        trimmed = ctx.trim_messages(msgs, keep_recent=10, max_tokens=5000)
        kinds = [type(m).__name__ for m in trimmed]
        tool_pos = [i for i, k in enumerate(kinds) if k == "ToolMessage"]
        assert all(i > 0 and kinds[i - 1] == "AIMessage" for i in tool_pos), kinds
        ai_pos = [i for i, k in enumerate(kinds) if k == "AIMessage"]
        assert all(i + 1 < len(kinds) and kinds[i + 1] == "ToolMessage" for i in ai_pos), kinds

    def test_no_trim_when_under_budget(self):
        small = [SystemMessage(content="s"), HumanMessage(content="h")]
        assert ctx.trim_messages(small, max_tokens=5000) == small


class TestWithStepDurations:
    def test_fills_duration_without_mutating(self):
        tl = [{"step": "mode_decision"}, {"step": "generation"}, {"step": "tool_call"}]
        out = _with_step_durations(tl)
        assert all("duration" in e for e in out)
        assert "duration" not in tl[0]  # 不污染原事件

    def test_preserves_existing_duration(self):
        tl = [{"step": "a", "duration": 12.0}, {"step": "b"}]
        out = _with_step_durations(tl)
        assert out[0]["duration"] == 12.0
        assert "duration" in out[1]

    def test_duration_uses_real_ts(self):
        base = 1000.0
        tl = [{"step": "a", "ts": base},
              {"step": "b", "ts": base + 0.5},
              {"step": "c", "ts": base + 1.5}]
        out = _with_step_durations(tl)
        # 首个事件相对自身 ts 起点 → 0;后续按真实 ts 相邻间隔
        assert out[0]["duration"] == 0.0
        assert out[1]["duration"] == 500.0
        assert out[2]["duration"] == 1000.0
        assert tl[0]["ts"] == base  # 不污染原事件

    def test_ts_backdated_clamped_to_zero(self):
        # 极端:ts 倒序时负间隔钳制为 0,不出现负耗时
        tl = [{"step": "a", "ts": 5.0}, {"step": "b", "ts": 3.0}]
        out = _with_step_durations(tl)
        assert out[0]["duration"] == 0.0
        assert out[1]["duration"] == 0.0

    def test_mixed_ts_and_legacy(self):
        base = 2000.0
        tl = [{"step": "a", "ts": base}, {"step": "b", "ts": base + 0.2},
              {"step": "c"}]  # 无 ts 的旧事件,回退遍历近似且不报错
        out = _with_step_durations(tl)
        assert out[0]["duration"] == 0.0
        assert out[1]["duration"] == 200.0
        assert "duration" in out[2]
        assert "ts" in out[2]


class TestScenicParamGuard:
    def _spec(self):
        return ToolSpec(
            id="t1", name="query_sentiment", description="舆情查询", type="http",
            json_schema={"type": "object", "properties": {"scenic_id": {"type": "string"}},
                         "required": []},
            risk_level="low", type_config={}, allow_override=False, forced_params={},
        )

    async def _call(self, raw_args: dict, scenic_id: str, role: str):
        db = AsyncMock()
        return await execute_single_tool_call(
            db, task_id="t1", tool_name="query_sentiment", raw_arguments=raw_args,
            bound_tools={"query_sentiment": self._spec()},
            scenic_id=scenic_id, role=role, user_id="u1",
        )

    @pytest.mark.asyncio
    async def test_scenic_role_rejected_when_id_mismatch(self):
        r = await self._call({"scenic_id": "PFTSCA01018088"}, "PFTSCX00012325", "scenic_admin")
        assert r.kind == "format_error"
        assert "景区" in str(r.content)

    @pytest.mark.asyncio
    async def test_group_role_allowed_across_scenic(self):
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool",
                   new=AsyncMock(return_value="ok")) as m:
            r = await self._call({"scenic_id": "PFTSCA01018088"}, "PFTSCX00012325", "group_admin")
        assert r.kind == "ok"
        m.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scenic_role_allowed_own_scenic(self):
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool",
                   new=AsyncMock(return_value="ok")) as m:
            r = await self._call({"scenic_id": "PFTSCX00012325"}, "PFTSCX00012325", "scenic_admin")
        assert r.kind == "ok"
        m.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_scenic_field_skips_guard(self):
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool",
                   new=AsyncMock(return_value="ok")) as m:
            r = await self._call({"name": "张三"}, "PFTSCX00012325", "scenic_admin")
        assert r.kind == "ok"
        m.assert_awaited_once()
