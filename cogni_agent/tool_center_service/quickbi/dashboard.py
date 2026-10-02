"""仪表板 / 数据门户解析(小Q报表的「已有面板」数据来源)。

移植自官方 skill 的 dashboard/quickbi_openapi.py、dataportal_resolver.py 与
get_dashboard_json.js(精简版:只保留问数/解读需要的结构 —— 图表、字段、查询控件、
数据集),不再依赖 Node.js 子进程。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

CONTENT_TYPE_NAMES = {
    "report": "仪表板", "excel": "电子表格", "url": "外部链接", "page": "页面", "form": "数据填报",
    "download": "自助取数", "Cube": "数据集", "analysis": "即席分析", "screen": "数据大屏",
}

_ID = r"[a-zA-Z0-9\-_]+"


def is_dataportal_url(url: str) -> bool:
    return "/product/view.htm" in (url or "")


def extract_page_id(url_or_id: str) -> str:
    """仪表板 URL → pageId。直接传 pageId 也可以。

    支持:
      https://bi.aliyun.com/dashboard/view/pc.htm?pageId=xxx
      https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=xxx&accessTicket=...
    """
    s = (url_or_id or "").strip()
    if not s:
        raise ValueError("仪表板地址为空")
    m = re.search(rf"pageId=({_ID})", s)
    if m:
        return m.group(1)
    if re.fullmatch(_ID, s):
        return s
    raise ValueError(f"无法从地址中提取 pageId:{s}")


def extract_dataportal_ids(url: str) -> dict:
    p = re.search(rf"productId=({_ID})", url or "")
    if not p:
        raise ValueError(f"无法从数据门户地址中提取 productId:{url}")
    m = re.search(rf"menuId=({_ID})", url)
    return {"productId": p.group(1), "menuId": m.group(1) if m else None}


def url_access_ticket(url: str) -> str | None:
    """token3rd 嵌入链接里的 accessTicket(仅用于展示/回跳,OpenAPI 不用它)。"""
    try:
        q = parse_qs(urlparse(url).query)
        return (q.get("accessTicket") or q.get("accessToken") or [None])[0]
    except ValueError:
        return None


def standard_view_url(server_domain: str, page_id: str) -> str:
    return f"{server_domain.rstrip('/')}/dashboard/view/pc.htm?pageId={page_id}"


# ── 数据门户菜单树 ──────────────────────────────────────────────────────────

def _is_hide(menu: dict, template: str = "default") -> bool:
    cfg = menu.get("mobileConfig" if template == "mobile" else "pcConfig") or {}
    return bool(cfg.get("isHide", False))


def _find_first_child(menu: dict, template: str = "default") -> dict | None:
    hide = _is_hide(menu, template)
    children = menu.get("children") or []
    content = menu.get("content") or []
    if not menu.get("isEmpty") and not hide and not children:
        if content and content[0].get("type") and content[0].get("type") != "url":
            return menu
    if hide or not children:
        return None
    for child in children:
        found = _find_first_child(child, template)
        if found:
            return found
    return None


def _get_menu_by(menus: list, pred: Callable[[dict], bool]) -> dict | None:
    for m in menus or []:
        if pred(m):
            return m
        found = _get_menu_by(m.get("children") or [], pred)
        if found:
            return found
    return None


def select_menu(menus: list, menu_id: str | None = None) -> dict | None:
    if menu_id:
        return _get_menu_by(menus, lambda m: m.get("id") == menu_id)
    home = _get_menu_by(menus, lambda m: bool(m.get("isHome")))
    if home:
        first = _find_first_child(home)
        if first:
            return first
    return _find_first_child({"isEmpty": True, "children": menus})


def page_id_from_menu(menu: dict) -> tuple[str | None, str, str]:
    """→ (page_id|None, dashboard_name, message)"""
    content = menu.get("content") or []
    if not content:
        return None, "", "菜单没有关联任何内容"
    first = content[0]
    ctype, cid, cname = first.get("type"), first.get("id"), first.get("name") or "未命名"
    if ctype != "report":
        return None, cname, (f"该菜单关联的是「{CONTENT_TYPE_NAMES.get(ctype, ctype)}」({cname}),"
                             "不是仪表板,请提供指向仪表板的数据门户链接")
    if not cid:
        return None, cname, f"菜单「{cname}」未关联有效的仪表板 ID"
    return cid, cname, "ok"


# ── 仪表板 JSON 精简解析 ─────────────────────────────────────────────────────

def _loads(v: Any) -> Any:
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return v or {}


def _garbage(s: Any) -> bool:
    return not s or not isinstance(s, str) or bool(re.fullmatch(r"[a-f0-9]{10}", s, re.I))


def _schema_caption(path_id: str | None, fields: list | None) -> str | None:
    if not path_id or not fields:
        return None
    for f in fields:
        if f.get("uniqueId") == path_id or f.get("name") == path_id:
            return f.get("caption")
        for a in f.get("attributes") or []:
            if a.get("uniqueId") == path_id or a.get("id") == path_id:
                return a.get("caption")
    return None


def parse_dashboard(raw: dict, schema_map: dict | None = None) -> dict:
    """/openapi/v2/dashboard/query 返回 → 结构摘要。

    schema_map: {cubeId: {"cubeName": str, "fields": [...]}}(来自 dataset/batchGetSchema)
    """
    schema_map = schema_map or {}
    charts, controls, tabs, texts = [], [], [], []
    for comp in raw.get("components") or []:
        content = _loads(comp.get("componentContent"))
        query_input = _loads(comp.get("queryInput"))
        ctype = content.get("type") or comp.get("componentType") or comp.get("type")
        if ctype == "query2":
            fcs = (content.get("modelConfig") or {}).get("fieldConfigs") or []
            controls.append({
                "component_id": comp.get("componentId"),
                "fields": [{"label": f.get("labelName"), "widget": f.get("componentType"),
                            "required": bool(f.get("isRequired")), "default": f.get("defaultValue"),
                            "granularity": (f.get("config") or {}).get("timeGranularity")} for f in fcs],
            })
            continue
        if ctype == "tab":
            attr = content.get("attribute") or {}
            items = attr.get("tabs") or content.get("items") or []
            tabs.append({"component_id": comp.get("componentId"), "name": content.get("caption") or "Tab",
                         "tabs": [t.get("title") or t.get("text") for t in items if isinstance(t, dict)]})
            continue
        if ctype == "text":
            html = (content.get("attribute") or {}).get("content") or ""
            t = re.sub(r"<[^>]*>", "", html).strip()
            if t:
                texts.append(t[:500])
            continue
        attr = content.get("attribute") or {}
        source_id = query_input.get("sourceId") or comp.get("dataFromId")
        schema_fields = (schema_map.get(source_id) or {}).get("fields") if source_id else None
        fsm = attr.get("fieldSettingMap") or {}

        def caption(col: dict, fsm=fsm, schema_fields=schema_fields) -> str:
            alias = (fsm.get(col.get("uuid")) or {}).get("aliasName") if col.get("uuid") else None
            if alias and not _garbage(alias):
                return alias
            return _schema_caption(col.get("pathId"), schema_fields) or col.get("caption") or col.get("name") or ""

        dims, measures, filters = [], [], []
        for area in query_input.get("area") or []:
            cols = area.get("columnList") or []
            if area.get("id") == "drill":
                continue
            if area.get("id") == "filters" or area.get("queryAxis") == "filter":
                filters.extend(caption(c) for c in cols)
                continue
            for c in cols:
                item = {"name": caption(c), "aggregate": c.get("aggregator") or c.get("aggregateType")}
                (measures if c.get("itemType") == "measure" else dims).append(item)
        pos = content.get("tileLayout") or {}
        charts.append({
            "component_id": comp.get("componentId"),
            "name": attr.get("caption") or comp.get("componentName") or "",
            "chart_type": ctype,
            "cube_id": source_id,
            "cube_name": (schema_map.get(source_id) or {}).get("cubeName") if source_id else None,
            "dimensions": [d for d in dims if d["name"]],
            "measures": [m for m in measures if m["name"]],
            "filters": [f for f in filters if f],
            "position": {"x": pos.get("x", 0), "y": pos.get("y", 0), "w": pos.get("w", 1), "h": pos.get("h", 1)},
        })
    charts.sort(key=lambda c: (c["position"]["y"], c["position"]["x"]))
    cubes: dict[str, dict] = {}
    for c in charts:
        if c["cube_id"]:
            cubes.setdefault(c["cube_id"], {"cube_id": c["cube_id"], "cube_name": c["cube_name"] or c["cube_id"],
                                            "charts": []})["charts"].append(c["name"])
    for cid, info in schema_map.items():
        cubes.setdefault(cid, {"cube_id": cid, "cube_name": info.get("cubeName") or cid, "charts": []})
    return {
        "name": raw.get("name") or "",
        "page_id": raw.get("treeId") or raw.get("pageId") or raw.get("dashboardId"),
        "workspace_id": raw.get("workspaceId"),
        "gmt_modified": raw.get("gmtModified"),
        "charts": charts,
        "query_controls": controls,
        "tabs": tabs,
        "texts": texts,
        "cubes": list(cubes.values()),
    }


def suggest_questions(summary: dict, limit: int = 12) -> list[dict]:
    """按图表的维度/度量生成候选「固定问题」,运营可一键采纳。"""
    out: list[dict] = []
    seen = set()
    for c in summary.get("charts") or []:
        if not c.get("cube_id") or not c.get("measures"):
            continue
        m = "、".join(x["name"] for x in c["measures"][:2])
        d = c["dimensions"][0]["name"] if c.get("dimensions") else ""
        q = f"按{d}看{m}的分布" if d else f"{m}是多少"
        if c.get("name"):
            q = f"{c['name']}:{q}"
        if q in seen:
            continue
        seen.add(q)
        out.append({"question": q, "cube_id": c["cube_id"], "chart": c.get("name"), "capability": "dataset_qa"})
        if len(out) >= limit:
            break
    if summary.get("charts"):
        out.append({"question": "解读这个仪表板的核心指标变化、异常波动和趋势", "capability": "insight"})
    return out
