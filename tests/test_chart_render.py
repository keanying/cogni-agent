# -*- coding: utf-8 -*-
"""图表:补齐的 ECharts 能力 + 所有类型在真 ECharts 里画得出来。

补齐的几项都来自我们自己的数据形态(数据指标接口 30 个):
- combo 双轴:接口几乎都是「量 + 率」成对返回(票数 + 变化率、销售额 + 环比、占比 + TGI),
  同一根轴上 31% 在十万量级旁边就是贴着 0 的一条线;
- 极值 / 均值 / 基准线:「峰值在几点」「高于全国基准(TGI=100)吗」;
- 类别多时自动缩放;顶层调色板(饼图此前落到 ECharts 默认色);aria 读屏描述。
管理端也改用同一份 ECharts 画(EChartsView),不再是 recharts 另画一遍。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cogni_agent.runtime_service.engine import chart_templates as ct
from cogni_agent.runtime_service.engine import databuf
from cogni_agent.tool_center_service.builtin_tools import builtin_chart

ROOT = Path(__file__).resolve().parent.parent
ECHARTS = ROOT / "web" / "src" / "vendor" / "echarts-6.1.0.min.js"

DAY = [{"日期": f"2026-09-{d:02d}", "销售票数": 1000 + d * 37, "票数变化率": round((d % 7 - 3) * 4.2, 1),
        "核销": 800 + d * 20} for d in range(1, 29)]


def _draw(task: str, **kw) -> tuple[dict, dict]:
    r = builtin_chart(_task_id=task, **kw)
    assert r.get("ok"), r
    return r, databuf.charts(task)[-1]


def test_combo_puts_rates_on_a_right_axis():
    r, card = _draw("t-combo", rows=DAY, chart_type="combo", label_column="日期",
                    value_columns="销售票数", line_columns="票数变化率", baseline=0, baseline_label="零线")
    o = card["option"]
    assert isinstance(o["yAxis"], list) and len(o["yAxis"]) == 2
    kinds = [(s["type"], s.get("yAxisIndex")) for s in o["series"]]
    assert kinds == [("bar", None), ("line", 1)]
    base = o["series"][1]["markLine"]["data"][-1]
    assert base["yAxis"] == 0.0, "combo 的基准线画在率那根轴上"
    assert r["series"] == ["销售票数", "票数变化率"]
    assert card["table"]["chart_type"] == "combo" and [f["name"] for f in card["table"]["fields"]] == [
        "日期", "销售票数", "票数变化率"]


def test_combo_without_line_columns_is_refused_with_a_reason():
    r = builtin_chart(_task_id="t-c2", rows=DAY, chart_type="双轴", label_column="日期", value_columns="销售票数")
    assert "line_columns" in r.get("error", "")


def test_marks_and_baseline():
    _, card = _draw("t-marks", rows=DAY[:10], chart_type="line", label_column="日期",
                    value_columns="销售票数,核销", marks="峰值,最低,均值", baseline=1200, baseline_label="目标")
    s0 = card["option"]["series"][0]
    assert [p["type"] for p in s0["markPoint"]["data"]] == ["max", "min"]
    ml = [d.get("type") or d.get("name") for d in s0["markLine"]["data"]]
    assert ml == ["average", "目标"]
    assert card["option"]["grid"]["top"] >= 40, "极值气泡在点上方,顶部要留出位置"
    bad = builtin_chart(_task_id="t-m2", rows=DAY, chart_type="bar", label_column="日期",
                        value_columns="销售票数", baseline="高一点")
    assert "baseline" in bad.get("error", "")


def test_long_axes_get_zoom_short_ones_do_not():
    _, long_ = _draw("t-z1", rows=DAY, chart_type="bar", label_column="日期", value_columns="销售票数")
    _, short = _draw("t-z2", rows=DAY[:10], chart_type="bar", label_column="日期", value_columns="销售票数")
    assert [z["type"] for z in long_["option"]["dataZoom"]] == ["inside", "slider"]
    assert "dataZoom" not in short["option"]
    rows = [{"城市": f"城市{i}", "占比": i} for i in range(26)]
    _, hb = _draw("t-z3", rows=rows, chart_type="horizontal_bar", label_column="城市", value_columns="占比")
    # 排名图只留最大的 15 条,不再挂竖滑块(手机上 40 条要拖着看,读的人只看前几名)
    assert len(hb["option"]["yAxis"]["data"]) == ct.HBAR_MAX_ROWS
    assert hb["option"]["yAxis"]["data"][-1] == "城市25", "最大的在最上面"
    assert "dataZoom" not in hb["option"]


def test_every_chart_has_our_palette_and_aria():
    for k in sorted(ct.CHART_TYPES):
        spec = {"type": k, "title": "t", "x": ["a", "b"], "series": [{"name": "s", "data": [1, 2]}],
                "line_series": [{"name": "r", "data": [1, 2]}],
                "data": [{"name": "a", "value": 1}], "indicators": [{"name": "a", "max": 2}, {"name": "b", "max": 2}],
                "links": [{"source": "a", "target": "b", "value": 1}]}
        o = ct.build_chart(spec)
        if not o:
            continue
        assert o["aria"] == {"enabled": True}, k
        assert o["color"] == ct.PALETTE, k


def _node_and_playwright():
    node = shutil.which("node")
    for cand in ("/home/claude/.npm-global/lib/node_modules/playwright",):
        if node and Path(cand).exists():
            return node, cand
    return None, None


CASES = {
    "line": dict(rows=DAY, label_column="日期", value_columns="销售票数,核销", marks="max,min,average"),
    "area": dict(rows=DAY[:10], label_column="日期", value_columns="销售票数"),
    "stacked_area": dict(rows=DAY[:10], label_column="日期", value_columns="销售票数,核销"),
    "bar": dict(rows=DAY[:12], label_column="日期", value_columns="销售票数", baseline=1200),
    "stacked_bar": dict(rows=DAY[:8], label_column="日期", value_columns="销售票数,核销"),
    "horizontal_bar": dict(rows=[{"城市": f"城市{i}", "占比": i + 1} for i in range(26)],
                           label_column="城市", value_columns="占比", marks="average"),
    "combo": dict(rows=DAY, label_column="日期", value_columns="销售票数", line_columns="票数变化率", marks="max"),
    "pie": dict(rows=[{"渠道": c, "v": v} for c, v in [("携程", 40), ("抖音", 25), ("美团", 20)]],
                label_column="渠道", value_columns="v"),
    "donut": dict(rows=[{"渠道": "携程", "v": 40}, {"渠道": "抖音", "v": 25}], label_column="渠道", value_columns="v"),
    "radar": dict(rows=[{"指标": k, "本景区": v, "行业": v * 0.8} for k, v in
                        [("票房", 21085), ("客单价", 88), ("口碑", 3.79)]], label_column="指标", value_columns="本景区,行业"),
    "scatter": dict(rows=[{"x": i, "y": i * 1.3} for i in range(20)], label_column="x", value_columns="y"),
    "funnel": dict(rows=[{"步骤": k, "人数": v} for k, v in [("浏览", 1000), ("下单", 150), ("支付", 120)]],
                   label_column="步骤", value_columns="人数"),
    "gauge": dict(rows=[{"名": "达成率", "v": 137}], label_column="名", value_columns="v"),
    "treemap": dict(rows=[{"n": f"类{i}", "v": i + 1} for i in range(12)], label_column="n", value_columns="v"),
    "sankey": dict(rows=[{"流向": "携程→成人票", "v": 30}, {"流向": "抖音→成人票", "v": 20}],
                   label_column="流向", value_columns="v"),
    "heatmap": dict(rows=[{"时段": f"{h}点", "周一": h, "周二": h * 2} for h in range(8, 18)],
                    label_column="时段", value_columns="周一,周二"),
    "calendar": dict(rows=DAY, label_column="日期", value_columns="销售票数"),
    "boxplot": dict(rows=[{"渠道": c, "v": i * 3 + (7 if c == "抖音" else 0)} for c in ("携程", "抖音") for i in range(8)],
                    label_column="渠道", value_columns="v"),
    "sunburst": dict(rows=[{"p": "华东/上海", "v": 10}, {"p": "华东/杭州", "v": 6}, {"p": "华南/广州", "v": 8}],
                     label_column="p", value_columns="v"),
    "themeriver": dict(rows=DAY[:10], label_column="日期", value_columns="销售票数,核销"),
}


def test_all_types_render_in_real_echarts(tmp_path):
    """用仓库里与 APP 同一份 ECharts 真画一遍:不报错、不空白、读屏描述是中文。"""
    node, pw = _node_and_playwright()
    if not node or not ECHARTS.exists():
        pytest.skip("没有 node + playwright")
    assert set(CASES) == ct.CHART_TYPES, "新增了图表类型就要在这里加一个用例"
    opts = {}
    for k, kw in CASES.items():
        _, card = _draw(f"t-render-{k}", chart_type=k, **kw)
        opts[k] = card["option"]
    f = tmp_path / "options.json"
    f.write_text(json.dumps(opts, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([node, str(ROOT / "tests" / "web" / "render_echarts.cjs"), str(f), str(ECHARTS)],
                       capture_output=True, text=True, timeout=120, env={**os.environ, "PLAYWRIGHT_MODULE": pw})
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert "fatal" not in res, res
    assert res["errors"] == [] and res["console"] == [], res
    assert res["blank"] == [], f"这些图画出来是空白:{res['blank']}"
    assert all("图表" in a for a in res["aria"].values()), res["aria"]


def test_console_renders_the_same_option_with_echarts():
    web = ROOT / "web" / "src"
    card = (web / "pages" / "Tools" / "QuickBI" / "ResultView.tsx").read_text(encoding="utf-8")
    console = (web / "pages" / "Console.tsx").read_text(encoding="utf-8")
    view = (web / "components" / "charts" / "EChartsView.tsx").read_text(encoding="utf-8")
    assert "<EChartsView option={chart.option}" in card and "onFail={() => setEchartsFailed(true)}" in card
    assert "const useECharts = !!chart.option && !echartsFailed;" in card, "有 option 就要走 ECharts"
    assert "mode === 'chart' && plottable && useECharts ?" in card
    assert "option: c.option && typeof c.option === 'object' ? c.option : undefined" in console
    assert "echarts-6.1.0.min.js?url" in view and "locale: 'ZH'" in view
    assert ECHARTS.exists()
