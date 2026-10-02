"""结构化问数输出解析(04 智能问数方案 §4.4 落地)。

LLM 在回答中输出 ```json ... ``` 代码块,内容形如:
    {
      "cards":     [{"label": "今日在园人数", "value": "8523", "unit": "人", "trend": "up"}],
      "charts":    [{"title": "近7天客流趋势", "type": "line",
                     "x": ["08-19", "..."], "series": [{"name": "在园人数", "data": [8523, ...]}]}],
      "insight":   {"level": "warning", "content": "午后客流冲高,建议..."},
      "recommend": [{"question": "今日退票高峰在几点?", "theme": "票务"}]
    }

executor 收尾时调用 parse():
- 解析成功 → 结构化块从 final_answer 剥离(不展示给用户),charts 经
  chart_templates.build_chart 填充成完整 ECharts option,供 SSE 广播;
- 解析失败 / 无结构化块 → 原样返回答案,不产生任何事件,不影响现有流程。

图表只允许白名单类型、只填业务字段(见 chart_templates.py),单块图表不合法时
跳过该图表,不会让整条回答失败。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from cogni_agent.runtime_service.engine import chart_templates
from cogni_agent.runtime_service.engine import chart_view

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)

# 结构化块内允许的顶层键(其余忽略,防止 LLM 塞无关字段)
_ALLOWED_KEYS = {"cards", "charts", "insight", "recommend"}


@dataclass
class StructuredOutput:
    """从 final_answer 中解析出的结构化问数结果。

    - answer:     剥离结构化块后的纯文本答案(展示给用户)
    - cards:      [{label, value, unit, trend}] 数据卡片
    - charts:     [{title, type, option}] 已填充的 ECharts option
    - insight:    {level, content} | None 数据解读
    - recommends: [{question, theme}] 关联推荐追问
    """
    answer: str
    cards: list[dict] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    insight: dict | None = None
    recommends: list[dict] = field(default_factory=list)


def _parse_block(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_cards(obj: dict) -> list[dict]:
    items = obj.get("cards") or []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and it.get("label") is not None:
            out.append({
                "label": it.get("label", ""),
                "value": it.get("value", ""),
                "unit": it.get("unit", ""),
                "trend": it.get("trend", ""),
            })
    return out


def _gauges_as_cards(obj: dict) -> list[dict]:
    """画成仪表盘的变化率(环比/同比……)改走指标卡,见 chart_templates.gauge_as_card。"""
    out: list[dict] = []
    for spec in obj.get("charts") or []:
        card = chart_templates.gauge_as_card(spec) if isinstance(spec, dict) else None
        if card:
            out.append(card)
    return out


def _extract_charts(obj: dict) -> list[dict]:
    out: list[dict] = []
    for spec in obj.get("charts") or []:
        if isinstance(spec, dict) and chart_templates.gauge_as_card(spec):
            continue  # 已改成指标卡
        option = chart_templates.build_chart(spec)
        if option is None:
            continue  # 单块图表不合法:跳过,不影响整体
        # 两种形态一起带:option 给 APP(echarts),中性行列表给管理端(recharts)。
        # 只带 option 的话,模型自己画的图在操练台永远是空的 —— 而回答正文里
        # 还写着「图已生成」,看的人只会觉得系统坏了。table 由 option 现推,
        # 同源不会漂(不是另算一遍,两端的数字物理上是同一份)。
        one = {
            "title": str(spec.get("title") or ""),
            "type": str(spec.get("type") or "").lower(),
            "option": option,
        }
        out.append(chart_view.ensure_view(one))
    return out


def _extract_insight(obj: dict) -> dict | None:
    ins = obj.get("insight")
    if not isinstance(ins, dict) or not ins.get("content"):
        return None
    level = str(ins.get("level") or "normal")
    if level not in {"normal", "warning", "danger"}:
        level = "normal"
    return {"level": level, "content": str(ins.get("content"))}


def _extract_recommends(obj: dict) -> list[dict]:
    items = obj.get("recommend") or []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and it.get("question"):
            out.append({"question": it.get("question", ""), "theme": it.get("theme", "")})
    return out


def parse(final_answer: str) -> StructuredOutput:
    """解析 final_answer 中的结构化块;无块/解析失败则原样返回纯文本答案。"""
    if not final_answer:
        return StructuredOutput(answer=final_answer or "")

    for match in _JSON_BLOCK_RE.finditer(final_answer):
        obj = _parse_block(match.group(1))
        if obj is None:
            continue
        structured = StructuredOutput(
            answer=(final_answer[: match.start()] + final_answer[match.end():]).strip(),
            cards=_extract_cards(obj) + _gauges_as_cards(obj),
            charts=_extract_charts(obj),
            insight=_extract_insight(obj),
            recommends=_extract_recommends(obj),
        )
        # 没有任何结构化内容时视作普通回答:保留原文,不剥离代码块
        if not (structured.cards or structured.charts or structured.insight or structured.recommends):
            continue
        return structured

    return StructuredOutput(answer=final_answer)
