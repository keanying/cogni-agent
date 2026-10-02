"""图表护栏:APP 聊天回复里「40 块的饼 / 多维度混在一根轴上 / 带竖滑块的长排名」。

来源:用户截图「人生阶段:按标签值看占比的分布」—— 客群画像接口一次返回收入、消费水平、
手机价位、人生阶段…多个维度,模型拿「标签值」做类别直接画,三张图全是混在一起的类别。
"""
import ast
import pathlib

from cogni_agent.runtime_service.engine import chart_templates as ct
from cogni_agent.runtime_service.engine import databuf
from cogni_agent.tool_center_service import builtin_tools as bt

DIMS = {"收入": ["低", "中", "高", "次高"], "消费水平": ["低", "中", "高"],
        "人生阶段": ["单身", "已婚未育", "已婚已育", "中老年", "学生", "新婚", "孕期", "退休"],
        "手机价位": ["1001-2000", "2001-3000", "7001-8000"]}


def _profile_rows():
    rows = []
    for d, vs in DIMS.items():
        share = [round(100 / len(vs), 2)] * len(vs)
        share[-1] = round(100 - sum(share[:-1]), 2)          # 每个维度各自合计 100
        rows += [{"标签名": d, "标签值": v, "占比": share[i], "TGI": 90 + i * 7} for i, v in enumerate(vs)]
    return rows


def _chart(monkeypatch, **kw):
    got = {}
    monkeypatch.setattr(databuf, "stash_chart", lambda t, c: got.setdefault("card", c) and 1)
    out = bt.builtin_chart(_task_id="t-guard", **kw)
    return out, got.get("card")


# ── 多维度混画 ──

def test_mixed_dimensions_are_refused_with_the_group_list(monkeypatch):
    out, card = _chart(monkeypatch, rows=_profile_rows(), chart_type="pie", title="占比分布",
                       label_column="标签值", value_columns="占比")
    assert card is None
    assert "标签名" in out["error"] and "人生阶段" in out["error"] and "filter" in out["error"]


def test_title_naming_one_group_draws_only_that_group(monkeypatch):
    out, card = _chart(monkeypatch, rows=_profile_rows(), chart_type="bar", title="人生阶段占比",
                       label_column="标签值", value_columns="占比")
    assert out["ok"] and out["points"] == 8
    assert card["option"]["xAxis"]["data"] == DIMS["人生阶段"]
    assert "已按标题只画「人生阶段」" in out["note"]


def test_explicit_filter(monkeypatch):
    out, card = _chart(monkeypatch, rows=_profile_rows(), chart_type="combo", title="收入 TGI",
                       label_column="标签值", value_columns="占比", line_columns="TGI",
                       filter="标签名=收入")
    assert out["ok"] and card["option"]["xAxis"]["data"] == DIMS["收入"]
    bad, _ = _chart(monkeypatch, rows=_profile_rows(), chart_type="bar", label_column="标签值",
                    value_columns="占比", filter="标签名=不存在")
    assert "可选" in bad["error"]


def test_ordinary_grouping_column_is_not_flagged(monkeypatch):
    # 渠道排名里带一列「类型」(OTA/直营)是正常数据,不能误伤
    rows = [{"渠道": f"c{i}", "类型": "OTA" if i % 2 else "直营", "票数": 100 + i * 37} for i in range(10)]
    out, card = _chart(monkeypatch, rows=rows, chart_type="bar", label_column="渠道", value_columns="票数")
    assert out.get("ok") and card


# ── 饼图类别过多 ──

def test_pie_folds_tail_into_other():
    data = [{"name": "a", "value": 50}, {"name": "b", "value": 20}] + \
           [{"name": f"c{i}", "value": 1} for i in range(12)]
    opt = ct.build_chart({"type": "pie", "title": "t", "data": data})
    names = [d["name"] for d in opt["series"][0]["data"]]
    assert len(names) == ct.PIE_MAX_SLICES and names[-1] == "其它"
    assert opt["series"][0]["data"][-1]["value"] == 8   # 12 个 1 里留 4 个,其余 8 个合并
    assert opt["legend"] == {"show": False}, "标签已带名称,不再重复一份图例"
    assert opt["series"][0]["labelLayout"] == {"hideOverlap": True}


def test_long_tail_pie_becomes_ranking_bar():
    data = [{"name": f"c{i}", "value": 40 - i} for i in range(40)]
    for kind in ("pie", "donut"):
        opt = ct.build_chart({"type": kind, "title": "t", "data": data})
        assert opt["series"][0]["type"] == "bar", kind
        assert len(opt["yAxis"]["data"]) == ct.HBAR_MAX_ROWS
        assert "dataZoom" not in opt


def test_sys_chart_reports_pie_fallback(monkeypatch):
    rows = [{"类别": f"c{i}", "值": 40 - i} for i in range(40)]
    out, card = _chart(monkeypatch, rows=rows, chart_type="pie", label_column="类别", value_columns="值")
    assert out["type"] == "horizontal_bar" and card["type"] == "horizontal_bar"
    assert "排名条形图" in out["note"] and "最大的 15 项" in out["note"]


def test_small_pie_untouched():
    data = [{"name": n, "value": v} for n, v in (("a", 3), ("b", 2), ("c", 1))]
    opt = ct.build_chart({"type": "pie", "title": "t", "data": data})
    assert [d["name"] for d in opt["series"][0]["data"]] == ["a", "b", "c"]


# ── 回答排版规范进了 system prompt ──

def test_answer_layout_policy_is_in_both_prompts():
    src = pathlib.Path("cogni_agent/runtime_service/engine/graphs.py").read_text(encoding="utf-8")
    assert src.count("_answer_layout_policy()") >= 2
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_answer_layout_policy")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "graphs", "exec"), ns)
    text = ns["_answer_layout_policy"]()
    for kw in ("第一句", "最多 3 个小节", "归因", "4 列", "如需"):
        assert kw in text, kw
