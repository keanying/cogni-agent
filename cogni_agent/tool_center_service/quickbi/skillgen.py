"""仪表板 → 专属查询技能(对齐官方 skill「仪表板技能生成」,模板见 output_skill_template.zh.md)。

官方是把仪表板结构喂给模型,让它按模板写一份 SKILL.md。我们的技能是平台里的 Skill(系统提示 +
绑定工具),所以这里**确定性地**按同一套章节拼出系统提示,不再过一道模型:

    元数据(pageId / gmtModified)→ 基本信息 → 业务背景 → 意图路由矩阵(关键词 → 图表 → 组件 → 数据集)
    → 分析思路(L1 总览 → L2 趋势 → L3 分布 → L4 明细的下钻路径)→ 图表过滤条件 → 工具使用约定

拼出来的内容全部来自仪表板结构本身(图表名、度量、维度、过滤、查询控件、文本组件),
不让模型去「推断业务口径」—— 那一步官方也要求「仅输出可明确推断的」,而推错的口径比没有口径更糟。
运营可以在技能编辑页里再补。生成的是**草稿**,发布仍走技能的正常发布流程。
"""
from __future__ import annotations

import re
from typing import Any

GENERATOR = "cogni-agent · quickbi"

# 图表类型 → 分析层级(官方的 L1-L4 金字塔):总览指标 → 趋势 → 分布/构成 → 明细
_LEVELS = (
    ("L1", "总览", re.compile(r"indicator|kpi|card|gauge|指标|仪表", re.I)),
    ("L2", "趋势", re.compile(r"line|area|trend|折线|面积", re.I)),
    ("L4", "明细", re.compile(r"table|pivot|cross|list|明细|交叉|表格", re.I)),
    ("L3", "分布", re.compile(r".", re.S)),
)


def chart_level(chart_type: str | None) -> tuple[str, str]:
    t = str(chart_type or "")
    for level, label, pat in _LEVELS:
        if pat.search(t):
            return level, label
    return "L3", "分布"


