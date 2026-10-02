"""开放平台 · Quick BI 智能小Q(/v1/quickbi/*,API Key 鉴权)。

鉴权:Authorization: Bearer sk-zhilv-xxxx(管理后台「API Key」创建),走数据库真实校验
(吊销/过期)。可用连接 = api_enabled 的连接中:集团级 Key 全部可用;景区 Key 仅本景区或 public。

所有执行接口支持 stream(SSE,事件 = events.py 统一事件,event 名即 type)。
非流式返回运行记录:{id, status, answer, result:{charts, sql, reasoning, ...}, report_url, ...}。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from pydantic import BaseModel, Field

from cogni_agent.common.errors import ValidationError
from cogni_agent.tool_center_service.quickbi import service as svc
from cogni_agent.tool_center_service.quickbi.http_helpers import collect, guard, primed, read_upload, sse

router = APIRouter(prefix="/v1/quickbi", tags=["开放平台 · Quick BI"])


async def api_actor(authorization: str = Header(default=""),
                    x_end_user: str | None = Header(default=None)) -> svc.Actor:
    from cogni_agent.gateway_service.api_key_service import validate_api_key

    raw = authorization.removeprefix("Bearer ").strip()
    info = await validate_api_key(raw)
    if info.get("allowed_agent_ids"):
        from cogni_agent.common.errors import AgentNotAllowed

        raise AgentNotAllowed("该 API Key 限定了可调用的 Agent,不能调用 Quick BI 开放接口;请使用未限定 Agent 的 Key")
    user = f"apikey:{info['key_id']}" + (f":{x_end_user[:40]}" if x_end_user else "")
    return svc.Actor(user_id=user[:64], scenic_id=info.get("scenic_id") or None, role="api", source="api",
                     api_key_id=info["key_id"])


@router.get("/capabilities")
async def capabilities(actor: svc.Actor = Depends(api_actor)):
    o = svc.overview()
    return {"capabilities": o["capabilities"], "scenarios": o["scenarios"], "limits": o["limits"]}


@router.get("/connections")
async def connections(actor: svc.Actor = Depends(api_actor)):
    views = await svc.list_connections(actor)
    return {"items": [{"id": v.id, "name": v.name, "server_domain": v.cfg.base, "is_default": v.is_default,
                       "default_locale": v.cfg.locale} for v in views]}


@router.get("/datasets")
async def datasets(connection_id: str | None = None, keyword: str | None = None,
                   actor: svc.Actor = Depends(api_actor)):
    return {"items": await guard(svc.list_cubes(actor, connection_id, keyword))}


@router.get("/assets")
async def assets(connection_id: str | None = None, kind: str | None = None, scenic_id: str | None = None,
                 actor: svc.Actor = Depends(api_actor)):
    """已登记的仪表板 / 数据集(集团级 Key 可见全部,景区 Key 仅本景区)。

    scenic_id:只看某个景区的资产(集团级资产始终一并返回,它们对每个景区都适用)。
    """
    return {"items": await svc.list_assets(actor, connection_id, kind, scenic_id=scenic_id)}


@router.post("/files")
async def upload(file: UploadFile = File(...), connection_id: str | None = Form(default=None),
                 actor: svc.Actor = Depends(api_actor)):
    name, content = await read_upload(file)
    return await guard(svc.save_file(actor, connection_id or None, name, content))


class QueryReq(BaseModel):
    question: str = ""
    connection_id: str | None = None
    capability: str | None = Field(default=None, description="dataset_qa/file_qa/dashboard_qa/insight/report;空=自动路由")
    target: dict = Field(default_factory=dict, description="cube_id/cube_name/asset_id/page_id/dashboard/file_id/data")
    locale: str | None = None
    stream: bool = False
    scenic_id: str | None = Field(default=None, description="本次提问归属的景区;不传按 API Key 所属景区。"
                                                            "没指定 target 时按该景区挑已登记的仪表板/数据集")
    fixed_question_id: str | None = Field(default=None, description="按固定问题发起:只传 id 即可,"
                                                                    "问题文本/能力/目标数据从该条配置带出")


@router.post("/query")
async def query(req: QueryReq, actor: svc.Actor = Depends(api_actor)):
    """一次性提问(问数 / 报表 / 解读;capability=report 时等同创建报告,非流式立即返回)。"""
    if req.capability == "report" and not req.stream and not req.fixed_question_id:
        return await guard(svc.start_report(actor, connection_id=req.connection_id, question=req.question,
                                            target=req.target, locale=req.locale, mode="direct",
                                            scenic_id=req.scenic_id))
    agen = svc.run_stream(actor, connection_id=req.connection_id, capability=req.capability,
                          question=req.question, target=req.target, locale=req.locale, mode="direct",
                          report_async=not req.stream, scenic_id=req.scenic_id,
                          fixed_question_id=req.fixed_question_id)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor, c["run_id"]) if c["run_id"] else c


# ── 固定问题 ────────────────────────────────────────────────────────

class FixedRunReq(BaseModel):
    params: dict = Field(default_factory=dict)
    stream: bool = False
    scenic_id: str | None = Field(default=None, description="换个景区问同一条固定问题;不传沿用问题绑定的景区")


@router.get("/fixed-questions")
async def fixed_questions(connection_id: str | None = None, category: str | None = None,
                          domain: str | None = None, ask_type: str | None = None,
                          scenic_id: str | None = None,
                          actor: svc.Actor = Depends(api_actor)):
    """固定问题清单(可按 数据域 / 请求类别 / 景区 筛)。要连目标数据一起拿请用 /catalog。"""
    # 景区 Key:公开(public)+ 本景区的固定问题;集团级 Key:全部
    items = await svc.list_fixed(svc.Actor(**{**actor.__dict__, "role": "scenic_operator"}),
                                 connection_id=connection_id, category=category, active=True,
                                 domain=domain, ask_type=ask_type, scenic_id=scenic_id or None)
    return {"items": [{k: i[k] for k in ("id", "title", "question", "capability", "capability_label",
                                         "domain", "ask_type", "ask_type_label", "category",
                                         "scenic_id", "connection_id")} for i in items]}


@router.get("/catalog")
async def catalog(connection_id: str | None = None, scenic_id: str | None = None,
                  ask_type: str | None = Query(default=None, description="qa 问数 / insight 解读 / report 报告"),
                  domain: str | None = Query(default=None, description="数据域:票务/财务/舆情/客群/核销/营销/流量/画像…"),
                  include_assets: bool = True,
                  actor: svc.Actor = Depends(api_actor)):
    """**目录**:这个景区能问什么、能问哪些数据。

    每条固定问题都带上 `target` —— 里面是可以直接拿去发起提问的数据标识
    (仪表板给 `page_id`,数据集给 `cube_id`,都附 `asset_id`)。
    拿到之后两种发起方式:

    - `POST /v1/quickbi/query {"fixed_question_id": "..."}` —— 原样问这条固定问题;
    - `POST /v1/quickbi/query {"question": "...", "target": {"cube_id": "..."}}` —— 自己组问题打在同一份数据上。

    `include_assets=false` 时只返回问题清单,不返回该景区全部数据集/仪表板。
    """
    return await guard(svc.question_catalog(
        svc.Actor(**{**actor.__dict__, "role": "scenic_operator"}),
        connection_id=connection_id, scenic_id=scenic_id or None,
        ask_type=ask_type, domain=domain, include_assets=include_assets))


@router.post("/fixed-questions/{fq_id}/run")
async def run_fixed(fq_id: str, req: FixedRunReq, actor: svc.Actor = Depends(api_actor)):
    scoped = svc.Actor(**{**actor.__dict__, "role": "scenic_operator"})
    agen = svc.run_fixed(scoped, fq_id, params=req.params, report_async=not req.stream, scenic_id=req.scenic_id)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor, c["run_id"]) if c["run_id"] else c


# ── 对话 ─────────────────────────────────────────────────────────────

class SessionReq(BaseModel):
    connection_id: str | None = None
    capability: str = "auto"
    target: dict = Field(default_factory=dict)
    title: str | None = None


class MessageReq(BaseModel):
    question: str
    capability: str | None = None
    target: dict | None = None
    stream: bool = False


@router.post("/sessions")
async def create_session(req: SessionReq, actor: svc.Actor = Depends(api_actor)):
    return await svc.create_session(actor, connection_id=req.connection_id, capability=req.capability,
                                    target=req.target, title=req.title)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, actor: svc.Actor = Depends(api_actor)):
    return await svc.session_detail(actor, session_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: MessageReq, actor: svc.Actor = Depends(api_actor)):
    if not req.question.strip():
        raise ValidationError("question 不能为空")
    agen = svc.chat(actor, session_id, req.question, capability=req.capability, target=req.target,
                    report_async=not req.stream)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor, c["run_id"]) if c["run_id"] else c


# ── 报告 ─────────────────────────────────────────────────────────────

class ReportReq(BaseModel):
    question: str
    connection_id: str | None = None
    target: dict = Field(default_factory=dict)
    locale: str | None = None


@router.post("/reports")
async def create_report(req: ReportReq, actor: svc.Actor = Depends(api_actor)):
    """异步创建报告,立即返回 {id(run_id), status=running, chat_id}。之后轮询 GET /reports/{id}。"""
    return await guard(svc.start_report(actor, connection_id=req.connection_id, question=req.question,
                                        target=req.target, locale=req.locale))


@router.get("/reports/{run_id}")
async def get_report(run_id: str, actor: svc.Actor = Depends(api_actor)):
    return await guard(svc.refresh_report(actor, run_id))


@router.get("/reports/{run_id}/stream")
async def stream_report(run_id: str, actor: svc.Actor = Depends(api_actor)):
    return sse(await primed(svc.stream_report(actor, run_id)))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, actor: svc.Actor = Depends(api_actor)):
    return await svc.get_run(actor, run_id)
