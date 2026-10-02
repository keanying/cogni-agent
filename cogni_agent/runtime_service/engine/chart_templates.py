"""ECharts 图表模板(04 智能问数方案 §4.3「图表模板」落地)。

预定义 line/bar/pie/radar 四种图表骨架,LLM 结构化输出块只填
系列数据/坐标轴/指标等业务字段,由这里填充成完整的 ECharts option spec;
APP 端拿到 option 直接渲染,服务端不产图、不传图片。

为什么用模板而不是让 LLM 自由生成完整 option:
- 自由生成容易出非法 JSON / 缺字段 / 类型错乱,前端渲染直接崩;
- 固定骨架 + 字段白名单,LLM 只填业务数据,option 结构 100% 稳定;
- 后续新增图表类型只需在这里加一个模板 + 白名单,skill 提示词零改动。

为什么 option 里不放 title:
- 图表标题只由 SSE chart 事件的外层 title 字段承载(见 streaming.py),APP 用它渲染卡片头;
- 早期版本在 option 里也写了一份 ECharts 自带 title,导致同一句话被渲染两次、
  卡片头文字与 ECharts 标题在画布顶部重叠。结构化块的 spec["title"] 仍然必填,
  只是不再重复写进 option。
"""
from __future__ import annotations

import re

from typing import Any

# 图表类型分两档,分界线是「管理端(recharts)画不画得出来」。
#
# 两个前端装的不是同一个图表库:APP(uni)是 echarts,管理端操练台是 recharts。
# 一档的类型 recharts 有对应实现,两端都真能画;二档是 ECharts 独有的,
# 管理端拿到会自动退成数据表 + 一句说明(见 web ChartCard)。
#
# **二档不是「先加上以后再说」** —— 每一个都必须在管理端有明确的降级路径,
# 否则就会回到「回答里写着『图已生成 ✅』、页面上什么都没有」那个坑。
TIER1_TYPES = {
    "line",            # 走势
    "area",            # 走势 + 体量(单系列)
    "stacked_area",    # 构成随时间变化
    "bar",             # 比大小
    "stacked_bar",     # 部分与整体
    "horizontal_bar",  # 排名(类别名长、条目多时横着放)
    "pie",             # 占比(类别少)
    "donut",           # 占比 + 中间留白放总数
    "radar",           # 多指标看形状
    "scatter",         # 两个数值列的相关性
    "funnel",          # 转化漏斗(逐级递减)
    "gauge",           # 单个完成率/达成率
    "treemap",         # 层级占比(类别多,饼图切不开时)
    "sankey",          # 流向/分流
}
TIER2_TYPES = {
    "combo",           # 量 + 率 双轴(柱 = 量,左轴;线 = 率,右轴)
    "heatmap",         # 二维网格密度(时段 × 星期)
    "calendar",        # 日历热力(一整年逐日)
    "boxplot",         # 分布与离群
    "sunburst",        # 多层层级占比
    "themeriver",      # 构成随时间的流动
}
CHART_TYPES = TIER1_TYPES | TIER2_TYPES

# 管理端(recharts)画不出来的那些:前端据此显示数据表而不是空白画布。
# 这份清单由服务端下发,前端不自己维护一份 —— 两份清单迟早对不上,
# 而对不上的表现就是某个类型两边都以为对方会渲染,结果谁都没画。
ECHARTS_ONLY_TYPES = sorted(TIER2_TYPES)

# 白名单:图表对象允许的顶层字段(其余字段一律丢弃,防止 LLM 注入不稳定配置)
ALLOWED_CHART_FIELDS = {"title", "type", "x", "series", "data", "indicators", "unit",
                        "nodes", "links", "max", "min", "y", "categories",
                        "line_series", "line_unit", "marks", "baseline", "baseline_label"}

# 直角坐标系里类别多过这个数就自动加缩放(手机上 30 个柱子挤在 350px 里,标签全部重叠)
ZOOM_THRESHOLD = 20
# 排名条形图最多画几条(按值取最大的这些)
HBAR_MAX_ROWS = 15
# 标记:max/min 在极值点上标数,average 画一条均值虚线
MARK_KINDS = ("max", "min", "average")

# 分类配色。顺序固定、不循环使用 —— 第 9 个系列不是「再生成一个颜色」,
# 那种颜色在色觉障碍下和已有的分不开。八个之后由调用方合并成「其他」或分面。
# 这一组过了色觉分离与明度带校验(旧的 #eab308 太亮、#64748b 没有彩度,都换掉了)。
PALETTE = ["#2563eb", "#ea580c", "#059669", "#7c3aed",
           "#dc2626", "#0891b2", "#ca8a04", "#db2777"]


