# -*- coding: utf-8 -*-
"""一张图必须两端都画得出来,历史记录也一样。

两个前端装的不是同一个图表库:APP(uni)装 echarts、吃 `option`,管理端操练台装
recharts、吃不了 option、只认中性行列表 `table`。少哪一种,对应那端就是
「回答里写着『雷达图已生成 ✅』,页面上什么都没有」——比没有图更糟,因为它看起来
像系统在说谎。

`sys_chart` 画的图一直两种都带;**模型自己写 ```json``` 结构化块画的图**只带 option,
那条路在管理端一直是空的,而且**已经落库的历史数据也是这样**。所以补全必须能作用在
读取时:历史接口取出来时顺手补一次,不迁移数据也能修好旧记录。
"""
from __future__ import annotations

from cogni_agent.runtime_service.engine import chart_templates, chart_view


def _built(spec: dict) -> dict:
    """走和线上同一套模板造 option —— 手搓的 option 迟早和模板漂开。"""
    opt = chart_templates.build_chart(spec)
    assert opt is not None, f"模板拒绝了这份 spec: {spec}"
    return {"title": spec["title"], "type": spec["type"], "option": opt}


class TestOptionToTable:
    """只有 option 的图(模型自己画的、历史数据)要能补出行列表。"""

    def test_bar_roundtrip_keeps_the_numbers(self):
        chart = _built({"title": "各渠道客流", "type": "bar", "x": ["携程", "美团", "直销"],
                        "series": [{"name": "人次", "data": [120, 90, 45]}]})
        out = chart_view.ensure_view(chart)
        t = out["table"]
        assert t["chart_type"] == "bar"
        assert t["row_count"] == 3
        assert [r["名称"] for r in t["rows"]] == ["携程", "美团", "直销"]
        assert [r["人次"] for r in t["rows"]] == [120, 90, 45]
        # option 原样保留 —— 补全不是改写
        assert out["option"] is chart["option"]

    def test_multi_series_line_becomes_multi_column(self):
        chart = _built({"title": "近三日", "type": "line", "x": ["周一", "周二", "周三"],
                        "series": [{"name": "本景区", "data": [1, 2, 3]},
                                   {"name": "同类", "data": [4, 5, 6]}]})
        t = chart_view.ensure_view(chart)["table"]
        metrics = [f["name"] for f in t["fields"] if f["role"] == "metric"]
        assert metrics == ["本景区", "同类"]
        assert t["rows"][1] == {"名称": "周二", "本景区": 2, "同类": 5}

    def test_pie_uses_name_value_pairs(self):
        chart = _built({"title": "年龄分布", "type": "pie",
                        "data": [{"name": "18-25", "value": 30}, {"name": "26-35", "value": 70}]})
        t = chart_view.ensure_view(chart)["table"]
        assert t["chart_type"] == "pie"
        assert t["rows"] == [{"名称": "18-25", "数值": 30}, {"名称": "26-35", "数值": 70}]

    def test_radar_axes_become_rows(self):
        """截图里那张图:5 个指标轴 × 本景区/同类景区两条线。"""
        chart = _built({
            "title": "景区 PFTSCA01018088 vs 同类景区", "type": "radar",
            "indicators": [{"name": n, "max": 100} for n in
                           ["客单价", "总客流", "票房收入", "口碑分", "核销票量"]],
            "series": [{"name": "本景区", "data": [100, 11, 70, 73, 42]},
                       {"name": "同类景区", "data": [78, 100, 100, 100, 100]}],
        })
        t = chart_view.ensure_view(chart)["table"]
        assert t["chart_type"] == "radar"
        assert t["row_count"] == 5
        assert t["rows"][0] == {"名称": "客单价", "本景区": 100, "同类景区": 78}
        assert t["rows"][3] == {"名称": "口碑分", "本景区": 73, "同类景区": 100}

    def test_series_type_wins_over_a_blank_outer_type(self):
        """历史数据里外层 type 常常是空的;option 才是渲染时真正生效的那份。"""
        chart = _built({"title": "x", "type": "line", "x": ["a", "b"],
                        "series": [{"name": "v", "data": [1, 2]}]})
        chart["type"] = ""
        assert chart_view.ensure_view(chart)["table"]["chart_type"] == "line"


class TestTableToOption:
    """反向:只有行列表时要能补出 option,否则 APP 那端空着。"""

    def test_bar_table_gets_an_echarts_option(self):
        chart = {"title": "客流", "type": "bar", "table": {
            "chart_type": "bar", "title": "客流",
            "fields": [{"name": "日期", "type": "string", "role": "dimension"},
                       {"name": "人次", "type": "number", "role": "metric"}],
            "rows": [{"日期": "9-18", "人次": 90}, {"日期": "9-19", "人次": 120}],
            "row_count": 2}}
        opt = chart_view.ensure_view(chart)["option"]
        assert opt["xAxis"]["data"] == ["9-18", "9-19"]
        assert opt["series"][0]["data"] == [90, 120]
        assert opt["series"][0]["type"] == "bar"

    def test_radar_axes_are_scaled_per_axis(self):
        """每根轴按自己的量程定 max:共用量程会让「票房两万 vs 口碑 3.6」里的
        口碑贴在圆心上,看图的人以为口碑是 0 —— 图出来了、颜色也对,没人会发现。"""
        chart = {"title": "对比", "type": "radar", "table": {
            "chart_type": "radar", "title": "对比",
            "fields": [{"name": "指标", "type": "string", "role": "dimension"},
                       {"name": "本景区", "type": "number", "role": "metric"}],
            "rows": [{"指标": "票房收入", "本景区": 21085}, {"指标": "口碑分", "本景区": 3.63}],
            "row_count": 2}}
        opt = chart_view.ensure_view(chart)["option"]
        inds = opt["radar"]["indicator"]
        assert inds[0]["max"] > 21085
        assert inds[1]["max"] < 10, "口碑轴不能跟着票房的量级走"


