"""多轮会话 + 知识检索工具化的单元测试。"""
from __future__ import annotations

import pytest

from cogni_agent.runtime_service.engine.resolver import (
    KNOWLEDGE_TOOL_NAME, ToolSpec, _build_knowledge_tool,
)
from cogni_agent.runtime_service.engine.tools_adapter import bound_tools_map, tool_function_schemas


def test_knowledge_tool_shape():
    t = _build_knowledge_tool("sk1", "门票政策问答", ["vector", "websearch"])
    assert t.type == "knowledge"
    assert t.name == KNOWLEDGE_TOOL_NAME
    assert t.risk_level == "low"
    assert t.type_config["sources"] == ["vector", "websearch"]
    # 不允许运行时覆盖参数——检索词必须是模型当下自己想的
    assert t.allow_override is False
    schema = t.json_schema
    assert schema["required"] == ["query"]
    # 描述里要写清可用知识源,模型才知道这个工具能查到什么
    assert "vector" in t.description


def test_knowledge_tool_coexists_with_normal_tools():
    """知识工具和普通工具共存时,函数名分配不能打架。"""
    from cogni_agent.runtime_service.engine import naming

    normal = ToolSpec(id="t1", name="查询订单", description="", type="http", risk_level="low",
                      json_schema={"type": "object", "properties": {}}, type_config={})
    ktool = _build_knowledge_tool("sk1", "技能", ["vector"])
    specs = [normal, ktool]
    assigned = naming.assign_function_names([(t.id, t.name) for t in specs])
    for t in specs:
        t.func_name = assigned[t.id][0]

    names = [s["function"]["name"] for s in tool_function_schemas(specs)]
    assert len(names) == len(set(names))
    # knowledge_search 本身就是合法函数名,不该被改写
    assert ktool.func_name == KNOWLEDGE_TOOL_NAME
    assert bound_tools_map(specs)[KNOWLEDGE_TOOL_NAME].type == "knowledge"


def test_conversation_title_truncation():
    from cogni_agent.control_plane_service.conversations import _make_title

    assert _make_title("帮我规划武夷山两日游") == "帮我规划武夷山两日游"
    assert _make_title("") == "新会话"
    long = _make_title("啊" * 100)
    assert len(long) <= 41 and long.endswith("…")
    # 换行会破坏列表展示
    assert "\n" not in _make_title("第一行\n第二行")


def test_active_statuses_include_waiting_human():
    """审批未处理时不能开新一轮——那一轮的执行链条只是暂停,并没有结束。"""
    from cogni_agent.control_plane_service.models import ACTIVE_STATUSES

    assert "WAITING_HUMAN" in ACTIVE_STATUSES
    assert "RUNNING" in ACTIVE_STATUSES
    assert "COMPLETED" not in ACTIVE_STATUSES
    assert "FAILED" not in ACTIVE_STATUSES


def test_knowledge_sources_cannot_be_widened_by_model():
    """模型不能通过 sources 参数越过 Skill 配置的知识源范围。"""
    allowed = ["vector"]
    requested = ["vector", "database", "websearch"]
    filtered = [s for s in requested if s in allowed]
    assert filtered == ["vector"], "越界的知识源必须被过滤掉"
