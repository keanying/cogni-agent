"""智能小Q 能力编排 —— 只依赖 QuickBIClient,不碰数据库。

每个 run_* 都是 async generator,产出 events.py 定义的统一事件;service 层负责
落库、会话与鉴权,路由层负责把事件转成 SSE / JSON。

能力清单(capability):
    dataset_qa    小Q问数 · 数据集        问题 + cubeId(可省略,自动智能选表)
    file_qa       小Q问数 · 上传文件      问题 + 上传文件(copilot/parse 得到 fileId)
    dashboard_qa  小Q报表 · 仪表板问数    问题 + 仪表板(限定在仪表板用到的数据集里选表)
    insight       小Q解读                 问题 + 仪表板快照 / 上传文件 / 直接给数据文本
    report        小Q报告                 问题 + 可选引用文件(上传文件 / 仪表板快照转 CSV)
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from difflib import SequenceMatcher
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from cogni_agent.tool_center_service.quickbi import dashboard as dash
from cogni_agent.tool_center_service.quickbi import filesupport as fs
from cogni_agent.tool_center_service.quickbi.client import QuickBIClient, unwrap
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
from cogni_agent.tool_center_service.quickbi.events import (
    DatasetQaNormalizer, FileQaNormalizer, InterpretationNormalizer, ReportNormalizer, parse_report_events,
)

# label 是**给用户看的名字**,统一成四个字面:问数 / 报表 / 解读 / 报告。
# 不带「小Q」前缀,也不在名字里塞数据来源 —— 用户要的是「这次干了什么」,
# 至于走的是数据集还是上传文件,目标数据那一栏已经写着了,名字里再说一遍是噪音。
# 上游产品名保留在 product 字段(排障/对账时要能对上 Quick BI 的叫法),
# 来源差异留在 source 与 desc 里。
CAPABILITIES: dict[str, dict] = {
    "dataset_qa": {"label": "问数", "source": "数据集", "product": "小Q问数", "stream": True,
                   "desc": "对 Quick BI 数据集做自然语言问数;不指定数据集时自动智能选表"},
    "file_qa": {"label": "问数", "source": "上传文件", "product": "小Q问数", "stream": True,
                "desc": "对上传的 xls/xlsx/csv(≤5MB)做自然语言问数"},
    "dashboard_qa": {"label": "报表", "source": "仪表板", "product": "小Q报表", "stream": True,
                     "desc": "基于已有仪表板提问,在仪表板引用的数据集范围内智能选表后问数"},
    "insight": {"label": "解读", "source": "仪表板 / 上传文件", "product": "小Q解读", "stream": True,
                "desc": "对仪表板快照 / 上传文件做趋势、异常、归因解读"},
    "report": {"label": "报告", "source": "仪表板 / 上传文件", "product": "小Q报告", "stream": False,
               "desc": "异步生成专业分析报告(可引用上传文件或仪表板数据),产出报告回放链接"},
}

URI = {
    "cube_list": "/openapi/v2/smartq/query/llmCubeWithThemeList",
    "table_search": "/openapi/v2/smartq/tableSearch",
    "qa_stream": "/openapi/v2/smartq/queryByQuestionStream",
    "qa_sync": "/openapi/v2/smartq/queryByQuestion",
    "file_parse": "/openapi/v2/copilot/parse",
    "file_qa_stream": "/openapi/v2/smartq/queryByQuestionStreamByFile",
    "interpretation": "/openapi/v2/smartq/dataInterpretationStream",
    "snapshot": "/openapi/v2/snapshot/calling/shot",
    "dashboard_handle": "/openapi/v2/skills/dashboard/handle",
    "dashboard_query": "/openapi/v2/dashboard/query",
    "dataset_schema": "/openapi/v2/dataset/batchGetSchema",
    "dataportal_query": "/openapi/v2/dataportal/query",
    "report_upload": "/openapi/v2/qreport/uploadReferenceFile",
    "report_create": "/openapi/v2/smartq/createQreportChat",
    "report_data": "/openapi/v2/smartq/qreportChatData",
    "workspace_list": "/openapi/v2/workspace/list",
    "user_query": "/openapi/v2/organization/user/queryByAccount",
}

REPORT_REPLAY_PATH = "/copilot/qreportReplay?caseId={chat_id}"
TABLE_SEARCH_BATCH_SIZES = (30, 10)
INSIGHT_GUARD_PROMPT = (
    "【关键要求】:若输入数据中未包含用户提问的相关内容,则直接回复“不存在”,禁止随意捏造数据。"
    "若报告中包含相关数据,且用户问题有明确指向,则须在报告开头设置“用户问题解答”章节,"
    "针对问题作出直接、明确的回答,并予以标注。"
)
LLM_TABLE_SEARCH = "SYSTEM_deepseek-r1-0528"

Sleep = Callable[[float], Awaitable[Any]]


def status(stage: str, message: str, **extra) -> dict:
    return {"type": "status", "stage": stage, "message": message, **extra}


# ═══════════════════════════ 意图路由(对话场景) ═══════════════════════════

_REPORT_KW = ("报告", "报表生成", "复盘", "周报", "月报", "年报", "总结报告", "分析报告", "report")
_INSIGHT_KW = ("解读", "洞察", "趋势", "异常", "波动", "归因", "规律", "insight", "interpret", "trend", "anomal")


def route_capability(question: str, *, has_file: bool = False, has_dashboard: bool = False,
                     has_dataset: bool = False) -> str:
    """与官方 skill 的路由优先级一致:报告 > 解读 > 具体问数;默认数据集问数。"""
    q = (question or "").lower()
    if any(k in q for k in _REPORT_KW):
        return "report"
    if any(k in q for k in _INSIGHT_KW) and (has_file or has_dashboard):
        return "insight"
    if has_file:
        return "file_qa"
    if has_dashboard and not has_dataset:
        return "dashboard_qa"
    return "dataset_qa"


def detect_locale(question: str) -> str:
    """按提问句式判断语言:去掉引号里的实体名后,含中文即 zh_CN。"""
    stripped = re.sub(r"[「『\"'“‘《].*?[」』\"'”’》]", "", question or "")
    if re.search(r"[\u4e00-\u9fff]", stripped):
        return "zh_CN"
    if re.search(r"[A-Za-z]", stripped):
        return "en_US"
    return ""  # 判断不了(纯数字/符号)→ 由调用方回落到连接默认语言


# ═══════════════════════════ 基础查询 ═══════════════════════════

async def check_connection(qbi: QuickBIClient) -> dict:
    """连通性自检:签名 + user_token 权限一次性验证(拉取可问数数据集列表)。"""
    cubes = await list_cubes(qbi)
    return {"ok": True, "cube_count": len(cubes), "sample": cubes[:5]}


async def list_cubes(qbi: QuickBIClient) -> list[dict]:
    data = await qbi.get(URI["cube_list"], {"userId": qbi.user_id, "runningBySkill": "true"})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    mapping = (data.get("cubeIds") if isinstance(data, dict) else None) or {}
    themes = (data.get("themes") or data.get("themeList")) if isinstance(data, dict) else None
    out = [{"cube_id": cid, "cube_name": name} for cid, name in mapping.items()]
    if not out and isinstance(data, list):
        out = [{"cube_id": i.get("cubeId"), "cube_name": i.get("cubeName") or i.get("caption")}
               for i in data if isinstance(i, dict) and i.get("cubeId")]
    if themes and isinstance(themes, list):
        for o in out:
            o.setdefault("themes", [])
    return out


def _relevance(question: str, name: str) -> float:
    q, n = (question or "").lower(), (name or "").lower()
    if not n:
        return 0.0
    score = 100.0 if n in q else (80.0 if q and q in n else 0.0)
    m = SequenceMatcher(None, q, n).find_longest_match(0, len(q), 0, len(n))
    if m.size:
        score += m.size / max(len(n), 1) * 50.0
    if set(n):
        score += len(set(q) & set(n)) / len(set(n)) * 30.0
    return score


def rank_cubes(question: str, cubes: Sequence[dict]) -> list[dict]:
    return sorted(cubes, key=lambda c: _relevance(question, c.get("cube_name") or ""), reverse=True)


def match_cube_name(name: str, cubes: Sequence[dict]) -> dict:
    hint = (name or "").strip().lower()
    for c in cubes:
        if (c.get("cube_name") or "").strip().lower() == hint:
            return {"status": "exact", "cube": c}
    scored = [(s, c) for c in cubes if (s := _relevance(hint, c.get("cube_name") or "")) >= 10.0]
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return {"status": "not_found", "candidates": []}
    return {"status": "similar", "candidates": [{**c, "score": round(s, 1)} for s, c in scored[:3]]}


async def table_search(qbi: QuickBIClient, question: str, cube_ids: Sequence[str] | None = None) -> list[str]:
    payload: dict[str, Any] = {"userId": qbi.user_id, "userQuestion": question,
                               "llmNameForInference": LLM_TABLE_SEARCH, "runningBySkill": True}
    if cube_ids:
        payload["cubeIds"] = list(cube_ids)
    data = await qbi.post_json(URI["table_search"], payload)
    if isinstance(data, str) and data not in ("", "null"):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = []
    if isinstance(data, list):
        return [str(x.get("cubeId") if isinstance(x, dict) else x) for x in data if x]
    return []


async def resolve_cube(
    qbi: QuickBIClient, question: str, *,
    cube_name: str | None = None, candidates: Sequence[str] | None = None, restrict: bool = False,
    events: list | None = None,
) -> dict:
    """智能选表。返回 {cube_id, cube_name, how}。

    restrict=True(仪表板问数):只在 candidates 范围内选。
    """
    ev = events if events is not None else []
    accessible = await list_cubes(qbi)
    ev.append(status("cube_list", f"当前用户可问数的数据集 {len(accessible)} 个"))
    by_id = {c["cube_id"]: c for c in accessible}
    pool = accessible
    if restrict and candidates:
        pool = [by_id[c] for c in candidates if c in by_id]
        if not pool:
            # 仪表板用到的数据集当前用户都没开问数权限:直接用第一个,让服务端给出明确的权限错误
            first = candidates[0]
            ev.append(status("cube_select", "仪表板引用的数据集均未对当前用户开放问数权限,将直接尝试", cube_id=first))
            return {"cube_id": first, "cube_name": first, "how": "dashboard_fallback"}
        if len(pool) == 1:
            return {**pool[0], "how": "dashboard_single"}
    if not pool:
        raise QuickBIError("当前 user_token 没有任何可问数的数据集,请在 Quick BI 中为该用户开通数据集问数权限",
                           kind="business", code="NO_CUBE")
    if cube_name:
        m = match_cube_name(cube_name, pool)
        if m["status"] == "exact":
            return {**m["cube"], "how": "name_exact"}
        if m["status"] == "similar":
            ev.append(status("cube_select", f"未精确匹配数据集「{cube_name}」,相似候选:"
                             + "、".join(c["cube_name"] for c in m["candidates"])))
    ranked_ids = [c["cube_id"] for c in rank_cubes(question, pool)]
    if candidates and not restrict:
        ranked_ids = list(dict.fromkeys([c for c in candidates if c in by_id] + ranked_ids))
    matched: list[str] = []
    for size in TABLE_SEARCH_BATCH_SIZES:
        batch = ranked_ids[:size]
        if not batch:
            continue
        try:
            matched = await table_search(qbi, question, batch)
        except QuickBIError as exc:
            if "over limit" in str(exc):
                continue
            ev.append(status("cube_select", f"智能选表失败,改用名称相关性:{exc}"))
            matched = []
            break
        if matched:
            break
    if matched:
        cid = matched[0]
        return {"cube_id": cid, "cube_name": (by_id.get(cid) or {}).get("cube_name", cid), "how": "table_search",
                "alternatives": matched[1:5]}
    best = rank_cubes(question, pool)[0]
    return {**best, "how": "relevance"}


# ═══════════════════════════ 小Q问数 ═══════════════════════════

_CONTENT_TYPES = {"text", "chart", "reasoning", "sql", "code", "error"}


async def _guarded(raw_events: AsyncIterator[dict], norm, uri: str) -> AsyncIterator[dict]:
    """逐个归一化;遇到终止事件即停。流提前结束且没有任何有效内容时报协议错误,
    避免「网关返回空流 / 非 SSE 页面」被当作成功的空回答记录下来。"""
    produced = False
    async for raw in raw_events:
        for e in norm.feed(raw):
            produced = produced or e.get("type") in _CONTENT_TYPES
            yield e
        if norm.finished:
            return
    if not produced:
        raise QuickBIError(f"{uri} 事件流提前结束且没有返回内容(可能是网关中断或返回了非 SSE 响应)",
                           kind="protocol")

async def run_dataset_qa(
    qbi: QuickBIClient, *, question: str, cube_id: str | None = None, cube_name: str | None = None,
    cube_ids: Sequence[str] | None = None, restrict: bool = False, locale: str = "zh_CN",
) -> AsyncIterator[dict]:
    if not (question or "").strip():
        raise QuickBIError("问题不能为空", kind="input")
    if not cube_id:
        yield status("cube_select", "未指定数据集,正在智能选表…")
        pre: list[dict] = []
        picked = await resolve_cube(qbi, question, cube_name=cube_name, candidates=cube_ids, restrict=restrict,
                                    events=pre)
        for e in pre:
            yield e
        cube_id = picked["cube_id"]
        yield {"type": "meta", "cube_id": cube_id, "cube_name": picked.get("cube_name"), "cube_how": picked.get("how")}
        yield status("cube_select", f"已选定数据集:{picked.get('cube_name') or cube_id}", cube_id=cube_id)
    else:
        yield {"type": "meta", "cube_id": cube_id}
    payload = {
        "userQuestion": question, "userId": qbi.user_id, "cubeId": cube_id,
        "llmNameForData": "nvl", "llmNameForInference": "nvl",
        "runningBySkill": True, "locale": locale,
    }
    norm = DatasetQaNormalizer(question)
    yield status("querying", "问数中…")
    async for e in _guarded(qbi.stream(URI["qa_stream"], payload), norm, URI["qa_stream"]):
        yield e


def _validate(name: str, content: bytes) -> None:
    try:
        fs.validate_upload(name, len(content))
    except ValueError as exc:
        raise QuickBIError(str(exc), kind="input") from exc


async def upload_chat_file(qbi: QuickBIClient, name: str, content: bytes) -> dict:
    """copilot/parse:上传 Excel/CSV 并解析结构,返回 {file_id, detail}。"""
    if fs.suffix(name) not in fs.QA_SUFFIXES:
        raise QuickBIError("问数仅支持 xls/xlsx/csv 文件", kind="input")
    _validate(name, content)
    form = {"fileName": name, "isSave": "false", "fileId": "", "oApiUserId": qbi.user_id, "runningBySkill": "true"}
    files = {"file": (name, content, fs.MIME.get(fs.suffix(name), "application/octet-stream"))}
    data = await qbi.request("POST", URI["file_parse"], form=form, files=files, sign_params=None, timeout=120)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    file_id = (data or {}).get("fileId") if isinstance(data, dict) else None
    if not file_id:
        raise QuickBIError(f"文件上传成功但未返回 fileId:{str(data)[:300]}", kind="protocol")
    detail = {k: v for k, v in data.items() if k != "fileId"} if isinstance(data, dict) else {}
    return {"file_id": file_id, "detail": detail}


async def run_file_qa(qbi: QuickBIClient, *, question: str, file_id: str, locale: str = "zh_CN") -> AsyncIterator[dict]:
    if not file_id:
        raise QuickBIError("缺少 fileId,请先上传文件", kind="input")
    payload = {"fileId": file_id, "userId": qbi.user_id, "userQuestion": question,
               "runningBySkill": True, "locale": locale}
    norm = FileQaNormalizer(question)
    yield {"type": "meta", "file_id": file_id}
    yield status("querying", "文件问数中…")
    async for e in _guarded(qbi.stream(URI["file_qa_stream"], payload), norm, URI["file_qa_stream"]):
        yield e


# ═══════════════════════════ 小Q报表(仪表板) ═══════════════════════════

async def resolve_page(qbi: QuickBIClient, url_or_id: str) -> dict:
    """仪表板 URL / 数据门户 URL / pageId → {page_id, name?, portal?}"""
    s = (url_or_id or "").strip()
    if dash.is_dataportal_url(s):
        ids = dash.extract_dataportal_ids(s)
        data = await qbi.get(URI["dataportal_query"], {"dataPortalId": ids["productId"]})
        data = data if isinstance(data, dict) else {}
        menus = data.get("menu") or data.get("menus") or []
        if isinstance(menus, dict):
            menus = [menus]
        if not menus:
            raise QuickBIError("数据门户没有配置任何菜单", kind="input")
        menu = dash.select_menu(menus, ids.get("menuId"))
        if not menu:
            raise QuickBIError(f"数据门户中找不到菜单 {ids.get('menuId') or '(首页)'}", kind="input")
        page_id, name, message = dash.page_id_from_menu(menu)
        if not page_id:
            raise QuickBIError(message, kind="input")
        return {"page_id": page_id, "name": name, "portal": {"product_id": ids["productId"],
                "menu_id": menu.get("id"), "portal_name": data.get("name"), "menu_title": menu.get("title")}}
    try:
        return {"page_id": dash.extract_page_id(s)}
    except ValueError as exc:
        raise QuickBIError(str(exc), kind="input") from exc


async def precheck_dashboard(qbi: QuickBIClient, page_id: str) -> Any:
    """skills/dashboard/handle:确认当前用户对仪表板有权限并完成预处理。"""
    return await qbi.post_json(URI["dashboard_handle"], {"id": page_id, "userId": qbi.user_id, "runningBySkill": True})


async def fetch_dashboard(qbi: QuickBIClient, url_or_id: str, *, precheck: bool = True) -> dict:
    """一站式获取仪表板结构(图表/字段/查询控件/数据集),对应官方 fetch_dashboard_data。"""
    page = await resolve_page(qbi, url_or_id)
    page_id = page["page_id"]
    if precheck:
        await precheck_dashboard(qbi, page_id)
    raw = await qbi.get(URI["dashboard_query"], {"dashboardId": page_id, "viewType": "view", "queryFavorite": "true"})
    if not isinstance(raw, dict):
        raise QuickBIError("仪表板结构返回格式异常", kind="protocol")
    cube_ids = sorted({c.get("dataFromId") for c in raw.get("components") or [] if c.get("dataFromId")})
    schema_map: dict[str, dict] = {}
    if cube_ids:
        try:
            schemas = await qbi.post_json(URI["dataset_schema"], {"cubeIds": cube_ids})
        except QuickBIError:
            schemas = []
        if isinstance(schemas, list):
            pairs = [(i.get("cubeId") if isinstance(i, dict) else None, i) for i in schemas]
        elif isinstance(schemas, dict):
            pairs = [((v.get("cubeId") if isinstance(v, dict) else None) or k, v) for k, v in schemas.items()]
        else:
            pairs = []
        for cid, item in pairs:
            if not isinstance(item, dict):
                continue
            cs = ((item.get("data") or {}).get("cubeSchema")) or item.get("cubeSchema") or {}
            if cid:
                schema_map[cid] = {"cubeName": cs.get("caption") or item.get("cubeName") or cid,
                                   "fields": cs.get("fields") or []}
    summary = dash.parse_dashboard(raw, schema_map)
    summary["page_id"] = summary.get("page_id") or page_id
    summary["name"] = summary.get("name") or page.get("name") or ""
    summary["view_url"] = dash.standard_view_url(qbi.cfg.base, page_id)
    if page.get("portal"):
        summary["portal"] = page["portal"]
    summary["suggested_questions"] = dash.suggest_questions(summary)
    return summary


async def dashboard_snapshot(qbi: QuickBIClient, page_id: str, *, poll_interval: float = 3.0,
                             max_polls: int = 60, sleep: Sleep = asyncio.sleep) -> AsyncIterator[dict]:
    """snapshot/calling/shot 轮询:产出 status 事件,最后产出 {"type":"snapshot","markdown":...}。"""
    payload = {"worksId": page_id, "worksType": "dashboard", "userId": qbi.user_id, "targetType": "excel"}
    for attempt in range(1, max_polls + 1):
        data = await qbi.post_json(URI["snapshot"], payload)
        data = data if isinstance(data, dict) else {}
        st = str(data.get("status") or "").strip().lower()
        if st == "success":
            md = data.get("resultMarkdownText") or ""
            if not md:
                raise QuickBIError("仪表板快照为空(仪表板可能没有数据或查询控件必填未设置默认值)", kind="business")
            yield {"type": "snapshot", "markdown": md}
            return
        if st == "failed":
            raise QuickBIError(f"仪表板快照失败:{json.dumps(data.get('errorInfo') or {}, ensure_ascii=False)}",
                               kind="business")
        if st != "processing":
            raise QuickBIError(f"仪表板快照返回未知状态:{data.get('status')}", kind="protocol")
        yield status("snapshot", f"仪表板取数中(第 {attempt} 次轮询)…", attempt=attempt)
        await sleep(poll_interval)
    raise QuickBIError(f"仪表板快照超时({int(max_polls * poll_interval)} 秒)", kind="network")


# ─────────────── 多数据集问数(官方 SmartqQueryAbility · multipleCubeIds) ───────────────
#
# 调研结论(官方 skill quickbi-smartq-chat v1.3.0 + 阿里云 SmartqQueryAbility 文档):
# - `POST /openapi/v2/smartq/queryByQuestion`(同步)支持 `multipleCubeIds`(**JSON 数组**;官方 skill 脚本里
#   传的是逗号分隔字符串,线上会报 AE0510000038「Cannot construct instance of java.util.ArrayList」,
#   见运行 ec525170),
#   「模型从列表里选一张或多张表生成回答」;单表才用 cubeId;
# - 官方「仪表板问数」(小Q报表技能)按问题匹配图表 → 用该图表的数据集问;
#   **匹配不到具体图表时,把仪表板全部数据集 ID 作为 multipleCubeIds 一起问**。
# 我们以前多候选时先智能选表**只挑一份**再问:仪表板里明明有票务数据,
# 挑中的却是舆情那份 —— 答「不存在」或答非所问,就是这么来的。

MULTI_CUBE_LIMIT = 30   # 与智能选表批次上限同口径;再多先按名称相关度截断


async def run_multi_cube_qa(qbi: QuickBIClient, *, question: str, cube_ids: Sequence[str],
                            locale: str = "zh_CN", timeout: float = 600) -> AsyncIterator[dict]:
    """多数据集问数:一次调用,由 Quick BI 在这些数据集里自己选一张或多张表回答。"""
    if not (question or "").strip():
        raise QuickBIError("问题不能为空", kind="input")
    ids = list(dict.fromkeys(str(c) for c in cube_ids if c))
    if not ids:
        raise QuickBIError("多数据集问数至少需要一个数据集", kind="input")
    if len(ids) == 1:
        async for e in run_dataset_qa(qbi, question=question, cube_id=ids[0], locale=locale):
            yield e
        return
    if len(ids) > MULTI_CUBE_LIMIT:
        # 候选太多:按问题与数据集名称的相关度留前 N 个(名称拿不到的排在后面)
        try:
            names = {c["cube_id"]: c for c in await list_cubes(qbi)}
            pool = [names.get(i) or {"cube_id": i, "cube_name": ""} for i in ids]
            ids = [c["cube_id"] for c in rank_cubes(question, pool)][:MULTI_CUBE_LIMIT]
        except QuickBIError:
            ids = ids[:MULTI_CUBE_LIMIT]
        yield status("cube_select", f"候选数据集超过 {MULTI_CUBE_LIMIT} 个,按与问题的相关度取前 {MULTI_CUBE_LIMIT} 个")
    yield {"type": "meta", "cube_ids": ids, "cube_how": "multiple"}
    yield status("querying", f"多数据集问数中(候选 {len(ids)} 个数据集,由小Q选表)…", cube_count=len(ids))
    payload = {"userQuestion": question, "userId": qbi.user_id, "multipleCubeIds": list(ids)}  # 必须是数组,不能拼成逗号字符串
    data = await qbi.post_json(URI["qa_sync"], payload, timeout=timeout)
    events = multi_cube_events(data, question=question)
    for e in events:
        yield e
    if any(chart_has_values(e.get("chart") or {}) for e in events if e.get("type") == "chart"):
        return
    # 小Q只选了一张表,而那张表这一期恰好是空的(全是「-」)—— 运行 31427b3e:
    # 选中「供应商每日报表」,昨日一行「-」,解读如实答「不存在」;同一个问题逐个数据集取数
    # (运行 027eee3d)能在「销售报表」里取到 181 单 / 364 票。所以选中的表没数时,
    # 并发地逐个数据集再取一遍,把**有值**的结果补上。
    async for e in _fanout_cube_qa(qbi, question=question, cube_ids=ids[:FANOUT_LIMIT], locale=locale):
        yield e


FANOUT_LIMIT = 12        # 兜底逐个取数最多查几个数据集
FANOUT_CONCURRENCY = 4   # 同时在跑的问数请求数(别把 Quick BI 打出限流)


def chart_has_values(c: dict) -> bool:
    """图表的度量列有没有实际值(全是「-」/空 = 这一期没数据)。没标角色就看全部列。"""
    fields = [f for f in (c.get("fields") or []) if f.get("name") is not None]
    names = [str(f["name"]) for f in fields if f.get("role") == "metric"] or [str(f["name"]) for f in fields]
    return any(str(r.get(n, "")).strip() not in ("", "-", "None", "null")
               for r in (c.get("rows") or []) for n in names)


async def _fanout_cube_qa(qbi: QuickBIClient, *, question: str, cube_ids: Sequence[str],
                          locale: str) -> AsyncIterator[dict]:
    """逐个数据集问同一个问题(限并发),只把有值的图表往下发。"""
    yield status("fetching", f"小Q选中的表这一期没有数据,改为逐个数据集取数(共 {len(cube_ids)} 个)…",
                 cube_count=len(cube_ids))
    sem = asyncio.Semaphore(FANOUT_CONCURRENCY)

    async def one(cid: str) -> list[dict]:
        async with sem:
            got: list[dict] = []
            try:
                async for e in run_dataset_qa(qbi, question=question, cube_id=cid, locale=locale):
                    if e.get("type") == "chart" and chart_has_values(e.get("chart") or {}):
                        got.append({**e, "cube_id": cid})
                    elif e.get("type") == "sql":
                        got.append(e)
            except QuickBIError:
                pass          # 单个数据集失败不影响别的
            return got

    results = await asyncio.gather(*(one(c) for c in cube_ids))
    found = 0
    for evs in results:
        if any(e.get("type") == "chart" for e in evs):
            found += 1
            for e in evs:
                yield e
    yield status("fetching", f"逐个取数完成:{found}/{len(cube_ids)} 个数据集有这一期的数据", found=found)


def multi_cube_events(data: Any, *, question: str) -> list[dict]:
    """queryByQuestion 的同步返回 → 平台事件。

    返回结构与流式的 olapResult 同形(chartType / metaType / values / logicSql / conclusionText);
    多步场景结果在 dataList 里,每一步一张图。
    """
    from cogni_agent.tool_center_service.quickbi.events import chart_from_olap

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return [{"type": "text", "section": "answer", "delta": data}] if data.strip() else []
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    # dataList 的元素线上是 **JSON 字符串**(运行 31427b3e),不是对象 —— 以前按 dict 过滤直接全丢了
    steps = []
    for x in data.get("dataList") or []:
        if isinstance(x, str):
            try:
                x = json.loads(x)
            except ValueError:
                continue
        if isinstance(x, dict):
            steps.append(x)
    # 只有一步时顶层就是这一步的结果(顶层字段更全),用顶层
    steps = steps if len(steps) > 1 else [data]
    for i, step in enumerate(steps, 1):
        chart = chart_from_olap(step, question=question if len(steps) == 1 else f"{question}({i})")
        if chart and chart.get("rows"):
            out.append({"type": "chart", "chart": chart})
        if step.get("logicSql"):
            out.append({"type": "sql", "delta": str(step["logicSql"])})
    conclusion = str(data.get("conclusionText") or "").strip() or "\n".join(
        str(x.get("conclusionText") or "").strip() for x in steps[1:] if x.get("conclusionText"))
    if conclusion:
        out.append({"type": "text", "section": "answer", "delta": conclusion})
    if not any(e["type"] == "chart" for e in out) and not conclusion:
        out.append({"type": "text", "section": "answer",
                    "delta": "在所选数据集里没有查到和这个问题相关的数据。可以换个问法,或确认问题和数据集是同一类数据。"})
    return out


def _meaningful_chart_name(name: str) -> bool:
    n = str(name or "").strip()
    return len(n) >= 2 and not re.match(r"^[0-9a-f]{8}-", n, re.I) and not re.match(r"^(查询|图表|chart)\s*\d*$", n, re.I)


def match_chart_by_question(question: str, charts: Sequence[dict]) -> dict | None:
    """官方做法第一步:问题里点到了某张图表的名字 → 用这张图的数据集。取最长的那个命中。"""
    q = re.sub(r"\s+", "", question or "")
    best: dict | None = None
    for c in data_charts(charts):
        name = re.sub(r"\s+", "", _norm_title(c.get("name") or ""))
        if _meaningful_chart_name(name) and name in q and (
                best is None or len(name) > len(_norm_title(best.get("name") or ""))):
            best = c
    return best


def dashboard_candidate_cubes(charts: Sequence[dict] | None, chart_ids: Sequence[str] | None,
                              fallback: Sequence[str] | None = None) -> list[str]:
    """仪表板问数的候选数据集:所选组件(或全部组件)用到的数据集,按看板上的顺序去重。"""
    wanted = set(chart_ids or [])
    cs = [c for c in data_charts(charts) if not wanted or c.get("component_id") in wanted]
    ids = list(dict.fromkeys(c["cube_id"] for c in cs if c.get("cube_id")))
    return ids or list(dict.fromkeys(fallback or []))


async def run_dashboard_qa(qbi: QuickBIClient, *, question: str, dashboard: str | None = None,
                           cube_ids: Sequence[str] | None = None, cube_id: str | None = None,
                           charts: Sequence[dict] | None = None, chart_ids: Sequence[str] | None = None,
                           single_cube: bool = False, locale: str = "zh_CN") -> AsyncIterator[dict]:
    """仪表板问数(对齐官方「小Q报表技能」):

    1. 问题点到了某张图表的名字 → 用这张图的数据集问(单表,流式);
    2. 否则 → 仪表板(或所选组件)的全部数据集走 multipleCubeIds 多表问数,由小Q选一张或多张表;
    single_cube=True 保留老跑法(候选里智能选表只挑一份)—— 只给显式选了「挑一份数据集」的存量配置用。
    """
    if cube_id:
        async for e in run_dataset_qa(qbi, question=question, cube_id=cube_id, locale=locale):
            yield e
        return
    if charts is None and dashboard:
        yield status("dashboard", "解析仪表板结构…")
        summary = await fetch_dashboard(qbi, dashboard)
        charts = summary.get("charts") or []
        yield {"type": "meta", "page_id": summary.get("page_id"), "dashboard_name": summary.get("name"),
               "dashboard_cubes": [c["cube_id"] for c in summary.get("cubes") or []]}
        cube_ids = cube_ids or [c["cube_id"] for c in summary.get("cubes") or []]
    cids = dashboard_candidate_cubes(charts, chart_ids, cube_ids)
    if not cids:
        raise QuickBIError("该仪表板(所选组件)没有引用任何数据集,无法问数;可改用「解读」", kind="input")
    if single_cube:
        async for e in run_dataset_qa(qbi, question=question, cube_ids=cids, restrict=True, locale=locale):
            yield e
        return
    hit = match_chart_by_question(question, [c for c in (charts or [])
                                             if not chart_ids or c.get("component_id") in set(chart_ids)])
    if hit and hit.get("cube_id"):
        yield status("cube_select", f"问题对应图表「{hit.get('name')}」,用它的数据集问数",
                     component_id=hit.get("component_id"), cube_id=hit["cube_id"])
        async for e in run_dataset_qa(qbi, question=question, cube_id=hit["cube_id"], locale=locale):
            yield e
        return
    async for e in run_multi_cube_qa(qbi, question=question, cube_ids=cids, locale=locale):
        yield e


# ═══════════════════════════ 小Q解读 ═══════════════════════════

async def run_interpretation(qbi: QuickBIClient, *, question: str, markdown: str,
                             locale: str = "zh_CN") -> AsyncIterator[dict]:
    if not markdown or not markdown.strip():
        raise QuickBIError("没有可解读的数据", kind="input")
    if len(markdown) > fs.MAX_INSIGHT_CHARS:
        raise QuickBIError(f"数据量过大({len(markdown)} 字符,上限 {fs.MAX_INSIGHT_CHARS}),请缩小仪表板筛选范围或拆分文件",
                           kind="input")
    payload = {
        "stringData": base64.b64encode(markdown.encode("utf-8")).decode("utf-8"),
        "userQuestion": INSIGHT_GUARD_PROMPT + question,
        "oapiUserId": qbi.user_id, "locale": locale, "runningBySkill": True,
    }
    norm = InterpretationNormalizer()
    yield status("interpreting", "解读中…", data_chars=len(markdown))
    answer = ""
    async for e in _guarded(qbi.stream(URI["interpretation"], payload), norm, URI["interpretation"]):
        if e.get("type") == "text" and e.get("section", "answer") == "answer":
            answer += str(e.get("delta") or "")
        yield e
    # 「不存在」是我们自己的守卫提示词(INSIGHT_GUARD_PROMPT)要它在「数据里没有相关内容」时说的 ——
    # 不编数是对的,但只回三个字,用户不知道是没数据、问错了,还是系统坏了。
    # 现场:问「昨日票务销售情况」,绑的却是一块舆情看板,答「不存在」。
    # 补一句用的是哪份数据、里面有什么,换数据源还是换问法一眼就能判断。
    if answer.strip().rstrip("。.") == "不存在":
        yield {"type": "text", "section": "answer", "delta": no_related_data_note(markdown)}


NO_RELATED_DATA_MARK = "所用数据里没有和这个问题相关的内容"


def no_related_data_note(markdown: str, *, limit: int = 12) -> str:
    """说清「用的数据里有什么」:取 Markdown 里的表标题(## 开头)。"""
    heads = [ln[3:].strip() for ln in (markdown or "").splitlines() if ln.startswith("## ")]
    heads = [h for h in dict.fromkeys(heads) if h][:limit]
    have = f"它包含:{'、'.join(heads)}。" if heads else ""
    return (f"\n\n{NO_RELATED_DATA_MARK}。{have}"
            "请确认问题和所选数据源是同一类数据(比如票务问题要配票务看板 / 数据集),或换一个数据源再试。")


async def run_insight(qbi: QuickBIClient, *, question: str, markdown: str | None = None,
                      dashboard: str | None = None, page_id: str | None = None, locale: str = "zh_CN",
                      poll_interval: float = 3.0, sleep: Sleep = asyncio.sleep) -> AsyncIterator[dict]:
    """小Q解读。数据来源优先级:markdown(上传文件已转好) > 仪表板(pageId/URL)。"""
    if not markdown:
        if not page_id:
            if not dashboard:
                raise QuickBIError("解读需要数据来源:上传文件,或仪表板 pageId / 地址", kind="input")
            page = await resolve_page(qbi, dashboard)
            page_id = page["page_id"]
        yield {"type": "meta", "page_id": page_id}
        yield status("snapshot", "正在拉取仪表板快照…")
        async for e in dashboard_snapshot(qbi, page_id, poll_interval=poll_interval, sleep=sleep):
            if e["type"] == "snapshot":
                markdown = e["markdown"]
                yield status("snapshot", f"快照完成,共 {len(markdown)} 字符")
            else:
                yield e
    async for e in run_interpretation(qbi, question=question, markdown=markdown or "", locale=locale):
        yield e


# ─────────────── 解读 · 仪表板「图表范围」(全选 / 勾选部分图表) ───────────────
#
# Quick BI 自己的「智能解读」是先让人确认图表范围(默认全选)再解读的。我们原来只做了
# 「整块仪表板快照 → 解读」,两个缺口:
#   1. 选不了图表 —— 只想解读其中几张做不到;
#   2. 快照只覆盖**当前页签**:带页签的看板(九龙潭游客画像 / 大金湖游客画像 / 舆情 三个页签)
#      一次快照只拿到第一个页签的十几张图,其余页签的图表根本没进解读 —— 而且不报错。
# 这里按图表来:快照里有的直接用;快照里没有的(别的页签),按该图表的数据集 + 维度/度量
# 走一次问数补取数据;补不到的明确告诉用户是哪几张。

# 不是「图表」的组件:容器 / 布局 / 装饰。它们在结构里也占一行,但没有数据
# 注意别误伤 table / cross-table(明细表、交叉表)这类真图表,所以 tab / text / image 是整词匹配
_NON_CHART = re.compile(r"(container$|^tab$|^tabs$|^text$|^query2$|iframe|^image$|^picture$|^layout|divider|blank)", re.I)


def is_data_chart(c: dict) -> bool:
    """结构里真正带数据的图表:有数据集,且不是容器类组件。"""
    return bool(c.get("cube_id")) and not _NON_CHART.search(str(c.get("chart_type") or ""))


def data_charts(charts: Sequence[dict] | None) -> list[dict]:
    return [c for c in (charts or []) if is_data_chart(c)]


def _norm_title(t: str) -> str:
    """快照的表标题会带序号后缀(「情绪占比4」「词云8」),名字比对前去掉空白和尾部数字。"""
    return re.sub(r"\d+$", "", re.sub(r"\s+", "", str(t or ""))).strip()


def split_snapshot(markdown: str) -> list[tuple[str, str]]:
    """快照 Markdown → [(标题, 整段)],按 `## ` 切。标题前的零碎内容并进第一段。"""
    out: list[tuple[str, str]] = []
    cur_title: str | None = None
    cur: list[str] = []
    for ln in (markdown or "").splitlines():
        if ln.startswith("## "):
            if cur_title is not None or any(x.strip() for x in cur):
                out.append((cur_title or "", "\n".join(cur).strip()))
            cur_title, cur = ln[3:].strip(), [ln]
        else:
            cur.append(ln)
    if cur_title is not None or any(x.strip() for x in cur):
        out.append((cur_title or "", "\n".join(cur).strip()))
    return out


def match_snapshot(sections: list[tuple[str, str]], charts: Sequence[dict]) -> tuple[dict[str, str], list[int]]:
    """把快照段落对到图表上:先比原名,再比去掉序号后的名字。返回 ({component_id: 段落}, 已用段落下标)。"""
    used: set[int] = set()
    hit: dict[str, str] = {}
    for exact in (True, False):
        for c in charts:
            cid = c.get("component_id")
            name = str(c.get("name") or "")
            if not cid or cid in hit or not name:
                continue
            for i, (title, body) in enumerate(sections):
                if i in used:
                    continue
                if (title == name) if exact else (_norm_title(title) == _norm_title(name) and _norm_title(name)):
                    hit[cid] = body
                    used.add(i)
                    break
    return hit, sorted(used)


def _cap_table_rows(section: str, max_rows: int) -> str:
    """一段 Markdown 表只留前 max_rows 行数据(表头、分隔行不算),多出来的注明总行数。"""
    lines = section.splitlines()
    table = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("|")]
    if len(table) <= max_rows + 2:
        return section
    keep_until = table[0] + 2 + max_rows
    dropped = len(table) - 2 - max_rows
    kept = [ln for i, ln in enumerate(lines) if i < keep_until or i not in table]
    return "\n".join(kept) + f"\n\n(本表共 {len(table) - 2} 行,为控制解读长度只列前 {max_rows} 行,省略 {dropped} 行)"


