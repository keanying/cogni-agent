# -*- coding: utf-8 -*-
"""APP 图形化展示(现场截图):

1. 「环比人数环比 2.99」画成了仪表盘:0~100 的表盘上一根贴底的指针,刻度字、标题、数值叠成一团;
2. 两根柱子(今日 69 / 昨日 67)不标数,第二个日期标签被自动隐藏;
3. 总结表「结论」一整句不换行,截在屏幕外;
4. 图表一律 220px 高,仪表盘 / 排名图都挤。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from cogni_agent.runtime_service.engine import chart_templates as ct
from cogni_agent.runtime_service.engine import structured_output as so

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNI = pathlib.Path("/home/claude/work/uni/digital-brain-uni-v1.1.1/src")


# ── 仪表盘只给完成率;变化率改指标卡 ──

@pytest.mark.parametrize("name,val,card", [
    ("人数环比", 2.99, {"label": "人数环比", "value": "+2.99", "unit": "%", "trend": "up"}),
    ("核销同比", -23.87, {"label": "核销同比", "value": "-23.87", "unit": "%", "trend": "down"}),
    ("净利润", -5, {"label": "净利润", "value": "-5", "unit": "", "trend": "down"}),
])
def test_change_rate_gauge_becomes_card(name, val, card):
    assert ct.gauge_as_card({"type": "gauge", "data": [{"name": name, "value": val}]}) == card


def test_completion_rate_stays_gauge():
    spec = {"type": "gauge", "unit": "%", "data": [{"name": "核销完成率", "value": 76.6}]}
    assert ct.gauge_as_card(spec) is None
    s = ct.build_chart(spec)["series"][0]
    assert s["pointer"]["show"] is False, "小尺寸下指针只会压住数值"
    assert s["axisLabel"]["fontSize"] <= 10 and s["title"]["offsetCenter"] != s["detail"]["offsetCenter"]


def test_structured_output_moves_change_gauge_into_cards():
    ans = ('今日在园 69 人\n```json\n{"cards":[{"label":"在园","value":"69","unit":"人"}],'
           '"charts":[{"type":"gauge","title":"人数环比","data":[{"name":"人数环比","value":2.99}]},'
           '{"type":"bar","x":["今日","昨日"],"series":[{"name":"在园人数","data":[69,67]}]}]}\n```')
    out = so.parse(ans)
    assert [c["label"] for c in out.cards] == ["在园", "人数环比"]
    assert [c["type"] for c in out.charts] == ["bar"], "变化率不再画表盘"


def test_builtin_chart_refuses_change_rate_gauge():
    src = (ROOT / "cogni_agent" / "tool_center_service" / "builtin_tools.py").read_text(encoding="utf-8")
    assert "仪表盘只适合完成率 / 达成率" in src
    assert ct.gauge_as_card({"type": "gauge", "title": "日期 的 人数环比",
                             "data": [{"name": "2026-09-21", "value": 2.99}]}), "列名里的「环比」也算"


# ── 柱子少时标数、类目名全显示 ──

def test_few_bars_get_value_labels_and_all_category_names():
    o = ct.build_chart({"type": "bar", "x": ["2026-09-21 今日", "2026-09-20 昨日"],
                        "series": [{"name": "在园人数", "data": [69, 67]}]})
    assert o["series"][0]["label"]["show"] is True and o["grid"]["top"] >= 24
    lab = o["xAxis"]["axisLabel"]
    assert lab["interval"] == 0 and lab["overflow"] == "break" and lab["width"] > 0


def test_many_bars_stay_clean():
    x = [f"d{i}" for i in range(15)]
    o = ct.build_chart({"type": "bar", "x": x, "series": [{"name": "a", "data": list(range(15))}]})
    assert "label" not in o["series"][0] and "axisLabel" not in o["xAxis"]


def test_ranking_value_labels_have_room():
    o = ct.build_chart({"type": "horizontal_bar", "x": ["a", "b", "c"], "series": [{"name": "v", "data": [3, 2, 1]}]})
    assert o["series"][0]["label"]["position"] == "right" and o["grid"]["right"] >= 48


# ── APP(uni)侧 ──

def _chat():
    p = UNI / "pages" / "ai" / "chat.vue"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_app_chart_height_module():
    node = shutil.which("node")
    if not node or not (UNI / "utils" / "chartLayout.js").exists():
        pytest.skip("没有 node / uni 工程")
    r = subprocess.run([node, str(ROOT / "tests" / "web" / "app_chart_layout.check.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_app_chat_uses_height_resize_and_wrapping_tables():
    s = _chat()
    if not s:
        pytest.skip("uni 工程不在当前检出里")
    assert ":style=\"{ height: chartHeight(seg.option) + 'px' }\"" in s
    assert "new ResizeObserver(() => chart.resize())" in s and "chartObservers.get(seg)?.disconnect()" in s
    tbl = s[s.index(".md-body :deep(th),"):s.index(".md-body :deep(th:first-child),")]
    assert "white-space: normal" in tbl and "white-space: nowrap" not in tbl, "长文本单元格要换行"
    assert "padding: 6px 30px" not in s
    t = s[s.index(".md-body :deep(table) {"):]
    assert "width: 100%;" in t[:200], "按气泡宽度排版,列多才横滚"
