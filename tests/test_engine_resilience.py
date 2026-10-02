"""
工具韧性与意图分诊测试。

对应两个真实失败模式(都不会报错,只会安静烧光预算然后给个烂答案):
- 模型拿相同参数反复调同一个工具
- 某个工具的上游挂了,模型不知道"它现在不可用",继续尝试
"""
from __future__ import annotations

import pytest

import cogni_agent.config as cfgmod
from cogni_agent.runtime_service.engine import executor
from cogni_agent.runtime_service.engine import resilience as res
from cogni_agent.runtime_service.engine import resolver
from cogni_agent.runtime_service.engine.resolver import ResolvedSkill


@pytest.fixture(autouse=True)
def _cfg():
    r = cfgmod.get_settings().raw.setdefault("runtime", {}).setdefault("resilience", {})
    saved = dict(r)
    r.update({"repeat_threshold": 2, "max_consecutive_tool_failures": 3})
    yield
    r.clear()
    r.update(saved)


def test_signature_ignores_key_order_and_whitespace():
    """模型两次生成的 JSON 可能键序/空格不同但语义一样,只比字符串会漏掉这种实质重复。"""
    a = res.call_signature("t", '{"city":"厦门","days":3}')
    b = res.call_signature("t", '{ "days": 3 , "city" : "厦门" }')
    assert a == b
    assert a != res.call_signature("t", '{"city":"武夷山","days":3}')
    assert a != res.call_signature("other", '{"city":"厦门","days":3}')


def test_bad_json_arguments_still_comparable():
    """参数本身是坏 JSON 时退回原始串比较,不能因为解析失败就当成每次都不同。"""
    t = res.ToolCallTracker()
    args = '{"city": 厦门'          # 非法 JSON
    assert res.check_repeat(t, "x", args).is_repeat is False
    assert res.check_repeat(t, "x", args).is_repeat is True


def test_repeat_detected_on_second_identical_call():
    t = res.ToolCallTracker()
    first = res.check_repeat(t, "get_weather", '{"city":"厦门"}')
    assert first.is_repeat is False and first.times == 1

    res.remember_result(t, "get_weather", '{"city":"厦门"}', '{"temp": 28}')
    second = res.check_repeat(t, "get_weather", '{"city":"厦门"}')
    assert second.is_repeat is True and second.times == 2
    assert second.previous_result and "28" in second.previous_result


def test_repeat_notice_is_actionable():
    """提示要告诉模型**该怎么办**,只说"别重复"它下一步还是不知道干什么。"""
    v = res.RepeatVerdict(is_repeat=True, times=2, previous_result='{"temp":28}')
    msg = res.repeat_notice("get_weather", v)
    assert "get_weather" in msg
    assert "28" in msg, "应把上次结果回给模型"
    assert ("换" in msg or "其它" in msg), "应给出可行动的下一步"


def test_tool_disabled_after_consecutive_failures():
    t = res.ToolCallTracker()
    assert res.record_outcome(t, "broken", ok=False) is None      # 1
    assert res.record_outcome(t, "broken", ok=False) is None      # 2
    notice = res.record_outcome(t, "broken", ok=False)            # 3 -> 摘掉
    assert notice and "broken" in notice
    assert "broken" in t.disabled


def test_success_resets_failure_streak():
    """只统计**连续**失败。偶发抖动不该让工具被摘掉。"""
    t = res.ToolCallTracker()
    res.record_outcome(t, "flaky", ok=False)
    res.record_outcome(t, "flaky", ok=False)
    res.record_outcome(t, "flaky", ok=True)                       # 成功一次清零
    assert res.record_outcome(t, "flaky", ok=False) is None
    assert "flaky" not in t.disabled


def test_disabled_tool_removed_from_options():
    """摘掉必须是**物理移除**:只要工具还在 tools 数组里,模型就有可能再选它。"""
    class _T:
        def __init__(self, n): self.func_name, self.name = n, n

    tools = [_T("good"), _T("broken")]
    t = res.ToolCallTracker(disabled=["broken"])
    remaining = [x.func_name for x in res.usable_tools(tools, t)]
    assert remaining == ["good"]


def test_tracker_survives_serialization():
    """轨迹随 checkpoint 持久化,HITL 暂停恢复后必须还记得哪个工具坏了。"""
    t = res.ToolCallTracker()
    res.check_repeat(t, "a", "{}")
    res.record_outcome(t, "a", ok=False)
    restored = res.ToolCallTracker.from_dict(t.as_dict())
    assert restored.seen == t.seen and restored.failures == t.failures


# ── 意图分诊 ──
def test_intent_mode_off_by_default():
    """默认关闭:它给每次对话增加一次模型调用,而且误判比不判更烦人。"""
    from cogni_agent.runtime_service.engine import intent

    r = cfgmod.get_settings().raw.setdefault("runtime", {}).setdefault("intent_check", {})
    r["mode"] = "off"
    assert intent.enabled_for(["x"], True) is False


def test_intent_side_effects_mode():
    """side_effects 模式只在存在写操作时开:
    纯查询猜错了只是答得不对,写操作猜错了是真的把事情做了。"""
    from cogni_agent.runtime_service.engine import intent

    r = cfgmod.get_settings().raw.setdefault("runtime", {}).setdefault("intent_check", {})
    r["mode"] = "side_effects"
    assert intent.enabled_for(["x"], has_side_effects=True) is True
    assert intent.enabled_for(["x"], has_side_effects=False) is False


