"""Quick BI SSE / 报告事件 → 平台统一事件。

平台统一事件(dict,均含 type):
    status     {stage, message}                  阶段提示(选表/上传/快照/报告生成中…)
    trace      {trace_id}                        Quick BI 侧 traceId,排障用
    reasoning  {delta}                           思考过程(增量)
    text       {delta, section}                  回答正文(增量);section=answer/summary/conclusion/check/report/plan…
    sql        {delta}                           生成的 SQL(增量)
    code       {delta}                           文件问数生成的 Python 代码(增量)
    chart      {chart}                           结构化图表/表格(见 make_chart)
    step       {title, desc, level}              多步骤进度(文件问数/报告)
    usage      {prompt_tokens, completion_tokens, total_tokens}
    report     {chat_id, report_url, finished}   报告状态
    error      {message, code}
    done       {...}                             由 service 层在收尾时补发

归一化逻辑移植自官方 skill 的 smartq_stream_query.py / file_stream_query.py /
q_insights.py / common/utils.py(报告部分),去掉了本地出图(matplotlib)与写文件,
改为把结构化数据交给前端渲染。
"""
from __future__ import annotations

import json
import re
import time
from html import unescape
from typing import Any, Iterable

from cogni_agent.tool_center_service.quickbi.errors import describe, scan_known_code

# olapResult.chartType → 平台图表类型(前端据此选择渲染方式)
OLAP_CHART_TYPE_MAP = {
    "NEW_TABLE": "table", "DETAIL_TABLE": "table", "CROSS_TABLE": "table",
    "BAR": "bar", "LINE": "line", "PIE": "pie", "SCATTER_NEW": "scatter",
    "INDICATOR_CARD": "indicator", "RANKING_LIST": "ranking",
    "MAP_COLOR_NEW": "bar", "PROGRESS_NEW": "horizontal_bar", "FUNNEL_NEW": "funnel",
}
GENERIC_CHART_TYPE_MAP = {
    "indicator": "indicator", "indicator-card": "indicator", "line": "line", "combo": "combo",
    "combination": "combo", "bar": "bar", "column": "bar", "grouped_bar": "bar", "stacked_bar": "bar",
    "stackedBar": "bar", "percent_bar": "bar", "horizontal_bar": "horizontal_bar",
    "horizontalBar": "horizontal_bar", "strip": "horizontal_bar", "ranking": "ranking", "rank": "ranking",
    "ranking-list": "ranking", "pie": "pie", "funnel": "funnel", "scatter": "scatter", "bubble": "scatter",
    "table": "table", "wordCloud": "bar",
}
DATA_TYPE_MAP = {"number": "numerical", "string": "string", "date": "time", "datetime": "time", "boolean": "string"}

_SKIP_QA = {"heartbeat", "locale", "message", "timestamp", "feedback", "token", "dsl", "answer_meta"}


# ── 上游自己标出来的「这是模型在想」 ────────────────────────────────
# Quick BI 的流里,推理和回答用的是**同一个 type**(text / summary),
# 靠 subType 区分:model_reasoning = 模型的思维链。以前没读这个字段,
# 于是整条思维链被当成回答灌进气泡 —— 用户看到的就是「思考过程和结果粘在一起」。
# 有这个标记时一切都不用猜;下面的文本切分器只是没有标记时的兜底。
_REASONING_SUBTYPES = frozenset({
    "model_reasoning", "reasoning", "thinking", "cot", "chain_of_thought", "model_thinking",
})


def is_reasoning_event(evt: dict) -> bool:
    """上游是否明确标了「这是思考过程」。"""
    sub = str(evt.get("subType") or evt.get("sub_type") or "").strip().lower()
    if not sub:
        return False
    return sub in _REASONING_SUBTYPES or sub.endswith("_reasoning") or "thinking" in sub


# ─────────────────────────── 文本工具 ───────────────────────────