class TestIdempotentAndSafe:
    def test_complete_chart_is_returned_untouched(self):
        chart = _built({"title": "t", "type": "bar", "x": ["a"],
                        "series": [{"name": "v", "data": [1]}]})
        once = chart_view.ensure_view(chart)
        twice = chart_view.ensure_view(once)
        assert once == twice
        assert twice["table"] == once["table"]

    def test_unrecognisable_option_is_left_alone_not_guessed(self):
        """认不出来就原样放回 —— 宁可一端少一张图,也不能靠猜拼出数字对不上的图。"""
        chart = {"title": "x", "type": "sankey", "option": {"series": [{"type": "sankey"}]}}
        assert chart_view.ensure_view(chart) == chart

    def test_garbage_does_not_raise(self):
        for junk in (None, "", 123, [], {}, {"option": None}, {"table": {}}):
            chart_view.ensure_view(junk)

    def test_ensure_views_skips_bad_items_without_losing_good_ones(self):
        good = _built({"title": "g", "type": "bar", "x": ["a"], "series": [{"name": "v", "data": [1]}]})
        out = chart_view.ensure_views([None, good, "junk"])
        assert len(out) == 3
        assert "table" in out[1]


class TestNormalizeStructured:
    def test_object_shape(self):
        st = {"charts": [_built({"title": "t", "type": "bar", "x": ["a"],
                                 "series": [{"name": "v", "data": [1]}]})],
              "insight": {"level": "normal", "content": "x"}}
        out = chart_view.normalize_structured(st)
        assert "table" in out["charts"][0]
        assert out["insight"] == st["insight"], "只补图表,别的原样带走"

    def test_array_shape_from_early_data(self):
        block = {"charts": [_built({"title": "t", "type": "pie",
                                    "data": [{"name": "a", "value": 1}]})]}
        out = chart_view.normalize_structured([block])
        assert isinstance(out, list) and "table" in out[0]["charts"][0]

    def test_none_and_empty_pass_through(self):
        assert chart_view.normalize_structured(None) is None
        assert chart_view.normalize_structured({"cards": [{"label": "a"}]}) == {"cards": [{"label": "a"}]}

    def test_does_not_mutate_the_input(self):
        """历史记录取出来的是 ORM 上的 JSON 列,就地改会把补出来的形态写回库。"""
        chart = _built({"title": "t", "type": "bar", "x": ["a"], "series": [{"name": "v", "data": [1]}]})
        st = {"charts": [chart]}
        chart_view.normalize_structured(st)
        assert "table" not in chart, "必须返回新对象,不能改到传进来的那一份"


class TestWiredIntoTheReadPaths:
    """光有函数不够 —— 得真的接在那几条读取路径上。"""

    @staticmethod
    def _src(rel: str) -> str:
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")

    def test_history_turns_normalize(self):
        src = self._src("cogni_agent/control_plane_service/conversations.py")
        assert src.count("chart_view.normalize_structured") == 2, (
            "list_turns 和 list_turns_page 两条历史路径都要补(管理端 / APP 各走一条)"
        )

    def test_task_result_normalizes(self):
        src = self._src("cogni_agent/control_plane_service/queue.py")
        assert "chart_view.normalize_structured" in src, (
            "SSE 断线后前端走 get_task 补终态,那时实时 chart 事件已经错过了"
        )

    def test_prewarm_hit_normalizes(self):
        src = self._src("cogni_agent/gateway_service/routes_app.py")
        assert "chart_view.normalize_structured" in src, (
            "预热缓存直出不经过执行引擎,缓存行里的图同样可能只有一种形态"
        )

    def test_model_written_charts_normalize_at_write_time(self):
        src = self._src("cogni_agent/runtime_service/engine/structured_output.py")
        assert "chart_view.ensure_view" in src


class TestAppRendersLiveCharts:
    """APP 侧:服务端发的事件名是 chart(单数),只匹配 charts 等于一张都收不到。"""

    @staticmethod
    def _chat_vue() -> str:
        import pathlib
        p = pathlib.Path("/home/claude/work/uni/digital-brain-uni-v1.1.1/src/pages/ai/chat.vue")
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_event_name_matches_backend(self):
        src = self._chat_vue()
        if not src:
            return  # uni 工程不在当前检出里时跳过
        from cogni_agent.runtime_service import streaming
        assert streaming.EVENT_CHART == "chart"
        assert "structured|charts|chart" in src, "APP 必须认 chart 单数事件"

    def test_history_replay_reads_structured(self):
        src = self._chat_vue()
        if not src:
            return
        assert "item.structured" in src

    def test_no_debug_console_log_of_user_data(self):
        src = self._chat_vue()
        if not src:
            return
        assert "block=====" not in src, "调试日志别留在生产代码里"