def fit_sections(sections: list[str], limit: int) -> tuple[str, int | None]:
    """拼成一份给解读的 Markdown;超过上限就逐级压每张表的行数(200→100→50→20→10)。

    返回 (markdown, 用到的行数上限或 None)。压到 10 行还超,原样返回,由解读那一步报「数据量过大」。
    """
    md = "\n\n".join(x for x in sections if x)
    if len(md) <= limit:
        return md, None
    for cap in (200, 100, 50, 20, 10):
        md = "\n\n".join(_cap_table_rows(x, cap) for x in sections if x)
        if len(md) <= limit:
            return md, cap
    return md, 10


async def run_dashboard_insight(
    qbi: QuickBIClient, *, question: str, page_id: str, charts: Sequence[dict] | None,
    chart_ids: Sequence[str] | None = None, locale: str = "zh_CN",
    poll_interval: float = 3.0, sleep: Sleep = asyncio.sleep, max_chars: int | None = None,
) -> AsyncIterator[dict]:
    """解读 · 仪表板(按图表范围)。chart_ids 为空 = 全选。

    1. 快照(官方解读的取数方式)—— 但它只导出当前页签;
    2. 快照没覆盖到的组件:把它们用到的数据集**按用户的问题一次多表取数**(multipleCubeIds),
       取回的数据排在最前(它是按问题取的,最相关);
    3. 两部分合起来交给解读。
    v3.55 的做法是对缺的图表逐张问数补取 —— 现场 78 张补了 187 秒、失败 50 张,
    票务的图一张没进解读,模型如实答「不存在」。一次按问题的多表取数替掉了它。
    """
    all_charts = data_charts(charts)
    wanted = set(chart_ids or [])
    selected = [c for c in all_charts if not wanted or c.get("component_id") in wanted]
    if wanted and not selected:
        raise QuickBIError("所选图表在这块仪表板里都找不到了(看板可能改过),请刷新资产后重新选择", kind="input")
    scope = "全部" if not wanted else "所选"
    yield {"type": "meta", "page_id": page_id, "chart_scope": {
        "mode": "all" if not wanted else "selected", "selected": len(selected), "total": len(all_charts)}}
    yield status("snapshot", f"正在拉取仪表板快照({scope} {len(selected)} 张图表)…")
    snapshot = ""
    async for e in dashboard_snapshot(qbi, page_id, poll_interval=poll_interval, sleep=sleep):
        if e["type"] == "snapshot":
            snapshot = e["markdown"]
        else:
            yield e
    sections = split_snapshot(snapshot)
    hit, used = match_snapshot(sections, selected)
    extra: list[str] = []
    if not wanted:
        # 全选:快照里对不上名字的段落也是这块看板的内容,照样带上
        extra = [body for i, (_, body) in enumerate(sections) if i not in used]
    missing = [c for c in selected if c.get("component_id") not in hit]
    cubes = list(dict.fromkeys(c["cube_id"] for c in missing if c.get("cube_id")))
    yield status("snapshot", f"快照覆盖 {len(hit)} 张图表"
                 + (f",另有 {len(missing)} 张不在快照里(通常在其它页签),涉及 {len(cubes)} 个数据集" if missing else ""),
                 covered=len(hit), missing=len(missing))

    fetched: list[dict] = []
    fetch_error: str | None = None
    if cubes:
        yield status("fetching", f"按问题对这 {len(cubes)} 个数据集做一次多表取数…", cube_count=len(cubes))
        try:
            async for e in run_multi_cube_qa(qbi, question=question, cube_ids=cubes, locale=locale):
                et = e.get("type")
                if et == "chart":
                    fetched.append(e.get("chart") or {})
                    yield e
                elif et == "text":
                    d = str(e.get("delta") or "")   # 流式问数是逐字下发的,每段后面补换行会变成一字一行
                    if d:   # 取数这一步的文字只是中间产物,进思考过程
                        yield {"type": "reasoning", "delta": d, "channel": "thinking"}
                elif et == "error":
                    fetch_error = str(e.get("message") or e.get("data") or "取数失败")
                elif et in ("done", "finish") or (et == "status" and e.get("stage") == "finish"):
                    continue
                else:
                    yield e
        except QuickBIError as exc:
            fetch_error = str(exc)
        if fetch_error and not fetched:
            yield status("fetching", f"快照外的数据集取数失败,只用快照里的数据解读:{fetch_error}")

    ordered = ([charts_to_markdown(fetched)] if fetched else []) \
        + [hit[c["component_id"]] for c in selected if c.get("component_id") in hit] + extra
    limit = int(max_chars or fs.MAX_INSIGHT_CHARS)
    markdown, capped = fit_sections(ordered, limit)
    if not markdown.strip():
        raise QuickBIError("所选图表一张都没有取到数据,无法解读(仪表板可能没有数据,或查询控件必填未设默认值)",
                           kind="business")
    if capped:
        yield status("fitting", f"数据较多,每张表只保留前 {capped} 行交给解读", max_rows=capped)
    yield status("snapshot", f"共 {len(ordered)} 段数据、{len(markdown)} 字符,开始解读")
    async for e in run_interpretation(qbi, question=question, markdown=markdown, locale=locale):
        yield e
    if fetch_error and not fetched and missing:
        yield {"type": "text", "section": "answer",
               "delta": f"\n\n(说明:看板上另有 {len(missing)} 张图表不在快照里,按问题补取数据失败,"
                        f"本次解读只用了快照里的 {len(hit)} 张)"}


