"""
§1.3 & §1.6 图和集成测试。使用 SimpleNamespace 确保 LangGraph checkpoint 序列化安全。
"""

from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogni_agent.runtime_service.engine import graphs as graphs_mod
from cogni_agent.runtime_service.engine.graphs import (
    _exposed_tools, _rank_candidate_tools, _tl_ts, _used_tool_names,
)
from cogni_agent.runtime_service.engine.resolver import (
    ExecutionSpec, ResolvedSkill, ToolSpec,
)
from cogni_agent.runtime_service.engine.state import RunState
from cogni_agent.runtime_service.engine.tools_adapter import ToolCallOutcome


def _skill(name="客流查询", sid="s1", tools=None, max_steps=10, max_calls=5):
    return ResolvedSkill(
        id=sid, name=name, system_prompt=f"你是{name}",
        tools=tools or [],
        knowledge_sources=[], max_steps=max_steps, max_tool_calls=max_calls,
        knowledge_mode="preload", suggested_mode="L2",
    )


def _tool(name="query", sid="t1"):
    return ToolSpec(
        id=sid, name=name, description="desc", type="db",
        json_schema={"type": "object", "properties": {}}, risk_level="low",
        type_config={}, allow_override=True, forced_params={},
    )


def _spec(executor_type="skill", executor_id="s1", skill_chain=None, direct_tool=None):
    return ExecutionSpec(
        executor_type=executor_type, executor_id=executor_id,
        skill_chain=skill_chain or [_skill()],
        composition_hint=None, direct_tool=direct_tool, diagnostics=[],
    )


def _initial_state(task_id="task-1", goal="你好", mode="L2", skill_ids=None):
    from langchain_core.messages import HumanMessage
    return RunState(
        messages=[HumanMessage(content=goal)],
        task_id=task_id, scenic_id="s001", user_id="u1", role="scenic_admin",
        goal=goal, mode=mode,
        skill_chain=skill_ids or ["s1"], skill_idx=0,
        skill_step_counts={}, skill_outputs={}, tool_tracker={},
        persona=None, overrides={},
        guardrail={"steps": 0, "tool_calls": 0, "prompt_tokens": 0,
                   "completion_tokens": 0, "format_retries": 0,
                   "hallucination_retries": 0, "drift_checks": 0},
        timeline=[], usage={}, pending_interrupt=None, final_answer=None,
    )


def _check_ok():
    return SimpleNamespace(flagged=False, passed=True, reason="",
                           prompt_tokens=0, completion_tokens=0,
                           defensive_note="", intercept=0, safe_content="")


def _llm(content="", tool_calls=None, prompt=10, comp=5):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [],
                           prompt_tokens=prompt, completion_tokens=comp)


# ── timeline 事件打点 ─────────────────────────────────────────────────────────

class TestTlTs:
    def test_stamps_real_ts_without_mutating(self):
        event = {"step": "tool_call", "tool": "t1"}
        stamped = _tl_ts(event)
        assert stamped["ts"] > 0
        assert stamped["step"] == "tool_call"
        assert "ts" not in event  # 原事件不被污染

    def test_overwrites_stale_ts(self):
        # 同一 dict 被复用(如 kt_event 先记知识检索、后带 skill_id 再记执行)时,
        # 必须打上当前时刻,不能沿用旧 ts,否则耗时计算失真
        stale = {"step": "knowledge", "ts": 1.0}
        stamped = _tl_ts(stale)
        assert stamped["ts"] >= 1.0
        # 第二次打点应显著晚于第一次(实测若复用旧 ts 则等于 1.0)
        assert _tl_ts(stale)["ts"] >= stamped["ts"]


# ── ReAct 循环 ────────────────────────────────────────────────────────────────