def _color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def _as_number(value: Any) -> Any:
    """尽力把值转成数字(ECharts 数值轴要求),转不了保留原文。"""
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "").replace("万", "0000").replace("亿", "00000000"))
    except (TypeError, ValueError):
        return value


def _clean_data(values: list | None) -> list:
    return [_as_number(v) for v in (values or []) if v is not None]


def _axis_chart(chart_type: str, spec: dict) -> dict:
    """line/bar 共用的直角坐标系骨架:categories(x) + 多条 series。"""
    categories = list(spec.get("x") or [])
    series: list[dict] = []
    for s in spec.get("series") or []:
        if not isinstance(s, dict):
            continue
        series.append({
            "name": s.get("name", ""),
            "type": chart_type,
            "data": _clean_data(s.get("data")),
            # 分类色按**系列顺序**固定取,不循环生成新色:第 9 个系列在色觉障碍下
            # 和已有的分不开,该由调用方合并成「其他」而不是再造一个颜色。
            "itemStyle": {"color": _color(len(series))},
        })
    if not series:
        return None  # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0},
        # 无 ECharts 自带标题,顶部不再留 40px 空档(标题由 APP 卡片头渲染)
        "grid": {"left": "3%", "right": "4%", "bottom": 44, "top": 12, "containLabel": True},
        "xAxis": {"type": "category", "data": categories},
        "yAxis": {"type": "value", "name": spec.get("unit", "")},
        "series": series,
    }


# 饼图最多几块。手机上一张饼切到第 8 块以后,标签必然互相压住、图例挤成几行,
# 颜色也超出 PALETTE 开始重复 —— 用户截图里 40 块的饼就是这样一团看不清的色块。
PIE_MAX_SLICES = 7
# 前几块合计至少占到这个比例,才把剩下的合并成「其它」;
# 否则「其它」本身就是最大的一块,饼图讲不出任何东西,改画排名条形图。
PIE_TOP_SHARE = 0.7


def pie_fold(data: list[dict], max_slices: int = PIE_MAX_SLICES) -> tuple[list[dict] | None, str]:
    """饼图类别过多时的处理,返回 (新的 data 或 None, 说明)。

    - 类别 ≤ max_slices:原样返回;
    - 前 max_slices-1 块合计 ≥ 70%:保留它们,其余合并成「其它」;
    - 否则返回 None,调用方改画 horizontal_bar(长尾分布用饼图没法读)。
    """
    if len(data) <= max_slices:
        return data, ""
    nums = [d for d in data if isinstance(d.get("value"), (int, float)) and not isinstance(d.get("value"), bool)]
    total = sum(max(float(d["value"]), 0.0) for d in nums)
    if total <= 0:
        return None, f"{len(data)} 个类别,饼图切不开"
    nums.sort(key=lambda d: float(d["value"]), reverse=True)
    keep = nums[:max_slices - 1]
    rest = sum(max(float(d["value"]), 0.0) for d in nums[max_slices - 1:])
    if (total - rest) / total < PIE_TOP_SHARE:
        return None, f"{len(data)} 个类别且分布分散(前 {max_slices - 1} 个只占 {round((total - rest) / total * 100)}%)"
    other = round(rest, 4)
    return keep + [{"name": "其它", "value": other}], \
        f"{len(data)} 个类别,已保留前 {max_slices - 1} 个、其余 {len(data) - max_slices + 1} 个合并为「其它」"


def _pie_chart(spec: dict) -> dict:
    data = []
    for d in spec.get("data") or []:
        if isinstance(d, dict) and d.get("name") is not None:
            data.append({"name": d.get("name"), "value": _as_number(d.get("value"))})
    if not data:
        return None  # type: ignore[return-value]
    folded, _ = pie_fold(data)
    if folded is None:
        # 长尾:结构化块(模型自己写的 json)走到这里没法回头改类型,就地换成排名条形图
        return _horizontal_bar({"title": spec.get("title"), "unit": spec.get("unit", ""),
                                "x": [d["name"] for d in data],
                                "series": [{"name": str(spec.get("title") or "占比"),
                                            "data": [d["value"] for d in data]}]})
    data = folded
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
        # 标签上已经写了名称和占比,底部图例是同一份信息的第二遍,
        # 类别一多图例就挤成好几行、把饼往上顶,所以不再放
        "legend": {"show": False},
        "series": [{
            "name": str(spec.get("title") or ""), "type": "pie",
            "radius": "58%", "center": ["50%", "50%"],
            "avoidLabelOverlap": True,
            # 太窄的扇区不标(标了也只会和邻居叠在一起),点一下看 tooltip
            "minShowLabelAngle": 6,
            "label": {"formatter": "{b}\n{d}%", "fontSize": 11, "lineHeight": 14},
            "labelLine": {"length": 8, "length2": 8},
            "labelLayout": {"hideOverlap": True},
            "data": data,
        }],
    }