def charts_to_markdown(charts: Sequence[dict], *, max_rows: int = 500) -> str:
    """问数取回的图表(fields + rows)→ 给解读用的 Markdown 表。只拼数据,不拼图表配置。"""
    parts: list[str] = []
    for i, c in enumerate(charts, 1):
        fields = [str(f.get("name")) for f in (c.get("fields") or []) if f.get("name") is not None]
        rows = c.get("rows") or []
        if not fields or not rows:
            continue
        title = str(c.get("title") or f"查询结果 {i}")
        lines = [f"## {title}", "", "| " + " | ".join(fields) + " |",
                 "| " + " | ".join("---" for _ in fields) + " |"]
        for r in rows[:max_rows]:
            lines.append("| " + " | ".join(str(r.get(f, "")) for f in fields) + " |")
        if len(rows) > max_rows:
            lines += ["", f"(共 {len(rows)} 行,这里只列前 {max_rows} 行)"]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def run_dataset_insight(qbi: QuickBIClient, *, question: str, cube_id: str | None = None,
                              cube_ids: Sequence[str] | None = None, restrict: bool = False,
                              locale: str = "zh_CN") -> AsyncIterator[dict]:
    """解读 · 数据集:先按问题从数据集里**取数**(小Q问数),再把取回的数据交给解读。

    解读接口本身只吃「一段数据文本」,不认数据集。以前指定了数据集(或问题上绑的是数据集)
    的解读直接报「解读需要数据来源」—— 而数据就在那个数据集里。
    用问数取数而不是整表导出:数据集动辄几十万行,解读有字符上限;问数按问题只取相关的那部分。

    取数这一步的文字回答只进思考过程(它是中间产物,不是给用户的结论);
    取回的图照常下发 —— 解读说的数,用户能在图上对得上。
    """
    charts: list[dict] = []
    failed: str | None = None
    yield status("fetching", "正在从数据集取数…")
    multi = [c for c in (cube_ids or []) if c]
    # 多选了数据集:一次多表取数(multipleCubeIds),不再先挑一份
    src = (run_multi_cube_qa(qbi, question=question, cube_ids=multi, locale=locale)
           if not cube_id and len(multi) > 1 else
           run_dataset_qa(qbi, question=question, cube_id=cube_id, cube_ids=cube_ids, restrict=restrict,
                          locale=locale))
    async for e in src:
        et = e.get("type")
        if et == "chart":
            charts.append(e.get("chart") or {})
            yield e
        elif et == "text":
            d = str(e.get("delta") or "")   # 流式问数是逐字下发的,每段后面补换行会变成一字一行
            if d:
                yield {"type": "reasoning", "delta": d, "channel": "thinking"}
        elif et == "error":
            failed = str(e.get("message") or e.get("data") or "取数失败")
            yield e
        elif et in ("done", "finish") or (et == "status" and e.get("stage") == "finish"):
            continue                      # 取数结束不是整轮结束,后面还有解读
        else:
            yield e
    if failed:
        return
    markdown = charts_to_markdown(charts)
    if not markdown:
        raise QuickBIError("按这个问题在数据集里没有取到数据,无法解读。"
                           "可以换个问法,或确认问题和所选数据集是不是同一类数据", kind="business")
    yield status("fetching", f"取数完成,{sum(len(c.get('rows') or []) for c in charts)} 行,开始解读")
    async for e in run_interpretation(qbi, question=question, markdown=markdown, locale=locale):
        yield e


