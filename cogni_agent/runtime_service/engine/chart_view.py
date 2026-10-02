"""一张图的两种形态互补:ECharts `option`(给 APP)和中性行列 `table`(给管理端)。

**为什么需要这个模块。** 两个前端装的不是同一个图表库:APP(uni)装 echarts,直接吃
option;管理端操练台装 recharts,吃不了 option、只认行列表。于是一张图必须两种形态
一起带,少哪一种,对应那端就是「回答里写着『雷达图已生成 ✅』,页面上什么都没有」——
比没有图更糟,因为它看起来像系统在说谎。

`sys_chart` 画的图从一开始就两种都带。但**模型自己写 ```json``` 结构化块**画的图
(structured_output._extract_charts)只带 option —— 那条路在管理端一直是空的,
而且**已经落库的历史数据也是这样**。所以补全必须能作用在读取时:
`ensure_view()` 是幂等的,历史记录取出来的时候顺手补一次,不需要数据迁移,
也不会把一张本来就完整的图改坏。

反向(只有 table 没有 option)同样支持 —— 哪天某条来源只产行列表,APP 也不该空着。

**不猜、不编。** 认不出来的 option 就原样放回去,宁可那一端少一张图,也不能靠猜
拼出一张数字对不上的图:一张画错的图会被当成结论直接用掉,而没人会回去核对。
"""
from __future__ import annotations

from typing import Any

__all__ = ["ensure_view", "ensure_views", "normalize_structured"]

# 行列表里维度列的兜底名。option 里没有维度列名这个概念(x 轴就是 x 轴),
# 补出来的表得有个列名,recharts 才知道拿哪一列当横轴。
_DIM = "名称"


def _num(v: Any) -> Any:
    return v if isinstance(v, (int, float)) else v


def _fields(dim: str, metrics: list[str]) -> list[dict]:
    return ([{"name": str(dim), "type": "string", "role": "dimension"}]
            + [{"name": str(m), "type": "number", "role": "metric"} for m in metrics])


def _table_from_axis(option: dict, title: str, kind: str) -> dict | None:
    """line / bar:xAxis.data 是维度,每条 series 是一列指标。"""
    axis = option.get("xAxis") or {}
    if isinstance(axis, list):
        axis = axis[0] if axis else {}
    labels = list((axis or {}).get("data") or [])
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not labels or not series:
        return None
    names = [str(s.get("name") or f"系列{i + 1}") for i, s in enumerate(series)]
    rows = []
    for i, lab in enumerate(labels):
        row: dict[str, Any] = {_DIM: lab}
        for name, s in zip(names, series):
            data = s.get("data") or []
            row[name] = _num(data[i]) if i < len(data) else None
        rows.append(row)
    return {"chart_type": kind, "title": title, "fields": _fields(_DIM, names),
            "rows": rows, "row_count": len(rows)}


def _table_from_pie(option: dict, title: str) -> dict | None:
    """pie:series[0].data 是 [{name, value}],维度是 name,只有一列指标。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    data = [d for d in (series[0].get("data") or []) if isinstance(d, dict)]
    if not data:
        return None
    rows = [{_DIM: d.get("name"), "数值": _num(d.get("value"))} for d in data]
    return {"chart_type": "pie", "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": len(rows)}


def _table_from_radar(option: dict, title: str) -> dict | None:
    """radar:radar.indicator 是维度(每个轴一行),series[0].data 每项是一条折线。"""
    radar = option.get("radar") or {}
    if isinstance(radar, list):
        radar = radar[0] if radar else {}
    axes = [str(i.get("name") or "") for i in ((radar or {}).get("indicator") or [])
            if isinstance(i, dict)]
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not axes or not series:
        return None
    lines = [d for d in (series[0].get("data") or []) if isinstance(d, dict)]
    if not lines:
        return None
    names = [str(d.get("name") or f"系列{i + 1}") for i, d in enumerate(lines)]
    rows = []
    for i, axis_name in enumerate(axes):
        row: dict[str, Any] = {_DIM: axis_name}
        for name, d in zip(names, lines):
            vals = d.get("value") or []
            row[name] = _num(vals[i]) if i < len(vals) else None
        rows.append(row)
    return {"chart_type": "radar", "title": title, "fields": _fields(_DIM, names),
            "rows": rows, "row_count": len(rows)}


def _table_from_hbar(option: dict, title: str) -> dict | None:
    """排名图(横向条):类别在 **y 轴**,数值在 x 轴 —— 和竖着的柱状图正好反过来。

    不单独认这一种的话,`_table_from_axis` 会去读 xAxis.data(横向图里那是数值轴,
    根本没有 data),拿到空列表直接判定「画不出行列表」,于是管理端那边又是一片空白。
    """
    ya = option.get("yAxis") or {}
    if isinstance(ya, list):
        ya = ya[0] if ya else {}
    labels = list((ya or {}).get("data") or [])
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not labels or not series:
        return None
    names = [str(s_.get("name") or f"系列{i + 1}") for i, s_ in enumerate(series)]
    rows = []
    for i, lab in enumerate(labels):
        row: dict = {_DIM: lab}
        for nm, s_ in zip(names, series):
            d = s_.get("data") or []
            row[nm] = _num(d[i]) if i < len(d) else None
        rows.append(row)
    # 排名图的 option 里是自下而上倒序存的(ECharts y 轴方向),给人看的表要从大到小
    rows.reverse()
    return {"chart_type": "horizontal_bar", "title": title, "fields": _fields(_DIM, names),
            "rows": rows, "row_count": len(rows)}


def _table_from_pie_like(option: dict, title: str, kind: str, series_type: str) -> dict | None:
    """饼/环/树图/漏斗:series[0].data 是 [{name, value}]。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    data = [d for d in (series[0].get("data") or []) if isinstance(d, dict)]
    if not data:
        return None
    rows = [{_DIM: d.get("name"), "数值": _num(d.get("value"))} for d in data]
    return {"chart_type": kind, "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": len(rows)}