def clean_html(text: Any) -> str:
    if not text:
        return ""
    s = unescape(str(text))
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<p[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def truncate(text: str, limit: int = 800) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip()
        if v[:1] in ("{", "["):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return value
    return value


def _num(v: Any) -> Any:
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        f = float(str(v).replace(",", ""))
        return int(f) if f.is_integer() and "." not in str(v) else f
    except (TypeError, ValueError):
        return v


# ─────────────────────────── 图表结构 ───────────────────────────

def infer_chart_type(fields: list[dict], hint: str = "", row_count: int = 0) -> str:
    hint_n = GENERIC_CHART_TYPE_MAP.get(hint or "", OLAP_CHART_TYPE_MAP.get(hint or "", ""))
    metrics = [f for f in fields if f.get("role") == "metric"]
    dims = [f for f in fields if f.get("role") != "metric"]
    if not metrics:
        return "table"
    if hint_n:
        return hint_n
    if not dims:
        return "indicator" if row_count <= 1 else "table"
    if any(f.get("type") == "time" for f in dims):
        return "line"
    return "bar"


def make_chart(*, title: str, fields: list[dict], rows: list, chart_type: str = "",
               sql: str = "", source: str = "", extra: dict | None = None) -> dict:
    """统一图表结构。rows 支持 list[list](按 fields 顺序)或 list[dict]。"""
    norm_rows: list[dict] = []
    names = [f["name"] for f in fields]
    for r in rows or []:
        if isinstance(r, dict):
            row = dict(r)
        elif isinstance(r, (list, tuple)):
            row = {n: (r[i] if i < len(r) else "") for i, n in enumerate(names)}
        else:
            continue
        for f in fields:
            if f.get("type") == "numerical" and f["name"] in row:
                row[f["name"]] = _num(row[f["name"]])
        norm_rows.append(row)
    chart = {
        "id": f"chart_{int(time.time() * 1000)}_{len(norm_rows)}",
        "title": title or "查询结果",
        "chart_type": chart_type or infer_chart_type(fields, "", len(norm_rows)),
        "fields": fields,
        "rows": norm_rows,
        "row_count": len(norm_rows),
        "sql": sql or "",
        "source": source,
    }
    if extra:
        chart.update(extra)
    return chart


def _fields_from_generic(field_info: list) -> list[dict]:
    out = []
    for i, f in enumerate(field_info or []):
        if not isinstance(f, dict):
            continue
        out.append({
            "name": str(f.get("fieldName") or f.get("name") or f.get("caption") or f"field_{i}"),
            "type": f.get("type") or "string",
            "role": "metric" if str(f.get("role", "")).lower() in ("metric", "measure") else "dimension",
        })
    return out


def charts_from_datalist(payload: Any, *, default_title: str, source: str) -> list[dict]:
    """文件问数 result/html_result、报告 structuredChart 的 dataList → 图表列表。"""
    payload = _loads(payload)
    if isinstance(payload, dict):
        data_list = payload.get("dataList")
        if data_list is None and isinstance(payload.get("chart"), (dict, list)):
            ch = payload["chart"]
            data_list = ch if isinstance(ch, list) else ch.get("dataList")
    elif isinstance(payload, list):
        data_list = payload
    else:
        data_list = None
    charts = []
    for ds in data_list or []:
        if not isinstance(ds, dict):
            continue
        fields = _fields_from_generic(ds.get("fieldInfo") or [])
        rows = ds.get("data") or []
        if not fields and rows and isinstance(rows[0], dict):
            fields = [{"name": k, "type": "numerical" if isinstance(v, (int, float)) else "string",
                       "role": "metric" if isinstance(v, (int, float)) else "dimension"} for k, v in rows[0].items()]
        hint = str(ds.get("chartType") or "")
        charts.append(make_chart(
            title=str(ds.get("title") or default_title), fields=fields, rows=rows,
            chart_type=infer_chart_type(fields, hint, len(rows)), source=source,
        ))
    return charts


def _field_names_from_dsl(param_info: dict | None) -> list[str] | None:
    if not param_info:
        return None
    dsl = _loads(param_info.get("abstractDsl") or param_info.get("specificDsl") or "")
    if not isinstance(dsl, dict) or not isinstance(dsl.get("select"), list):
        return None
    names = []
    for item in dsl["select"]:
        if not isinstance(item, dict):
            continue
        name, agg, calc = item.get("name", ""), item.get("aggregate", ""), item.get("calculate", "")
        if agg and calc:
            names.append(f"{name}({agg}+{calc})")
        elif agg:
            names.append(f"{name}({agg})")
        elif calc:
            names.append(f"{name}({calc})")
        else:
            names.append(name)
    return names or None


_INSTRUCTION_MARKS = ("\n口径要求:", "\n(取数要求:")


def strip_instruction(question: str) -> str:
    """去掉我们追加在问题后面的「口径要求 / 取数要求」—— 选表、匹配图表名用这一份。

    口径说明是给解读 / 回答用的约束,拿它去选表会被带偏(口径里提一句「参考客流趋势图」,
    仪表板问数就锁到那张图的数据集上了)。追问的上文保留:选表需要它。
    """
    q = question or ""
    for m in _INSTRUCTION_MARKS:
        if m in q:
            q = q.split(m, 1)[0]
    return q.strip()


def display_question(question: str) -> str:
    """图表标题用的问题:去掉口径 / 取数要求,追问只留这一句(「上文;追问:那销售额呢」→「那销售额呢」)。"""
    q = strip_instruction(question)
    return q.split(";追问:", 1)[1].strip() if ";追问:" in q else q


_OP_LABEL = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "gte": "≥", "lt": "<", "le": "≤", "lte": "≤",
             "in": "∈", "not_in": "∉", "like": "含", "contain": "含", "between": "介于"}


