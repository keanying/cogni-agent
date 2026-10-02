# -*- coding: utf-8 -*-
"""把「时间对齐」「数值计算」真正做成内置能力。

后台 Tool 中心里手建的那两行(`type=builtin`,名字是中文)**从建出来那天起就调不通** ——
`type=builtin` 的分发是 `builtin_tools.get(spec.name)`,按**工具名**去内置注册表里找函数,
而注册表的键是 `sys_date_range` / `sys_calc`,中文名一个都找不到。平时看不出来,
是因为真正的内置工具本来就自动注入给每个技能,模型用的一直是那一份。

而那两行描述里写的能力,内置版**确实缺**:
  - 时间:季度、节假日(五一/春节/国庆)一个都不认,「上个月」这个说法也不认;
  - 数值:只收裸表达式,同比/环比/占比/达成率的**公式和分母**还是模型自己写。

这两处缺口的共同点是**错了不报错**:认不出的周期按单日处理,于是「本季度的票房」
被当成「今天的票房」答掉;`(今-去)/今` 写成分母错的版本,照样得到一个量级合理的百分比。
"""
from __future__ import annotations

import datetime as dt

import pytest

from cogni_agent.tool_center_service import builtin_tools as bi

BASE = "2026-09-21"          # 周一;2026 Q3


def rng(period, end=BASE):
    return bi.builtin_date_range(period=period, end=end)


class TestQuarters:
    @pytest.mark.parametrize("period,start,end", [
        ("本季度", "2026-07-01", BASE),          # 季初至今
        ("上季度", "2026-04-01", "2026-06-30"),
        ("去年同季度", "2025-07-01", "2025-09-30"),
        ("2026Q1", "2026-01-01", "2026-03-31"),
        ("2026年第1季度", "2026-01-01", "2026-03-31"),
        ("Q3", "2026-07-01", "2026-09-30"),
        ("三季度", "2026-07-01", "2026-09-30"),   # 运营嘴里说的是中文数字
        ("第三季度", "2026-07-01", "2026-09-30"),
        ("3季度", "2026-07-01", "2026-09-30"),
    ])
    def test_quarter_forms(self, period, start, end):
        r = rng(period)
        assert (r.get("start"), r.get("end")) == (start, end), r

    def test_q1_this_quarter_rolls_back_a_year(self):
        """1 月问「上季度」,要的是去年 Q4,不是今年 Q0。"""
        r = rng("上季度", end="2026-01-15")
        assert (r["start"], r["end"]) == ("2025-10-01", "2025-12-31")

    def test_quarter_is_not_silently_treated_as_a_single_day(self):
        """这正是旧行为的害处:认不出就按单日,「本季度票房」变成「今天票房」,不报错。"""
        r = rng("本季度")
        assert r["days"] > 1


class TestSpokenAliases:
    @pytest.mark.parametrize("said,same_as", [
        ("上个月", "上月"), ("上一个月", "上月"),
        ("这个月", "本月"), ("当月", "本月"),
        ("这周", "本周"), ("上个星期", "上周"),
        ("本年度", "今年"),
    ])
    def test_spoken_forms_match_canonical(self, said, same_as):
        a, b = rng(said), rng(same_as)
        assert (a["start"], a["end"]) == (b["start"], b["end"]), f"{said} != {same_as}"

    @pytest.mark.parametrize("period,start,end", [
        ("前天", "2026-09-19", "2026-09-19"),
        ("去年", "2025-01-01", "2025-12-31"),
        ("近14天", "2026-09-08", BASE),
        ("最近3天", "2026-09-19", BASE),
        ("过去60天", "2026-07-24", BASE),
    ])
    def test_more_periods(self, period, start, end):
        r = rng(period)
        assert (r.get("start"), r.get("end")) == (start, end), r