def _table_from_scatter(option: dict, title: str) -> dict | None:
    """散点:每个点是 [x, y]。维度列放 x —— 它本来就是数值,但在行列表里它是「每一行是谁」。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    pts = []
    names = []
    for s_ in series:
        nm = str(s_.get("name") or "y")
        names.append(nm)
        for pair in (s_.get("data") or []):
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                pts.append((pair[0], nm, pair[1]))
    if not pts:
        return None
    # 按 x 归并成一行多列,x 相同的点落在同一行
    xs: list = []
    by_x: dict = {}
    for x, nm, y in pts:
        if x not in by_x:
            by_x[x] = {}
            xs.append(x)
        by_x[x][nm] = _num(y)
    rows = [{_DIM: x, **by_x[x]} for x in xs]
    return {"chart_type": "scatter", "title": title, "fields": _fields(_DIM, names),
            "rows": rows, "row_count": len(rows)}


def _table_from_gauge(option: dict, title: str) -> dict | None:
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    data = [d for d in (series[0].get("data") or []) if isinstance(d, dict)]
    if not data:
        return None
    rows = [{_DIM: str(data[0].get("name") or title or "值"), "数值": _num(data[0].get("value"))}]
    return {"chart_type": "gauge", "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": 1}


def _table_from_sankey(option: dict, title: str) -> dict | None:
    """桑基:一条流一行(从 → 到 → 数值)。管理端画不出流向图,但这张表读得懂。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    links = [l for l in (series[0].get("links") or []) if isinstance(l, dict)]
    if not links:
        return None
    rows = [{_DIM: f"{l.get('source')} → {l.get('target')}", "数值": _num(l.get("value"))}
            for l in links]
    return {"chart_type": "sankey", "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": len(rows)}


def _table_from_heatmap(option: dict, title: str) -> dict | None:
    """热力网格 → 行是 y 类别、列是 x 类别的二维表。"""
    xa = option.get("xAxis") or {}
    ya = option.get("yAxis") or {}
    xs = [str(v) for v in ((xa if isinstance(xa, dict) else {}).get("data") or [])]
    ys = [str(v) for v in ((ya if isinstance(ya, dict) else {}).get("data") or [])]
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not xs or not ys or not series:
        return None
    grid = {}
    for d in (series[0].get("data") or []):
        if isinstance(d, (list, tuple)) and len(d) >= 3:
            try:
                grid[(int(d[0]), int(d[1]))] = _num(d[2])
            except (TypeError, ValueError):
                continue
    if not grid:
        return None
    rows = [{_DIM: ys[yi], **{xs[xi]: grid.get((xi, yi)) for xi in range(len(xs))}}
            for yi in range(len(ys))]
    return {"chart_type": "heatmap", "title": title, "fields": _fields(_DIM, xs),
            "rows": rows, "row_count": len(rows)}


