"""Token 预算护栏的三态语义 + 「刷新后图表丢失」那条链路的落库/透出。

这里钉住的是两件被用户在生产上撞见的事:

1. **「默认不限制」必须真的是不限制。** 护栏历史上写的是 `task_budget or 默认值`,
   于是「显式配 0 = 不限制」会被 `or` 吃掉、回落成默认值继续熔断。三态
   (None / 0 / 正数)必须分得清清楚楚,否则「默认不限制」这句承诺是假的。
2. **前端不许自己编预算。** 执行详情里那条 `17,587 / 8,000 接近上限` 的红条,
   8,000 是前端写死的常数,跟后端毫无关系 —— 正常的一轮被画成即将熔断。
   所以后端必须把**实际生效**的预算随 result.usage 下发。
"""
from __future__ import annotations

import pytest

from cogni_agent.common.errors import GuardrailTripped
from cogni_agent.runtime_service.engine import guardrails as gr


def _state(prompt: int, completion: int) -> gr.GuardrailState:
    s = gr.new_guardrail_state()
    gr.record_tokens(s, prompt, completion)
    return s


class TestResolveTokenBudget:
    """预算解析的三态。"""

    def test_none_falls_back_to_global_default(self, monkeypatch):
        """没配过 -> 回退全局默认。"""
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 50000})
        assert gr.resolve_token_budget(None) == 50000

    def test_explicit_zero_means_unlimited_not_global_default(self, monkeypatch):
        """显式 0 = 不限制,**不得**回落成全局默认。

        这正是旧代码 `task_budget or default` 的 bug:0 是 falsy,配了等于没配。
        """
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 50000})
        assert gr.resolve_token_budget(0) == 0

    def test_positive_value_wins(self, monkeypatch):
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 50000})
        assert gr.resolve_token_budget(20000) == 20000

    def test_global_default_zero_is_unlimited(self, monkeypatch):
        """出厂配置 default_task_token_budget: 0 -> 不限制。"""
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        assert gr.resolve_token_budget(None) == 0

    def test_missing_global_key_is_unlimited(self, monkeypatch):
        """配置里压根没有这个键(老配置文件)也按不限制,不要凭空冒出一个上限。"""
        monkeypatch.setattr(gr, "_limits", lambda: {})
        assert gr.resolve_token_budget(None) == 0

    def test_negative_is_clamped_to_unlimited(self, monkeypatch):
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 50000})
        assert gr.resolve_token_budget(-1) == 0

    def test_garbage_config_does_not_kill_tasks(self, monkeypatch):
        """配置写成非数字时按不限制处理 —— 配置错误不该表现为「任务莫名被熔断」。"""
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": "五万"})
        assert gr.resolve_token_budget(None) == 0


class TestCheckTokenBudget:
    """熔断行为本身。"""

    def test_unlimited_never_trips_however_big(self, monkeypatch):
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        st = _state(10_000_000, 5_000_000)
        gr.check_token_budget(st, None)          # 不抛就是通过

    def test_explicit_zero_never_trips(self, monkeypatch):
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 8000})
        gr.check_token_budget(_state(30_000, 900), 0)

    def test_positive_budget_still_trips(self, monkeypatch):
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        with pytest.raises(GuardrailTripped) as ei:
            gr.check_token_budget(_state(9_000, 2_000), 10_000)
        assert "10000" in str(ei.value)

    def test_exactly_at_budget_is_allowed(self, monkeypatch):
        """等于预算不熔断(> 而非 >=):刚好用满不该被判出局。"""
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        gr.check_token_budget(_state(6_000, 4_000), 10_000)

    def test_tokens_are_recorded_even_when_unlimited(self, monkeypatch):
        """「默认不限制,但是也要记录消耗」—— 记账与熔断是两件事。"""
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        st = _state(12_683, 4_904)
        gr.check_token_budget(st, None)
        assert st.prompt_tokens == 12_683
        assert st.completion_tokens == 4_904
        assert st.as_dict()["prompt_tokens"] == 12_683

    def test_the_screenshot_case_no_longer_trips(self, monkeypatch):
        """用户截图那一轮:17,587 token,前端画成 /8,000 即将熔断。

        真实生效预算是「不限制」,这一轮从头到尾都没有危险 —— 钉住它。
        """
        monkeypatch.setattr(gr, "_limits", lambda: {"default_task_token_budget": 0})
        gr.check_token_budget(_state(12_683, 4_904), None)


