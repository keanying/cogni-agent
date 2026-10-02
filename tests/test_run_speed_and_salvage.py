# -*- coding: utf-8 -*-
"""执行提速 + 失败收尾不丢账(现场任务 f34e0686)。

那一轮:模型取完数后反复请求同一批数据,每轮都被「跳过重复调用」拦下,但每轮照样付一次
完整生成(上下文大时 40 秒);工具调用计数不动,「额度用尽即收尾」永远触发不了。
9 分半后一轮并发调用把计数顶过 100,整任务熔断成 FAILED ——
取回的数据、31 万 token 全作废,导出里「整轮没有调用任何工具」「没有用量记录」,
这部分消耗也从没进成本账。
"""
from __future__ import annotations

from pathlib import Path

from cogni_agent.runtime_service.engine import guardrails as gr
from cogni_agent.runtime_service.engine import resilience as res

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "cogni_agent" / "runtime_service" / "engine"


def test_remaining_tool_calls():
    st = gr.GuardrailState()
    st.tool_calls = 98
    assert gr.remaining_tool_calls(st, max_tool_calls=100) == 2
    st.tool_calls = 100
    assert gr.remaining_tool_calls(st, max_tool_calls=100) == 0
    st.tool_calls = 105
    assert gr.remaining_tool_calls(st, max_tool_calls=100) == 0


def test_stall_counter_survives_checkpoint_and_trips_at_two():
    t = res.ToolCallTracker()
    assert not res.stalled(t)
    t.stalls = 2
    back = res.ToolCallTracker.from_dict(t.as_dict())
    assert back.stalls == 2 and res.stalled(back)
    assert res.ToolCallTracker.from_dict({}).stalls == 0, "老检查点里没有这个键"


def _graphs() -> str:
    return (ENGINE / "graphs.py").read_text(encoding="utf-8")


def test_over_budget_calls_are_answered_not_fatal():
    s = _graphs()
    i = s.index("_left = gr.remaining_tool_calls(")
    assert s.index("over_budget = to_run[_left:]") > i
    assert s.index("to_run = to_run[:_left]") < s.index("gr.check_and_increment_tool_call(guardrail, max_tool_calls=max_tool_calls)")
    assert "for call in over_budget:" in s, "超额的调用也要逐个回 ToolMessage(协议要求)"


def test_stalled_rounds_force_the_final_answer():
    s = _graphs()
    assert "tracker.stalls = 0 if to_run else tracker.stalls + (0 if over_budget else 1)" in s
    assert "_stalled = bool(tools) and res.stalled(tracker)" in s
    assert "or _stalled" in s[s.index("_stalled = bool(tools)"):s.index("_stalled = bool(tools)") + 200]
    assert "res.stalled(sub_tracker)" in s, "线性子循环同样要能收尾"


def test_every_failure_path_salvages_timeline_and_usage():
    s = (ENGINE / "executor.py").read_text(encoding="utf-8")
    body = s[s.index("async def _salvage("):s.index("if interrupted_payload is not None:")]
    for kind in ("GUARDRAIL_TRIPPED", "OUTPUT_BLOCKED", "TASK_TIMEOUT", "UNEXPECTED_ERROR"):
        seg = body[body.index(f"TaskErrorKind.{kind}"):][:400]
        assert "_salvage(" in body[max(0, body.index(f"TaskErrorKind.{kind}") - 120):body.index(f"TaskErrorKind.{kind}")] \
            or "_salvage(" in seg, kind
    assert body.count("usage_event=_uev") >= 5, "四条失败 + 用户取消都要补记用量"
    assert 'graph.aget_state(config), timeout=_FINALIZE_STEP_TIMEOUT' in body, "取检查点要有超时"


def test_console_only_follows_when_at_bottom():
    s = (ROOT / "web" / "src" / "pages" / "Console.tsx").read_text(encoding="utf-8")
    assert "useEffect(() => { scrollToBottom(); }, [messages]);" not in s
    assert "if (stickToBottomRef.current) scrollToBottom();" in s
    assert "onScroll={onMessagesScroll}" in s and "回到最新" in s