def _radar_chart(spec: dict) -> dict:
    indicators = [
        {"name": i.get("name", ""), "max": _as_number(i.get("max"))}
        for i in (spec.get("indicators") or [])
        if isinstance(i, dict) and i.get("name")
    ]
    series = [
        {"value": _clean_data(s.get("data")), "name": s.get("name", "")}
        for s in (spec.get("series") or [])
        if isinstance(s, dict)
    ]
    if not indicators or not series:
        return None  # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item"},
        "legend": {"bottom": 0},
        "radar": {"indicator": indicators},
        "series": [{"type": "radar", "data": series}],
    }


def _stacked_axis(chart_type: str, spec: dict) -> dict:
    """堆叠柱 / 堆叠面积:同一个 stack 名把各系列摞起来,看的是「构成」。

    分界线:**问「各自多少」用分组柱,问「合起来怎么构成」才用堆叠**。
    堆叠图上除了最底下那条,其余系列的起点都不在 0,肉眼比不了它们之间的大小 ——
    用它回答「哪个渠道最大」会被看错,而图本身毫无异常。
    """
    base = "bar" if chart_type == "stacked_bar" else "line"
    opt = _axis_chart(base, spec)
    if opt is None:
        return None                              # type: ignore[return-value]
    for i, srs in enumerate(opt["series"]):
        srs["stack"] = "total"
        srs["itemStyle"] = {"color": _color(i)}
        if base == "line":
            srs["areaStyle"] = {}
            srs["smooth"] = True
    opt["tooltip"] = {"trigger": "axis", "axisPointer": {"type": "shadow" if base == "bar" else "line"}}
    return opt


def _area_chart(spec: dict) -> dict:
    opt = _axis_chart("line", spec)
    if opt is None:
        return None                              # type: ignore[return-value]
    for i, srs in enumerate(opt["series"]):
        srs["areaStyle"] = {"opacity": 0.18}
        srs["smooth"] = True
        srs["itemStyle"] = {"color": _color(i)}
    return opt


def _horizontal_bar(spec: dict) -> dict:
    """排名图。类别名长、条目多时横着放 —— 竖着放标签会挤成斜的,读不了。

    顺序按值从大到小重排:排名图的意义就在顺序上,保持原始行序等于没排名。
    ECharts 的 y 轴自下而上,所以倒序写入,最大的才会出现在最上面。
    """
    opt = _axis_chart("bar", spec)
    if opt is None:
        return None                              # type: ignore[return-value]
    cats = list(opt["xAxis"]["data"])
    first = opt["series"][0]["data"] if opt["series"] else []
    order = sorted(range(len(cats)),
                   key=lambda i: (first[i] if i < len(first) and isinstance(first[i], (int, float)) else float("-inf")))
    # 只留最大的 HBAR_MAX_ROWS 条。40 条的排名在手机上要拖一条竖滑块才看得完,
    # 读的人其实只看前面几名;要全量看表格。sys_chart 会把「截了多少」告诉模型写进正文。
    order = order[-HBAR_MAX_ROWS:]
    cats = [cats[i] for i in order]
    for j, srs in enumerate(opt["series"]):
        d = srs["data"]
        srs["data"] = [d[i] if i < len(d) else None for i in order]
        srs["itemStyle"] = {"color": _color(j)}
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        # 只有一个系列时图例就是标题的重复,不放,底部也不用留位置
        "legend": {"bottom": 0, "show": len(opt["series"]) > 1},
        "grid": {"left": "3%", "right": "6%", "bottom": 44 if len(opt["series"]) > 1 else 12,
                 "top": 12, "containLabel": True},
        "xAxis": {"type": "value", "name": spec.get("unit", "")},
        # 最多 15 条,每条的名字都要显示(默认会隔一个藏一个,看不出第二条是谁)
        "yAxis": {"type": "category", "data": cats, "axisLabel": {"interval": 0, "fontSize": 11}},
        "series": opt["series"],
    }


def _donut_chart(spec: dict) -> dict:
    opt = _pie_chart(spec)
    if opt is None:
        return None                              # type: ignore[return-value]
    if opt["series"][0].get("type") != "pie":
        return opt                               # 类别太散,已改成排名条形图
    # 外圈 58% 与饼图一致:70% 时外侧标签在手机宽度下会伸出画布
    opt["series"][0]["radius"] = ["36%", "58%"]
    return opt