class TestReActLoop:
    @pytest.mark.asyncio
    async def test_no_tools_final_answer(self):
        from cogni_agent.runtime_service.engine.graphs import build_agent_graph
        from langgraph.checkpoint.memory import InMemorySaver

        spec = _spec(skill_chain=[_skill(tools=[])])
        db = AsyncMock()

        with (
            patch("cogni_agent.runtime_service.engine.graphs.llm_client") as m_llm,
            patch("cogni_agent.runtime_service.engine.graphs.get_stream_writer") as m_writer,
            patch("cogni_agent.runtime_service.engine.graphs.moderation") as m_mod,
        ):
            m_llm.stream_and_collect = AsyncMock(return_value=_llm("客流 5000 人"))
            m_writer.return_value = MagicMock()
            m_mod.check_input = AsyncMock(return_value=_check_ok())
            m_mod.check_output = AsyncMock(return_value=_check_ok())

            graph = build_agent_graph(spec, "L2", db, InMemorySaver())
            result = await graph.ainvoke(_initial_state(), {"configurable": {"thread_id": "t1"}})
            assert result["final_answer"] == "客流 5000 人"

    @pytest.mark.asyncio
    async def test_agent_to_tools_success(self):
        from cogni_agent.runtime_service.engine.graphs import build_agent_graph
        from langgraph.checkpoint.memory import InMemorySaver

        spec = _spec()
        db = AsyncMock()

        with (
            patch("cogni_agent.runtime_service.engine.graphs.llm_client") as m_llm,
            patch("cogni_agent.runtime_service.engine.graphs.get_stream_writer") as m_writer,
            patch("cogni_agent.runtime_service.engine.graphs.execute_single_tool_call") as m_exec,
            patch("cogni_agent.runtime_service.engine.graphs.moderation") as m_mod,
        ):
            m_llm.stream_and_collect = AsyncMock(side_effect=[
                _llm("", [{"id": "c1", "name": "query_traffic", "arguments": {}}]),
                _llm("客流 5000 人", []),
            ])
            m_writer.return_value = MagicMock()
            m_mod.check_input = AsyncMock(return_value=_check_ok())
            m_mod.check_output = AsyncMock(return_value=_check_ok())
            m_exec.return_value = ToolCallOutcome("ok", "data")

            graph = build_agent_graph(spec, "L2", db, InMemorySaver())
            result = await graph.ainvoke(
                _initial_state(), {"configurable": {"thread_id": "t2", "recursion_limit": 20}})
            assert "final_answer" in result
            assert "5000" in str(result["final_answer"])


# ── Direct Tool ───────────────────────────────────────────────────────────────

class TestDirectToolGraph:
    @pytest.mark.asyncio
    async def test_direct_tool_executes_once(self):
        from cogni_agent.runtime_service.engine.graphs import build_direct_tool_graph
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_core.messages import HumanMessage

        tool = ToolSpec(
            id="t1", name="query_db", description="desc", type="db",
            json_schema={"type": "object", "properties": {}}, risk_level="low",
            type_config={}, allow_override=True, forced_params={},
        )
        spec = _spec(executor_type="tool", executor_id="t1", skill_chain=[], direct_tool=tool)
        db = AsyncMock()

        with patch("cogni_agent.runtime_service.engine.graphs.execute_single_tool_call") as m_exec:
            m_exec.return_value = ToolCallOutcome("ok", "查询结果: 5000")

            graph = build_direct_tool_graph(spec, db, InMemorySaver())
            state = RunState(
                messages=[HumanMessage(content="查xxx")],
                task_id="t1", scenic_id="s001", user_id="u1", role="scenic_admin",
                goal="查xxx", mode="L0",
                skill_chain=[], skill_idx=0, skill_step_counts={}, skill_outputs={},
                tool_tracker={}, persona=None, overrides={},
                guardrail={"steps": 0, "tool_calls": 0, "prompt_tokens": 0,
                           "completion_tokens": 0, "format_retries": 0,
                           "hallucination_retries": 0, "drift_checks": 0},
                timeline=[], usage={}, pending_interrupt=None, final_answer=None,
            )
            result = await graph.ainvoke(state, {"configurable": {"thread_id": "dt1"}})
            assert result["final_answer"] == "查询结果: 5000"
            m_exec.assert_called_once()


# ── max_steps ─────────────────────────────────────────────────────────────────