class TestShippedConfigDefault:
    """出厂 config.yaml 的默认值本身就得是「不限制」。"""

    def test_config_yaml_default_is_unlimited(self):
        import pathlib
        import yaml

        root = pathlib.Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["runtime"]["guardrails"]["default_task_token_budget"] == 0, (
            "出厂默认必须是 0(不限制);要加上限请配在 Skill 护栏约束或系统设置的每日上限上"
        )


class TestSkillConstraintParsing:
    """Skill → constraints.token_budget 的解析。"""

    def test_optional_budget_three_states(self):
        from cogni_agent.runtime_service.engine.resolver import _optional_budget

        assert _optional_budget(None) is None         # 没配
        assert _optional_budget("") is None           # 表单清空
        assert _optional_budget(0) == 0               # 显式不限制
        assert _optional_budget("0") == 0
        assert _optional_budget(20000) == 20000
        assert _optional_budget("20000") == 20000
        assert _optional_budget(-5) == 0              # 负数按不限制
        assert _optional_budget("abc") is None        # 写歪了当没配,不改变熔断行为

    def test_zero_is_not_swallowed_as_unset(self):
        """0 与 None 必须可区分 —— 整个「默认不限制」就靠这一点。"""
        from cogni_agent.runtime_service.engine.resolver import _optional_budget

        assert _optional_budget(0) is not None
        assert _optional_budget(0) == 0


class TestChainRollup:
    """技能链上多个技能各自配预算时,任务级取哪个。"""

    @staticmethod
    def _spec(budgets):
        from cogni_agent.runtime_service.engine.resolver import ExecutionSpec, ResolvedSkill

        chain = [
            ResolvedSkill(id=f"s{i}", name=f"skill{i}", system_prompt="", tools=[],
                          knowledge_sources=[], max_steps=25, max_tool_calls=10,
                          token_budget=b)
            for i, b in enumerate(budgets)
        ]
        return ExecutionSpec(executor_type="agent", executor_id="a1", skill_chain=chain)

    @staticmethod
    def _rollup(spec, overrides=None):
        """复刻 resolve_execution_spec 尾部那段汇总逻辑(不拉起整条解析链)。"""
        override = (overrides or {}).get("token_budget")
        if override is not None:
            return override
        caps = [sk.token_budget for sk in spec.skill_chain
                if sk.token_budget is not None and sk.token_budget > 0]
        all_unlimited = bool(spec.skill_chain) and all(
            sk.token_budget == 0 for sk in spec.skill_chain)
        return min(caps) if caps else (0 if all_unlimited else None)

    def test_none_configured_falls_through_to_global(self):
        assert self._rollup(self._spec([None, None])) is None

    def test_strictest_positive_wins(self):
        assert self._rollup(self._spec([50000, 20000, None])) == 20000

    def test_unlimited_skill_does_not_loosen_a_capped_one(self):
        """配 0 的技能表达「我不提要求」,不是「把别人也放开」。"""
        assert self._rollup(self._spec([0, 20000])) == 20000

    def test_all_explicitly_unlimited_stays_unlimited(self):
        assert self._rollup(self._spec([0, 0])) == 0

    def test_payload_override_beats_skill_config(self):
        """离线预热带 overrides.token_budget,必须压过技能配置。"""
        assert self._rollup(self._spec([20000]), {"token_budget": 150000}) == 150000

    def test_override_zero_is_honoured(self):
        assert self._rollup(self._spec([20000]), {"token_budget": 0}) == 0
