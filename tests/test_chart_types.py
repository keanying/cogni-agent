# -*- coding: utf-8 -*-
"""19 种图表:两端都要有归宿,选错图要有代价。

原先只有 line / bar / pie / radar 四种,白名单写死在 chart_templates 里。扩到 19 种之后,
**风险从「画不出来」变成「画出来但是错的图」** —— 选错的图不会报错,只会把一份对的数据
画成一张误导人的图,而图出来了、颜色也对,没人会怀疑它。所以这里钉三件事:

1. **每一种都能出 option**(APP 用 echarts,全量包里这些类型都在);
2. **每一种都能出中性行列表** —— 管理端(recharts)画不了的那几种要退成数据表,
   退不出表就是一片空白,而 APP 那边是好好的图,两边对不上却没有任何解释;
3. **形状不对时明确报错**,不画一张看着正常的假图(桑基的标签不是「从→到」、
   散点的横轴是文本、箱线每类点太少)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from cogni_agent.runtime_service.engine import chart_templates as ct
from cogni_agent.runtime_service.engine import chart_view as cv
from cogni_agent.runtime_service.engine import databuf
from cogni_agent.tool_center_service import builtin_tools as bi

ROOT = Path(__file__).resolve().parents[1]
RESULT_VIEW = ROOT / "web" / "src" / "pages" / "Tools" / "QuickBI" / "ResultView.tsx"

# 每种类型一份最小可用的 spec
SPECS = {
    "line": {"x": ["a", "b"], "series": [{"name": "v", "data": [1, 2]}]},
    "area": {"x": ["a", "b"], "series": [{"name": "v", "data": [1, 2]}]},
    "stacked_area": {"x": ["a", "b"], "series": [{"name": "A", "data": [1, 2]}]},
    "bar": {"x": ["a", "b"], "series": [{"name": "v", "data": [1, 2]}]},
    "stacked_bar": {"x": ["a", "b"], "series": [{"name": "A", "data": [1, 2]},
                                                {"name": "B", "data": [3, 4]}]},
    "horizontal_bar": {"x": ["甲", "乙", "丙"], "series": [{"name": "人次", "data": [10, 50, 30]}]},
    "pie": {"data": [{"name": "x", "value": 1}, {"name": "y", "value": 2}]},
    "donut": {"data": [{"name": "x", "value": 1}, {"name": "y", "value": 2}]},
    "radar": {"indicators": [{"name": "a", "max": 10}], "series": [{"name": "s", "data": [5]}]},
    "scatter": {"x": [1, 2, 3], "series": [{"name": "s", "data": [4, 5, 6]}]},
    "funnel": {"data": [{"name": "曝光", "value": 100}, {"name": "下单", "value": 20}]},
    "gauge": {"data": [{"name": "达成率", "value": 70.3}], "max": 100, "unit": "%"},
    "treemap": {"data": [{"name": "a", "value": 5}, {"name": "b", "value": 3}]},
    "sankey": {"links": [{"source": "携程", "target": "成人票", "value": 10}]},
    "heatmap": {"x": ["周一", "周二"], "y": ["上午", "下午"], "data": [[0, 0, 5], [1, 1, 9]]},
    "calendar": {"data": [["2026-01-01", 3], ["2026-01-02", 7]]},
    "boxplot": {"x": ["A"], "data": [[1, 2, 3, 4, 5]]},
    "sunburst": {"data": [{"name": "渠道", "children": [{"name": "携程", "value": 5}]}]},
    "themeriver": {"data": [["2026-01-01", 5, "A"], ["2026-01-02", 6, "A"]]},
    "combo": {"x": ["a", "b"], "series": [{"name": "票数", "data": [100, 200]}],
              "line_series": [{"name": "变化率", "data": [3.1, -2.0]}]},
}


def _opt(kind: str) -> dict:
    return ct.build_chart({**SPECS[kind], "type": kind, "title": "t"})


class TestEveryTypeBuilds:
    def test_all_declared_types_have_a_spec_here(self):
        """新增类型时别忘了在这里补一份样例 —— 否则它一行测试都没有。"""
        assert set(SPECS) == ct.CHART_TYPES

    @pytest.mark.parametrize("kind", sorted(SPECS))
    def test_builds_an_echarts_option(self, kind):
        opt = _opt(kind)
        assert opt is not None, f"{kind} 没能生成 option"
        assert opt.get("series"), f"{kind} 的 option 没有 series,APP 侧会直接跳过它"

    @pytest.mark.parametrize("kind", sorted(SPECS))
    def test_yields_a_neutral_table(self, kind):
        """管理端只认行列表。推不出表的类型在后台就是一片空白。"""
        card = cv.ensure_view({"title": "t", "type": kind, "option": _opt(kind)})
        table = card.get("table") or {}
        assert table.get("rows"), f"{kind} 推不出行列表"
        assert table.get("fields"), f"{kind} 的行列表没有列定义"

    @pytest.mark.parametrize("kind", sorted(SPECS))
    def test_ensure_view_is_idempotent(self, kind):
        once = cv.ensure_view({"title": "t", "type": kind, "option": _opt(kind)})
        assert cv.ensure_view(once) == once

    def test_unknown_type_is_refused(self):
        assert ct.build_chart({"type": "地球3d", "data": [{"name": "a", "value": 1}]}) is None


class TestTierSplit:
    def test_two_tiers_do_not_overlap(self):
        assert not (ct.TIER1_TYPES & ct.TIER2_TYPES)

    def test_tiers_cover_everything(self):
        assert ct.TIER1_TYPES | ct.TIER2_TYPES == ct.CHART_TYPES

    def test_echarts_only_list_matches_tier2(self):
        assert set(ct.ECHARTS_ONLY_TYPES) == ct.TIER2_TYPES

    def test_frontend_list_agrees_with_backend(self):
        """两边各维护一份清单迟早对不上,而对不上的表现是:两边都以为对方会渲染,谁都没画。"""
        src = RESULT_VIEW.read_text(encoding="utf-8")
        m = re.search(r"ECHARTS_ONLY = new Set\(\[([^\]]*)\]\)", src)
        assert m, "前端缺少 ECHARTS_ONLY 清单"
        front = {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}
        assert front == ct.TIER2_TYPES, f"前后端清单不一致:前端 {front} / 后端 {ct.TIER2_TYPES}"

    @pytest.mark.parametrize("kind", sorted(ct.TIER1_TYPES))
    def test_tier1_has_a_renderer_in_the_console(self, kind):
        """一档的承诺是「两端都真能画」。前端没有对应分支就画不出来。"""
        src = RESULT_VIEW.read_text(encoding="utf-8")
        # 堆叠/排名走的是共用的 Bar/Area 分支,按常量名认
        shared = {"stacked_bar": "STACKED", "stacked_area": "STACKED",
                  "horizontal_bar": "horizontal_bar", "bar": "BarChart", "line": "'line'"}
        needle = shared.get(kind, f"'{kind}'")
        assert needle in src, f"一档类型 {kind} 在管理端没有渲染分支"


class TestNumbersSurviveTheRoundTrip:
    def test_bar_values(self):
        t = cv.ensure_view({"title": "t", "type": "bar", "option": _opt("bar")})["table"]
        assert [r["v"] for r in t["rows"]] == [1, 2]

    def test_stacked_keeps_every_series(self):
        t = cv.ensure_view({"title": "t", "type": "stacked_bar", "option": _opt("stacked_bar")})["table"]
        metrics = [f["name"] for f in t["fields"] if f["role"] == "metric"]
        assert metrics == ["A", "B"]

    def test_horizontal_bar_is_sorted_biggest_first(self):
        """排名图的意义就在顺序上 —— 保持原始行序等于没排名。"""
        t = cv.ensure_view({"title": "t", "type": "horizontal_bar",
                            "option": _opt("horizontal_bar")})["table"]
        assert [r["名称"] for r in t["rows"]] == ["乙", "丙", "甲"]
        assert [r["人次"] for r in t["rows"]] == [50, 30, 10]

    def test_sankey_keeps_direction_in_the_label(self):
        t = cv.ensure_view({"title": "t", "type": "sankey", "option": _opt("sankey")})["table"]
        assert "携程" in t["rows"][0]["名称"] and "成人票" in t["rows"][0]["名称"]

    def test_boxplot_five_numbers(self):
        t = cv.ensure_view({"title": "t", "type": "boxplot", "option": _opt("boxplot")})["table"]
        assert [f["name"] for f in t["fields"]][1:] == ["最小值", "下四分位", "中位数", "上四分位", "最大值"]

    def test_heatmap_becomes_a_grid(self):
        t = cv.ensure_view({"title": "t", "type": "heatmap", "option": _opt("heatmap")})["table"]
        assert [f["name"] for f in t["fields"]][1:] == ["周一", "周二"]
        assert t["row_count"] == 2

    def test_sunburst_path_is_readable(self):
        t = cv.ensure_view({"title": "t", "type": "sunburst", "option": _opt("sunburst")})["table"]
        assert t["rows"][0]["名称"] == "渠道 / 携程"


class TestGaugeAndFunnelSemantics:
    def test_gauge_does_not_clamp_over_achievement(self):
        """超额完成不能被截断成刚好完成 —— 那是把好消息画没了。"""
        opt = ct.build_chart({"type": "gauge", "title": "t", "max": 100,
                              "data": [{"name": "达成率", "value": 137}]})
        assert opt["series"][0]["max"] >= 137

    def test_funnel_does_not_reorder_the_data_itself(self):
        """sort=descending 只是显示顺序,数据本身没递减是业务事实,不该被掩盖。"""
        opt = _opt("funnel")
        vals = [d["value"] for d in opt["series"][0]["data"]]
        assert vals == [100, 20]


class TestPalette:
    def test_backend_and_frontend_use_the_same_colors(self):
        """同一张图在 APP 和后台该是同一个颜色,否则两边对图说话时对不上。"""
        src = RESULT_VIEW.read_text(encoding="utf-8")
        m = re.search(r"const PALETTE = \[([^\]]*)\]", src)
        front = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
        assert front == ct.PALETTE

    def test_colors_are_assigned_in_fixed_order_not_generated(self):
        """第 9 个系列不是「再生成一个颜色」—— 生成出来的在色觉障碍下和已有的分不开。"""
        assert ct._color(0) == ct.PALETTE[0]
        assert ct._color(8) == ct.PALETTE[0]          # 回到第一个,而不是造新色
        assert len(set(ct.PALETTE)) == len(ct.PALETTE)

    def test_no_slot_reads_as_gray(self):
        """旧配色里的 #64748b 没有彩度,画出来像「没数据」;#eab308 太亮看不清。"""
        assert "#64748b" not in ct.PALETTE
        assert "#eab308" not in ct.PALETTE


