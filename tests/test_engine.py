"""
执行引擎(runtime_service.engine)端到端行为测试——不连真实 DB/真实模型,
monkeypatch llm_client.stream_and_collect 与 tools_adapter.invoke_tool 模拟
一个真实模型 API 会返回的各种情形,验证图本身(graphs.py)对"不确定性"的处理:

1. 幻觉工具名 -> 自纠正(打回让模型重选)且计入 hallucination_retries,超限熔断
2. 参数格式错误(非法 JSON)-> 自纠正且计入 format_retries,超限熔断
3. 护栏(最大步数)按 Skill 级配置熔断
4. 高风险工具(risk_level=high)在真正执行前触发 interrupt(),暂停整张图;
   审批通过 -> 从检查点恢复继续执行;审批驳回 -> ApprovalRejected,任务应判 CANCELLED
5. L1 固定编排链按 Agent.bound_skill_ids 顺序推进,Skill 边界正确触发 skill_advance

需要真实 Postgres 检查点持久化 + 真实 Redis 事件流 + 真实 HTTP/DB/Python 工具执行的
完整链路验证(含"跨进程恢复"这种只有真实 Postgres 检查点才能证明的场景),见 README
「手动集成校验」一节的脚本化步骤,不适合作为无外部依赖的单元测试。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from cogni_agent.common.errors import ApprovalRejected, GuardrailTripped
from cogni_agent.runtime_service.engine import graphs as G
from cogni_agent.runtime_service.engine import tools_adapter
from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill, ToolSpec
from cogni_agent.runtime_service.llm.client import LLMResult


def _make_tool(name: str = "get_weather", risk: str = "low") -> ToolSpec:
    return ToolSpec(
        id=f"tool_{name}", name=name, description="查询天气", type="http", risk_level=risk,
        json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        type_config={},
    )


def _initial_state(task_id: str, goal: str, mode: str, skill_ids: list[str], persona: dict | None = None) -> dict:
    return {
        "messages": [HumanMessage(content=goal)],
        "task_id": task_id, "scenic_id": "sc1", "user_id": "u1", "role": "scenic_operator",
        "goal": goal, "mode": mode, "skill_chain": skill_ids, "skill_idx": 0,
        "skill_step_counts": {}, "persona": persona, "overrides": {}, "guardrail": {},
        "timeline": [], "usage": {}, "pending_interrupt": None, "final_answer": None,
    }


@pytest.mark.asyncio
async def test_hallucination_format_error_then_success(monkeypatch):
    tool = _make_tool()
    skill = ResolvedSkill(id="s1", name="天气助手", system_prompt="你负责查询天气。", tools=[tool],
                           knowledge_sources=[], max_steps=25, max_tool_calls=10)
    spec = ExecutionSpec(executor_type="skill", executor_id="s1", skill_chain=[skill])

    call_seq = [
        LLMResult(content="", tool_calls=[{"id": "c1", "name": "not_a_real_tool", "arguments": "{}"}]),
        LLMResult(content="", tool_calls=[{"id": "c2", "name": "get_weather", "arguments": "{city: beijing"}]),
        LLMResult(content="", tool_calls=[{"id": "c3", "name": "get_weather", "arguments": '{"city": "beijing"}'}]),
        LLMResult(content="北京今天晴天,25度。", tool_calls=[]),
    ]

    async def fake_stream(model, messages, tools=None, tool_choice=None, writer=None, **kw):
        return call_seq.pop(0)

    async def fake_invoke_tool(db, tool_id, scenic_id, role, params):
        return {"weather": "晴", "temp_c": 25, "city": params.get("city")}

    monkeypatch.setattr(G.llm_client, "stream_and_collect", fake_stream)
    monkeypatch.setattr(tools_adapter, "invoke_tool", fake_invoke_tool)

    graph = G.build_agent_graph(spec, "L2", db=None, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-1"}}
    final = await graph.ainvoke(_initial_state("t-1", "北京天气怎么样", "L2", ["s1"]), config=config)

    assert final["guardrail"]["hallucination_retries"] == 1
    assert final["guardrail"]["format_retries"] == 1
    assert final["guardrail"]["tool_calls"] == 3
    assert final["messages"][-1].content == "北京今天晴天,25度。"


@pytest.mark.asyncio
async def test_guardrail_trips_on_runaway_steps(monkeypatch):
    tool = _make_tool()
    skill = ResolvedSkill(id="s1", name="循环助手", system_prompt="x", tools=[tool],
                           knowledge_sources=[], max_steps=2, max_tool_calls=10)
    spec = ExecutionSpec(executor_type="skill", executor_id="s1", skill_chain=[skill])

    async def infinite(model, messages, tools=None, tool_choice=None, writer=None, **kw):
        return LLMResult(content="", tool_calls=[{"id": "c", "name": "get_weather", "arguments": '{"city":"x"}'}])

    async def fake_invoke_tool(db, tool_id, scenic_id, role, params):
        return {"ok": True}

    monkeypatch.setattr(G.llm_client, "stream_and_collect", infinite)
    monkeypatch.setattr(tools_adapter, "invoke_tool", fake_invoke_tool)

    graph = G.build_agent_graph(spec, "L2", db=None, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-2"}}
    with pytest.raises(GuardrailTripped):
        await graph.ainvoke(_initial_state("t-2", "一直查", "L2", ["s1"]), config=config)


@pytest.mark.asyncio
async def test_hitl_interrupt_approve_and_reject(monkeypatch):
    tool = _make_tool(name="delete_booking", risk="high")
    skill = ResolvedSkill(id="s1", name="订单助手", system_prompt="x", tools=[tool],
                           knowledge_sources=[], max_steps=25, max_tool_calls=10)
    spec = ExecutionSpec(executor_type="skill", executor_id="s1", skill_chain=[skill])

    async def fake_invoke_tool(db, tool_id, scenic_id, role, params):
        return {"cancelled": True}

    monkeypatch.setattr(tools_adapter, "invoke_tool", fake_invoke_tool)
    checkpointer = InMemorySaver()
    graph = G.build_agent_graph(spec, "L2", db=None, checkpointer=checkpointer)

    # 通过路径
    seq_ok = [
        LLMResult(content="", tool_calls=[{"id": "c1", "name": "delete_booking", "arguments": '{"city":"x"}'}]),
        LLMResult(content="已为您取消预订。", tool_calls=[]),
    ]
    async def fake_stream_ok(model, messages, tools=None, tool_choice=None, writer=None, **kw):
        return seq_ok.pop(0)
    monkeypatch.setattr(G.llm_client, "stream_and_collect", fake_stream_ok)

    config_a = {"configurable": {"thread_id": "t-3a"}}
    r1 = await graph.ainvoke(_initial_state("t-3a", "取消预订", "L2", ["s1"]), config=config_a)
    assert "__interrupt__" in r1
    assert r1["__interrupt__"][0].value["tool_name"] == "delete_booking"
    r2 = await graph.ainvoke(Command(resume=True), config=config_a)
    assert r2["messages"][-1].content == "已为您取消预订。"

    # 驳回路径(独立 thread_id,避免复用上面已经消费掉的 checkpoint)
    seq_reject = [LLMResult(content="", tool_calls=[{"id": "c1", "name": "delete_booking", "arguments": '{"city":"x"}'}])]
    async def fake_stream_reject(model, messages, tools=None, tool_choice=None, writer=None, **kw):
        return seq_reject.pop(0)
    monkeypatch.setattr(G.llm_client, "stream_and_collect", fake_stream_reject)

    config_b = {"configurable": {"thread_id": "t-3b"}}
    r1b = await graph.ainvoke(_initial_state("t-3b", "取消预订", "L2", ["s1"]), config=config_b)
    assert "__interrupt__" in r1b
    with pytest.raises(ApprovalRejected):
        await graph.ainvoke(Command(resume=False), config=config_b)


@pytest.mark.asyncio
async def test_l1_chain_advances_between_skills(monkeypatch):
    tool_a, tool_b = _make_tool(name="lookup_a"), _make_tool(name="lookup_b")
    skill_a = ResolvedSkill(id="sa", name="A能力", system_prompt="你是能力A。", tools=[tool_a],
                             knowledge_sources=[], max_steps=25, max_tool_calls=10)
    skill_b = ResolvedSkill(id="sb", name="B能力", system_prompt="你是能力B。", tools=[tool_b],
                             knowledge_sources=[], max_steps=25, max_tool_calls=10)
    spec = ExecutionSpec(executor_type="agent", executor_id="ag1", skill_chain=[skill_a, skill_b], composition_hint="chain")

    seq = [
        LLMResult(content="", tool_calls=[{"id": "c1", "name": "lookup_a", "arguments": '{"city":"x"}'}]),
        LLMResult(content="A完成", tool_calls=[]),
        LLMResult(content="", tool_calls=[{"id": "c2", "name": "lookup_b", "arguments": '{"city":"y"}'}]),
        LLMResult(content="B完成,最终结果如下。", tool_calls=[]),
    ]

    async def fake_stream(model, messages, tools=None, tool_choice=None, writer=None, **kw):
        return seq.pop(0)

    async def fake_invoke_tool(db, tool_id, scenic_id, role, params):
        return {"ok": True}

    monkeypatch.setattr(G.llm_client, "stream_and_collect", fake_stream)
    monkeypatch.setattr(tools_adapter, "invoke_tool", fake_invoke_tool)

    graph = G.build_agent_graph(spec, "L1", db=None, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-4"}}
    init = _initial_state("t-4", "做一个复合任务", "L1", ["sa", "sb"], persona={"name": "小智", "persona_desc": "严谨", "tone_style": "简洁"})
    final = await graph.ainvoke(init, config=config)

    assert final["skill_idx"] == 1
    assert final["messages"][-1].content == "B完成,最终结果如下。"
    assert "skill_advance" in [t.get("step") for t in final["timeline"]]
