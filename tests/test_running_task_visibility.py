# -*- coding: utf-8 -*-
"""任务「还在跑」这件事,三个地方都不能表现成「任务坏了」。

同一个根因,三种症状:**执行链、用量、最终回答都是收尾时才落库的**,任务执行中这些
一律是空的。谁把「空」当成「没有」,谁就会给出一个与事实相反的结论。

1. **刷新后运行中的过程没有了。** SSE 断点游标原先存在 sessionStorage 里 ——
   而它记的是「这些事件我已经渲染过了」,渲染结果却活在 React state 里。
   一刷新 state 全没了、游标还在,重新挂流时后端只推新事件,已经跑过的十几步
   **永远不会回来**;任务还在 RUNNING,历史接口也给不出 timeline,页面就是一片空白。

2. **调试导出把一个跑得好好的任务写成疑似启动即失败。** 导出读的是
   `task.result.timeline`,执行中必然为空,于是同时报出「整轮没有调用任何工具」
   「没有生成步骤也没有回答」「没有用量记录」三条 —— 而现场那一轮已经调了十几次工具。
   这份文件是转给别人看的,对方只会得到一个结论:这任务彻底坏了。

3. **目标校验每轮重复做。** 触发条件写的是 `tool_calls % N == 0`,而重复调用会被
   拦截、不计入 tool_calls,于是计数停在 N 的倍数上不动时**每轮都再自检一次**。
   模型一旦原地打转,打转本身不烧时间,替它做的自检才烧(现场:4 条 0ms 的
   「跳过重复调用」,紧跟一条 3141ms 的目标校验)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "web" / "src" / "pages" / "Console.tsx"
SIDEBAR = ROOT / "web" / "src" / "components" / "Layout" / "Sidebar.tsx"


def _code(p: Path) -> str:
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", s)


class TestSseCursorDoesNotOutliveItsRender:
    def test_cursor_is_not_persisted_across_reloads(self):
        code = _code(CONSOLE)
        assert "sessionStorage" not in code, (
            "SSE 游标不能进 sessionStorage —— 它比对应的渲染结果活得久,刷新后会把执行链吃掉"
        )

    def test_cursor_lives_in_memory(self):
        code = _code(CONSOLE)
        assert "_sseCursors" in code
        assert "_sseCursors.set(task_id" in code, "游标仍要按任务记,同一页内断线重连才能续上"

    def test_dedup_still_guards_a_full_replay(self):
        """刷新后是全量重放,靠 _sseId 去重,不能把这道保险一起删掉。"""
        code = _code(CONSOLE)
        assert "seenSseIds" in code and "_sseId" in code

    def test_reattach_still_happens_for_running_turns(self):
        code = _code(CONSOLE)
        assert "resumeTaskIds" in code and "consumeStream(tid" in code


class TestDebugExportOnRunningTask:
    @staticmethod
    def _checks(status: str, **kw):
        from cogni_agent.control_plane_service import task_debug as td

        return td._self_checks(kw.get("timeline", []), kw.get("prompts", []),
                               kw.get("usage", []), kw.get("result", {}), status)

    @pytest.mark.parametrize("status", ["RUNNING", "QUEUED", "CREATED", "WAITING_HUMAN"])
    def test_running_task_gets_one_honest_note_not_three_false_alarms(self, status):
        out = self._checks(status)
        assert len(out) == 1, f"执行中不该报依赖完整执行链的判断,实际报了:{[c['title'] for c in out]}"
        assert out[0]["level"] == "info"
        assert "还在执行中" in out[0]["title"]

    @pytest.mark.parametrize("status", ["RUNNING", "QUEUED"])
    def test_running_task_never_claims_no_tools_were_called(self, status):
        titles = " ".join(c["title"] for c in self._checks(status))
        assert "没有调用任何工具" not in titles
        assert "没有生成步骤" not in titles

    def test_finished_task_still_gets_the_real_checks(self):
        """别把判断一起关掉 —— 任务结束后这几条正是导出的价值所在。"""
        out = self._checks("COMPLETED")
        titles = " ".join(c["title"] for c in out)
        assert "没有调用任何工具" in titles
        assert all(c["level"] != "info" or "还在执行中" not in c["title"] for c in out)

    def test_markdown_says_running_instead_of_failed_to_start(self):
        src = (ROOT / "cogni_agent" / "control_plane_service" / "task_debug.py").read_text(encoding="utf-8")
        assert "任务还在执行中(状态" in src
        # 「启动即失败」那句必须只留给真正的终态
        i = src.index("_时间线为空 —— 任务可能在进入执行图之前就失败了")
        assert "ACTIVE_STATUSES" in src[max(0, i - 600):i], "那句话要被终态判断包着"

    def test_active_statuses_are_shared_not_re_listed(self):
        from cogni_agent.control_plane_service import task_debug as td
        from cogni_agent.control_plane_service.models import ACTIVE_STATUSES

        assert td.ACTIVE_STATUSES is ACTIVE_STATUSES, "口径要和任务状态的唯一定义一致"


class TestDriftCheckFiresOnCrossingOnly:
    """目标校验按「跨过第 N 个刻度」触发,而不是「当前计数恰好是 N 的倍数」。"""

    @staticmethod
    def _fires(tool_calls: int, drift_checks: int, every_n: int = 4) -> bool:
        bucket = tool_calls // every_n if every_n else 0
        return bool(every_n) and bucket > drift_checks

    def test_fires_when_crossing(self):
        assert self._fires(tool_calls=4, drift_checks=0)
        assert self._fires(tool_calls=8, drift_checks=1)

    def test_does_not_fire_again_while_the_count_stands_still(self):
        """现场那一幕:4 条 0ms 的「跳过重复调用」之后又来一条 3141ms 的目标校验。

        重复调用被拦截、不计入 tool_calls,计数停在 8 不动 —— 旧条件 8 % 4 == 0
        每轮都成立,于是模型每打一次转,系统就替它多做一次整轮模型调用。
        """
        assert not self._fires(tool_calls=8, drift_checks=2)

    def test_does_not_fire_below_the_first_bucket(self):
        assert not self._fires(tool_calls=0, drift_checks=0)
        assert not self._fires(tool_calls=3, drift_checks=0)

    def test_catches_up_when_several_buckets_are_crossed_at_once(self):
        """一轮并行跑了 8 个工具:该补的自检还是要做,只是不重复做。"""
        assert self._fires(tool_calls=8, drift_checks=0)

    def test_disabled_when_n_is_zero(self):
        assert not self._fires(tool_calls=100, drift_checks=0, every_n=0)

    def test_source_uses_the_crossing_condition(self):
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "graphs.py").read_text(encoding="utf-8")
        assert "_drift_bucket > guardrail.drift_checks" in src
        assert "guardrail.tool_calls % drift_every_n == 0" not in src, "旧的取模条件必须去掉"

    def test_duplicate_calls_still_do_not_count_as_tool_calls(self):
        """这条是上面那个 bug 的前提,别哪天被改掉了没人发现。"""
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "graphs.py").read_text(encoding="utf-8")
        assert "for _ in to_run:" in src
        assert "gr.check_and_increment_tool_call(guardrail" in src


class TestSidebarGroupsCollapsedByDefault:
    def test_no_group_is_expanded_initially(self):
        code = _code(SIDEBAR)
        m = re.search(r"useState<string\[\]>\(([^)]*)\)", code)
        assert m, "找不到展开状态的初始值"
        assert m.group(1).strip() in ("[]", ""), f"分组默认应全收起,实际:{m.group(1)}"

    def test_current_route_group_still_opens(self):
        """全收起之后,当前所在分组要自动展开 —— 否则用户不知道自己在哪儿。"""
        code = _code(SIDEBAR)
        assert "location.pathname" in code
        assert "navItems.find" in code

    def test_manual_toggle_is_kept(self):
        code = _code(SIDEBAR)
        assert "toggleExpand" in code