def where_label_from_dsl(param_info: dict | None) -> str:
    """查询 DSL 的过滤条件 → 「时间类型=week · 数据域=OTA直销」。

    小Q 对一个问题常拆成多条查询(现场运行 84b4f93c:按数据域拆成 5 条 SQL),每张结果表的
    标题却都是同一句问题 —— 交给解读时它分不清哪张是 OTA 直销、哪张是舆情,只好把 OTA 的数
    挪到「票务」「总览」里凑。过滤条件正是这几张表的区别,拿它来给表起名。
    """
    if not param_info:
        return ""
    dsl = _loads(param_info.get("abstractDsl") or param_info.get("specificDsl") or "")
    if not isinstance(dsl, dict):
        return ""
    parts = []
    for w in dsl.get("where") or []:
        if not isinstance(w, dict):
            continue
        col = w.get("column") or w.get("col") or {}
        name = (col.get("name") or col.get("n")) if isinstance(col, dict) else col
        val = w.get("value", w.get("v"))
        if not name or val in (None, "", []):
            continue
        val = "、".join(str(x) for x in val) if isinstance(val, list) else str(val)
        op = str(w.get("operator") or w.get("op") or "eq").lower()
        parts.append(f"{name}{_OP_LABEL.get(op, op)}{val}")
    return " · ".join(parts)


def chart_from_olap(olap: Any, *, question: str, param_info: dict | None = None) -> dict | None:
    """数据集问数 olapResult 事件 → 图表。"""
    olap = _loads(olap)
    if not isinstance(olap, dict):
        return None
    values = olap.get("values") or []
    metas = olap.get("metaType") or []
    raw_type = str(olap.get("chartType") or "")
    names = _field_names_from_dsl(param_info) or []
    fields = []
    for idx, meta in enumerate(metas):
        t = str(meta.get("t") or "")
        role = ("metric" if t.lower() == "measure" else "dimension") if t else (
            "metric" if meta.get("type") == "column" else "dimension")
        name = names[idx] if idx < len(names) and names[idx] else str(meta.get("k") or f"field_{idx}")
        fields.append({"name": name, "type": DATA_TYPE_MAP.get(str(meta.get("v") or "string"), "string"), "role": role})
    rows = [v.get("row") or [] for v in values if isinstance(v, dict)]
    chart_type = OLAP_CHART_TYPE_MAP.get(raw_type) or infer_chart_type(fields, "", len(rows))
    return make_chart(
        title=question, fields=fields, rows=rows, chart_type=chart_type,
        sql=str(olap.get("logicSql") or ""), source="dataset",
        extra={"raw_chart_type": raw_type, "conclusion": str(olap.get("conclusionText") or ""),
               "where_label": where_label_from_dsl(param_info)},
    )


def error_event(data: Any) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    code = None
    message = text
    if isinstance(data, dict):
        code = str(data.get("code") or "") or None
        message = str(data.get("message") or data.get("msg") or text)
    code = code or scan_known_code(text)
    return {"type": "error", "message": describe(code, clean_html(message) or "Quick BI 返回错误"), "code": code}


# ─────────────────────────── 流式问数 ───────────────────────────

class DatasetQaNormalizer:
    """/openapi/v2/smartq/queryByQuestionStream 事件归一化。"""

    TERMINAL = {"finish"}

    def __init__(self, question: str):
        # 只用于图表标题:不带口径说明 / 追问上文(带着的话每张图的标题都是一长段约束)
        self.question = display_question(question)
        self.param_infos: list[dict] = []
        self.finished = False

    def feed(self, evt: dict) -> list[dict]:
        et = str(evt.get("type") or "")
        data = evt.get("data", "")
        if et in _SKIP_QA or et in ("answer", "python"):
            return []
        if et == "trace":
            return [{"type": "trace", "trace_id": str(data)}]
        if et == "reasoning" or is_reasoning_event(evt):
            return [{"type": "reasoning", "delta": str(data)}]
        if et in ("text", "summary", "conclusion", "check"):
            return [{"type": "text", "delta": str(data), "section": "answer" if et == "text" else et}]
        if et == "relatedInfo":
            return [{"type": "text", "delta": str(data), "section": "related"}]
        if et == "sql":
            return [{"type": "sql", "delta": str(data)}]
        if et == "param":
            pd = _loads(data)
            info = ((pd or {}).get("result") or {}).get("info") or {} if isinstance(pd, dict) else {}
            if info:
                self.param_infos.append({
                    "question": info.get("question", ""), "cubeId": info.get("cubeId", ""),
                    "specificDsl": info.get("specificDsl", ""), "abstractDsl": info.get("abstractDsl", ""),
                })
                return [{"type": "status", "stage": "dsl", "message": "已生成查询 DSL",
                         "cube_id": info.get("cubeId", "")}]
            return []
        if et == "olapResult":
            pi = self.param_infos[-1] if self.param_infos else None
            chart = chart_from_olap(data, question=self.question, param_info=pi)
            return [{"type": "chart", "chart": chart}] if chart else []
        if et == "error":
            return [error_event(data)]
        if et == "finish":
            self.finished = True
            return [{"type": "status", "stage": "finish", "message": str(data or "")}]
        if data in (None, ""):
            return []
        return [{"type": "text", "delta": str(data), "section": et or "other"}]


