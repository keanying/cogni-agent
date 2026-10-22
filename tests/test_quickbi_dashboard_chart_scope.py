# -*- coding: utf-8 -*-
"""解读 · 仪表板「图表范围」:全选 / 勾选部分图表(对齐 Quick BI 智能解读的「请确认图表范围」)。

现场:Quick BI 自己的智能解读能全选看板上的图表(泰宁双景区 59 张)一键解读;
我们只能整块快照 —— 而快照只覆盖**当前页签**,其它页签的图表根本没进解读,也不报错。
"""
from __future__ import annotations

import asyncio

import pytest

from cogni_agent.tool_center_service.quickbi import capabilities as cap

SNAP = ("## 总评趋势\n\n| 日期 | 评论数 |\n|---|---|\n| 0920 | 284 |\n\n"
        "## 情绪占比4\n\n| 类型 | 占比 |\n|---|---|\n| 好评 | 0.12 |\n\n"
        "## 页脚说明\n\n| 备注 |\n|---|\n| 数据截至昨日 |\n")
CHARTS = [
    {"component_id": "c1", "name": "总评趋势", "chart_type": "indicator-trend", "cube_id": "k1",
     "dimensions": [{"name": "日期"}], "measures": [{"name": "评论数"}]},
    {"component_id": "c2", "name": "情绪占比", "chart_type": "pie", "cube_id": "k1",
     "dimensions": [{"name": "类型"}], "measures": [{"name": "占比"}]},
    # 另一个页签上的图:快照里没有,要按数据集补取
    {"component_id": "c3", "name": "年龄分布", "chart_type": "bar", "cube_id": "k2",
     "dimensions": [{"name": "年龄段"}], "measures": [{"name": "占比"}]},
    # 容器:不是图表,不算数也不补取
    {"component_id": "box", "name": "", "chart_type": "concat-container", "cube_id": None},
]


def _run(agen):
    async def go():
        return [e async for e in agen]
    return asyncio.run(go())


@pytest.fixture
def fake(monkeypatch):
    seen = {"qa": [], "md": None}

    async def snapshot(qbi, page_id, **kw):
        yield {"type": "snapshot", "markdown": SNAP}

    async def qa(qbi, *, question, cube_ids, locale="zh_CN", **kw):
        seen["qa"].append((tuple(cube_ids), question))
        if "FAIL" in question:
            yield {"type": "error", "message": "boom"}
            return
        yield {"type": "text", "section": "answer", "delta": "取数说明"}
        yield {"type": "chart", "chart": {"title": "年龄分布", "fields": [{"name": "年龄段"}, {"name": "占比"}],
                                          "rows": [{"年龄段": "18-30", "占比": 23}]}}

    async def interp(qbi, *, question, markdown, locale="zh_CN"):
        seen["md"] = markdown
        yield {"type": "text", "section": "answer", "delta": "解读完成"}

    monkeypatch.setattr(cap, "dashboard_snapshot", snapshot)
    monkeypatch.setattr(cap, "run_multi_cube_qa", qa)
    monkeypatch.setattr(cap, "run_interpretation", interp)
    return seen


def test_select_all_fetches_uncovered_cubes_once_by_question(fake):
    ev = _run(cap.run_dashboard_insight(None, question="整体解读", page_id="p", charts=CHARTS))
    md = fake["md"]
    assert "## 总评趋势" in md and "## 情绪占比4" in md, "快照里的图表直接用(名字带序号也对得上)"
    assert "18-30" in md, "快照外的数据按问题取回"
    assert md.index("## 总评趋势") < md.index("18-30"), "快照(看板本来的样子)在前,补取的排在后面"
    assert "## 页脚说明" in md, "全选时快照里对不上名字的段落也属于这块看板,照样带上"
    assert fake["qa"] == [(("k2",), "整体解读")], \
        "快照没覆盖的组件用到的数据集,按用户的问题一次多表取数(不再逐张图补取)"
    meta = next(e for e in ev if e.get("type") == "meta")
    assert meta["chart_scope"] == {"mode": "all", "selected": 3, "total": 3}, "容器组件不算图表"
    assert any(e.get("type") == "reasoning" and "取数说明" in e["delta"] for e in ev), "取数文字只进思考过程"


def test_snapshot_only_matches_the_dashboard(fake):
    """「只用仪表板快照」= 官方 / 在仪表板上点解读的取数方式:不补取其它页签。"""
    ev = _run(cap.run_dashboard_insight(None, question="整体解读", page_id="p", charts=CHARTS, fetch_missing=False))
    assert fake["qa"] == [] and "18-30" not in fake["md"] and "## 总评趋势" in fake["md"]
    assert any("只用仪表板快照" in str(e.get("message")) for e in ev)