# ═══════════════════════════ 小Q报告 ═══════════════════════════

_ICON = {"doc": "word", "docx": "word", "xls": "excel", "xlsx": "excel", "csv": "excel", "pdf": "pdf"}


async def upload_report_file(qbi: QuickBIClient, name: str, content: bytes) -> dict:
    """qreport/uploadReferenceFile → {fileId, fileName, fileType}"""
    if fs.suffix(name) not in fs.REPORT_SUFFIXES:
        raise QuickBIError("报告引用文件仅支持 doc/docx/xls/xlsx/csv", kind="input")
    _validate(name, content)
    data = await qbi.request(
        "POST", URI["report_upload"], form={"chatType": "manus", "userId": qbi.user_id},
        files={"file": (name, content, fs.MIME.get(fs.suffix(name), "application/octet-stream"))},
        sign_params=None, timeout=120,
    )
    if isinstance(data, dict) and isinstance(data.get("data"), dict) and data["data"].get("fileId"):
        data = data["data"]
    if not isinstance(data, dict) or not all(data.get(k) for k in ("fileId", "fileName", "fileType")):
        raise QuickBIError(f"报告引用文件上传返回不完整:{str(data)[:300]}", kind="protocol")
    return {"fileId": str(data["fileId"]), "fileName": str(data["fileName"]), "fileType": str(data["fileType"])}