class FileQaNormalizer:
    """/openapi/v2/smartq/queryByQuestionStreamByFile 事件归一化。"""

    TERMINAL = {"finish", "error", "check"}

    def __init__(self, question: str):
        self.question = question
        self.finished = False
        self.react_starts = 0

    def feed(self, evt: dict) -> list[dict]:
        et = str(evt.get("type") or "")
        data = evt.get("data", "")
        if et in _SKIP_QA:
            return []
        if et == "react" and evt.get("subType") == "EVENT_START":
            self.react_starts += 1
        if et in self.TERMINAL:
            self.finished = True
        if et == "trace":
            return [{"type": "trace", "trace_id": str(data)}]
        if et in ("reasoning", "actionThinking") or is_reasoning_event(evt):
            return [{"type": "reasoning", "delta": str(data)}] if data else []
        if et in ("text", "answer"):
            return [{"type": "text", "delta": str(data), "section": "answer"}] if data else []
        if et == "reporter":
            return [{"type": "text", "delta": str(data), "section": "report"}] if data else []
        if et in ("conclusion", "summary"):
            return [{"type": "text", "delta": str(data), "section": et}] if data else []
        if et == "relatedInfo":
            return [{"type": "text", "delta": str(data), "section": "related"}] if data else []
        if et == "code":
            return [{"type": "code", "delta": str(data)}] if data else []
        if et == "python":
            pd = _loads(data)
            out = []
            if isinstance(pd, dict):
                if pd.get("code"):
                    out.append({"type": "code", "delta": str(pd["code"])})
                if pd.get("result"):
                    out.append({"type": "step", "title": "代码执行结果", "desc": truncate(str(pd["result"]), 1500), "level": 2})
            return out
        if et == "sql":
            return [{"type": "sql", "delta": str(data)}]
        if et in ("result", "html_result"):
            charts = charts_from_datalist(data, default_title=self.question, source="file")
            if charts:
                return [{"type": "chart", "chart": c} for c in charts]
            return [{"type": "step", "title": "执行结果", "desc": truncate(str(data), 800), "level": 2}] if data else []
        if et in ("html", "unStructuredChart"):
            return [{"type": "step", "title": "图表(HTML)", "desc": "", "level": 2, "html": str(data)}] if data else []
        if et == "plan":
            return [{"type": "step", "title": "分析计划", "desc": clean_html(data), "level": 1}] if data else []
        if et == "question":
            pd = _loads(data)
            if isinstance(pd, dict):
                return [{"type": "step", "title": str(pd.get("title") or "子问题"), "desc": str(pd.get("desc") or ""), "level": 1}]
            return [{"type": "step", "title": "子问题", "desc": str(data), "level": 1}] if data else []
        if et in ("step", "subStep", "rewrite", "adaptation", "schedule", "selector", "systemSelector",
                  "table_retrieve", "schema_retrieve", "resource_info", "onlineSearchResult", "olapResult"):
            if not data:
                return []
            label = {"step": "步骤", "subStep": "子步骤", "rewrite": "问题改写", "adaptation": "问题改写",
                     "schedule": "调度", "table_retrieve": "表召回", "schema_retrieve": "字段召回",
                     "resource_info": "资源信息", "onlineSearchResult": "联网搜索"}.get(et, et)
            return [{"type": "step", "title": label, "desc": truncate(clean_html(data), 800),
                     "level": 2 if et != "step" else 1}]
        if et == "python_error":
            return [{"type": "step", "title": "代码执行报错(模型会自动修正)", "desc": truncate(str(data), 800), "level": 2}]
        if et == "error":
            e = error_event(data)
            if self.react_starts >= 2:
                e["message"] += "(文件解析可能失败,请检查表头是否规范、是否有合并单元格)"
            return [e]
        if et in ("check", "reject"):
            return [{"type": "text", "delta": str(data), "section": "check"}] if data else []
        if et == "finish":
            return [{"type": "status", "stage": "finish", "message": str(data or "")}]
        if et == "react":
            return []
        return [{"type": "step", "title": et, "desc": truncate(str(data), 300), "level": 2}] if data else []


class InterpretationNormalizer:
    """/openapi/v2/smartq/dataInterpretationStream 事件归一化(小Q解读)。"""

    TERMINAL = {"finish"}

    def __init__(self):
        self.finished = False

    def feed(self, evt: dict) -> list[dict]:
        et = str(evt.get("type") or "")
        data = evt.get("data", "")
        if et == "trace":
            return [{"type": "trace", "trace_id": str(data)}]
        if et == "reasoning" or is_reasoning_event(evt):
            return [{"type": "reasoning", "delta": str(data)}]
        if et in ("text", "summary"):
            return [{"type": "text", "delta": str(data), "section": "answer"}]
        if et == "error":
            return [error_event(data)]
        if et == "finish":
            self.finished = True
            return [{"type": "status", "stage": "finish", "message": str(data or "")}]
        return []


# ─────────────────────────── 小Q报告 ───────────────────────────

def _decode_data_field(item: dict) -> dict:
    event = dict(item)
    dv = event.get("data")
    event["parsedData"] = _loads(dv) if isinstance(dv, str) and dv.strip() else dv
    return event