def _table_from_calendar(option: dict, title: str) -> dict | None:
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    rows = [{_DIM: str(d[0]), "数值": _num(d[1])}
            for d in (series[0].get("data") or [])
            if isinstance(d, (list, tuple)) and len(d) >= 2]
    if not rows:
        return None
    return {"chart_type": "calendar", "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": len(rows)}


def _table_from_boxplot(option: dict, title: str) -> dict | None:
    xa = option.get("xAxis") or {}
    cats = [str(v) for v in ((xa if isinstance(xa, dict) else {}).get("data") or [])]
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not cats or not series:
        return None
    cols = ["最小值", "下四分位", "中位数", "上四分位", "最大值"]
    rows = []
    for i, box in enumerate(series[0].get("data") or []):
        if not isinstance(box, (list, tuple)) or len(box) < 5 or i >= len(cats):
            continue
        rows.append({_DIM: cats[i], **{cols[j]: _num(box[j]) for j in range(5)}})
    if not rows:
        return None
    return {"chart_type": "boxplot", "title": title, "fields": _fields(_DIM, cols),
            "rows": rows, "row_count": len(rows)}


def _table_from_sunburst(option: dict, title: str) -> dict | None:
    """旭日 → 每个叶子一行,维度写成「一级 / 二级 / 叶子」的路径。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    rows: list[dict] = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        here = path + [str(node.get("name") or "")]
        kids = node.get("children") or []
        if kids:
            for k in kids:
                walk(k, here)
        else:
            rows.append({_DIM: " / ".join(here), "数值": _num(node.get("value"))})

    for n in (series[0].get("data") or []):
        walk(n, [])
    if not rows:
        return None
    return {"chart_type": "sunburst", "title": title, "fields": _fields(_DIM, ["数值"]),
            "rows": rows, "row_count": len(rows)}


def _table_from_themeriver(option: dict, title: str) -> dict | None:
    """主题河流 → 行是时间、列是系列。"""
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    if not series:
        return None
    times: list[str] = []
    names: list[str] = []
    cell: dict = {}
    for d in (series[0].get("data") or []):
        if not isinstance(d, (list, tuple)) or len(d) < 3:
            continue
        t, v, nm = str(d[0]), _num(d[1]), str(d[2])
        if t not in cell:
            cell[t] = {}
            times.append(t)
        if nm not in names:
            names.append(nm)
        cell[t][nm] = v
    if not times:
        return None
    rows = [{_DIM: t, **{n: cell[t].get(n) for n in names}} for t in times]
    return {"chart_type": "themeriver", "title": title, "fields": _fields(_DIM, names),
            "rows": rows, "row_count": len(rows)}


def _table_from_option(option: dict, title: str, kind: str) -> dict | None:
    if not isinstance(option, dict):
        return None
    # 图形类型优先信 series[0].type —— 外层 type 可能是空的(历史数据里常见),
    # 而 option 是渲染时真正生效的那一份,以它为准两端才不会画成两种图。
    series = [s for s in (option.get("series") or []) if isinstance(s, dict)]
    real = str((series[0].get("type") if series else "") or kind or "").lower()
    outer = str(kind or "").lower()

    # 派生类型的 option 里 series.type 是它的基础类型(堆叠柱还是 bar、环图还是 pie),
    # 所以先认**外层声明的类型**,认不出来再按 option 的形状兜底。
    if outer == "heatmap" and option.get("calendar") is None:
        return _table_from_heatmap(option, title)
    if outer == "calendar" or option.get("calendar") is not None:
        return _table_from_calendar(option, title)
    if outer in {"treemap", "funnel", "donut"} or real in {"treemap", "funnel"}:
        return _table_from_pie_like(option, title, outer or real, real)
    if outer == "gauge" or real == "gauge":
        return _table_from_gauge(option, title)
    if outer == "sankey" or real == "sankey":
        return _table_from_sankey(option, title)
    if outer == "boxplot" or real == "boxplot":
        return _table_from_boxplot(option, title)
    if outer == "sunburst" or real == "sunburst":
        return _table_from_sunburst(option, title)
    if outer == "themeriver" or real == "themeriver":
        return _table_from_themeriver(option, title)
    if outer == "scatter" or real == "scatter":
        return _table_from_scatter(option, title)
    if outer == "horizontal_bar" or (isinstance(option.get("yAxis"), dict)
                                     and (option["yAxis"] or {}).get("type") == "category"):
        return _table_from_hbar(option, title)

    if option.get("radar") is not None or real == "radar":
        return _table_from_radar(option, title)
    if real == "pie":
        return _table_from_pie(option, title)
    if real in {"line", "bar"} or option.get("xAxis") is not None:
        # 堆叠/面积/排名都是 line/bar 的变体,行列表形状一样,只是 chart_type 要保留
        # 外层那个 —— 管理端据它决定横着画还是摞起来画。
        base = outer if outer in {"area", "stacked_area", "stacked_bar",
                                  "horizontal_bar", "line", "bar", "combo"} else \
            (real if real in {"line", "bar"} else "bar")
        return _table_from_axis(option, title, base)
    return None


def _option_from_table(table: dict, title: str, kind: str) -> dict | None:
    """只有行列表时反推 option。走和在线渲染同一套模板,不手搓 option ——
    手搓的那份迟早和模板漂开,两端就会画出不一样的图。"""
    from cogni_agent.runtime_service.engine import chart_templates

    if not isinstance(table, dict):
        return None
    rows = table.get("rows") or []
    fields = table.get("fields") or []
    if not rows or not fields:
        return None
    dim = next((str(f.get("name")) for f in fields
                if isinstance(f, dict) and f.get("role") == "dimension"), None)
    metrics = [str(f.get("name")) for f in fields
               if isinstance(f, dict) and f.get("role") != "dimension"]
    if dim is None or not metrics:
        return None
    labels = [r.get(dim) for r in rows if isinstance(r, dict)]
    ctype = str(table.get("chart_type") or kind or "bar").lower()
    if ctype not in chart_templates.CHART_TYPES:
        ctype = "bar"
    spec: dict[str, Any] = {"title": title, "type": ctype}
    if ctype == "pie":
        spec["data"] = [{"name": labels[i], "value": rows[i].get(metrics[0])}
                        for i in range(len(labels))]
    elif ctype == "radar":
        series = [{"name": m, "data": [r.get(m) for r in rows]} for m in metrics]
        # 每根轴按自己的量程定 max:共用量程的话「票房两万 vs 口碑 3.6」会让口碑贴在
        # 圆心上,看图的人以为口碑是 0 —— 而图出来了、颜色也对,没有任何地方提示它误导人。
        inds = []
        for i in range(len(labels)):
            vals = [s["data"][i] for s in series
                    if i < len(s["data"]) and isinstance(s["data"][i], (int, float))]
            top = max(vals) if vals else 0
            inds.append({"name": labels[i], "max": round(top * 1.15, 4) if top > 0 else 1})
        spec["indicators"] = inds
        spec["series"] = series
    else:
        spec["x"] = labels
        spec["series"] = [{"name": m, "data": [r.get(m) for r in rows]} for m in metrics]
    return chart_templates.build_chart(spec)


def ensure_view(chart: Any) -> Any:
    """补全一张图的另一种形态。幂等:两种都有就原样返回,认不出来也原样返回。"""
    if not isinstance(chart, dict):
        return chart
    title = str(chart.get("title") or "")
    kind = str(chart.get("type") or "")
    has_table = isinstance(chart.get("table"), dict) and (chart["table"].get("rows") or [])
    option = chart.get("option")
    has_option = isinstance(option, dict) and (option.get("series") or option.get("xAxis"))

    if has_option and not has_table:
        table = _table_from_option(option, title, kind)
        if table:
            return {**chart, "table": table}
        return chart
    if has_table and not has_option:
        opt = _option_from_table(chart["table"], title, kind)
        if opt:
            return {**chart, "option": opt}
        return chart
    return chart


def ensure_views(charts: Any) -> Any:
    if not isinstance(charts, list):
        return charts
    return [ensure_view(c) for c in charts]


def normalize_structured(structured: Any) -> Any:
    """把 result.structured 里的图表补全成两端都能渲染的形态。

    对象和数组两种形态都吃(早期数据是数组)。历史接口在**读取时**调用它,
    于是已经落库的旧记录不用迁移也能在两端正常显示 —— 迁移改的是历史事实,
    而这里改的只是「怎么把同一份事实讲给两个前端听」,后者显然更该是读时的事。
    """
    if isinstance(structured, list):
        return [normalize_structured(b) for b in structured]
    if not isinstance(structured, dict):
        return structured
    charts = structured.get("charts")
    if not isinstance(charts, list) or not charts:
        return structured
    return {**structured, "charts": ensure_views(charts)}