def _attachment(uploads: Sequence[dict]) -> str:
    files = []
    for u in uploads:
        ft = u.get("fileType", "")
        files.append({"fileId": u.get("fileId", ""), "fileType": ft, "iconType": _ICON.get(ft, ft),
                      "file": {"name": f"{u.get('fileName', '')}{'.' + ft if ft else ''}"},
                      "fileName": u.get("fileName", "")})
    return json.dumps({"resource": {"files": files, "pages": [], "cubes": [], "dashboardFiles": []},
                       "useOnlineSearch": True}, ensure_ascii=False)


_RUNNING = re.compile(r"当前用户已有运行中的任务.*?问题[::]\s*(.+?)[,,]\s*chatId[::]\s*([a-zA-Z0-9\-]{1,64})")


def report_url(base: str, chat_id: str) -> str:
    return base.rstrip("/") + REPORT_REPLAY_PATH.format(chat_id=chat_id)


async def create_report(qbi: QuickBIClient, *, question: str, uploads: Sequence[dict] = (),
                        locale: str = "zh_CN", web_search: bool = True, chat_id: str | None = None) -> dict:
    """createQreportChat(异步)。返回 {chat_id, message_id, reused, running_question}。"""
    if not (question or "").strip():
        raise QuickBIError("报告主题不能为空", kind="input")
    chat_id = chat_id or str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    resources = [{"id": u["fileId"], "title": u["fileName"], "type": u["fileType"]} for u in uploads]
    payload = {
        "async": True, "chatId": chat_id, "messageId": message_id, "oapiUserId": qbi.user_id,
        "reGenerate": True, "needReplay": True, "userQuestion": question, "needWebSearch": bool(web_search),
        "autoAcceptedPlan": True, "runningBySkill": True, "resources": resources, "interruptFeedback": "",
        "messages": [{"role": "user", "content": question}],
        "attachment": _attachment(uploads),
        "bizArgs": {"qbiHost": qbi.cfg.base, "qbiLocale": locale},
    }
    text = await qbi.request("POST", URI["report_create"], json_body=payload, sign_params=None, raw=True)
    text = (text or "").strip()
    running = _RUNNING.search(text)
    if running:  # 与官方 skill 一致:先识别「已有运行中的任务」,不管外层是不是 success=false
        return {"chat_id": running.group(2).strip()[:64], "message_id": message_id, "reused": True,
                "running_question": running.group(1).strip(), "notice": text}
    try:
        body = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        inner = unwrap(body, uri=URI["report_create"])  # success=false 在这里抛出
        text = inner if isinstance(inner, str) else (json.dumps(inner, ensure_ascii=False) if inner else "")
    elif isinstance(body, str):
        text = body
    m = _RUNNING.search(text or "")
    if m:
        return {"chat_id": m.group(2).strip()[:64], "message_id": message_id, "reused": True,
                "running_question": m.group(1).strip(), "notice": text}
    final = (text or "").strip().strip('"').strip("'")
    if not re.fullmatch(r"[a-zA-Z0-9\-]{8,64}", final or ""):
        final = chat_id
    return {"chat_id": final, "message_id": message_id, "reused": False}