def parse_report_events(raw_text: str) -> list[dict]:
    """qreportChatData 返回(JSON 数组 / 信封 / SSE 文本)→ 事件列表。"""
    if not raw_text or not raw_text.strip():
        return []
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        inner = parsed.get("data")
        if isinstance(inner, str) and inner.strip():
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                inner = None
        if isinstance(inner, list):
            return [_decode_data_field(i) for i in inner if isinstance(i, dict)]
        if parsed.get("success") is False or str(parsed.get("success")).lower() == "false":
            return [{"type": "error", "data": parsed.get("message") or text,
                     "parsedData": {"content": parsed.get("message"), "code": parsed.get("code")}}]
        return []
    if isinstance(parsed, list):
        return [_decode_data_field(i) for i in parsed if isinstance(i, dict)]
    events = []
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines = [ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:")]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            outer = json.loads(raw)
        except json.JSONDecodeError:
            events.append({"type": "unknown", "data": raw, "parsedData": raw})
            continue
        if isinstance(outer, dict):
            events.append(_decode_data_field(outer))
    return events


def _content(pd: Any, raw: Any = None) -> str:
    if isinstance(pd, dict):
        return str(pd.get("content") or "")
    if isinstance(pd, str):
        return pd
    return raw if isinstance(raw, str) else ""


_REPORT_FUNC_LABEL = {"thinking": "思考", "learn": "学习", "refuse": "拒答", "mainText": "正文", "interrupt": "中断"}


class ReportNormalizer:
    """小Q报告事件归一化。报告接口是「每次返回全量累积事件」,调用方需传入已处理数。"""

    def __init__(self):
        self.finished = False
        self.success = False
        self.error: str | None = None
        self.usage: dict | None = None

    def feed(self, event: dict) -> list[dict]:
        et = str(event.get("type") or "")
        pd = event.get("parsedData")
        func = pd.get("function", "") if isinstance(pd, dict) else ""
        if et in ("heartbeat", "locale", "check", "time", "finish"):
            return []
        if et == "trace":
            c = _content(pd, event.get("data")).strip()
            return [{"type": "trace", "trace_id": c}] if c else []
        if et == "error":
            msg = clean_html(_content(pd, event.get("data"))) or "报告生成失败"
            code = (pd or {}).get("code") if isinstance(pd, dict) else None
            self.error = describe(code or scan_known_code(msg), msg)
            self.finished = True
            return [{"type": "error", "message": self.error, "code": code}]
        if func in ("usedToken", "qreportUsedToken"):
            if isinstance(pd, dict):
                self.usage = {"prompt_tokens": pd.get("promptTokens"), "completion_tokens": pd.get("completionTokens"),
                              "total_tokens": pd.get("totalTokens")}
            out = [{"type": "usage", **(self.usage or {})}]
            if et == "qreport" and func == "qreportUsedToken":
                self.finished = True
                self.success = self.error is None
            return out
        if et in ("plan", "actionThinking"):
            c = clean_html(_content(pd, event.get("data")))
            if not c:
                return []
            if func in ("mainText",):
                return [{"type": "text", "delta": c, "section": "report"}]
            if func == "refuse":
                return [{"type": "text", "delta": c, "section": "check"}]
            return [{"type": "reasoning", "delta": c, "label": _REPORT_FUNC_LABEL.get(func, func or et)}]
        if et == "schedule":
            c = clean_html(_content(pd, event.get("data")))
            return [{"type": "status", "stage": "schedule", "message": truncate(c, 500)}] if c else []
        if et == "step":
            if not isinstance(pd, dict):
                return []
            return [{"type": "step", "title": str(pd.get("title") or "步骤"), "desc": str(pd.get("desc") or ""), "level": 1}]
        if et == "subStep":
            return self._substep(pd)
        if et == "qreport":
            if func == "onlineSearchResult":
                return self._web(pd, level=1)
            if func == "structuredChart":
                charts = charts_from_datalist(pd, default_title="报告图表", source="report")
                return [{"type": "chart", "chart": c} for c in charts]
            if func == "unStructuredChart":
                items = (pd or {}).get("chart") or [] if isinstance(pd, dict) else []
                return [{"type": "step", "title": str(i.get("title") or i.get("purpose") or "报告片段"),
                         "desc": truncate(clean_html(i.get("data") or i.get("content") or ""), 800), "level": 2}
                        for i in items if isinstance(i, dict)]
            c = clean_html(_content(pd, event.get("data")))
            return [{"type": "text", "delta": c, "section": "report", "function": func}] if c else []
        c = clean_html(_content(pd, event.get("data")))
        return [{"type": "step", "title": et or "message", "desc": truncate(c, 800), "level": 2}] if c else []

    @staticmethod
    def _web(pd: Any, level: int) -> list[dict]:
        items = (pd or {}).get("webItems") or [] if isinstance(pd, dict) else []
        links = [{"title": truncate(str(i.get("title") or ""), 100), "link": i.get("link"), "host": i.get("hostName")}
                 for i in items[:5] if isinstance(i, dict)]
        return [{"type": "step", "title": f"联网搜索:{len(items)} 条结果", "desc": "", "level": level, "links": links}]

    def _substep(self, pd: Any) -> list[dict]:
        if not isinstance(pd, dict):
            return [{"type": "step", "title": "子步骤", "desc": truncate(str(pd), 800), "level": 2}]
        fn = pd.get("function", "subStep")
        if fn == "usedToken":
            return []
        if fn == "onlineSearchResult":
            return self._web(pd, level=2)
        if fn == "knowledgeBaseResult":
            items = pd.get("knowledgeItems") or []
            names = "、".join(truncate(str(i.get("resourceName") or ""), 60) for i in items[:3] if isinstance(i, dict))
            return [{"type": "step", "title": f"知识库:{len(items)} 条", "desc": names, "level": 2}]
        if fn == "structuredChart":
            return [{"type": "chart", "chart": c} for c in charts_from_datalist(pd, default_title="报告图表", source="report")]
        c = clean_html(pd.get("content") or "")
        return [{"type": "step", "title": {"text": "分析"}.get(fn, fn), "desc": truncate(c, 1500), "level": 2}]


# ─────────────────────────── 结果汇总 ───────────────────────────

# ── 把「思考过程 …… 开始输出 …… 正式回答」切成两段 ────────────────────
# 小Q(尤其是解读/报告)经常把整条思维链和最终回答放在**同一段 answer 文本**里流出来:
#     思考过程: 1. 第一步:用户意图识别 …… 完美,开始输出。### 用户问题解答 …… 
# 事件级的 channel 分不开这种情况 —— 它本来就是一个事件。于是在文本层再切一刀:
# 分界之前的算推理(进执行过程),之后的才是回答(进结论区)。
_THINK_HEAD = re.compile(
    r"^\s*(?:<think>|<thinking>|思考过程|思考步骤|推理过程|分析过程|我的思考|Thinking|Let me think)",
    re.I)
# 数据集问数还有另一种形态:小Q会先把「怎么把这句话翻成 SQL」原原本本讲一遍 ——
#     问题选中数据集: 客群洞察-到访偏好…
#     提及字段及维值: 偏好类型(景点偏好);偏好对象;偏好占比;TGI值
#     判定0: 按偏好对象看占比与 TGI 的分布…
#     判定1: 单步 SQL;仅分组展示…
#     查询的逻辑SQL如下:
# 它没有「思考过程」这种字样,但每一行都是过程而不是结论。这类开头特征很硬
# (普通回答不会以「问题选中数据集」「判定0」起头),所以单独识别,
# 并且即便整段没等到分界标志,也按推理处理 —— 它本来就不是给用户读的。
_NL2SQL_HEAD = re.compile(
    r"^\s*(?:问题选中数据集|选中数据集|提及字段及维值|提及字段|判定\s*\d|已选数据集)[::]?",
)
# 分界标志两类:① 命中后**从标志之后**开始是回答;② 命中后**从标志本身**开始是回答(标题要留着)
_ANSWER_AFTER = re.compile(
    r"(?:</think>|</thinking>|开始输出|最终输出|正式输出|开始回答|以下是正式回答|以下为正式回答"
    # NL2SQL 前言的收尾:这一句之后才是结论(SQL 本身走 sql 通道,不在这条流里)
    r"|查询的逻辑SQL如下|以下是查询结果|查询结果如下)"
    r"[。:：.!! ]*\n?")
_ANSWER_AT = re.compile(
    r"(?m)^\s*#{1,6}\s*(?:用户问题解答|正式回答|最终回答|分析结果|解读结果|回答)\s*$")
# 流式里标志可能被切在两个 delta 中间,尾部留这么多字符不急着定性
_TAIL_KEEP = 24


class AnswerSplitter:
    """按上面的规则把 answer 文本流切成 reasoning / answer 两路(流式,逐 delta)。

    状态机三态:
      unknown —— 还看不出开头是不是思维链,先攒一点再判(攒够 _TAIL_KEEP 或遇到换行);
      thinking —— 正在思维链里,找分界标志;找到就切换并把剩下的当回答;
      answer  —— 正常回答,原样透传。
    没有思维链特征的文本(绝大多数问数回答)在第一次判定后就一路直通,零额外开销。
    """

    def __init__(self):
        self.mode = "unknown"
        self.buf = ""
        # strong=True:开头特征足够硬(NL2SQL 前言),没等到分界也按推理收尾;
        # strong=False:只是看着像思维链,拿不准时宁可当回答吐出去,绝不吞内容。
        self.strong = False

    def feed(self, e: dict) -> list[dict]:
        """输入一个统一事件,返回 0~N 个事件。非 answer 文本原样返回。"""
        if e.get("type") != "text" or (e.get("section") or "answer") != "answer":
            # **别的类型的事件插进来 = 这段思维链到此为止。**
            #
            # 小Q 把推理和最终答复发成**两段独立的文本**,中间隔着别的事件(feedback、
            # sql、chart 之类)。而这个拼接器是一路攒下来的:一旦开头被认成 NL2SQL 前言
            # (strong=True),后面那段答复会被继续当成同一段推理攒进去,
            # 最后在 flush 里整段归进「思考过程」——
            # 于是运行状态是 success,回答栏是空的,用户看到一张白卡片。
            # (现场那次:小Q 说「该问题与小Q问数的数据集无关,请重新提问」,
            # 这句话本该原样给用户看,却被归了档。)
            #
            # 所以在这里断开:把已攒的按推理收尾,状态复位,后面的文本重新判定。
            # 只在 thinking 态下断 —— answer 态本来就直通,不受影响。
            head: list[dict] = []
            if self.mode == "thinking":
                head = self._drain(final=True)
                self.mode, self.buf, self.strong = "unknown", "", False
            return head + [e]
        self.buf += str(e.get("delta") or "")
        return self._drain(final=False)

    def flush(self) -> list[dict]:
        """流结束时把尾巴吐出来 —— 否则最后二十几个字符会凭空消失。"""
        return self._drain(final=True)

    def _emit(self, text: str, kind: str) -> list[dict]:
        if not text:
            return []
        if kind == "reasoning":
            return [{"type": "reasoning", "delta": text}]
        return [{"type": "text", "delta": text, "section": "answer"}]

    def _drain(self, *, final: bool) -> list[dict]:
        out: list[dict] = []
        if self.mode == "unknown":
            # 判定要么等攒够、要么等第一个换行,要么流已经结束
            if not final and len(self.buf) < _TAIL_KEEP and "\n" not in self.buf:
                return out
            if _NL2SQL_HEAD.match(self.buf):
                self.mode, self.strong = "thinking", True
            elif _THINK_HEAD.match(self.buf):
                self.mode, self.strong = "thinking", False
            else:
                self.mode = "answer"
        if self.mode == "answer":
            out += self._emit(self.buf, "answer")
            self.buf = ""
            return out
        # thinking:找分界
        m_after = _ANSWER_AFTER.search(self.buf)
        m_at = _ANSWER_AT.search(self.buf)
        cut = None
        if m_after and m_at:
            cut = (m_after.start(), m_after.end()) if m_after.start() <= m_at.start() else (m_at.start(), m_at.start())
        elif m_after:
            cut = (m_after.start(), m_after.end())
        elif m_at:
            cut = (m_at.start(), m_at.start())
        # 标志刚好卡在缓冲区末尾时先别切:后面可能还跟着「。」「:」或换行,
        # 急着切会把它们留给回答开头(表现为回答以「。###」起头)。
        if cut and not final and cut[1] >= len(self.buf):
            cut = None
        if cut:
            out += self._emit(self.buf[:cut[0]], "reasoning")
            self.buf = self.buf[cut[1]:]
            self.mode = "answer"
            out += self._emit(self.buf, "answer")
            self.buf = ""
            return out
        # 还没到分界:尾部留一截防止标志被切断,其余作为推理先吐出去
        if final:
            # 没等到分界:强特征(NL2SQL 前言)按推理收尾,它本来就不是给用户读的;
            # 弱特征拿不准,当回答吐出去 —— 宁可少切一刀,也不能把内容吞掉。
            out += self._emit(self.buf, "reasoning" if self.strong else "answer")
            self.buf = ""
        elif len(self.buf) > _TAIL_KEEP:
            out += self._emit(self.buf[:-_TAIL_KEEP], "reasoning")
            self.buf = self.buf[-_TAIL_KEEP:]
        return out


# ── 思考过程 / 正式数据两条通道 ──────────────────────────────────────
# 小Q把「怎么想的」(推理、拆子问题、生成 SQL、跑代码)和「结论是什么」(回答正文、
# 图表、报告)混在同一条 SSE 流里。混着渲染的结果是:正式数据被推理文字冲得看不见,
# 而用户真正要读的只有后者。所以每个事件出厂时就打上通道标记,前端据此分区渲染,
# 非流式结果也按同样口径分成 thinking / data 两块。
THINKING_SECTIONS = frozenset({"related", "check", "other"})
DATA_SECTIONS = frozenset({"answer", "report", "conclusion", "summary"})


def event_channel(e: dict) -> str:
    """返回 "thinking" 或 "data"。判定只看事件自身,不依赖上下文,便于流式逐条打标。"""
    t = e.get("type")
    if t == "text":
        # 未知 section 一律算思考:宁可把它放进过程区,也不要让不确定的内容冒充结论
        return "data" if (e.get("section") or "answer") in DATA_SECTIONS else "thinking"
    # error 归数据侧:它是这次提问的结果,用户必须看见,不能折叠进过程区
    if t in ("chart", "table", "report", "error"):
        return "data"
    # reasoning / step / sql / code / status / trace / usage / error / meta
    return "thinking"


def with_channel(e: dict) -> dict:
    """给事件补上 channel 字段(已有的不覆盖,便于上游显式指定)。"""
    if "channel" not in e:
        e = {**e, "channel": event_channel(e)}
    return e


class ResultAccumulator:
    """把统一事件流累积成最终结果(非流式接口 / 运行记录落库用)。"""

    def __init__(self):
        self.sections: dict[str, list[str]] = {}
        self.reasoning: list[str] = []
        self.sql: list[str] = []
        self.code: list[str] = []
        self.charts: list[dict] = []
        self.steps: list[dict] = []
        self.errors: list[dict] = []
        self.trace_ids: list[str] = []
        self.usage: dict | None = None
        self.report: dict | None = None
        self.meta: dict = {}

    def add(self, e: dict) -> None:
        t = e.get("type")
        if t == "text":
            self.sections.setdefault(e.get("section") or "answer", []).append(e.get("delta") or "")
        elif t == "reasoning":
            self.reasoning.append(e.get("delta") or "")
        elif t == "sql":
            self.sql.append(e.get("delta") or "")
        elif t == "code":
            self.code.append(e.get("delta") or "")
        elif t == "chart" and e.get("chart"):
            self.charts.append(e["chart"])
        elif t == "step":
            self.steps.append({k: v for k, v in e.items() if k != "type"})
        elif t == "error":
            self.errors.append({"message": e.get("message"), "code": e.get("code")})
        elif t == "trace" and e.get("trace_id"):
            self.trace_ids.append(e["trace_id"])
        elif t == "usage":
            self.usage = {k: v for k, v in e.items() if k != "type"}
        elif t == "report":
            self.report = {**(self.report or {}), **{k: v for k, v in e.items() if k != "type"}}
        elif t == "meta":
            self.meta.update({k: v for k, v in e.items() if k != "type"})

    def extend(self, events: Iterable[dict]) -> None:
        for e in events:
            self.add(e)

    def text(self, section: str) -> str:
        return "".join(self.sections.get(section, []))

    @property
    def answer(self) -> str:
        """面向人阅读的最终回答:正文 > 报告 > 结论/摘要 > 校验提示;都没有时用图表结论兜底。"""
        parts = []
        for sec in ("answer", "report", "conclusion", "summary"):
            t = self.text(sec).strip()
            if t and t not in parts:
                parts.append(t)
        if not parts:
            concl = [c.get("conclusion") for c in self.charts if c.get("conclusion")]
            parts.extend(concl)
        if not parts and self.text("check").strip():
            parts.append(self.text("check").strip())
        return "\n\n".join(parts)

    def to_result(self) -> dict:
        # thinking / data 是同一份内容的分区视图(不是副本之外的新数据):
        # 顶层扁平字段保留给既有调用方,新调用方直接读这两块即可。
        answer_sections = {k: "".join(v) for k, v in self.sections.items() if k in DATA_SECTIONS}
        return {
            "answer": self.answer,
            "data": {
                "answer": self.answer,
                "sections": answer_sections,
                "charts": self.charts,
                "report": self.report,
            },
            "thinking": {
                "reasoning": "".join(self.reasoning),
                "sql": "".join(self.sql),
                "code": "".join(self.code),
                "steps": self.steps[-200:],
                "sections": {k: "".join(v) for k, v in self.sections.items() if k not in DATA_SECTIONS},
            },
            "sections": {k: "".join(v) for k, v in self.sections.items()},
            "reasoning": "".join(self.reasoning),
            "sql": "".join(self.sql),
            "code": "".join(self.code),
            "charts": self.charts,
            "steps": self.steps[-200:],
            "errors": self.errors,
            "trace_id": self.trace_ids[-1] if self.trace_ids else None,
            "usage": self.usage,
            "report": self.report,
            **({"meta": self.meta} if self.meta else {}),
        }

    def as_llm_text(self, max_rows: int = 30, max_charts: int = 5) -> str:
        """给大模型看的精简文本(工具调用返回值):回答 + 每张图表前 N 行 Markdown 表。

        一个问题被拆成几条查询时各表标题相同,用查询条件(where_label)区分;0 行的表写明「无数据」,
        超出张数的写明还有几张 —— 不说的话模型会以为它看到的就是全部。
        """
        out = []
        if self.answer:
            out.append(self.answer)
        for c in self.charts[:max_charts]:
            names = [f["name"] for f in c.get("fields", [])]
            if not names:
                continue
            title = str(c.get("title") or "数据").replace("\n", " ")
            if c.get("where_label"):
                title = f"{title}({c['where_label']})"
            if not c.get("rows"):
                out.append(f"### {title}\n(这个条件下没有数据)")
                continue
            lines = [f"### {title}", "| " + " | ".join(names) + " |",
                     "|" + "|".join(["---"] * len(names)) + "|"]
            for r in c.get("rows", [])[:max_rows]:
                lines.append("| " + " | ".join(str(r.get(n, "")) for n in names) + " |")
            if c.get("row_count", 0) > max_rows:
                lines.append(f"(共 {c['row_count']} 行,仅展示前 {max_rows} 行)")
            out.append("\n".join(lines))
        if len(self.charts) > max_charts:
            out.append(f"(另有 {len(self.charts) - max_charts} 张表未列出)")
        if self.report and self.report.get("report_url"):
            out.append(f"报告链接:{self.report['report_url']}")
        if self.errors:
            out.append("错误:" + ";".join(str(e.get("message")) for e in self.errors))
        return "\n\n".join(out) or "(Quick BI 未返回内容)"