def _scatter_chart(spec: dict) -> dict:
    """散点图:看两个数值之间有没有关系。x 与 y 都是数值列,不是类别。"""
    xs = [_as_number(v) for v in (spec.get("x") or [])]
    series = []
    for i, sname in enumerate(spec.get("series") or []):
        if not isinstance(sname, dict):
            continue
        ys = _clean_data(sname.get("data"))
        pts = [[xs[j], ys[j]] for j in range(min(len(xs), len(ys)))
               if isinstance(xs[j], (int, float)) and isinstance(ys[j], (int, float))]
        if not pts:
            continue
        series.append({"name": sname.get("name", ""), "type": "scatter",
                       "symbolSize": 10, "data": pts,
                       "itemStyle": {"color": _color(i)}})
    if not series:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item"},
        "legend": {"bottom": 0},
        "grid": {"left": "3%", "right": "4%", "bottom": 44, "top": 12, "containLabel": True},
        "xAxis": {"type": "value", "name": spec.get("categories") or ""},
        "yAxis": {"type": "value", "name": spec.get("unit", "")},
        "series": series,
    }


def _funnel_chart(spec: dict) -> dict:
    """转化漏斗。只用第一个系列 —— 漏斗讲的是一条路径逐级掉了多少人。"""
    data = []
    for d in spec.get("data") or []:
        if isinstance(d, dict) and d.get("name") is not None:
            data.append({"name": str(d.get("name")), "value": _as_number(d.get("value"))})
    data = [d for d in data if isinstance(d["value"], (int, float))]
    if not data:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
        "legend": {"bottom": 0},
        "series": [{
            "type": "funnel", "left": "10%", "width": "80%", "top": 12, "bottom": 44,
            # sort=descending 是**显示顺序**,不改数值。漏斗的语义是逐级递减,
            # 数据本身没递减时不替它排序掩盖问题 —— 那是业务上该被看见的事实。
            "sort": "descending", "gap": 2,
            "label": {"show": True, "position": "inside"},
            "data": data,
        }],
    }


# 「变化率」不是仪表盘该画的东西:仪表盘表达的是「离满格还有多远」(完成率、达成率、满载率),
# 环比 +2.99% 画在 0~100 的表盘上只是一根几乎不动的指针,负数(环比下降)更是画不出来。
# 现场 APP 就是这样一张「环比人数环比 2.99」的表盘,指针贴在底、刻度字和标题挤成一团。
_CHANGE_RATE = re.compile(r"(环比|同比|增长|增幅|涨幅|降幅|跌幅|变化|变动|增速|增减)")


def gauge_as_card(spec: dict) -> dict | None:
    """仪表盘画的是变化率(或负数)时,改成一张指标卡:{label, value, unit, trend}。不是则返回 None。"""
    if str(spec.get("type") or "").lower() != "gauge":
        return None
    data = [d for d in (spec.get("data") or []) if isinstance(d, dict)]
    if not data:
        return None
    val = _as_number(data[0].get("value"))
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return None
    label = str(data[0].get("name") or spec.get("title") or "").strip()
    text = f"{label}{spec.get('title') or ''}"
    if not (_CHANGE_RATE.search(text) or val < 0):
        return None
    unit = str(spec.get("unit") or ("%" if _CHANGE_RATE.search(text) else ""))
    shown = f"{'+' if val > 0 and _CHANGE_RATE.search(text) else ''}{val:g}"
    return {"label": label or str(spec.get("title") or "变化"), "value": shown, "unit": unit,
            "trend": "up" if val > 0 else "down" if val < 0 else "flat"}


def _gauge_chart(spec: dict) -> dict:
    """仪表盘:一个完成率/达成率。超过 100% 也照实画到刻度顶,不截断成 100。

    版式按**小尺寸**定(APP 聊天气泡里只有 ~260px 宽、200px 高):
    刻度字、标题、数值各占一层,互不重叠 —— 默认版式在这个尺寸下三者会叠成一团。
    """
    data = [d for d in (spec.get("data") or []) if isinstance(d, dict)]
    if not data:
        return None                              # type: ignore[return-value]
    val = _as_number(data[0].get("value"))
    if not isinstance(val, (int, float)):
        return None                              # type: ignore[return-value]
    mx = _as_number(spec.get("max")) if spec.get("max") is not None else None
    if not isinstance(mx, (int, float)) or mx <= 0:
        mx = 100
    # 刻度必须**盖得住这个值**,哪怕调用方写死了 max。
    # 不然 137% 的达成率会把指针顶在满格上,画出来和「刚好完成 100%」一模一样 ——
    # 超额完成被画没了,而图本身没有任何异常。
    mx = max(mx, val)
    unit = spec.get("unit") or ""
    return {
        "tooltip": {"formatter": "{b}: {c}" + unit},
        "series": [{
            "type": "gauge", "min": 0, "max": mx,
            "center": ["50%", "58%"], "radius": "88%",
            "startAngle": 210, "endAngle": -30,
            "progress": {"show": True, "width": 10, "roundCap": True},
            "axisLine": {"roundCap": True, "lineStyle": {"width": 10}},
            # 指针和进度条说的是同一件事,小尺寸下指针只会压住数值
            "pointer": {"show": False},
            "axisTick": {"show": False},
            "splitLine": {"length": 6, "distance": 2, "lineStyle": {"width": 1, "color": "#cbd5e1"}},
            "axisLabel": {"distance": 12, "fontSize": 10, "color": "#94a3b8"},
            "title": {"offsetCenter": [0, "42%"], "fontSize": 12, "color": "#64748b"},
            "detail": {"valueAnimation": False, "offsetCenter": [0, "0%"], "fontSize": 22, "fontWeight": "bold",
                       "formatter": "{value}" + unit},
            "data": [{"value": val, "name": str(data[0].get("name") or spec.get("title") or "")}],
        }],
    }