class TestHolidays:
    """**节假日是一张表,不是一套算法 —— 这一点是刻意的。**

    春节/端午/中秋按农历,清明按节气,调休每年由国务院单独发文,三者都推不出来。
    而猜一个日期是这里最坏的结果:模型会拿着它去取数,取回一段普通日子的数,
    然后一本正经地写「今年五一客流同比下滑」—— 报告结构完整、数字也真实,
    只是那几天根本不是五一,没人会回头核对日期。
    """

    @pytest.mark.parametrize("said,start,end", [
        ("五一", "2026-05-01", "2026-05-05"),
        ("劳动节", "2026-05-01", "2026-05-05"),
        ("国庆", "2026-10-01", "2026-10-08"),
        ("十一", "2026-10-01", "2026-10-08"),
        ("春节", "2026-02-15", "2026-02-23"),
        ("过年", "2026-02-15", "2026-02-23"),
        ("元旦", "2026-01-01", "2026-01-03"),
        ("清明", "2026-04-04", "2026-04-06"),
        ("端午", "2026-06-19", "2026-06-21"),
        ("中秋", "2026-09-25", "2026-09-27"),
    ])
    def test_known_holidays(self, said, start, end):
        r = rng(said)
        assert (r.get("start"), r.get("end")) == (start, end), r

    @pytest.mark.parametrize("said,start", [
        ("去年国庆", "2025-10-01"),
        ("2025五一", "2025-05-01"),
        ("2025年春节", "2025-01-28"),
    ])
    def test_year_prefixes(self, said, start):
        assert rng(said).get("start") == start

    def test_unknown_year_says_so_instead_of_guessing(self):
        """表里没有的年份必须**明说不知道**,而且绝不能退回按单日处理。"""
        r = rng("2030春节")
        assert "error" in r, r
        assert r.get("start") is None
        assert "2030" in r["error"]
        assert r["holiday"] == "春节" and r["year"] == 2030

    def test_unknown_year_is_not_silently_swapped_for_this_year(self):
        """问 2030 年春节,不能给今年春节的日期 —— 返回里连年份都写着今年,谁也看不出被换掉了。

        (旧的 _holiday_year 就是这样:在 base.year±几年里找字符串,范围外的静默退回今年。)
        """
        r2030 = rng("2030春节")
        r2026 = rng("2026春节")
        assert r2030.get("start") != r2026.get("start")

    def test_holiday_span_covers_the_whole_break_not_just_the_day(self):
        """运营说的「五一」指的是那段假期,不是 5 月 1 日一天。"""
        r = rng("五一")
        assert r["days"] >= 3, r

    def test_table_years_are_internally_consistent(self):
        for year, table in bi._HOLIDAYS.items():
            for name, (s, e) in table.items():
                sd, ed = dt.date.fromisoformat(s), dt.date.fromisoformat(e)
                assert sd <= ed, f"{year} {name} 起止反了"
                assert sd.year == year and ed.year == year, f"{year} {name} 跨年了"

    def test_every_alias_points_at_a_real_key(self):
        keys = set(bi._HOLIDAYS[2026])
        for alias, key in bi._HOLIDAY_ALIASES.items():
            assert key in keys, f"别名「{alias}」指向了表里没有的「{key}」"