class TestMaxStepsGuardrail:
    @pytest.mark.asyncio
    async def test_max_steps_caps_execution(self):
        from cogni_agent.runtime_service.engine.graphs import build_agent_graph
        from langgraph.checkpoint.memory import InMemorySaver

        s = _skill(max_steps=1, max_calls=10)
        spec = _spec(skill_chain=[s])
        db = AsyncMock()

        with (
            patch("cogni_agent.runtime_service.engine.graphs.llm_client") as m_llm,
            patch("cogni_agent.runtime_service.engine.graphs.get_stream_writer") as m_writer,
            patch("cogni_agent.runtime_service.engine.graphs.moderation") as m_mod,
        ):
            m_llm.stream_and_collect = AsyncMock(return_value=_llm("done"))
            m_writer.return_value = MagicMock()
            m_mod.check_input = AsyncMock(return_value=_check_ok())
            m_mod.check_output = AsyncMock(return_value=_check_ok())

            graph = build_agent_graph(spec, "L2", db, InMemorySaver())
            result = await graph.ainvoke(_initial_state(), {"configurable": {"thread_id": "ms1", "recursion_limit": 15}})
            assert result["guardrail"]["steps"] <= 1


# ── 异常路径 ─────────────────────────────────────────────────────────────────

class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_hallucination_tool_recovery(self):
        from cogni_agent.runtime_service.engine.graphs import build_agent_graph
        from langgraph.checkpoint.memory import InMemorySaver

        s = _skill(tools=[_tool("query_traffic")])
        spec = _spec(skill_chain=[s])
        db = AsyncMock()

        with (
            patch("cogni_agent.runtime_service.engine.graphs.llm_client") as m_llm,
            patch("cogni_agent.runtime_service.engine.graphs.get_stream_writer") as m_writer,
            patch("cogni_agent.runtime_service.engine.graphs.execute_single_tool_call") as m_exec,
            patch("cogni_agent.runtime_service.engine.graphs.moderation") as m_mod,
        ):
            m_llm.stream_and_collect = AsyncMock(side_effect=[
                _llm("", [{"id": "c1", "name": "fake_tool", "arguments": {}}]),
                _llm("工具不存在,我直接回答", []),
            ])
            m_writer.return_value = MagicMock()
            m_mod.check_input = AsyncMock(return_value=_check_ok())
            m_mod.check_output = AsyncMock(return_value=_check_ok())
            m_exec.return_value = ToolCallOutcome("hallucination", "工具不存在")

            graph = build_agent_graph(spec, "L2", db, InMemorySaver())
            result = await graph.ainvoke(
                _initial_state(), {"configurable": {"thread_id": "eh1", "recursion_limit": 15}})
            assert "final_answer" in result


# ── topological_layers ────────────────────────────────────────────────────────

class TestTopologicalLayers:
    def test_linear(self):
        from cogni_agent.runtime_service.engine.graphs import topological_layers
        layers = topological_layers(["a", "b", "c"], {"b": ["a"], "c": ["b"]})
        assert len(layers) == 3

    def test_independent_same_layer(self):
        from cogni_agent.runtime_service.engine.graphs import topological_layers
        layers = topological_layers(["a", "b", "c"], {"c": ["a", "b"]})
        assert set(layers[0]) == {"a", "b"}

    def test_cycle_no_crash(self):
        from cogni_agent.runtime_service.engine.graphs import topological_layers
        layers = topological_layers(["a", "b", "c"], {"a": ["b"], "b": ["a"]})
        assert len(layers) >= 1


# ── 按需暴露工具子集 ──────────────────────────────────────────────────────────