def _treemap_chart(spec: dict) -> dict:
    """矩形树图:类别多到饼图切不开时看占比(饼图超过七八块就没法读了)。"""
    data = []
    for i, d in enumerate(spec.get("data") or []):
        if isinstance(d, dict) and d.get("name") is not None:
            v = _as_number(d.get("value"))
            if isinstance(v, (int, float)):
                data.append({"name": str(d["name"]), "value": v,
                             "itemStyle": {"color": _color(i)}})
    if not data:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
        "series": [{"type": "treemap", "roam": False, "nodeClick": False,
                    "breadcrumb": {"show": False},
                    "label": {"show": True, "formatter": "{b}"}, "data": data}],
    }


def _sankey_chart(spec: dict) -> dict:
    """桑基图:谁流向谁、流了多少(渠道 → 票种、来源 → 去向)。"""
    links = []
    names: list[str] = []
    for l in spec.get("links") or []:
        if not isinstance(l, dict):
            continue
        src, tgt = str(l.get("source") or ""), str(l.get("target") or "")
        val = _as_number(l.get("value"))
        if not src or not tgt or not isinstance(val, (int, float)):
            continue
        # 自环会让 ECharts 直接报错并且整张图不渲染,这里静默丢掉
        if src == tgt:
            continue
        links.append({"source": src, "target": tgt, "value": val})
        for n in (src, tgt):
            if n not in names:
                names.append(n)
    if not links:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item", "triggerOn": "mousemove"},
        "series": [{"type": "sankey", "emphasis": {"focus": "adjacency"},
                    "data": [{"name": n, "itemStyle": {"color": _color(i)}}
                             for i, n in enumerate(names)],
                    "links": links}],
    }


def _heatmap_chart(spec: dict) -> dict:
    """热力网格:x 类别 × y 类别 × 数值(时段 × 星期最典型)。

    用连续色阶(一个色相、深浅表示大小),不是分类色 —— 热力图表达的是量级,
    彩虹配色会让人以为不同颜色是不同类别。
    """
    xs = [str(v) for v in (spec.get("x") or [])]
    ys = [str(v) for v in (spec.get("y") or [])]
    pts = []
    vals = []
    for d in spec.get("data") or []:
        if not isinstance(d, (list, tuple)) or len(d) < 3:
            continue
        v = _as_number(d[2])
        if not isinstance(v, (int, float)):
            continue
        pts.append([int(d[0]), int(d[1]), v])
        vals.append(v)
    if not xs or not ys or not pts:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"position": "top"},
        "grid": {"left": "3%", "right": "4%", "bottom": 60, "top": 12, "containLabel": True},
        "xAxis": {"type": "category", "data": xs, "splitArea": {"show": True}},
        "yAxis": {"type": "category", "data": ys, "splitArea": {"show": True}},
        "visualMap": {"min": min(vals), "max": max(vals), "calculable": True,
                      "orient": "horizontal", "left": "center", "bottom": 8,
                      "inRange": {"color": ["#eff6ff", "#60a5fa", "#1d4ed8"]}},
        "series": [{"type": "heatmap", "data": pts,
                    "label": {"show": False},
                    "emphasis": {"itemStyle": {"shadowBlur": 6}}}],
    }


def _calendar_chart(spec: dict) -> dict:
    """日历热力:一整年每天一格。data 是 [["2026-01-01", 数值], ...]。"""
    pts, vals, years = [], [], []
    for d in spec.get("data") or []:
        if not isinstance(d, (list, tuple)) or len(d) < 2:
            continue
        day = str(d[0])[:10]
        v = _as_number(d[1])
        if len(day) != 10 or not isinstance(v, (int, float)):
            continue
        pts.append([day, v])
        vals.append(v)
        if day[:4] not in years:
            years.append(day[:4])
    if not pts:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item"},
        "visualMap": {"min": min(vals), "max": max(vals), "calculable": True,
                      "orient": "horizontal", "left": "center", "bottom": 8,
                      "inRange": {"color": ["#eff6ff", "#60a5fa", "#1d4ed8"]}},
        "calendar": {"range": years[0] if len(years) == 1 else [min(years), max(years)],
                     "cellSize": ["auto", 14], "top": 40, "left": 40, "right": 20},
        "series": [{"type": "heatmap", "coordinateSystem": "calendar", "data": pts}],
    }