class TestMetricDelta:
    def test_yoy_denominator_is_the_compare_period(self):
        """最常见的错法:分母取成本期。两种写法都给出像样的百分比,报告里看不出区别。"""
        r = bi.builtin_metric_delta(kind="同比", current=386.2, previous=354.1)
        assert r["percent"] == 9.07                      # (386.2-354.1)/354.1
        assert r["percent"] != round((386.2 - 354.1) / 386.2 * 100, 2)

    def test_mom(self):
        r = bi.builtin_metric_delta(kind="环比", current=703, previous=511)
        assert r["percent"] == 37.57 and r["direction"] == "上升" and r["diff"] == 192.0

    def test_decline_direction(self):
        r = bi.builtin_metric_delta(kind="环比", current=400, previous=500)
        assert r["percent"] == -20.0 and r["direction"] == "下降"

    def test_flat(self):
        assert bi.builtin_metric_delta(kind="同比", current=5, previous=5)["direction"] == "持平"

    def test_share(self):
        r = bi.builtin_metric_delta(kind="占比", current=27, total=90)
        assert r["percent"] == 30.0

    def test_completion_rate(self):
        r = bi.builtin_metric_delta(kind="达成率", current=703, target=1000)
        assert r["percent"] == 70.3 and r["reached"] is False and r["gap"] == -297.0

    def test_diff(self):
        r = bi.builtin_metric_delta(kind="差额", current=703, previous=511)
        assert r["diff"] == 192.0 and "percent" not in r

    def test_zero_base_returns_no_percentage_at_all(self):
        """从 0 涨到 500 在业务上没有百分比可言 —— 它是「从无到有」。

        返回 0% 或 100% 都会被原样写进报告,而两个都是错的。
        """
        r = bi.builtin_metric_delta(kind="环比", current=500, previous=0)
        assert r["percent"] is None
        assert "没有意义" in r["note"]
        assert "从 0 增加到" in r["note"]

    def test_missing_base_is_distinguished_from_a_zero_base(self):
        """「没给总量」和「总量是 0」是两件事,提示不能混。"""
        missing = bi.builtin_metric_delta(kind="占比", current=1)
        zero = bi.builtin_metric_delta(kind="占比", current=1, total=0)
        assert "error" in missing and "没给" in missing["error"]
        assert "error" not in zero and zero["percent"] is None

    def test_numbers_with_separators_and_percent_signs(self):
        r = bi.builtin_metric_delta(kind="环比", current="1,203", previous="1,000")
        assert r["percent"] == 20.3

    def test_garbage_input_is_named_not_swallowed(self):
        r = bi.builtin_metric_delta(kind="环比", current="约一千", previous=100)
        assert "error" in r and "current" in r["error"]

    def test_unknown_kind_lists_the_valid_ones(self):
        r = bi.builtin_metric_delta(kind="增长率", current=1, previous=2)
        assert "error" in r and "同比" in r["error"]

    def test_digits(self):
        r = bi.builtin_metric_delta(kind="占比", current=1, total=3, digits=4)
        assert r["percent"] == 33.3333

    def test_registered_as_a_builtin(self):
        assert "sys_metric_delta" in bi.BUILTIN
        assert bi.get("sys_metric_delta") is bi.builtin_metric_delta
        schema = bi.BUILTIN["sys_metric_delta"]["schema"]
        assert set(schema["required"]) == {"kind", "current"}


class TestSupersededRows:
    """后台那两行:标出来、不自动删,而且已经绑了它们的技能不能因此断掉。"""

    @pytest.mark.parametrize("legacy,builtin_name", [
        ("时间对齐", "sys_date_range"),
        ("时间对其", "sys_date_range"),      # 现场确实有写成「对其」的
        ("数值计算", "sys_calc"),
    ])
    def test_legacy_names_map_to_a_builtin(self, legacy, builtin_name):
        assert bi.superseded_by(legacy) == builtin_name
        assert bi.get(legacy) is bi.BUILTIN[builtin_name]["fn"]

    def test_real_builtin_names_are_not_marked_superseded(self):
        for name in bi.BUILTIN:
            assert bi.superseded_by(name) is None

    def test_unknown_name_is_not_marked(self):
        assert bi.superseded_by("雷达图指标") is None

    def test_every_supersede_target_exists(self):
        for legacy, target in bi.SUPERSEDED.items():
            assert target in bi.BUILTIN, f"「{legacy}」指向了不存在的内置工具 {target}"

    def test_resolver_drops_them_instead_of_showing_two_same_named_tools(self):
        """摘掉而不是改名指过去:改名会得到两个同名工具,模型会随机挑一个 —— 很难复现。"""
        import inspect

        from cogni_agent.runtime_service.engine import resolver

        src = inspect.getsource(resolver._drop_superseded_builtins)
        assert "superseded_by" in src
        assert "tool_superseded_by_builtin" in src

    def test_api_marks_the_row(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "cogni_agent" / "tool_center_service" / "routes.py").read_text(encoding="utf-8")
        assert '"superseded_by"' in src and '"superseded_note"' in src

    def test_list_page_shows_the_badge(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "web" / "src" / "pages" / "Tools" / "ToolList.tsx").read_text(encoding="utf-8")
        assert "superseded_by" in src and "superseded_note" in src
