"""
§1.4 tools_adapter 集成测试。

覆盖: HTTP/DB/Python/knowledge 工具调用、Schema 校验、HITL 中断、
参数格式错误自愈。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogni_agent.runtime_service.engine.tools_adapter import (
    bound_tools_map, ToolCallOutcome,
    clear_param_cache, execute_single_tool_call, _parse_arguments,
)
from cogni_agent.runtime_service.engine.resolver import ToolSpec


def _tool_spec(sid="t1", name="query_db", risk="low", schema=None, allow_override=True, forced=None):
    s = schema or {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    return ToolSpec(
        id=sid, name=name, description="desc", type="db",
        json_schema=s, risk_level=risk, type_config={},
        allow_override=allow_override, forced_params=forced or {},
    )


# ── bound_tools_map ───────────────────────────────────────────────────────────

class TestBoundToolsMap:
    def test_uses_func_name_when_set(self):
        t1 = _tool_spec(sid="t1", name="查询订单")
        t1.func_name = "query_order"
        m = bound_tools_map([t1])
        assert "query_order" in m
        assert m["query_order"].name == "查询订单"

    def test_fallsback_to_name(self):
        t1 = _tool_spec(sid="t1", name="查询订单")
        m = bound_tools_map([t1])
        assert "查询订单" in m

    def test_same_name_different_specs_no_overwrite(self):
        t1 = _tool_spec(sid="t1", name="query")
        t1.func_name = "query_t1"
        t2 = _tool_spec(sid="t2", name="query")
        t2.func_name = "query_t2"
        m = bound_tools_map([t1, t2])
        assert len(m) == 2
        assert m["query_t1"].id == "t1"
        assert m["query_t2"].id == "t2"


# ── 参数解析 ──────────────────────────────────────────────────────────────────

class TestParseArguments:
    def test_dict_passthrough(self):
        args, err = _parse_arguments({"name": "张三"})
        assert args == {"name": "张三"}
        assert err is None

    def test_json_string(self):
        args, err = _parse_arguments('{"name": "张三"}')
        assert args == {"name": "张三"}
        assert err is None

    def test_empty_none(self):
        args, err = _parse_arguments(None)
        assert args == {}
        assert err is None

    def test_invalid_json(self):
        args, err = _parse_arguments("not json")
        assert args is None
        assert "JSON" in err

    def test_array_not_dict(self):
        args, err = _parse_arguments("[1,2,3]")
        assert args is None
        assert "对象" in err


# ── execute_single_tool_call ──────────────────────────────────────────────────

class TestExecuteSingleToolCall:
    @pytest.mark.asyncio
    async def test_hallucination_tool_not_bound(self):
        db = AsyncMock()
        result = await execute_single_tool_call(
            db, task_id="t1", tool_name="fake_tool", raw_arguments={},
            bound_tools={"real_tool": _tool_spec(sid="t1", name="real_tool")},
            scenic_id="s001", role="scenic_admin", user_id="u1",
        )
        assert result.kind == "hallucination"

    @pytest.mark.asyncio
    async def test_format_error_bad_json(self):
        db = AsyncMock()
        t = _tool_spec()
        result = await execute_single_tool_call(
            db, task_id="t1", tool_name="query_db", raw_arguments="bad json",
            bound_tools={"query_db": t}, scenic_id="s001", role="scenic_admin", user_id="u1",
        )
        assert result.kind == "format_error"

    @pytest.mark.asyncio
    async def test_schema_validation_failure(self):
        db = AsyncMock()
        t = _tool_spec(schema={
            "type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]
        })
        result = await execute_single_tool_call(
            db, task_id="t1", tool_name="query_db", raw_arguments={"name": "张三"},
            bound_tools={"query_db": t}, scenic_id="s001", role="scenic_admin", user_id="u1",
        )
        assert result.kind == "format_error"

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        db = AsyncMock()
        t = _tool_spec()
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.return_value = "查询结果: 5000"
            result = await execute_single_tool_call(
                db, task_id="t1", tool_name="query_db", raw_arguments={"name": "张三"},
                bound_tools={"query_db": t}, scenic_id="s001", role="scenic_admin", user_id="u1",
            )
            assert result.kind == "ok"
            assert "5000" in str(result.content)

    @pytest.mark.asyncio
    async def test_tool_execution_error_does_not_crash(self):
        db = AsyncMock()
        t = _tool_spec()
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = RuntimeError("DB connection lost")
            result = await execute_single_tool_call(
                db, task_id="t1", tool_name="query_db", raw_arguments={"name": "张三"},
                bound_tools={"query_db": t}, scenic_id="s001", role="scenic_admin", user_id="u1",
            )
            assert result.kind == "error"
            assert "DB connection lost" in result.content

    @pytest.mark.asyncio
    async def test_forced_params_merged(self):
        db = AsyncMock()
        t = _tool_spec(schema={
            "type": "object", "properties": {"name": {"type": "string"}, "scenic_id": {"type": "string"}},
            "required": ["name"]
        }, forced={"scenic_id": "s001"})

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.return_value = "ok"
            await execute_single_tool_call(
                db, task_id="t1", tool_name="query_db", raw_arguments={"name": "张三"},
                bound_tools={"query_db": t}, scenic_id="s001", role="scenic_admin", user_id="u1",
            )
            call_args = m_invoke.call_args
            merged = call_args[0][4]
            assert merged.get("scenic_id") == "s001"
            assert merged.get("name") == "张三"


# ── 参数来源解析(x-param-source) ─────────────────────────────────────────────

class TestParamSources:
    """供应商ID等派生参数:解析期从模型可见 Schema 剥离,执行期自动调前置工具注入。"""

    def _upstream_spec(self, risk="low"):
        s = ToolSpec(
            id="t_up", name="通过景区ID获取供应商ID列表", description="upstream",
            type="http", risk_level=risk,
            json_schema={"type": "object", "properties": {"scenicId": {"type": "string"}},
                         "required": ["scenicId"]},
            type_config={}, allow_override=True, forced_params={},
        )
        s.func_name = "getSupplierIdByScenicId"
        return s

    def _target_spec(self):
        schema = {
            "type": "object",
            "properties": {
                "scenicId": {"type": "string"},
                "supplierId": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["scenicId"],
        }
        s = ToolSpec(
            id="t_target", name="查询客流", description="target", type="http", risk_level="low",
            json_schema=schema, validate_schema=schema, type_config={},
            allow_override=True, forced_params={},
            param_sources={"supplierId": {
                "tool": "getSupplierIdByScenicId", "key": "scenicId", "path": "data.supplierId"}},
        )
        s.func_name = "query_passenger"
        return s

    def _tools(self):
        return bound_tools_map([self._upstream_spec(), self._target_spec()])

    @pytest.mark.asyncio
    async def test_auto_resolve_before_execute(self):
        db = AsyncMock()
        clear_param_cache()
        tools = self._tools()

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"supplierId": [1757026]}}
            return "客流结果"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "ok"
            up_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_up"]
            assert len(up_calls) == 1
            assert up_calls[0].args[4] == {"scenicId": "PFTSCA01018088"}
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_target"]
            assert target_calls[0].args[4] == {"scenicId": "PFTSCA01018088", "supplierId": [1757026]}

    @pytest.mark.asyncio
    async def test_result_cached_per_task(self):
        db = AsyncMock()
        clear_param_cache()
        tools = self._tools()

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"supplierId": [1757026]}}
            return "客流结果"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            for _ in range(2):
                r = await execute_single_tool_call(
                    db, task_id="task1", tool_name="query_passenger",
                    raw_arguments={"scenicId": "PFTSCA01018088"},
                    bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
                )
                assert r.kind == "ok"
            up_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_up"]
            assert len(up_calls) == 1  # 同 task 缓存命中,只查一次
        clear_param_cache()

    @pytest.mark.asyncio
    async def test_model_filled_value_overridden(self):
        db = AsyncMock()
        clear_param_cache()
        tools = self._tools()

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"supplierId": [1757026]}}
            return "客流结果"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088", "supplierId": [999999]},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "ok"
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_target"]
            assert target_calls[0].args[4]["supplierId"] == [1757026]  # 臆造值被真实值覆盖
        clear_param_cache()

    @pytest.mark.asyncio
    async def test_missing_upstream_tool(self):
        db = AsyncMock()
        clear_param_cache()
        target = self._target_spec()
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            result = await execute_single_tool_call(
                db, task_id="task2", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools={"query_passenger": target}, scenic_id="s001",
                role="group_admin", user_id="u1",
            )
            assert result.kind == "error"  # 配置/上游问题,不是模型格式错误(任务 ababbb8c)
            assert "getSupplierIdByScenicId" in result.content
            m_invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_upstream_failure_propagates(self):
        db = AsyncMock()
        clear_param_cache()
        tools = self._tools()

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                raise RuntimeError("供应商接口不可用")
            return "客流结果"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "error"  # 配置/上游问题,不是模型格式错误(任务 ababbb8c)
            assert "自动获取参数 supplierId 失败" in result.content
            assert "供应商接口不可用" in result.content
            # 目标工具未被调用
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_target"]
            assert not target_calls
        clear_param_cache()

    @pytest.mark.asyncio
    async def test_bad_path_declaration(self):
        db = AsyncMock()
        clear_param_cache()
        target = self._target_spec()
        target.param_sources["supplierId"]["path"] = "data.supplierIds"  # 错误的字段名
        tools = bound_tools_map([self._upstream_spec(), target])

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"supplierId": [1757026]}}
            return "x"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "error"  # 配置/上游问题,不是模型格式错误(任务 ababbb8c)
            assert "data.supplierIds" in result.content
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_target"]
            assert not target_calls
        clear_param_cache()

    @pytest.mark.asyncio
    async def test_high_risk_upstream_blocked(self):
        db = AsyncMock()
        clear_param_cache()
        tools = bound_tools_map([self._upstream_spec(risk="high"), self._target_spec()])
        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "error"  # 配置/上游问题,不是模型格式错误(任务 ababbb8c)
            assert "高风险" in result.content
            m_invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_key_from_session_context(self):
        """真实场景:客流类工具自身没有 scenicId 参数,key 用 $ctx.scenicId 从会话绑定景区取。"""
        db = AsyncMock()
        clear_param_cache()
        s = ToolSpec(
            id="t_target", name="今日客流统计", description="target", type="http", risk_level="low",
            # json_schema 是解析期剥离后的可见 Schema:派生参数被移除、required 同步清空
            json_schema={"type": "object", "properties": {}, "required": []},
            validate_schema={"type": "object", "properties": {"supplierId": {"type": "array"}},
                             "required": ["supplierId"]},
            type_config={}, allow_override=True, forced_params={},
            param_sources={"supplierId": {
                "tool": "getSupplierIdByScenicId", "key": "$ctx.scenicId", "path": "data.supplierId"}},
        )
        s.func_name = "query_passenger"
        target = s
        tools = bound_tools_map([self._upstream_spec(), target])

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"supplierId": [1757026]}}
            return "客流结果"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="query_passenger",
                raw_arguments={},
                bound_tools=tools, scenic_id="PFTSCA01018088", role="group_admin", user_id="u1",
            )
            assert result.kind == "ok"
            up_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_up"]
            assert up_calls[0].args[4] == {"scenicId": "PFTSCA01018088"}
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_target"]
            assert target_calls[0].args[4] == {"supplierId": [1757026]}
        clear_param_cache()

    @pytest.mark.asyncio
    async def test_extract_path_with_index(self):
        db = AsyncMock()
        clear_param_cache()
        s = ToolSpec(
            id="t_pick", name="解析供应商", description="x", type="http", risk_level="low",
            json_schema={"type": "object", "properties": {"scenicId": {"type": "string"}}},
            type_config={}, allow_override=True, forced_params={},
            param_sources={"supplierId": {
                "tool": "getSupplierIdByScenicId", "key": "scenicId", "path": "data.list[0].id"}},
        )
        s.func_name = "parse_supplier"
        upstream = self._upstream_spec()
        tools = bound_tools_map([upstream, s])

        async def fake_invoke(_db, tool_id, _scenic, _role, params, **_kw):
            if tool_id == "t_up":
                return {"code": 0, "data": {"list": [{"id": 1757026}]}}
            return "ok"

        with patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke:
            m_invoke.side_effect = fake_invoke
            result = await execute_single_tool_call(
                db, task_id="task1", tool_name="parse_supplier",
                raw_arguments={"scenicId": "PFTSCA01018088"},
                bound_tools=tools, scenic_id="s001", role="group_admin", user_id="u1",
            )
            assert result.kind == "ok"
            target_calls = [c for c in m_invoke.call_args_list if c.args[1] == "t_pick"]
            assert target_calls[0].args[4] == {"scenicId": "PFTSCA01018088", "supplierId": 1757026}
        clear_param_cache()


# ── knowledge 类型 ────────────────────────────────────────────────────────────

class TestKnowledgeTool:
    @pytest.mark.asyncio
    async def test_knowledge_tool_routes_directly(self):
        db = AsyncMock()
        t = ToolSpec(
            id="__knowledge__s1", name="knowledge_search", description="search",
            type="knowledge", risk_level="low",
            json_schema={"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"]},
            type_config={"sources": ["vector"], "skill_id": "s1"},
            allow_override=False, forced_params={},
        )

        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.route_and_retrieve") as m_retrieve,
            patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke,
        ):
            m_retrieve.return_value = [{"source": "doc1", "text": "知识内容"}]
            result = await execute_single_tool_call(
                db, task_id="t1", tool_name="knowledge_search", raw_arguments={"query": "客流"},
                bound_tools={"knowledge_search": t}, scenic_id="s001",
                role="scenic_admin", user_id="u1",
            )
            assert result.kind == "ok"
            m_retrieve.assert_awaited_once()
            m_invoke.assert_not_called()


# ── HITL ─────────────────────────────────────────────────────────────────────

class TestHITL:
    @pytest.mark.asyncio
    async def test_high_risk_tool_rejected(self):
        from cogni_agent.common.errors import ApprovalRejected

        db = AsyncMock()
        t = ToolSpec(
            id="t_high", name="publish", description="desc", type="http",
            json_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            risk_level="high", type_config={}, allow_override=False, forced_params={},
        )

        with patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt") as m_interrupt:
            m_interrupt.return_value = False
            with pytest.raises(ApprovalRejected):
                await execute_single_tool_call(
                    db, task_id="t1", tool_name="publish", raw_arguments={"content": "x"},
                    bound_tools={"publish": t}, scenic_id="s001",
                    role="scenic_admin", user_id="u1",
                )

    @pytest.mark.asyncio
    async def test_high_risk_tool_approved(self):
        db = AsyncMock()
        t = ToolSpec(
            id="t_high", name="publish", description="desc", type="http",
            json_schema={"type": "object", "properties": {"content": {"type": "string"}}},
            risk_level="high", type_config={}, allow_override=False, forced_params={},
        )

        with (
            patch("cogni_agent.runtime_service.engine.tools_adapter.interrupt") as m_interrupt,
            patch("cogni_agent.runtime_service.engine.tools_adapter.invoke_tool") as m_invoke,
        ):
            m_interrupt.return_value = True
            m_invoke.return_value = "发布成功"
            result = await execute_single_tool_call(
                db, task_id="t1", tool_name="publish", raw_arguments={"content": "x"},
                bound_tools={"publish": t}, scenic_id="s001",
                role="scenic_admin", user_id="u1",
            )
            assert result.kind == "ok"
            assert "发布成功" in str(result.content)