# 报告事件接口返回的是累积全量,报告越长越慢;通用 60 秒超时在这里明显不够
REPORT_FETCH_TIMEOUT = 180.0


async def fetch_report_raw(qbi: QuickBIClient, chat_id: str, *, timeout: float | None = None,
                           attempts: int = 3) -> str:
    """拉一次报告的累积事件。

    这个接口返回的是**报告到目前为止的全部事件**,报告越长响应越大,60 秒的通用超时
    很容易不够 —— 于是表现成「请求 Quick BI 超时: GET /openapi/v2/smartq/qreportChatData」。
    所以给它单独放宽超时,并对网络类错误自带几次退避重试:
    超时是「这一次没拉到」,不是「报告失败了」,不该让调用方据此把任务判死。
    """
    params = {"chatId": chat_id, "userId": qbi.user_id}
    last: QuickBIError | None = None
    for i in range(max(1, attempts)):
        try:
            return await qbi.request("GET", URI["report_data"], params=params, raw=True,
                                     timeout=timeout or REPORT_FETCH_TIMEOUT)
        except QuickBIError as exc:
            if not exc.retryable:
                raise
            last = exc
            if i + 1 < attempts:
                await asyncio.sleep(2 * (i + 1))
    raise last if last else QuickBIError("拉取报告进度失败", kind="network")


