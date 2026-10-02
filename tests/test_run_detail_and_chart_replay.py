# -*- coding: utf-8 -*-
"""操练台右侧「执行详情」与图表回放的两个生产事故。

**事故一:假的预算告警。** 执行详情里写着 `Token 消耗 17,587 / 8,000`,红条拉满,
下面一行「接近预算上限,超出后会被护栏熔断」。而 8,000 是 RunDetailPanel.tsx 里
写死的默认参数、Console.tsx 又把字面量 8000 传了一遍 —— 后端根本没有这个数。
一轮完全正常的对话被画成即将熔断,看的人只能去猜是不是自己配错了什么。
前端不许自己编预算:进度条的分母只能来自后端 result.usage.token_budget。

**事故二:刷新一下图就没了。** 后端早就把 sys_chart 画的图并进 result.structured
落库了(APP 侧一直读得到),但操练台 openSession() 重建历史消息时读了 timeline、
usage、citations,唯独没读 structured。于是刷新后回答正文还写着「雷达图已生成 ✅」,
下面却空空如也 —— 比没有图更糟,因为它看起来像系统在说谎。

这两条都是「前端自作主张 / 少读一个字段」造成的,编译器查不出来,只能靠钉源码。
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "src"
PANEL = WEB / "components" / "common" / "RunDetailPanel.tsx"
CONSOLE = WEB / "pages" / "Console.tsx"
API = WEB / "lib" / "api.ts"
SKILL_EDIT = WEB / "pages" / "Tools" / "SkillEdit.tsx"
SKILL_DETAIL = WEB / "pages" / "Tools" / "SkillDetail.tsx"


def _src(p: Path) -> str:
    assert p.exists(), f"{p} 不存在"
    return p.read_text(encoding="utf-8")


def _code(p: Path) -> str:
    """去掉注释后的源码 —— 注释里出现 8000 是在解释这个 bug,不该被判为复发。"""
    s = _src(p)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    return s


class TestNoHardcodedBudget:
    def test_panel_has_no_default_budget_constant(self):
        code = _code(PANEL)
        assert "tokenBudget = 8000" not in code, (
            "RunDetailPanel 不能给 tokenBudget 一个写死的默认值 —— 那正是 17,587/8,000 的来源"
        )
        assert not re.search(r"tokenBudget\s*=\s*\d", code), "tokenBudget 不得有任何数字字面量默认值"

    def test_console_does_not_pass_a_made_up_budget(self):
        code = _code(CONSOLE)
        m = re.search(r"tokenBudget=\{([^}]*)\}", code)
        assert m, "Console 应显式说明 tokenBudget 的来源"
        expr = m.group(1)
        assert "8000" not in expr, "Console 不得再传字面量 8000"
        # Quick BI 显式传 0(不走平台额度),智能体侧传 undefined(用后端下发的真实值)
        assert "undefined" in expr, "智能体侧必须让面板回落到后端下发的预算"

    def test_panel_reads_budget_from_backend(self):
        code = _code(PANEL)
        assert "t?.budget" in code, "进度条分母必须取自后端下发的 info.tokens.budget"
        assert "effBudget" in code, "应有一个显式的「实际生效预算」变量"

    def test_unlimited_draws_no_progress_bar(self):
        """不限制(0/缺省)时只报消耗、不画条,更不能出现「即将熔断」。"""
        code = _code(PANEL)
        assert re.search(r"budgeted\s*=\s*!!effBudget\s*&&\s*effBudget\s*>\s*0", code), (
            "只有拿到正数预算才算「有护栏」"
        )
        # 「接近预算上限」那句必须锁在 budgeted 分支里(pct 只在有预算时才有意义)
        assert "pct > 90" in code


class TestBudgetPlumbedEndToEnd:
    def test_backend_emits_effective_budget(self):
        src = (Path(__file__).resolve().parent.parent
               / "cogni_agent" / "runtime_service" / "engine" / "executor.py").read_text(encoding="utf-8")
        assert '"token_budget": gr.resolve_token_budget(spec.token_budget)' in src, (
            "result.usage 必须带上本轮实际生效的预算,否则前端只能瞎猜"
        )

    def test_api_type_carries_budget(self):
        assert "token_budget?: number | null" in _src(API)

    def test_console_replays_budget_from_history(self):
        code = _code(CONSOLE)
        assert "t.usage.token_budget" in code, "翻历史会话时也要用当时生效的预算"
        assert "usage?.token_budget" in code, "done 事件要带上预算"


class TestChartReplay:
    def test_console_reads_structured_on_replay(self):
        code = _code(CONSOLE)
        assert "replayCharts(t.structured)" in code, (
            "openSession 重建历史消息时必须读 result.structured,否则刷新后图全丢"
        )

    def test_replay_helper_handles_both_shapes(self):
        """structured 在后端既可能是对象也可能是数组(早期数据),两种都得吃。"""
        code = _code(CONSOLE)
        m = re.search(r"function replayCharts[\s\S]*?\n}", code)
        assert m, "缺少 replayCharts"
        body = m.group(0)
        assert "Array.isArray(structured)" in body
        assert "b?.charts" in body
        assert "agentChartToCard" in body, "回放与实时事件必须走同一个转换,两侧渲染才一致"

    def test_replay_helper_is_defensive(self):
        """一条画不出来的脏数据不该让整段历史回放炸掉。"""
        body = re.search(r"function replayCharts[\s\S]*?\n}", _code(CONSOLE)).group(0)
        assert "if (card) out.push(card)" in body

    def test_sse_terminal_fallback_carries_structured(self):
        """断线后走 getChatResult 补终态的那条路,也必须把 structured 带上 ——
        那时实时 chart 事件已经错过了,不带就等于没有图。"""
        code = _code(API)
        m = re.search(r"event:\s*'done'[^;]*?structured", code)
        assert m, "ensureTerminal 的 done 事件必须携带 structured"

    def test_api_type_declares_structured(self):
        assert "structured?:" in _src(API), "ConversationTurn 要声明 structured,否则前端读不到"


class TestSkillGuardrailForm:
    """「Skill 的消耗在哪里设置」—— 护栏约束页得真有这个字段。"""

    def test_edit_form_has_token_budget(self):
        code = _code(SKILL_EDIT)
        assert "tokenBudget" in code
        assert "token_budget:" in code, "保存时必须写进 constraints.token_budget"

    def test_empty_means_unlimited_not_zero_by_accident(self):
        """留空 -> null(交给全局默认);填 0 -> 0(显式不限制)。两者都不能变成 NaN。"""
        code = _code(SKILL_EDIT)
        assert "tokenBudget.trim() === '' ? null" in code, "留空必须传 null 而不是 0/NaN"

    def test_key_is_always_written_so_clearing_works(self):
        """token_budget 这个键必须无条件写进 constraints —— 否则把「有上限」改回
        「跟随全局」时,extraConstraints 里的旧值会被原样留下,改了等于没改。"""
        code = _code(SKILL_EDIT)
        m = re.search(r"constraints:\s*\{[^}]*\}", code, re.S)
        assert m and "token_budget" in m.group(0)

    def test_placeholder_says_unlimited(self):
        assert "留空 = 不限制" in _src(SKILL_EDIT), "得让配置的人一眼看出默认是不限制"

    def test_detail_page_shows_unlimited_instead_of_a_number(self):
        code = _code(SKILL_DETAIL)
        assert "token_budget" in code
        assert "不限制" in code, "详情页不限制时要写「不限制」,不能画一个看起来像上限的数字"