def _boxplot_chart(spec: dict) -> dict:
    """箱线图:看分布与离群。data 每项是一组已算好的 [min, Q1, 中位数, Q3, max]。

    **五个数必须是服务端算好的**,不让模型自己报分位数 —— 它报出来的分位数
    看着很合理,和真实分布对不上,而箱线图恰恰是用来看分布的。
    """
    cats = [str(v) for v in (spec.get("x") or [])]
    boxes = []
    for d in spec.get("data") or []:
        if not isinstance(d, (list, tuple)) or len(d) < 5:
            continue
        five = [_as_number(v) for v in d[:5]]
        if not all(isinstance(v, (int, float)) for v in five):
            continue
        boxes.append(sorted(five))          # ECharts 要求 min→max 有序
    if not cats or not boxes:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "item"},
        "grid": {"left": "3%", "right": "4%", "bottom": 44, "top": 12, "containLabel": True},
        "xAxis": {"type": "category", "data": cats},
        "yAxis": {"type": "value", "name": spec.get("unit", "")},
        "series": [{"type": "boxplot", "data": boxes,
                    "itemStyle": {"color": "#dbeafe", "borderColor": _color(0)}}],
    }


def _sunburst_chart(spec: dict) -> dict:
    """旭日图:多层层级占比。data 是嵌套的 {name, value, children:[...]}。"""
    def _clean(node, depth=0):
        if not isinstance(node, dict) or node.get("name") is None:
            return None
        out = {"name": str(node["name"])}
        v = _as_number(node.get("value"))
        if isinstance(v, (int, float)):
            out["value"] = v
        kids = [c for c in (_clean(k, depth + 1) for k in (node.get("children") or [])) if c]
        if kids:
            out["children"] = kids
        elif "value" not in out:
            return None                 # 既没有值也没有子节点,画不出来
        if depth == 0:
            out["itemStyle"] = {"color": _color(0)}
        return out

    data = [c for c in (_clean(n) for n in (spec.get("data") or [])) if c]
    if not data:
        return None                              # type: ignore[return-value]
    for i, n in enumerate(data):
        n["itemStyle"] = {"color": _color(i)}
    return {
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}"},
        "series": [{"type": "sunburst", "radius": [0, "90%"], "data": data,
                    "label": {"minAngle": 8}}],
    }


def _themeriver_chart(spec: dict) -> dict:
    """主题河流:构成随时间怎么变。data 是 [[时间, 数值, 系列名], ...]。"""
    pts = []
    for d in spec.get("data") or []:
        if not isinstance(d, (list, tuple)) or len(d) < 3:
            continue
        v = _as_number(d[1])
        if not isinstance(v, (int, float)):
            continue
        pts.append([str(d[0]), v, str(d[2])])
    if not pts:
        return None                              # type: ignore[return-value]
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
        "singleAxis": {"type": "time", "bottom": 50, "top": 20, "axisTick": {}, "axisLabel": {}},
        "series": [{"type": "themeRiver", "data": pts,
                    "label": {"show": False},
                    "emphasis": {"focus": "series"}}],
    }


def _combo_chart(spec: dict) -> dict | None:
    """量 + 率 双轴:柱画「量」(左轴),线画「率」(右轴)。

    我们的接口几乎都是成对返回的:销售票数 + 票数变化率、销售额 + 环比、占比 + TGI。
    放在同一根轴上,31% 在十万量级旁边就是贴着 0 的一条线 —— 图出来了、看着正常,
    读的人只会以为「变化率没什么变化」。拆成两张图又丢了「同一天量和率对照着看」。
    """
    opt = _axis_chart("bar", spec)
    if opt is None:
        return None
    lines = [s_ for s_ in (spec.get("line_series") or []) if isinstance(s_, dict)]
    if not lines:
        return None
    n_bar = len(opt["series"])
    for j, s_ in enumerate(lines):
        opt["series"].append({
            "name": s_.get("name", ""), "type": "line", "yAxisIndex": 1, "smooth": True,
            "symbolSize": 6, "data": _clean_data(s_.get("data")),
            "itemStyle": {"color": _color(n_bar + j)},
        })
    opt["yAxis"] = [
        {"type": "value", "name": spec.get("unit", "")},
        # 右轴不画网格线:两套网格叠在一起,读数时对不上哪根线是哪根轴的
        {"type": "value", "name": spec.get("line_unit", ""), "splitLine": {"show": False}},
    ]
    opt["tooltip"] = {"trigger": "axis", "axisPointer": {"type": "cross"}}
    return opt