def test_intent_mode_survives_yaml_bool_gotcha():
    """YAML 1.1 会把裸 off/on 解析成布尔。配置忘了加引号时不能崩,也不能误开。"""
    from cogni_agent.runtime_service.engine import intent

    r = cfgmod.get_settings().raw.setdefault("runtime", {}).setdefault("intent_check", {})
    r["mode"] = False          # 相当于 yaml 里写了裸 off
    assert intent.enabled_for(["x"], True) is False
    r["mode"] = True           # 裸 on
    assert intent.enabled_for(["x"], False) is True


def test_intent_result_defaults_to_execute():
    """分诊本身失败/返回异常时必须放行——分诊挂了不该挡住正常执行。"""
    from cogni_agent.runtime_service.engine.intent import IntentResult

    assert IntentResult().should_execute is True


# ── v1.1.5 框架级修复:Triager 缓存按会话隔离 ─────────────────────────
# conv_5a900be0 事故:Triager 判定会裁剪执行链(漏挂客流分析只挂参数解析),旧缓存
# key 不含会话维度,新会话问相同 goal + slots 恰好相同 → 直接复用旧会话的漏挂判定。
# key 必须含 conversation_id(无会话时用 task_id),与 checkpoint 的 thread_id 同粒度:
# 同一会话多轮共享(多轮命中率收益保留),跨会话不共享(错误判定不传染)。


def test_triage_cache_key_is_isolated_by_conversation():
    """跨会话不共享:goal/capabilities/slots/model 全相同,conversation_id 不同 → key 不同。"""
    from cogni_agent.runtime_service.engine.intent import _triage_cache_key

    caps = [{"name": "数字大脑-客流分析", "description": "查客流", "tools": ["客流分析"]}]
    slots = {"scenicId": "1001", "startTime": "2026-08-01", "endTime": "2026-08-31"}
    kwargs = dict(goal="查一下上个月客流", capabilities=caps, slots=slots, model="m1")

    a = _triage_cache_key(conversation_id="conv_A", **kwargs)
    b = _triage_cache_key(conversation_id="conv_B", **kwargs)
    assert a != b, "跨会话(不同 conversation_id)必须得到不同缓存 key"

    # 同一会话多轮仍共享缓存(多轮命中率收益保留)
    assert _triage_cache_key(conversation_id="conv_A", **kwargs) == a
    # 无会话(单轮任务,调用方传 task_id)与具体会话互不串用
    assert _triage_cache_key(conversation_id="task_123", **kwargs) not in (a, b)


# ── v1.1.5 规则兜底:constraints.route_hit_keywords 强制挂载(方案 A) ──
# 设计约束:不改 Triager 判定(框架级),关键词是技能侧配置,executor 只做通用合并。


def _mk_skill(skill_id: str, name: str, keywords: list[str] | None = None) -> ResolvedSkill:
    return ResolvedSkill(
        id=skill_id, name=name, system_prompt="", tools=[], knowledge_sources=[],
        max_steps=5, max_tool_calls=3, route_hit_keywords=keywords or [],
    )


def test_force_hit_skills_matches_route_hit_keywords():
    """goal 命中技能声明的关键词 -> 该技能必须被强制挂载(不依赖 Triager 判定)。"""
    skills = [
        _mk_skill("s1", "客流分析", ["客流", "人流量", "拥挤", "人多"]),
        _mk_skill("s2", "舆情分析", ["口碑", "评价"]),
        _mk_skill("s3", "票务经营报表", None),  # 未声明关键词,不参与兜底
    ]
    assert executor._force_hit_skills(skills, "周末景区人多吗") == ["客流分析"]
    assert executor._force_hit_skills(skills, "看看景区最近的评价") == ["舆情分析"]
    assert executor._force_hit_skills(skills, "帮我查一下上个月的客流数据") == ["客流分析"]
    assert executor._force_hit_skills(skills, "随便聊聊") == []


def test_route_hit_keywords_merge_is_order_preserved_dedup():
    """兜底合并:保留 Triager 判定顺序,追加漏挂技能,同技能不重复(保序去重)。"""
    skills = [
        _mk_skill("s1", "客流分析", ["客流"]),
        _mk_skill("s2", "舆情分析", ["口碑"]),
    ]
    target = ["舆情分析"]  # Triager 只判了舆情(漏挂客流,conv_5a900be0 式事故)
    forced = executor._force_hit_skills(skills, "上个月客流和口碑一起看")
    assert forced == ["客流分析", "舆情分析"]  # goal 同时命中两个技能关键词
    merged = list(dict.fromkeys([*target, *forced]))
    assert merged == ["舆情分析", "客流分析"]  # 舆情已在,补客流;舆情不重复
    # Triager 已正确挂上时,兜底不引入重复
    assert list(dict.fromkeys([*["客流分析", "舆情分析"], *forced])) == ["客流分析", "舆情分析"]


def test_route_hit_keywords_parse_defensive():
    """constraints.route_hit_keywords 解析:正常清洗 / 非法类型静默降级 / 空值剔除。"""
    assert resolver._route_hit_keywords(
        {"route_hit_keywords": [" 客流 ", "", "人流量"]}) == ["客流", "人流量"]
    assert resolver._route_hit_keywords({"route_hit_keywords": "客流"}) == []  # 字符串误配 -> 降级
    assert resolver._route_hit_keywords({"route_hit_keywords": 3}) == []       # 数字误配 -> 降级
    assert resolver._route_hit_keywords({}) == []                              # 未配置