def summarize_report(raw_text: str, *, base: str, chat_id: str, since: int = 0) -> dict:
    """解析报告累积事件。since = 已处理事件数(流式增量用)。"""
    all_events = parse_report_events(raw_text)
    norm = ReportNormalizer()
    new: list[dict] = []
    for idx, ev in enumerate(all_events):
        out = norm.feed(ev)
        if idx >= since:
            new.extend(out)
    done = norm.finished
    return {
        "events": new,
        "event_count": len(all_events),
        "finished": done,
        "success": done and norm.success and not norm.error,
        "error": norm.error,
        "usage": norm.usage,
        "report_url": report_url(base, chat_id) if (done and norm.success and not norm.error) else None,
    }


async def run_report(qbi: QuickBIClient, *, question: str, uploads: Sequence[dict] = (), locale: str = "zh_CN",
                     web_search: bool = True, poll_interval: float = 10.0, max_wait: float = 1800.0,
                     chat_id: str | None = None, sleep: Sleep = asyncio.sleep) -> AsyncIterator[dict]:
    """创建报告(或接续已有 chat_id)并轮询到结束。"""
    if chat_id is None:
        yield status("report_create", "正在创建报告任务…")
        created = await create_report(qbi, question=question, uploads=uploads, locale=locale, web_search=web_search)
        chat_id = created["chat_id"]
        if created.get("reused"):
            running_q = (created.get("running_question") or "").strip()
            mine = (question or "").strip()
            # 「接续」只有在那个正在跑的任务**就是这一条**时才成立(重复点同一个问题)。
            # 跑的是别的问题却去跟踪它,拿回来的是别人的报告 —— 而且那份报告一结束,
            # 这一边收到的是一个对不上的状态,报一句「报告异步生成状态已更新,请刷新页面重试」。
            # 现场三条主题报告在 3 秒内先后发起、全部几秒内失败,就是这么来的。
            if running_q and mine and running_q[:40] != mine[:40]:
                raise QuickBIError(
                    f"该 Quick BI 账号已有一个报告正在生成(问题:{running_q[:40]}),"
                    "小Q 同一账号同时只能跑一个报告。请等它完成后再发起这一条"
                    "—— 接续到那个任务上的话,拿回来的会是别人的报告。",
                    kind="business", code="REPORT_BUSY_UPSTREAM")
            yield status("report_create", f"该用户已有运行中的报告任务(问题:{created.get('running_question')}),"
                                          "已切换到该任务继续跟踪", reused=True)
    yield {"type": "report", "chat_id": chat_id, "finished": False, "report_url": None}
    async for e in poll_report(qbi, chat_id, poll_interval=poll_interval, max_wait=max_wait, sleep=sleep):
        yield e