def test_fetch_uses_raw_question_and_drops_unlabeled_tables(fake, monkeypatch):
    """取数用原问题(不带口径说明);只有一串数字、没有维值的表不进解读,并在结尾说明。"""
    seen = []

    async def qa(qbi, *, question, cube_ids, locale="zh_CN", **kw):
        seen.append(question)
        yield {"type": "chart", "chart": {"title": "TGI", "fields": [{"name": "TGI值"}, {"name": "占比"}],
                                          "rows": [{"TGI值": 244, "占比": "72.78%"}, {"TGI值": 190, "占比": "54.32%"}]}}
    monkeypatch.setattr(cap, "run_multi_cube_qa", qa)
    ev = _run(cap.run_dashboard_insight(None, question="日报\n口径要求:按 OTA、总览 顺序输出", page_id="p",
                                        charts=CHARTS, fetch_question="日报"))
    assert seen == ["日报"]
    assert "244" not in fake["md"]
    tail = "".join(e.get("delta", "") for e in ev if e.get("type") == "text")
    assert "未纳入解读:TGI" in tail
    assert cap.has_row_labels({"rows": [{"渠道": "携程", "金额": 1}, {"渠道": "美团", "金额": 2}],
                               "fields": [{"name": "渠道"}, {"name": "金额"}]})
    assert cap.has_row_labels({"rows": [{"v": 1}], "fields": [{"name": "v"}]}), "指标卡单行不需要维值"


def test_selected_charts_only(fake):
    _run(cap.run_dashboard_insight(None, question="q", page_id="p", charts=CHARTS, chart_ids=["c2"]))
    md = fake["md"]
    assert "情绪占比" in md and "总评趋势" not in md and "页脚说明" not in md and "18-30" not in md
    assert fake["qa"] == []


def test_fetch_failure_is_said_not_silently_dropped(fake):
    ev = _run(cap.run_dashboard_insight(None, question="FAIL", page_id="p", charts=CHARTS))
    tail = "".join(e.get("delta", "") for e in ev if e.get("type") == "text")
    assert "1 张图表不在快照里" in tail and "补取数据失败" in tail


def test_stale_selection_is_an_input_error(fake):
    with pytest.raises(cap.QuickBIError):
        _run(cap.run_dashboard_insight(None, question="q", page_id="p", charts=CHARTS, chart_ids=["gone"]))


def test_fit_sections_trims_rows_to_limit():
    big = "## T\n\n| a |\n|---|\n" + "\n".join(f"| {i} |" for i in range(1000))
    md, cap_rows = cap.fit_sections([big, "## U\n\n| b |\n|---|\n| 1 |"], 2000)
    assert len(md) <= 2000 and cap_rows and "## U" in md and "省略" in md


def test_chart_count_excludes_containers():
    from cogni_agent.tool_center_service.quickbi import service as svc

    class A:  # 只需要 asset_dict 读到的那些属性
        id = "a"; connection_id = "c"; kind = "dashboard"; name = "看板"; ref_id = "p"; scenic_id = None
        url = None; created_at = None; updated_at = None
        meta = {"charts": CHARTS, "cubes": []}
    d = svc.asset_dict(A())
    assert d["chart_count"] == 3 and d["component_count"] == 4
    assert cap.is_data_chart({"cube_id": "x", "chart_type": "cross-table"}), "交叉表是图表,别被 tab 误伤"


# ── 前端:操练台 / 固定问题 / 发布工具 都能确认图表范围 ──
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "src"


