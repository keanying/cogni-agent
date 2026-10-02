"""
§1.2 resolver 单元测试。

覆盖 agent/skill/tool 路由、Prompt 关联（Skill.version→Prompt）、
函数名唯一性、景区隔离、参数覆盖优先级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogni_agent.runtime_service.engine.resolver import (
    ExecutionSpec, ResolvedSkill, ToolSpec, Diagnostic,
    _Ctx, ResolveError, _visible, _positive_int, _assign_function_names,
    _collect_param_sources, _to_tool_spec, _build_json_schema,
)


# ── 轻量固件 ─────────────────────────────────────────────────────────────────

def _ctx(user_id="u1", role="super_admin", scenic_id="s001", overrides=None):
    return _Ctx(user_id=user_id, role=role, scenic_id=scenic_id, overrides=overrides or {})


def _tool_spec(sid="t1", name="query_traffic", risk="low"):
    return ToolSpec(
        id=sid, name=name, description="查客流", type="db",
        json_schema={"type": "object", "properties": {}}, risk_level=risk,
        type_config={}, allow_override=True, forced_params={},
    )


# ── 可见性判定 ───────────────────────────────────────────────────────────────

class TestVisibility:
    """G-AUTH 四级可见范围: private / scenic / group / public。"""

    def test_owner_always_visible(self):
        assert _visible("private", "u1", "s001", ctx=_ctx(user_id="u1", role="scenic_admin")) is True

    def test_public_visible_to_all(self):
        assert _visible("public", "owner", "s001", ctx=_ctx(user_id="u2", role="scenic_admin")) is True

    def test_group_only_super_admin_scenic_admin(self):
        assert _visible("group", "owner", None, ctx=_ctx(user_id="u2", role="super_admin")) is True
        assert _visible("group", "owner", None, ctx=_ctx(user_id="u2", role="group_admin")) is True
        assert _visible("group", "owner", None, ctx=_ctx(user_id="u2", role="scenic_admin")) is False

    def test_scenic_same_scenic_only(self):
        assert _visible("scenic", "owner", "s001", ctx=_ctx(user_id="u2", role="scenic_admin", scenic_id="s001")) is True
        assert _visible("scenic", "owner", "s001", ctx=_ctx(user_id="u2", role="scenic_admin", scenic_id="s002")) is False

    def test_private_other_user_blocked(self):
        assert _visible("private", "owner", "s001", ctx=_ctx(user_id="u2", role="super_admin")) is False


# ── 数值安全 ─────────────────────────────────────────────────────────────────

class TestPositiveInt:
    def test_normal_value(self):
        assert _positive_int(10, 25) == 10

    def test_zero_returns_default(self):
        assert _positive_int(0, 25) == 25

    def test_negative_returns_default(self):
        assert _positive_int(-5, 25) == 25

    def test_none_returns_default(self):
        assert _positive_int(None, 25) == 25

    def test_string_returns_default(self):
        assert _positive_int("abc", 25) == 25


# ── 函数名唯一性 ─────────────────────────────────────────────────────────────

class TestFunctionNameUniqueness:
    def test_no_tools_noop(self):
        spec = ExecutionSpec(executor_type="skill", executor_id="s1")
        _assign_function_names(spec, _ctx())
        # 不抛异常

    def test_direct_tool_gets_name(self):
        t = _tool_spec()
        spec = ExecutionSpec(executor_type="tool", executor_id="t1", direct_tool=t)
        _assign_function_names(spec, _ctx())
        assert t.func_name is not None
        assert t.name in t.func_name

    def test_two_skills_same_tool_unique_name(self):
        """同一工具被两个 Skill 绑定，全局唯一函数名一致。"""
        t1 = _tool_spec(sid="t1", name="query_traffic")
        t2 = _tool_spec(sid="t1", name="query_traffic")  # 相同 id,不同对象
        s1 = ResolvedSkill(id="s1", name="A", system_prompt="", tools=[t1],
                           knowledge_sources=[], max_steps=10, max_tool_calls=5)
        s2 = ResolvedSkill(id="s2", name="B", system_prompt="", tools=[t2],
                           knowledge_sources=[], max_steps=10, max_tool_calls=5)
        spec = ExecutionSpec(executor_type="skill", executor_id="s1", skill_chain=[s1, s2])
        _assign_function_names(spec, _ctx())
        assert t1.func_name == t2.func_name  # 同一 tool_id → 同一 func_name


# ── _build_json_schema ────────────────────────────────────────────────────────

class TestBuildJsonSchema:
    """db 类型从 SQL 模板推导 JSON Schema。"""

    def test_db_schema_derives_variables(self):
        from types import SimpleNamespace

        from cogni_agent.runtime_service.engine.resolver import _build_json_schema

        # v1.2(0051):内容只在版本快照视图(SimpleNamespace)上,不再构造 ORM Tool
        tool = SimpleNamespace(type="db", type_config={"sql_template": "SELECT * FROM table WHERE scenic_id = {{scenic_id}} AND name = {{keyword}}"})
        schema, allow = _build_json_schema(tool)
        # scenic_id 是内置变量,不应出现在 schema 里
        assert "scenic_id" not in schema.get("properties", {})
        assert "keyword" in schema.get("properties", {})
        assert allow is True

    def test_db_schema_all_builtins(self):
        from types import SimpleNamespace

        from cogni_agent.runtime_service.engine.resolver import _build_json_schema

        tool = SimpleNamespace(type="db", type_config={"sql_template": "SELECT * FROM t WHERE org_id = {{org_id}} AND user = {{user_id}}"})
        schema, allow = _build_json_schema(tool)
        # 全是内置变量,不需要模型提供任何参数
        assert not schema.get("properties", {})
        assert allow is True


# ── 参数覆盖优先级 ────────────────────────────────────────────────────────────

class TestParameterOverride:
    def test_forced_params_present(self):
        t = ToolSpec(
            id="t1", name="pub", description="发布", type="http",
            json_schema={"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
            risk_level="high", type_config={}, allow_override=True,
            forced_params={"scenic_id": "s001"},
        )
        assert t.forced_params == {"scenic_id": "s001"}
        # verify_schema_forward_compat — 不抛异常即通过
        assert t.allow_override is True

    def test_allow_override_false_blocks_forced(self):
        """MCP 工具 allow_override=False 时即使传了 forced_params 也应被忽略。"""
        from types import SimpleNamespace

        from cogni_agent.runtime_service.engine.resolver import _build_json_schema, _to_tool_spec

        # v1.2(0051):运行时工具内容走版本快照视图(SimpleNamespace),不再构造 ORM Tool
        tool = SimpleNamespace(
            id="mcp-1", name="mcp_tool", description="remote", type="mcp",
            risk_level="low",
            type_config={"input_schema": {"type": "object", "properties": {}}},
            active=True,
        )
        ctx = _ctx(overrides={"tool_params": {"mcp-1": {"forced_key": "forced_val"}}})
        spec = _to_tool_spec(tool, ctx=ctx)
        assert spec.allow_override is False
        assert spec.forced_params == {}  # 被忽略


# ── 执行模式判定 ────────────────────────────────────────────────────────────

class TestModeDecision:
    """多 Skill 的模式判定:**按可用工具规模在 ReAct(L2)与 Plan-and-Execute(L4)之间选**。

    原来是按 Agent.composition_mode(chain/dag/pool)决定编排形态,已废弃。
    那是「设计时把路径定死」的思路:建 Agent 时就要想清楚这几个 Skill 该串行还是并行,
    而同一个 Agent 面对不同问题需要的路径本来就不同 ——
    「今天客流多少」要一步,「分析 Q2 趋势并给建议」要五步,绑到同一种编排上,
    要么简单问题绕远路,要么复杂问题步子不够。

    现在判定依据是工具规模:工具越多,ReAct 每轮重新挑越容易跑偏,
    先规划把「用哪些」收敛成一次决策的收益就越大。
    """

    @staticmethod
    def _spec(n_tools_each: int, n_skills: int = 2, hint=None):
        skills = [
            ResolvedSkill(id=f"s{i}", name=f"技能{i}", system_prompt="",
                          tools=[_tool_spec() for _ in range(n_tools_each)],
                          knowledge_sources=[], max_steps=10, max_tool_calls=5)
            for i in range(n_skills)
        ]
        return ExecutionSpec(executor_type="agent", executor_id="a1",
                             skill_chain=skills, composition_hint=hint)

    @pytest.mark.asyncio
    async def test_many_tools_go_plan_and_execute(self):
        """工具多 -> 先规划再执行。过程可见、可中断,工具选择也更稳。"""
        from cogni_agent.runtime_service.engine.modes import decide_mode

        decision = await decide_mode(self._spec(n_tools_each=3))   # 共 6 个
        assert decision.mode == "L4"
        assert decision.intent == "planned_multi_step_task"

    @pytest.mark.asyncio
    async def test_few_tools_stay_react(self):
        """工具少 -> 规划的收益抵不过额外一次模型调用的延迟,走 ReAct。"""
        from cogni_agent.runtime_service.engine.modes import decide_mode

        decision = await decide_mode(self._spec(n_tools_each=1, n_skills=2))   # 共 2 个
        assert decision.mode == "L2"

    @pytest.mark.asyncio
    async def test_composition_hint_no_longer_decides_mode(self):
        """**composition_hint 不再影响模式判定** —— 同样的工具规模,
        不管标成 pool / dag / chain,结论都一样。"""
        from cogni_agent.runtime_service.engine.modes import decide_mode

        modes = set()
        for hint in (None, "pool", "dag", "chain"):
            d = await decide_mode(self._spec(n_tools_each=3, hint=hint))
            modes.add(d.mode)
        assert len(modes) == 1, f"编排模式不该再影响判定,实际得到 {modes}"

    @pytest.mark.asyncio
    async def test_reason_explains_the_choice(self):
        """判定理由要说清「为什么是这个模式」,而不只是给个档位 ——
        排查「为什么这次没规划」时,理由是唯一线索。"""
        from cogni_agent.runtime_service.engine.modes import decide_mode

        d = await decide_mode(self._spec(n_tools_each=3))
        assert d.reasons and any("工具" in r for r in d.reasons)


# ── 诊断收集 ──────────────────────────────────────────────────────────────────

class TestDiagnosticsCollection:
    def test_ctx_warn_adds_diagnostic(self):
        ctx = _ctx()
        ctx.warn("test_code", "test message", "ref-1")
        assert len(ctx.diagnostics) == 1
        assert ctx.diagnostics[0].level == "warn"
        assert ctx.diagnostics[0].code == "test_code"

    def test_ctx_info_adds_diagnostic(self):
        ctx = _ctx()
        ctx.info("info_code", "info message")
        assert len(ctx.diagnostics) == 1
        assert ctx.diagnostics[0].level == "info"

    def test_diagnostics_as_dicts(self):
        spec = ExecutionSpec(
            executor_type="skill", executor_id="s1",
            diagnostics=[Diagnostic("warn", "A", "msg", None), Diagnostic("info", "B", "msg2", "r2")],
        )
        ds = spec.diagnostics_as_dicts()
        assert len(ds) == 2
        assert ds[0]["level"] == "warn"


# ── 参数来源声明(x-param-source) ──────────────────────────────────────────────

class TestParamSourceCollection:
    def test_collect_from_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "scenicId": {"type": "string"},
                "supplierId": {
                    "type": "array", "items": {"type": "integer"},
                    "x-param-source": {"tool": "getSupplierIdByScenicId",
                                       "key": "scenicId", "path": "data.supplierId"},
                },
                "plain": {"type": "string"},
            },
            "required": ["scenicId"],
        }
        out = _collect_param_sources(schema)
        assert out == {"supplierId": {
            "tool": "getSupplierIdByScenicId", "key": "scenicId", "path": "data.supplierId"}}

    def test_missing_tool_or_key_ignored(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-param-source": {"key": "scenicId"}},
                "b": {"type": "string", "x-param-source": {"tool": "t1"}},
                "c": {"type": "string", "x-param-source": "不是对象"},
            },
        }
        assert _collect_param_sources(schema) == {}

    def test_default_path_is_dollar(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string",
                                 "x-param-source": {"tool": "t1", "key": "k"}}},
        }
        out = _collect_param_sources(schema)
        assert out["x"]["path"] == "$"

    def test_strip_from_visible_schema(self):
        """声明了来源的参数从模型可见 Schema 剥离,required 同步移除。"""
        from cogni_agent.runtime_service.engine.resolver import _strip_forced_params

        schema = {
            "type": "object",
            "properties": {
                "scenicId": {"type": "string"},
                "supplierId": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["scenicId", "supplierId"],
        }
        visible = _strip_forced_params(schema, {"supplierId"})
        assert "supplierId" not in visible["properties"]
        assert visible["required"] == ["scenicId"]