def _apply_marks(opt: dict, spec: dict, chart_type: str) -> None:
    """极值点 / 均值线 / 基准线。只加在直角坐标系的图上。

    - marks: ["max","min","average"] 的子集。多系列时只标前两条 —— 每条都标,
      图上会挤满气泡,反而找不到峰值;
    - baseline: 一条固定参考线(TGI=100、目标值、及格线)。排名图是横着的,参考线在 x 上。
    """
    series = [s_ for s_ in (opt.get("series") or []) if isinstance(s_, dict)]
    if not series:
        return
    marks = [m for m in (spec.get("marks") or []) if m in MARK_KINDS]
    if any(m in marks for m in ("max", "min")):
        # 极值气泡画在点的上方,顶部只留 12px 会被裁掉一半
        g = opt.setdefault("grid", {})
        if isinstance(g, dict) and chart_type != "horizontal_bar":
            g["top"] = max(int(g.get("top") or 0), 40)
    for s_ in series[:2]:
        pts = [{"type": m, "name": {"max": "最大", "min": "最小"}[m]} for m in marks if m in ("max", "min")]
        if pts:
            s_["markPoint"] = {"data": pts, "symbolSize": 44}
        if "average" in marks:
            s_["markLine"] = {"symbol": "none", "lineStyle": {"type": "dashed"},
                              "data": [{"type": "average", "name": "平均"}],
                              # 默认标签在线的右端、画布外面,会被裁成「均」一个字
                              "label": {"formatter": "均值 {c}", "position": "insideEndTop"}}
    base = spec.get("baseline")
    if isinstance(base, (int, float)) and not isinstance(base, bool):
        key = "xAxis" if chart_type == "horizontal_bar" else "yAxis"
        label = str(spec.get("baseline_label") or "基准")[:12]
        # combo 的参考线画在「率」那根轴上(0%、TGI=100 这类基准都是对率说的)
        host = next((x for x in series if x.get("yAxisIndex") == 1), series[0]) \
            if chart_type == "combo" else series[0]
        ml = host.setdefault("markLine", {"symbol": "none", "data": []})
        ml.setdefault("data", []).append({key: base, "name": label,
                                          "lineStyle": {"type": "solid", "color": "#64748b"},
                                          "label": {"formatter": f"{label} {base}",
                                                    "position": "insideEndTop"}})
        ml.setdefault("symbol", "none")


def _apply_zoom(opt: dict, chart_type: str) -> None:
    """类别多时加缩放:手指/滚轮拖动看局部,底部一条细滑块示意位置。"""
    axis = opt.get("yAxis") if chart_type == "horizontal_bar" else opt.get("xAxis")
    if isinstance(axis, list):
        axis = axis[0] if axis else {}
    n = len((axis or {}).get("data") or [])
    if n <= ZOOM_THRESHOLD:
        return
    end = round(ZOOM_THRESHOLD / n * 100)
    if chart_type == "horizontal_bar":
        # 排名图最大的在最上面(y 轴自下而上),默认窗口放在顶端
        opt["dataZoom"] = [{"type": "inside", "yAxisIndex": 0, "start": 100 - end, "end": 100},
                           {"type": "slider", "yAxisIndex": 0, "width": 12, "right": 4,
                            "start": 100 - end, "end": 100}]
        opt.setdefault("grid", {})["right"] = 28
    else:
        opt["dataZoom"] = [{"type": "inside", "start": 0, "end": end},
                           {"type": "slider", "height": 14, "bottom": 30, "start": 0, "end": end}]
        opt.setdefault("grid", {})["bottom"] = 72


def _apply_value_labels(opt: dict, chart_type: str) -> None:
    """柱子少时直接把数标在柱顶。

    手机上没有悬停,点一下才出 tooltip;两三根柱子(今日 69 / 昨日 67)不标数的话,
    看图只能看出「差不多高」—— 而这恰恰是要读的那个数。柱子多了标数会糊成一片,就不标。
    """
    if chart_type not in ("bar", "horizontal_bar", "stacked_bar"):
        return
    series = [x for x in (opt.get("series") or []) if isinstance(x, dict) and x.get("type") == "bar"]
    axis = opt.get("yAxis") if chart_type == "horizontal_bar" else opt.get("xAxis")
    if isinstance(axis, list):
        axis = axis[0] if axis else {}
    n = len((axis or {}).get("data") or [])
    if not series or n == 0 or n * len(series) > 12 or opt.get("dataZoom"):
        return
    pos = "inside" if chart_type == "stacked_bar" else ("right" if chart_type == "horizontal_bar" else "top")
    for x in series:
        x.setdefault("label", {"show": True, "position": pos, "fontSize": 11})
    if pos == "top":
        # 最高那根柱子的数标在柱顶上方,顶部只留 12px 会被裁掉半截
        g = opt.setdefault("grid", {})
        if isinstance(g, dict):
            g["top"] = max(int(g.get("top") or 0), 24)
    elif pos == "right":
        g = opt.setdefault("grid", {})
        if isinstance(g, dict):
            # 数标在柱尾右侧;containLabel 只管坐标轴标签、不管系列标签,右边距不够最长那根的数会出画布
            r = g.get("right")
            g["right"] = max(int(r), 48) if isinstance(r, (int, float)) else 48