def test_chart_scope_logic_runs_against_the_real_module():
    tsx = next((c for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx") if c and Path(c).exists()), None)
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "chart_scope.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_backend_passes_chart_ids_and_uses_chart_scope():
    s = (ROOT / "cogni_agent" / "tool_center_service" / "quickbi" / "service.py").read_text(encoding="utf-8")
    assert "chart_ids=t.get(\"chart_ids\")" in s
    assert "return _dashboard_insight(qbi, question=question, t=t, charts=dashboard_charts" in s


def test_console_and_target_picker_offer_chart_scope():
    c = (WEB / "pages" / "Console.tsx").read_text(encoding="utf-8")
    assert "<ChartScopePicker compact assetId={activeAsset.id} value={qbiChartIds} onChange={setQbiChartIds} />" in c
    assert "scopeFields(qbiCap, asset.kind === 'dashboard', qbiChartIds, qbiLegacyPickOne)" in c
    assert "(qbiCap === 'insight' && board0 ? ONE_CLICK_INSIGHT : '')" in c, "不写问题 = 一键解读"
    sh = (WEB / "pages" / "Tools" / "QuickBI" / "shared.tsx").read_text(encoding="utf-8")
    assert "value.asset_id && (capability === 'insight' ||" in sh and "chart_ids: ids" in sh
    a = (WEB / "pages" / "Tools" / "QuickBI" / "Assets.tsx").read_text(encoding="utf-8")
    assert "const charts = dataCharts(summary.charts);" in a, "资产预览只列真图表"


# ── v3.56.0:报表问数 · 按组件整体提问(配置问题 / 操练台) ──

def test_qa_scope_logic_runs_against_the_real_module():
    tsx = next((c for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx") if c and Path(c).exists()), None)
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "qa_scope.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_console_and_question_editor_offer_component_scope():
    c = (WEB / "pages" / "Console.tsx").read_text(encoding="utf-8")
    assert "...scopeFields(qbiCap, asset.kind === 'dashboard', qbiChartIds, qbiLegacyPickOne)" in c
    assert "按组件整体</option>" not in c, "v3.57:组件范围只剩「哪些组件」,不再有两种跑法的开关"
    assert "(qbiCap === 'insight' || qbiCap === 'dashboard_qa')" in c
    assert "applyScope(f.target);" in c and "applyScope(item.target);" in c, "固定问题 / 会话回填范围"
    assert "useEffect(() => { setQbiChartIds(undefined); }, [qbiTarget]);" not in c, \
        "不能在 effect 里重置 —— 回填后会被紧接着清掉"
    sh = (WEB / "pages" / "Tools" / "QuickBI" / "shared.tsx").read_text(encoding="utf-8")
    assert "value.asset_id && (capability === 'insight' || capability === 'dashboard_qa')" in sh
    assert "<DatasetMultiPicker" in sh, "数据集按分类多选 / 全选 / 单选"


def test_service_routes_dashboard_qa_official_way():
    s = (ROOT / "cogni_agent" / "tool_center_service" / "quickbi" / "service.py").read_text(encoding="utf-8")
    i = s.index('if capability == "dashboard_qa":')
    seg = s[i:i + 1500]
    assert 'single_cube=t.get("qa_scope") == "dataset"' in seg and "charts=dashboard_charts" in seg
    assert "_dashboard_insight" not in seg, "v3.56 的「按组件整体 = 快照+解读」已撤掉"
    j = s.index('if capability == "dataset_qa":')
    assert "cap.run_multi_cube_qa(" in s[j:j + 800], "多选数据集 = 多表问数"


# ── v3.57.0:对齐官方 —— 仪表板问数 / 多选数据集问数 ──

def test_match_chart_by_question_picks_longest_meaningful_name():
    charts = CHARTS + [{"component_id": "q1", "name": "查询 1", "chart_type": "table", "cube_id": "k9"},
                       {"component_id": "c9", "name": "年龄分布明细", "chart_type": "table", "cube_id": "k3"}]
    assert cap.match_chart_by_question("看下年龄分布明细", charts)["component_id"] == "c9"
    assert cap.match_chart_by_question("查询 1 的数据", charts) is None, "「查询 1」这种默认名不算命中"
    assert cap.match_chart_by_question("昨日票务销售情况", charts) is None


def test_dashboard_candidate_cubes_follow_selected_components():
    assert cap.dashboard_candidate_cubes(CHARTS, None) == ["k1", "k2"]
    assert cap.dashboard_candidate_cubes(CHARTS, ["c3"]) == ["k2"]
    assert cap.dashboard_candidate_cubes([], None, ["x"]) == ["x"], "结构缓存没有图表时退回登记的数据集"


def test_multi_cube_events_parse_datalist_and_single():
    multi = {"conclusionText": "总结", "dataList": [
        {"chartType": "BAR", "metaType": [{"k": "a", "t": "dimension"}, {"k": "b", "t": "measure"}],
         "values": [{"row": ["x", 1]}], "logicSql": "S1"},
        {"chartType": "LINE", "metaType": [{"k": "c", "t": "dimension"}, {"k": "d", "t": "measure"}],
         "values": [{"row": ["y", 2]}]}]}
    ev = cap.multi_cube_events(multi, question="q")
    assert [e["type"] for e in ev].count("chart") == 2 and ev[-1]["delta"] == "总结"
    single = {"chartType": "BAR", "metaType": [{"k": "a", "t": "dimension"}], "values": [], "conclusionText": ""}
    ev2 = cap.multi_cube_events(single, question="q")
    assert ev2 and "没有查到" in ev2[-1]["delta"]


def test_dataset_category_logic_runs_against_the_real_module():
    tsx = next((c for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx") if c and Path(c).exists()), None)
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "dataset_category.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_assets_page_categorizes_datasets_by_enter():
    a = (WEB / "pages" / "Tools" / "QuickBI" / "Assets.tsx").read_text(encoding="utf-8")
    assert "api.qbiSetAssetsCategory(ids, category)" in a
    assert "if (e.key === 'Enter') { e.preventDefault(); applyCategory(catInput); }" in a, "输入回车即存"
    assert "groupByCategory(datasets)" in a and "按分类" in a
