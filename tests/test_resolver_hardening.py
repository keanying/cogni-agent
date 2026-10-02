"""
Skill 解析层加固的回归测试。

重点是 test_same_name_tools_do_not_collide:两个不同 id 的同名工具(集团级和景区级
各有一个「查询订单」是很正常的),原来会在 bound_tools_map 里互相覆盖,导致模型调用
该名字时**静默执行错误的工具**——在有权限边界的系统里这是安全问题。
"""
from __future__ import annotations

import pytest

from cogni_agent.runtime_service.engine import naming
from cogni_agent.runtime_service.engine.resolver import (
    ExecutionSpec, ToolSpec, _build_skill_deps, _positive_int, _strip_forced_params,
    resolve_execution_spec,
)
from cogni_agent.runtime_service.engine.tools_adapter import bound_tools_map, tool_function_schemas

FUNC_NAME_RE = __import__("re").compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _spec(tool_id: str, display_name: str) -> ToolSpec:
    return ToolSpec(id=tool_id, name=display_name, description="", type="http", risk_level="low",
                    json_schema={"type": "object", "properties": {}}, type_config={})


def _assign(specs: list[ToolSpec]) -> list[ToolSpec]:
    assigned = naming.assign_function_names([(t.id, t.name) for t in specs])
    for t in specs:
        fn, renamed = assigned[t.id]
        t.func_name = fn
        t.model_description = naming.describe_for_model(t.name, t.description, fn, renamed)
    return specs


def test_chinese_tool_name_becomes_valid_function_name():
    """中文工具名直接发给厂商会被 400 拒掉,必须规范化;语义靠 description 承载。"""
    specs = _assign([_spec("t_abc12345", "查询订单记录")])
    fn = specs[0].func_name
    assert FUNC_NAME_RE.match(fn), f"函数名 {fn!r} 不满足厂商的 ^[a-zA-Z0-9_-]{{1,64}}$ 约束"
    # 原始中文名必须写进 description,否则模型只看到一个无语义的 tool_xxx
    assert "查询订单记录" in specs[0].model_description
    schema = tool_function_schemas(specs)[0]
    assert FUNC_NAME_RE.match(schema["function"]["name"])


def test_same_name_tools_do_not_collide():
    """两个不同工具同名时必须拿到不同函数名,且能各自被正确路由回原工具。"""
    a, b = _assign([_spec("tool_group_001", "查询订单"), _spec("tool_scenic_002", "查询订单")])
    assert a.func_name != b.func_name, "同名工具必须被区分,否则模型调用时会执行到错误的工具"

    m = bound_tools_map([a, b])
    assert len(m) == 2, "索引里必须保留两个工具,不能互相覆盖"
    assert m[a.func_name].id == "tool_group_001"
    assert m[b.func_name].id == "tool_scenic_002"

    # 发给模型的 tools 数组里函数名不能重复(厂商会直接报 duplicate function name)
    names = [s["function"]["name"] for s in tool_function_schemas([a, b])]
    assert len(names) == len(set(names))


def test_same_tool_bound_twice_keeps_one_name():
    """同一个工具 id 被多个 Skill 绑定时,必须拿到同一个函数名(而不是被当成冲突加后缀)。"""
    assigned = naming.assign_function_names([("t1", "query"), ("t1", "query")])
    assert assigned["t1"][0] == "query"


def test_function_name_is_stable_across_resolves():
    """函数名必须可复现——漂移会让模型在多轮里记住的工具名突然失效。"""
    first = naming.assign_function_names([("t_abc12345", "查询订单"), ("t_def67890", "查询订单")])
    second = naming.assign_function_names([("t_abc12345", "查询订单"), ("t_def67890", "查询订单")])
    assert first == second


def test_name_edge_cases():
    assert naming.sanitize_function_name("get_weather", fallback_id="x") == ("get_weather", False)
    # 纯符号/中文 -> 回落到基于 id 的稳定名
    fn, renamed = naming.sanitize_function_name("！！！", fallback_id="abcd1234efgh")
    assert renamed and FUNC_NAME_RE.match(fn)
    # 数字开头其实是合法的(厂商约束 ^[a-zA-Z0-9_-]{1,64}$ 并不禁止),应原样保留
    fn2, renamed2 = naming.sanitize_function_name("123tool", fallback_id="abcd1234")
    assert fn2 == "123tool" and renamed2 is False
    # 超长截断
    fn3, _ = naming.sanitize_function_name("a" * 200, fallback_id="x")
    assert len(fn3) <= 64