class TestBuiltinChartShapes:
    @staticmethod
    def _draw(rows, **kw):
        databuf.clear("tc")
        return bi.builtin_chart(rows=rows, _task_id="tc", **kw)

    def test_chinese_aliases_are_accepted(self):
        rows = [{"渠道": "携程", "人次": 5}, {"渠道": "美团", "人次": 3}]
        for said in ("饼图", "漏斗", "排名", "环图", "热力图"):
            r = self._draw(rows, chart_type=said, label_column="渠道", value_columns="人次")
            assert "error" not in r or "不支持的图表类型" not in r["error"], f"{said} 没被认出来"

    def test_sankey_label_must_carry_direction(self):
        """标签不是「从→到」时明确报错。猜一个方向画出来,流向是错的而图看着完全正常。"""
        r = self._draw([{"渠道": "携程", "金额": 1}],
                       chart_type="sankey", label_column="渠道", value_columns="金额")
        assert "error" in r and "从哪到哪" in r["error"]

    def test_scatter_refuses_a_text_x_axis(self):
        r = self._draw([{"渠道": "携程", "金额": 1}],
                       chart_type="scatter", label_column="渠道", value_columns="金额")
        assert "error" in r and "数值" in r["error"]

    def test_boxplot_refuses_too_few_points(self):
        """点太少画箱线图没有意义,而画出来的箱子看着一样正经。"""
        r = self._draw([{"类": "A", "值": i} for i in range(3)],
                       chart_type="boxplot", label_column="类", value_columns="值")
        assert "error" in r and "5 个数据点" in r["error"]

    def test_boxplot_computes_the_five_numbers_server_side(self):
        rows = [{"类": "A", "值": i} for i in range(9)]
        r = self._draw(rows, chart_type="boxplot", label_column="类", value_columns="值")
        assert r.get("ok")
        row = databuf.charts("tc")[0]["table"]["rows"][0]
        assert row["中位数"] == 4 and row["最小值"] == 0 and row["最大值"] == 8

    def test_single_value_charts_say_which_column_they_used(self):
        r = self._draw([{"渠道": "a", "x": 1, "y": 2}],
                       chart_type="pie", label_column="渠道", value_columns="x,y")
        assert r.get("ok") and "只用第一列" in r["note"]

    def test_gauge_only_takes_one_row_and_says_so(self):
        r = self._draw([{"名": "A", "v": 1}, {"名": "B", "v": 2}],
                       chart_type="gauge", label_column="名", value_columns="v")
        assert r.get("ok") and "只画一个数" in r["note"]

    def test_every_chart_asks_for_the_conclusion_in_text(self):
        """后台现在和 APP 用同一个 ECharts 画,不再有「后台只能看表」的类型 ——
        但图在消息推送、导出里看不到,结论写进正文对**每一种**图都成立。"""
        rows = [{"日": "2026-01-%02d" % i, "v": i} for i in range(1, 6)]
        for kind in ("calendar", "pie"):
            r = self._draw(rows, chart_type=kind, label_column="日", value_columns="v")
            assert r.get("ok") and "不能只说「见图」" in r["note"], kind
            assert "只显示数据表" not in r["note"], "这句话已经不成立了"

    def test_unknown_type_lists_what_is_available(self):
        r = self._draw([{"a": "x", "b": 1}],
                       chart_type="三维地球", label_column="a", value_columns="b")
        assert "error" in r and "line" in r["error"] and "funnel" in r["error"]

    def test_too_many_points_asks_for_aggregation(self):
        rows = [{"d": str(i), "v": i} for i in range(80)]
        r = self._draw(rows, chart_type="bar", label_column="d", value_columns="v")
        assert "error" in r and "sys_table_query" in r["error"]


class TestModelGuidance:
    @staticmethod
    def _spec() -> dict:
        return bi.BUILTIN["sys_chart"]

    def test_enum_covers_every_type(self):
        enum = set(self._spec()["schema"]["properties"]["chart_type"]["enum"])
        assert enum == ct.CHART_TYPES

    def test_description_tells_the_model_how_to_choose(self):
        d = self._spec()["description"]
        for job in ("看走势", "比大小", "看构成", "看相关性", "看转化", "看流向", "看分布"):
            assert job in d, f"选图指引缺少「{job}」这一类"

    def test_description_warns_about_the_classic_mistakes(self):
        d = self._spec()["description"]
        assert "7~8" in d, "类别太多不该用饼图"
        assert "两张图" in d, "量级差很远的两个指标不该塞进一张图"
        assert "堆叠" in d, "分组柱与堆叠柱的分界要讲清楚"

    def test_a_single_number_is_not_a_chart(self):
        assert "不要画图" in self._spec()["description"]