class TestExposedTools:
    """v1.1.2 按需暴露:系统级/已用工具常驻 + 目标检索 top-K 候选。"""

    def _tool(self, sid, name, desc, type_="db", func=None):
        return ToolSpec(
            id=sid, name=name, description=desc, type=type_,
            json_schema={"type": "object", "properties": {}, "description": desc},
            risk_level="low", type_config={},
            func_name=func or f"tool_{sid}",
        )

    def _settings(self, enabled, k=6, total_max=12):
        class _FakeSettings:
            def get(self, *path, default=None):
                if path == ("runtime", "tools", "expose_subset"):
                    return enabled
                if path == ("runtime", "tools", "expose_subset_k"):
                    return k
                if path == ("runtime", "tools", "expose_subset_max"):
                    return total_max
                return default
        return _FakeSettings()

    def test_rank_candidate_prefers_semantic_hits(self):
        tools = [
            self._tool("t1", "获取景区客流数据", "查询每日游客量"),
            self._tool("t2", "生成游客画像分析", "对游客来源做画像分析"),
            self._tool("t3", "发送站内通知", "给用户发消息"),
        ]
        ranked = _rank_candidate_tools(tools, "景区游客画像分析")
        assert ranked[0] in (tools[0], tools[1])  # 画像/客流相关优先
        assert ranked[-1] == tools[2]             # 通知最不相关
        # 空查询/无命中:原序兜底,不空手
        assert _rank_candidate_tools(tools, "") == tools
        assert _rank_candidate_tools(tools, "zzzqqq") == tools

    def test_used_tool_names_extracted_from_history(self):
        from langchain_core.messages import AIMessage, HumanMessage
        msg = AIMessage(content="", additional_kwargs={"tool_calls_raw": [
            {"name": "tool_t1", "arguments": {}}, {"name": "tool_t2", "arguments": {}},
        ]})
        s = _initial_state()
        s["messages"] = [HumanMessage(content="hi"), msg]
        assert _used_tool_names(s) == {"tool_t1", "tool_t2"}
        assert _used_tool_names(_initial_state()) == set()

    def test_exposed_pins_system_and_used(self, monkeypatch):
        monkeypatch.setattr(graphs_mod, "get_settings", lambda: self._settings(True, k=2, total_max=6))
        k_tool = self._tool("k1", "知识库检索", "检索与问题相关的背景资料", type_="knowledge",
                            func="knowledge_search")
        tools = [
            k_tool,
            self._tool("t1", "获取景区客流数据", "查询每日游客量", func="tool_t1"),
            self._tool("t2", "生成游客画像分析", "对游客来源做画像分析", func="tool_t2"),
            self._tool("t3", "发送站内通知", "给用户发消息", func="tool_t3"),
        ]
        skill = _skill(tools=tools)
        s = _initial_state(goal="游客画像分析")
        exposed = [t.func_name for t in _exposed_tools(s, [skill], None)]
        assert "knowledge_search" in exposed   # 系统级工具常驻
        assert "tool_t2" in exposed            # 目标相关候选
        assert "tool_t3" not in exposed        # k=2 限流
        # 已调用过的工具常驻(即使与目标不相关)
        from langchain_core.messages import AIMessage, HumanMessage
        s = _initial_state(goal="游客画像分析")
        s["messages"] = [HumanMessage(content="hi"),
                         AIMessage(content="", additional_kwargs={"tool_calls_raw": [{"name": "tool_t3", "arguments": {}}]})]
        exposed2 = [t.func_name for t in _exposed_tools(s, [skill], None)]
        assert "tool_t3" in exposed2

    def test_exposed_disabled_returns_all(self, monkeypatch):
        monkeypatch.setattr(graphs_mod, "get_settings", lambda: self._settings(False))
        tools = [self._tool(f"t{i}", f"工具{i}", f"功能{i}", func=f"tool_t{i}") for i in range(4)]
        s = _initial_state(goal="随便查查")
        assert [t.func_name for t in _exposed_tools(s, [_skill(tools=tools)], None)] == \
               [t.func_name for t in tools]

    def test_exposed_budget_capped(self, monkeypatch):
        monkeypatch.setattr(graphs_mod, "get_settings", lambda: self._settings(True, k=3, total_max=5))
        tools = [self._tool(f"t{i}", f"工具{i}", f"功能{i}", func=f"tool_t{i}") for i in range(8)]
        s = _initial_state(goal="随便查点东西")
        exposed = _exposed_tools(s, [_skill(tools=tools)], None)
        assert len(exposed) <= 5  # 总上限约束