async def poll_report(qbi: QuickBIClient, chat_id: str, *, since: int = 0, poll_interval: float = 10.0,
                      max_wait: float = 1800.0, sleep: Sleep = asyncio.sleep) -> AsyncIterator[dict]:
    waited = 0.0
    idle_hint = False
    while True:
        raw = await fetch_report_raw(qbi, chat_id)
        s = summarize_report(raw, base=qbi.cfg.base, chat_id=chat_id, since=since)
        if s["event_count"] < since:   # 服务端重置了累积结果
            since = 0
            continue
        for e in s["events"]:
            yield e
        if s["events"]:
            idle_hint = False
        since = s["event_count"]
        if s["finished"]:
            yield {"type": "report", "chat_id": chat_id, "finished": True, "success": s["success"],
                   "report_url": s["report_url"], "error": s["error"], "event_count": since}
            return
        if waited >= max_wait:
            raise QuickBIError(f"报告生成超时(已等待 {int(waited // 60)} 分钟),可稍后通过 chat_id 查询结果",
                               kind="network", code="REPORT_TIMEOUT")
        if not s["events"] and not idle_hint:
            yield status("report_generating", "报告生成中,请稍候…", event_count=since)
            idle_hint = True
        yield {"type": "report_progress", "chat_id": chat_id, "event_count": since}
        await sleep(poll_interval)
        waited += poll_interval


def _chart_table(c: dict) -> tuple[str, list[list[str]]] | None:
    """问数取回的图表 → (标题, 行);全是「-」/空的当没取到(昨日数据没出时 Quick BI 就回一行「-」)。"""
    fs_ = [f for f in (c.get("fields") or []) if f.get("name") is not None]
    fields = [str(f.get("name")) for f in fs_]
    rows = [[("" if r.get(f) is None else str(r.get(f))) for f in fields] for r in (c.get("rows") or [])]
    # 看度量列有没有值(维度列「31-45岁」这种有字不算);没标角色就看全部列
    measures = [i for i, f in enumerate(fs_) if f.get("role") == "metric"] or list(range(len(fields)))
    if not fields or not any(r[i] not in ("", "-") for r in rows for i in measures):
        return None
    return str(c.get("title") or "查询结果"), [fields] + rows


async def theme_report_tables(qbi: QuickBIClient, *, domain: str, snapshot_md: str,
                              charts: Sequence[dict] | None, locale: str = "zh_CN",
                              limit: int = fs.REPORT_REF_LIMIT) -> AsyncIterator[dict]:
    """主题解读报告的素材:只要本主题的图表数据。最后产出 {"type": "tables", "tables": [(标题, 行)]}。

    1. 快照里标题命中本主题关键词的表;
    2. 快照只导出**当前页签**(运行 5b37e479:88 张图的大看板,快照里只有舆情那一页),
       本主题的图表不在快照里时,按图表名对它们的数据集逐张取数(最多 limit 张,不是全看板);
    3. 两边都没有 → 一张不给,由调用方报「看板上没有本主题的图表」。
       **绝不拿别的主题的表凑数** —— 客群报告拿到舆情那一页的表,写出来的就是
       「客流热度…评论量峰值 284 条」,APP 客流画像卡片上显示的正是这一句。
    """
    tables = [t for t in fs.pick_theme_tables(fs.markdown_tables(snapshot_md), domain, limit)
              if fs.theme_hits(t[0], domain)]
    have = {_norm_title(t[0]) for t in tables}
    todo = [c for c in data_charts(charts)
            if fs.theme_hits(str(c.get("name") or ""), domain) and c.get("cube_id")
            and _norm_title(str(c.get("name") or "")) not in have]
    todo.sort(key=lambda c: -fs.theme_hits(str(c.get("name") or ""), domain))
    room = max(0, limit - len(tables))
    if tables:
        yield status("snapshot", f"快照里有 {len(tables)} 张「{domain}」相关图表")
    if todo and room:
        yield status("fetching", f"另有 {len(todo)} 张「{domain}」图表不在快照里(在其它页签),"
                                 f"按图表逐张取数(最多 {room} 张)…")
    for c in todo:
        if room <= 0:
            break
        name = str(c.get("name") or "")
        got = None
        try:
            async for e in run_multi_cube_qa(qbi, question=name, cube_ids=[c["cube_id"]], locale=locale):
                if e.get("type") == "chart" and got is None:
                    got = _chart_table({**(e.get("chart") or {}), "title": name})
        except QuickBIError as exc:
            yield status("fetching", f"「{name}」取数失败,跳过:{exc}")
            continue
        if got:
            tables.append(got)
            room -= 1
    yield {"type": "tables", "tables": tables}


async def report_uploads_from_markdown(qbi: QuickBIClient, markdown: str, *, base_name: str,
                                      domain: str | None = None) -> list[dict]:
    """仪表板快照 → CSV → 报告引用文件(让小Q报告基于已有面板数据生成)。

    domain:主题解读的数据域,给了就只挑本主题的图表(见 filesupport.pick_theme_tables)。
    """
    uploads = []
    for name, data in fs.markdown_to_csv_files(markdown, base_name=base_name, domain=domain):
        uploads.append(await upload_report_file(qbi, name, data))
    return uploads


async def report_uploads_from_tables(qbi: QuickBIClient, tables: Sequence, *, base_name: str) -> list[dict]:
    """(标题, 行) 列表 → CSV → 报告引用文件。"""
    uploads = []
    for name, data in fs.tables_to_csv_files(tables, base_name=base_name):
        uploads.append(await upload_report_file(qbi, name, data))
    return uploads
