"""
自动模式判定不能被「兜底默认值」架空。

**这是一个真实踩到的坑**:resolver 里写了
`suggested_mode = snap.get("suggested_mode") or "L2"`,
于是 suggested_mode 永远不为空,decide_mode 认为「管理后台显式声明了运行模式」
而直接采用 —— 自动判定(按工具规模在 ReAct / Plan-and-Execute 之间选)完全没机会执行。

更糟的是判定理由会写成「登记的运行模式建议值为 L2,直接采用」,
让排查的人以为真的有人配过这个值。
"""
from __future__ import annotations

import inspect
import re

import pytest


def test_resolver_does_not_default_suggested_mode():
    """没配就是没配 —— 交给 decide_mode 判断,不要在解析层兜底成某个档位。"""
    from cogni_agent.runtime_service.engine import resolver

    src = inspect.getsource(resolver)
    bad = re.findall(r'suggested_mode\s*=\s*snap\.get\("suggested_mode"\)\s*or\s*"(L\d)"', src)
    assert not bad, (
        f"resolver 把 suggested_mode 兜底成了 {bad},这会让自动模式判定形同虚设 —— "
        f"decide_mode 会当成「显式声明」直接采用。"
    )


@pytest.mark.asyncio
async def test_many_tools_choose_plan_execute():
    """工具多 -> L4 先规划。工具越多,ReAct 每轮重新挑越容易跑偏。"""
    from cogni_agent.runtime_service.engine.modes import decide_mode
    from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill

    skill = ResolvedSkill(id="s1", name="分析", system_prompt="", tools=[object()] * 5,
                          knowledge_sources=[], max_steps=10, max_tool_calls=5)
    spec = ExecutionSpec(executor_type="agent", executor_id="a1", skill_chain=[skill])
    d = await decide_mode(spec)
    assert d.mode == "L4", f"5 个工具应走 L4,实际 {d.mode}:{d.reasons}"


@pytest.mark.asyncio
async def test_few_tools_stay_react():
    """工具少 -> 规划的收益抵不过额外一次模型调用的延迟。"""
    from cogni_agent.runtime_service.engine.modes import decide_mode
    from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill

    skill = ResolvedSkill(id="s1", name="查询", system_prompt="", tools=[object()],
                          knowledge_sources=[], max_steps=10, max_tool_calls=5)
    spec = ExecutionSpec(executor_type="agent", executor_id="a1", skill_chain=[skill])
    d = await decide_mode(spec)
    assert d.mode == "L2", f"1 个工具应走 L2,实际 {d.mode}"


@pytest.mark.asyncio
async def test_explicit_mode_still_wins():
    """**显式声明仍然优先** —— 去掉兜底不等于忽略用户配置。"""
    from cogni_agent.runtime_service.engine.modes import decide_mode
    from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill

    skill = ResolvedSkill(id="s1", name="分析", system_prompt="", tools=[object()] * 5,
                          knowledge_sources=[], max_steps=10, max_tool_calls=5,
                          suggested_mode="L2")
    # 注意:decide_mode 读的是 **spec.suggested_mode**(执行规格级),
    # 由 resolver 从 skill 快照提升上来 —— 构造 spec 时必须一并传,
    # 只设 skill.suggested_mode 是不生效的。
    spec = ExecutionSpec(executor_type="agent", executor_id="a1", skill_chain=[skill],
                         suggested_mode="L2")
    d = await decide_mode(spec)
    assert d.mode == "L2", "管理后台显式配了 L2 就该用 L2,哪怕工具很多"