def _apply_category_labels(opt: dict, chart_type: str) -> None:
    """类目少时每个类目名都显示出来,放不下就折行。

    ECharts 默认按宽度自动隔一个藏一个:手机上两根柱子「2026-09-21 今日 / 2026-09-20 昨日」
    只剩第一个标签,第二根柱子是哪天完全看不出来。类目 ≤6 时强制全显示,按手机宽度(约 260px)
    均分给每个标签、超出折行;类目多了维持默认(本来也放不下)。
    """
    if chart_type == "horizontal_bar":
        return
    axis = opt.get("xAxis")
    if isinstance(axis, list):
        axis = axis[0] if axis else None
    if not isinstance(axis, dict) or axis.get("type") != "category":
        return
    n = len(axis.get("data") or [])
    if not 0 < n <= 6:
        return
    lab = axis.setdefault("axisLabel", {})
    lab.setdefault("interval", 0)
    lab.setdefault("hideOverlap", False)
    lab.setdefault("width", max(40, 260 // n - 8))
    lab.setdefault("overflow", "break")


_AXIS_TYPES = {"line", "bar", "area", "stacked_area", "stacked_bar", "horizontal_bar", "combo"}


def build_chart(spec: dict) -> dict | None:
    """把 LLM 结构化块里的一个图表对象填充成完整 ECharts option。

    校验失败(类型不在白名单 / 缺关键字段 / 无有效数据)返回 None,
    调用方跳过该图表即可,不阻塞整体收尾。
    """
    if not isinstance(spec, dict):
        return None
    chart_type = str(spec.get("type") or "").lower()
    if chart_type not in CHART_TYPES:
        return None
    builders = {
        "line": lambda: _axis_chart("line", spec),
        "bar": lambda: _axis_chart("bar", spec),
        "area": lambda: _area_chart(spec),
        "stacked_area": lambda: _stacked_axis("stacked_area", spec),
        "stacked_bar": lambda: _stacked_axis("stacked_bar", spec),
        "horizontal_bar": lambda: _horizontal_bar(spec),
        "pie": lambda: _pie_chart(spec),
        "donut": lambda: _donut_chart(spec),
        "radar": lambda: _radar_chart(spec),
        "scatter": lambda: _scatter_chart(spec),
        "funnel": lambda: _funnel_chart(spec),
        "gauge": lambda: _gauge_chart(spec),
        "treemap": lambda: _treemap_chart(spec),
        "sankey": lambda: _sankey_chart(spec),
        "heatmap": lambda: _heatmap_chart(spec),
        "calendar": lambda: _calendar_chart(spec),
        "boxplot": lambda: _boxplot_chart(spec),
        "sunburst": lambda: _sunburst_chart(spec),
        "themeriver": lambda: _themeriver_chart(spec),
        "combo": lambda: _combo_chart(spec),
    }
    fn = builders.get(chart_type)
    opt = fn() if fn else None
    if not opt:
        return opt
    if chart_type in ("pie", "donut") and isinstance(opt.get("yAxis"), dict):
        chart_type = "horizontal_bar"            # 饼图类别太散,模板已改画排名条形图
    if chart_type in _AXIS_TYPES:
        _apply_marks(opt, spec, chart_type)
        _apply_zoom(opt, chart_type)
        _apply_value_labels(opt, chart_type)
        _apply_category_labels(opt, chart_type)
    # 读屏软件用的图表描述(ECharts 按 option 自动生成「这是一张 xx 图,包含…」),
    # 对画面没有任何影响
    opt["aria"] = {"enabled": True}
    # 顶层调色板:饼图 / 漏斗 / 树图这些按数据项着色的,模板里没逐项上色,
    # 不设就落到 ECharts 默认色(那组里有亮黄绿和深灰,过不了色觉分离校验),
    # 同一个渠道在饼图和柱图里还会是两个颜色。
    opt.setdefault("color", list(PALETTE))
    return opt
