"""L3 确定性规则补全:intent.apply_param_defaults / _shift_time 单元测试。

对应 conv_a5c0d4c1e44943509a190c468fa623e0 教训:agent 提示词声明「对比期默认
环比」,但 Triager 漏填 compareStartTime/compareEndTime,执行层被迫反问。L3 在
Triager 输出后按工具声明的 x-default-rule(模式 = 平移参照时间)由代码确定性补全,
不反问、不依赖模型遵守率。纯函数测试,不依赖数据库。
"""

from types import SimpleNamespace

from cogni_agent.runtime_service.engine import intent


def _tool(name: str, param_defaults: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, param_defaults=param_defaults)


def _skill(name: str, tools: list) -> SimpleNamespace:
    return SimpleNamespace(name=name, tools=tools)


# 客流比对汇总:对比期缺省默认环比(与线上工具 x-default-rule 声明一致)
TOOL_COMPARE = _tool("客流比对汇总", {
    "compareStartTime": {"mode": "shift", "ref": ["startTime"], "period": "month", "offset": -1},
    "compareEndTime": {"mode": "shift", "ref": ["endTime"], "period": "month", "offset": -1},
})
TOOL_DAILY = _tool("日报表", {
    "reportDate": {"mode": "shift", "ref": ["queryDate"], "period": "day", "offset": -1},
})
SKILL_SCENIC = _skill("数字大脑-客流分析", [TOOL_COMPARE])
SKILL_REVENUE = _skill("数字大脑-收入分析", [TOOL_DAILY])

CHAIN = [SKILL_SCENIC, SKILL_REVENUE]


# ── L3 补全 ──────────────────────────────────────────────────────

def test_mom_fill_when_compare_missing():
    """事故场景:本期已填、对比期缺省 -> 按环比(上月同期)确定性补全。"""
    filled = {"supplierId": "PFTSCA01018088",
              "startTime": "2026-08-01", "endTime": "2026-08-31"}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=filled,
    )
    assert got == {"compareStartTime": "2026-07-01", "compareEndTime": "2026-07-31"}


def test_already_filled_not_overwritten():
    """已填参数不被覆盖,只补真正缺失的。"""
    filled = {"startTime": "2026-08-01", "endTime": "2026-08-31",
              "compareStartTime": "2026-06-01"}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=filled,
    )
    assert got == {"compareEndTime": "2026-07-31"}


def test_missing_ref_param_not_filled():
    """参照参数(本期时间)缺失 -> 无法计算,宁缺勿补(留给 L2 拦截)。"""
    filled = {"supplierId": "PFTSCA01018088"}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=filled,
    )
    assert got == {}


def test_non_target_skill_ignored():
    """不在 target_skills 里的技能,其工具缺省规则不生效。"""
    filled = {"startTime": "2026-08-01", "endTime": "2026-08-31"}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-收入分析"], filled=filled,
    )
    # 收入分析只有 day 粒度规则且 queryDate 缺失 -> 空
    assert got == {}


def test_empty_target_skills_returns_empty():
    """target_skills 为空(全量执行/降级)不补全 -> 恒空。"""
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=[], filled={"startTime": "2026-08-01"},
    )
    assert got == {}


def test_unparseable_time_not_filled():
    """参照时间无法解析 -> 该参数不补(宁缺勿臆造),其他参数独立判定不受影响。"""
    filled = {"startTime": "not-a-date", "endTime": "2026-08-31"}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=filled,
    )
    # startTime 解析失败 -> compareStartTime 不补;endTime 正常 -> compareEndTime 照补
    assert got == {"compareEndTime": "2026-07-31"}


def test_idempotent_second_call_returns_empty():
    """幂等:补全结果并入 filled 后再调用 -> 不再补(已覆盖)。"""
    filled = {"supplierId": "PFTSCA01018088",
              "startTime": "2026-08-01", "endTime": "2026-08-31"}
    merged = {**filled, **intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=filled)}
    got = intent.apply_param_defaults(
        skill_chain=CHAIN, target_skills=["数字大脑-客流分析"], filled=merged)
    assert got == {}


# ── 规则非法防御 ────────────────────────────────────────────────

def test_invalid_rule_ignored():
    """mode 非法/offset=0/ref 为空/period 非法 -> 规则被忽略,不补。"""
    tool = _tool("非法规则工具", {
        "a": {"mode": "whatever", "ref": ["x"], "period": "month", "offset": -1},
        "b": {"mode": "shift", "ref": ["x"], "period": "month", "offset": 0},
        "c": {"mode": "shift", "ref": [], "period": "month", "offset": -1},
        "d": {"mode": "shift", "ref": ["x"], "period": "fortnight", "offset": -1},
    })
    skill = _skill("非法规则技能", [tool])
    got = intent.apply_param_defaults(
        skill_chain=[skill], target_skills=["非法规则技能"],
        filled={"x": "2026-08-01", "a": "1", "b": "2", "c": "3", "d": "4"},
    )
    assert got == {}


def test_rule_ref_first_entry_used():
    """取 ref 第一个参照参数计算。"""
    tool = _tool("多参照工具", {
        "p": {"mode": "shift", "ref": ["primary", "backup"], "period": "month", "offset": -1},
    })
    skill = _skill("多参照技能", [tool])
    got = intent.apply_param_defaults(
        skill_chain=[skill], target_skills=["多参照技能"],
        filled={"primary": "2026-08-15", "backup": "2026-09-15"},
    )
    assert got == {"p": "2026-07-15"}


# ── 时间换算 _shift_time ────────────────────────────────────────

def test_shift_time_month_end_alignment():
    """月末对齐:3月31日 -1月 -> 2月28日(非闰年)。"""
    assert intent._shift_time("2026-03-31", "month", -1) == "2026-02-28"


def test_shift_time_leap_year_end_alignment():
    """闰年月末对齐:2024-03-31 -1月 -> 2024-02-29。"""
    assert intent._shift_time("2024-03-31", "month", -1) == "2024-02-29"


def test_shift_time_year_crossing():
    """跨年:2026-01-15 -1月 -> 2025-12-15。"""
    assert intent._shift_time("2026-01-15", "month", -1) == "2025-12-15"


def test_shift_time_year_negative():
    """同比:2026-08-01 -12月 -> 2025-08-01;2026-02-28 +1年闰年不变。"""
    assert intent._shift_time("2026-08-01", "month", -12) == "2025-08-01"
    assert intent._shift_time("2026-02-28", "year", 1) == "2027-02-28"


def test_shift_time_day_week():
    """day/week 平移。"""
    assert intent._shift_time("2026-08-01", "day", -7) == "2026-07-25"
    assert intent._shift_time("2026-08-01", "week", -1) == "2026-07-25"


def test_shift_time_preserves_datetime_format():
    """带时分秒的输入输出保持同格式。"""
    assert intent._shift_time("2026-08-01 08:30:00", "month", -1) == "2026-07-01 08:30:00"


def test_shift_time_invalid_input_returns_none():
    """解析失败/非法粒度 -> None。"""
    assert intent._shift_time("abc", "month", -1) is None
    assert intent._shift_time("2026-08-01", "fortnight", -1) is None