def test_forced_params_hidden_from_model_but_still_validated():
    """被强制覆盖的参数不该再让模型填,但校验实参时仍要按完整定义走。"""
    schema = {"type": "object",
              "properties": {"city": {"type": "string"}, "scenic_id": {"type": "string"}},
              "required": ["city", "scenic_id"]}
    stripped = _strip_forced_params(schema, {"scenic_id": "sc1"})
    assert "scenic_id" not in stripped["properties"]
    assert stripped["required"] == ["city"]
    # 原 schema 不能被就地改坏
    assert "scenic_id" in schema["properties"]


def test_positive_int_guard():
    """max_steps=0 会让任务第一步就熔断,多半是误填而不是"故意禁用"。"""
    assert _positive_int(5, 25) == 5
    assert _positive_int(0, 25) == 25
    assert _positive_int(-1, 25) == 25
    assert _positive_int(None, 25) == 25
    assert _positive_int("abc", 25) == 25
    assert _positive_int("7", 25) == 7


def test_db_tool_schema_excludes_builtin_vars():
    """SQL 模板里的系统内置变量由沙箱注入,不能反过来要求模型提供——
    否则模型可能瞎编一个 scenic_id,把真实租户上下文覆盖掉(越权风险)。"""
    from cogni_agent.runtime_service.engine.resolver import _build_json_schema

    class _T:
        type = "db"
        type_config = {"sql_template": "SELECT * FROM t WHERE scenic_id={{scenic_id}} AND day={{day}}"}

    schema, _ = _build_json_schema(_T())
    assert "day" in schema["properties"]
    assert "scenic_id" not in schema["properties"], "系统内置变量不应暴露给模型"


def test_skill_deps_still_correct():
    assert _build_skill_deps("chain", ["a", "b", "c"], None) == {"a": [], "b": ["a"], "c": ["b"]}
    assert _build_skill_deps("dag", ["a", "b"], {"b": ["a", "ghost"]}) == {"a": [], "b": ["a"]}


# ---------------------------------------------------------------------------
# 观察点1回归:gov_llm_usage.model 落库为空
# 根因:resolver 只取 overrides.model,从不回填默认模型名,导致 usage 事件里
# model 恒为空串(执行时 llm_client 内部解析出默认模型,但记账没拿到名字)。
# 修复:resolve_execution_spec 末尾,model_override 为空时复用 _model_cfg(None)
# 回填「实际会用的模型名」。
# ---------------------------------------------------------------------------

DB_SENTINEL = object()  # resolve_execution_spec 里 db 只传给被 mock 掉的 _resolve_by_type


async def _resolve_with_model(monkeypatch, spec_model: str | None, cfg_result=None, cfg_error: Exception | None = None):
    """公共桩:mock 掉 _resolve_by_type(返回预置 spec)与 llm client._model_cfg。"""
    from cogni_agent.runtime_service.engine import resolver as resolver_mod

    async def fake_resolve(db, executor_type, executor_id, ctx):
        assert db is DB_SENTINEL  # 防呆:被 mock 的路径不应真的用到 db 连接
        return ExecutionSpec(executor_type="agent", executor_id="agt_x", model_override=spec_model)

    async def fake_model_cfg(model_name):
        assert model_name is None
        if cfg_error is not None:
            raise cfg_error
        return cfg_result

    monkeypatch.setattr(resolver_mod, "_resolve_by_type", fake_resolve)
    monkeypatch.setattr("cogni_agent.runtime_service.llm.client._model_cfg", fake_model_cfg)
    return await resolve_execution_spec(
        DB_SENTINEL, executor_type="agent", executor_id="agt_x",
        scenic_id=None, role="super_admin", user_id="u1",
    )


async def test_model_override_backfilled_with_default_name(monkeypatch):
    """调用方没显式指定模型时,回填实际默认模型名——usage 落库不再为空。"""
    spec = await _resolve_with_model(monkeypatch, None, cfg_result=("test", {"provider": "openai"}))
    assert spec.model_override == "test"


async def test_model_override_explicit_wins(monkeypatch):
    """调用方显式指定模型时必须原样保留,不能覆盖。"""
    spec = await _resolve_with_model(monkeypatch, "gpt-4o")
    assert spec.model_override == "gpt-4o"


async def test_model_override_stays_none_when_no_model(monkeypatch):
    """没有任何可用模型时保持 None,不阻断解析(执行阶段仍会报错,行为与修复前一致)。"""
    spec = await _resolve_with_model(monkeypatch, None, cfg_error=ValueError("no model configured"))
    assert spec.model_override is None