def _names(items: list[dict] | None) -> list[str]:
    out = []
    for x in items or []:
        n = str((x or {}).get("name") or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def _cell(v: Any) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _filter_text(filters: list | None) -> str:
    parts = []
    for f in filters or []:
        if isinstance(f, dict):
            name = f.get("name") or f.get("caption") or ""
            cond = f.get("complexFilter") or f.get("condition") or f.get("value") or ""
            parts.append(f"{name} {cond}".strip())
        elif f:
            parts.append(f"按「{f}」过滤")   # 结构里只有过滤字段,没有条件值
    return " AND ".join(p for p in parts if p) or "none"


def build_skill(*, name: str, url: str | None, page_id: str, meta: dict,
                tool_names: dict[str, str] | None = None) -> dict:
    """返回 {name, description, prompt}。tool_names = {"qa": 问数工具名, "insight": 解读工具名}。"""
    title = meta.get("name") or name or page_id
    charts = [c for c in meta.get("charts") or [] if c.get("name") or c.get("measures")]
    measures: list[str] = []
    dims: list[str] = []
    for c in charts:
        measures += [m for m in _names(c.get("measures")) if m not in measures]
        dims += [d for d in _names(c.get("dimensions")) if d not in dims]
    cubes = meta.get("cubes") or []
    tools = tool_names or {}

    core = measures[:8]
    description = (f"针对 Quick BI「{title}」仪表板的专属查询技能。用户问到"
                   + ("、".join(core) if core else "这块看板上的数据")
                   + f"时使用本技能(而不是通用问数)。触发关键词:{title}"
                   + ("、" + "、".join(dims[:5]) if dims else "") + "。")

    lines: list[str] = [
        "<!-- SKILL_METADATA",
        f"dashboard_page_id: {page_id}",
        f"skill_generated_at: {meta.get('gmt_modified') or ''}",
        f"dashboard_name: {title}",
        f"generator_skill: {GENERATOR}",
        "-->",
        "",
        f"# {title} - 数据查询",
        "",
        f"针对「{title}」仪表板的专属查询技能,支持自然语言查询这块看板上的数据。",
        "",
        "## 基本信息",
        "",
        "| 属性 | 值 |",
        "|------|---|",
        f"| 名称 | {_cell(title)} |",
        f"| URL | `{_cell(url or '')}` |",
        f"| pageId | `{page_id}` |",
    ]
    if cubes:
        lines.append("| 数据集 | " + "、".join(_cell(f"{c.get('cube_name') or c.get('cube_id')}(`{c.get('cube_id')}`)")
                                       for c in cubes) + " |")

    lines += ["", "## 业务背景", ""]
    lines.append(f"- **核心指标**:{'、'.join(core) if core else '无'}")
    lines.append(f"- **分析维度**:{'、'.join(dims[:12]) if dims else '无'}")
    controls = [f.get("label") for qc in meta.get("query_controls") or [] for f in qc.get("fields") or []
                if f.get("label")]
    if controls:
        lines.append(f"- **查询控件**:{'、'.join(dict.fromkeys(controls))}(问题里给出这些条件时照写进问题)")
    tabs = [t for tab in meta.get("tabs") or [] for t in tab.get("tabs") or [] if t]
    if tabs:
        lines.append(f"- **页签**:{'、'.join(tabs)}")
    texts = [str(t.get("text") if isinstance(t, dict) else t).strip() for t in meta.get("texts") or []]
    texts = [t for t in texts if t and t != "None"]
    lines.append("- **统计口径**:" + ("见下方看板说明" if texts else "无特殊说明"))
    if texts:
        lines += ["", "看板说明(原文):", ""] + [f"> {t[:300]}" for t in texts[:5]]

    lines += ["", "## 意图路由矩阵", "",
              "| 关键词 | 目标图表 | 组件 ID | 数据集 ID | 分析层级 | 路由说明 |",
              "|--------|---------|--------|-----------|---------|----------|"]
    for c in charts:
        level, label = chart_level(c.get("chart_type"))
        kws = (_names(c.get("measures")) + _names(c.get("dimensions")))[:6]
        lines.append(f"| {_cell('、'.join(kws) or c.get('name'))} | {_cell(c.get('name'))} | "
                     f"`{_cell(c.get('component_id'))}` | `{_cell(c.get('cube_id'))}` | {level} {label} | "
                     f"问到{_cell('、'.join(_names(c.get('measures'))[:3]) or c.get('name'))}时用这张图 |")
    lines += ["", "路由规则(优先级递减):度量字段名 → 维度字段名 → 图表类型 → 都对不上时用全部数据集。"]

    by_level: dict[str, list[str]] = {}
    for c in charts:
        by_level.setdefault(chart_level(c.get("chart_type"))[0], []).append(c.get("name") or "")
    present = [lv for lv in ("L1", "L2", "L3", "L4") if by_level.get(lv)]
    if len(present) >= 2:
        lines += ["", "## 分析思路", "", "| 来源层级 | 来源图表 | 目标层级 | 目标图表 | 分析用途 |",
                  "|---------|---------|---------|---------|--------|"]
        purpose = {("L1", "L2"): "从总览下钻到趋势", ("L2", "L3"): "从趋势下钻到维度分布",
                   ("L3", "L4"): "从维度下钻到明细"}
        for a, b in zip(present, present[1:]):
            lines.append(f"| {a} | {_cell('、'.join(by_level[a][:3]))} | {b} | {_cell('、'.join(by_level[b][:3]))} | "
                         f"{purpose.get((a, b), '逐层下钻定位原因')} |")

    filtered = [(c.get("name"), _filter_text(c.get("filters"))) for c in charts]
    if any(f != "none" for _, f in filtered):
        lines += ["", "## 图表过滤条件", ""]
        lines += [f"- {_cell(n)}:{f}" for n, f in filtered if f != "none"]

    lines += ["", "## 工具使用", ""]
    if tools.get("qa"):
        lines.append(f"- 问具体数据(多少、排名、对比、占比…)→ 调用「{tools['qa']}」。question 写完整的指标、维度、"
                     "时间范围;用户点到上面矩阵里的某张图时,把图表名写进问题,工具会用那张图的数据集。")
    if tools.get("insight"):
        lines.append(f"- 要解读 / 看趋势 / 找异常 / 归因 → 调用「{tools['insight']}」。")
    lines += ["- 只依据工具返回的数据作答;工具没返回数据或报错时如实说明,不要编造数字。",
              "- 问题与这块看板无关时,直接说明本技能只覆盖「" + title + "」的数据。"]
    return {"name": f"{title} · 数据查询"[:120], "description": description[:500], "prompt": "\n".join(lines)}
