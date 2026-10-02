"""
工具中心管理接口——Tool / Skill / Agent / MCP 的增删改查、启用禁用、锁定、
版本(Prompt)管理,供管理后台「工具中心」页调用。

权限口径(与功能清单 0.2/2.2 一致但做了简化,原文档也承认当前体量下不必做太细的
角色矩阵):
- 列表/详情:走 G-AUTH 四级可见范围过滤(list_visible_tools 已有的算法,Skill/Agent/MCP
  复用同一套判定逻辑)。
- 创建:除 group_viewer(只读角色)外都可以创建,创建后归属当前用户/当前用户所在景区。
- 编辑/删除/启禁用/锁定:判定按**景区**,不按创建者 —— 见 common/permissions.py:
  集团三角色不受限;group_viewer 一律拒绝;景区角色只能动**本景区**的资源
  (集团级资源 scenic_id 为空,对景区角色是「能用不能改」);
  景区操作员能改不能删。
"""
from __future__ import annotations

import uuid
from datetime import datetime

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.common.time import now_cst
from cogni_agent.config import get_settings
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.tool_center_service.agent.models import Agent, ApiKey
from cogni_agent.tool_center_service.mcp.client import discover_tools
from cogni_agent.tool_center_service.mcp.models import McpServer
from cogni_agent.tool_center_service.skill.models import Prompt, Skill
from cogni_agent.tool_center_service.tool.models import ResourceGrant, Tool
from cogni_agent.tool_center_service.tool.registry import GROUP_ROLES, list_visible_tools, test_permission

router = APIRouter(prefix="/admin", tags=["工具中心"])


def _can_write(role: str) -> bool:
    from cogni_agent.common import permissions as perm

    return perm.can_create(role)


def _can_manage(owner_user_id: str, scenic_id: str | None, user: CurrentUser) -> bool:
    """能不能改这条资源 —— 按**景区**判,不按创建者判。

    原先是「谁建的谁能改」,结果是同景区的另一个管理员接手时动不了前任建的东西,
    只能找超管;而景区管理员本来就该管得了本景区的全部内容。创建者本人仍然能改
    自己建的(哪怕后来被调去了别的景区,那条记录还挂在原景区下)。
    """
    from cogni_agent.common import permissions as perm

    if not perm.can_create(user.role):        # group_viewer:只读
        return False
    if perm.can_manage_all(user.role):        # 集团三角色:跨景区
        return True
    if perm.is_group_role(user.role):
        return False
    if scenic_id is None:                     # 集团级资源:景区角色能用不能改
        return owner_user_id == user.user_id
    return bool(user.scenic_id) and scenic_id == user.scenic_id


def _can_remove(owner_user_id: str, scenic_id: str | None, user: CurrentUser) -> bool:
    """能不能删 —— 在「能改」的基础上再要求删除权(景区操作员没有)。"""
    from cogni_agent.common import permissions as perm

    return _can_manage(owner_user_id, scenic_id, user) and perm.can_delete(user.role)


def _deny_manage(user: CurrentUser, what: str) -> Forbidden:
    """把「为什么不能动」说清楚,别只回一句无权限 —— 用户看不出是角色问题还是景区问题。"""
    from cogni_agent.common import permissions as perm

    label = perm.ROLE_LABELS.get(user.role or "", user.role or "当前角色")
    if not perm.can_create(user.role):
        return Forbidden(f"{label}为只读角色,不能修改{what}")
    return Forbidden(f"该{what}不属于当前账号绑定的景区,无权操作")


def _deny_remove(user: CurrentUser, what: str) -> Forbidden:
    from cogni_agent.common import permissions as perm

    if not perm.can_delete(user.role):
        return Forbidden(f"景区操作员不能删除{what};删除请联系本景区的景区管理员")
    return _deny_manage(user, what)


async def _resource_row(db: AsyncSession, resource_type: str, resource_id: str):
    """按资源类型取主表行(版本/指派的权限判定要落到它归属的那个景区上)。"""
    model = {"tool": Tool, "skill": Skill, "agent": Agent}.get(resource_type)
    if model is None:
        return None
    return await db.get(model, resource_id)


async def _assert_can_view_user(db: AsyncSession, user: CurrentUser, target_user_id: str):
    """分发中心「按人看」:只能看**本景区的人**。

    这两条接口原先完全按 target 的角色/景区构造查询,当前登录者一次都没判 ——
    于是传一个别的景区的 user_id,就能拿到那个人的姓名/账号/手机号,
    外加他能用哪些 Agent/技能、钉在哪个版本。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service.models import User

    target = await db.get(User, target_user_id)
    if not target:
        raise NotFound("用户不存在")
    if perm.is_group_role(user.role) or target.id == user.user_id:
        return target
    mine = (user.scenic_id or "").strip()
    if not mine or (target.scenic_id or "") != mine:
        raise NotFound("用户不存在")      # 不暴露「这个人存在,只是不在你景区」
    return target


def _assert_visible(user: CurrentUser, owner_user_id: str | None,
                    scenic_id: str | None, visibility: str | None, what: str) -> None:
    """详情接口的读门禁:列表筛得好好的,详情却往往是一句 db.get(Model, id)。

    拿到 ID 就能读别的景区的资源 —— 而 ID 并不难拿到(列表里、日志里、别人发的链接里)。
    报 404 而不是 403:连「这条存在」都不必告诉对方。
    """
    from cogni_agent.common import permissions as perm

    if perm.is_group_role(user.role):        # 集团账号(含只读)看全平台
        return
    if owner_user_id and owner_user_id == user.user_id:
        return
    if (visibility or "") == "public":       # 公开资源本来就是给所有景区用的
        return
    if perm.can_read_obj(user.role, user.scenic_id, scenic_id):
        return
    raise NotFound(f"{what}不存在或无权访问")


async def _assert_resource_visible(db: AsyncSession, resource_type: str, resource_id: str,
                                   user: CurrentUser) -> None:
    """版本 / 指派这类挂在资源下面的**只读**接口,按它所属资源判可见性。"""
    row = await _resource_row(db, resource_type, resource_id)
    if row is None:
        return                                # 不存在:交给下游报 NotFound
    what = {"tool": "工具", "skill": "Skill", "agent": "Agent"}.get(resource_type, "资源")
    _assert_visible(user, row.owner_user_id, row.scenic_id, getattr(row, "visibility", None), what)


async def _assert_resource_manage(db: AsyncSession, resource_type: str, resource_id: str,
                                  user: CurrentUser, *, removing: bool = False) -> None:
    """版本 / 指派这类「不直接落在主表上」的写操作,权限按它所属的资源判。

    这些接口原先一个角色判定都没有:集团只读能发布版本、景区操作员能解除别人景区的指派 ——
    列表里看不见的资源,拿到 id 照样能动。
    """
    row = await _resource_row(db, resource_type, resource_id)
    if row is None:                    # 资源不存在:交给下游报 NotFound,这里不抢答
        return
    ok = (_can_remove if removing else _can_manage)(
        row.owner_user_id, row.scenic_id, user)
    if not ok:
        what = {"tool": "工具", "skill": "Skill", "agent": "Agent"}.get(resource_type, "资源")
        raise (_deny_remove if removing else _deny_manage)(user, what)


async def _assert_version_manage(db: AsyncSession, version_id: str, user: CurrentUser,
                                 *, removing: bool = False) -> None:
    """同上,但入口只有 version_id —— 先查出它挂在哪个资源上。"""
    from cogni_agent.tool_center_service import versioning as ver

    row = await ver.get_version(db, version_id)
    await _assert_resource_manage(db, row.resource_type, row.resource_id, user, removing=removing)


def _default_visibility(user: CurrentUser) -> str:
    return "group" if user.role in GROUP_ROLES else "private"


# ───────────────────────────── Tool ─────────────────────────────

class ToolCreateRequest(BaseModel):
    name: str
    description: str = ""
    type: str  # http / db / python
    type_config: dict = {}
    risk_level: str = "low"
    visibility: str | None = None


class ToolUpdateRequest(BaseModel):
    description: str | None = None
    type_config: dict | None = None
    risk_level: str | None = None
    visibility: str | None = None


@router.get("/tools/templates")
async def tool_templates(user: CurrentUser = Depends(get_current_user)):
    """工具配置模板:字段说明、示例、教程。由后端下发,前端不硬编码。

    放后端的理由:真正消费 type_config 的是执行引擎和沙箱,字段含义与必填性由它们决定。
    写在前端就是两份真相,后端加字段前端不知道,说明和实际行为对不上。
    """
    from cogni_agent.tool_center_service.tool.templates import all_templates

    return all_templates()


class PreviewRequest(BaseModel):
    type: str
    type_config: dict
    params: dict = {}


@router.post("/tools/preview")
async def preview_tool_request(req: PreviewRequest, user: CurrentUser = Depends(get_current_user)):
    """预览「这个配置最终会发出什么」。**只渲染不执行**,不会真的打到目标服务。

    没有这一步的话,配置里的 {{变量}} 到底替换成了什么、GET 的参数拼没拼进 URL、
    POST 的 body 长什么样,只能靠脑补;而配错的代价是保存后调用直接失败,还得回头猜。
    """
    from cogni_agent.tool_center_service.tool.templates import TEMPLATE_VARIABLES

    cfg = req.type_config or {}
    # 预览用的系统变量取当前登录者的真实上下文,这样能直观看出租户隔离生效在哪一段
    sysvars = {
        "scenic_id": user.scenic_id or "",
        "org_id": user.scenic_id or "group",
        "user_id": user.user_id,
        "now": now_cst().isoformat(),
    }
    merged = {**sysvars, **(req.params or {})}

    def render(text: str) -> str:
        out = str(text)
        for k, v in merged.items():
            out = out.replace(f"{{{{{k}}}}}", str(v))
        return out

    if req.type == "http":
        method = (cfg.get("method") or "GET").upper()
        url = render(cfg.get("url") or "")
        headers = {k: render(v) for k, v in (cfg.get("headers") or {}).items()}
        # 模型侧参数:params_schema 里声明的字段(系统变量不属于这一类)
        model_params = {k: v for k, v in (req.params or {}).items() if k not in sysvars}
        query, body = ({}, None)
        if method == "GET":
            query = model_params
            if query:
                from urllib.parse import urlencode

                url = f"{url}{'&' if '?' in url else '?'}{urlencode(query)}"
        else:
            body = model_params
        redacted = {k: ("***(凭证已隐藏)***" if "{{credential}}" in str(cfg.get("headers", {}).get(k, "")) else v)
                    for k, v in headers.items()}
        raw = f"{method} {url} HTTP/1.1\n" + "\n".join(f"{k}: {v}" for k, v in redacted.items())
        if body:
            raw += "\n\n" + json.dumps(body, ensure_ascii=False, indent=2)
        return {"ok": True, "kind": "http", "method": method, "url": url,
                "headers": redacted, "query": query, "body": body, "raw": raw,
                "notes": ["凭证在预览中以 *** 代替;真实调用时才解密注入",
                          "系统变量已用你当前的登录上下文渲染"]}

    if req.type == "db":
        sql = cfg.get("sql_template") or ""
        import re as _re

        var_names = _re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", sql)
        builtin = set(sysvars.keys())
        model_vars = [v for v in dict.fromkeys(var_names) if v not in builtin]
        # 展示参数化后的样子:变量变成占位符,值单独列出——强调不是字符串拼接
        placeholder_sql, values = sql, []
        for i, v in enumerate(dict.fromkeys(var_names), start=1):
            placeholder_sql = _re.sub(r"\{\{\s*" + v + r"\s*\}\}", f"${i}", placeholder_sql)
            values.append({"index": f"${i}", "name": v,
                           "source": "系统注入" if v in builtin else "模型填写",
                           "value": merged.get(v, "(待模型提供)")})
        return {"ok": True, "kind": "db", "datasource_id": cfg.get("datasource_id"),
                "raw": placeholder_sql, "bind_values": values, "model_params": model_vars,
                "notes": ["变量会转成驱动层参数占位符($1/%s)后执行,不是字符串拼接,因此免疫 SQL 注入",
                          "只允许 SELECT;未写 LIMIT 时系统会自动补上限"]}

    if req.type == "python":
        return {"ok": True, "kind": "python",
                "raw": json.dumps({k: v for k, v in (req.params or {}).items()}, ensure_ascii=False, indent=2),
                "notes": ["上面是将通过 stdin 传给脚本的 JSON",
                          "脚本需把结果以 JSON 打印到 stdout;调试信息请打到 stderr"]}

    if req.type == "mcp":
        return {"ok": True, "kind": "mcp",
                "raw": json.dumps({"jsonrpc": "2.0", "method": "tools/call",
                                   "params": {"name": cfg.get("remote_tool_name"),
                                              "arguments": req.params or {}}},
                                  ensure_ascii=False, indent=2),
                "notes": ["MCP 工具通过 JSON-RPC tools/call 调用,不是普通 HTTP 请求"]}

    if req.type == "quickbi":
        from cogni_agent.tool_center_service.quickbi.tool_type import input_schema as _qbi_schema, schema_note

        return {"ok": True, "kind": "quickbi",
                "raw": json.dumps({"type_config": cfg, "model_params": req.params or {},
                                   "input_schema": _qbi_schema(cfg)}, ensure_ascii=False, indent=2),
                "notes": ["Quick BI 工具由平台以连接里的 user_token 身份调用智能小Q OpenAPI(X-Gw 签名)",
                          schema_note(cfg) or "按 capability 路由到问数/报表/解读/报告",
                          "可在「工具中心 · Quick BI · 操练台」先验证同样的问题与目标数据"]}

    raise Forbidden(f"未知工具类型:{req.type}")


@router.get("/tools")
async def list_tools(keyword: str | None = None, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tools = await list_visible_tools(db, user.user_id, user.role, user.scenic_id, keyword)
    names = await _resolve_user_names(db, (t.owner_user_id for t in tools))
    # 15 号文档:列表不再携带 default_version;「已发布 N 版」徽标由 published 计数推导
    published = await _published_version_counts(db, [t.id for t in tools], resource_type="tool")
    # v1.1.8:列表内容 = 最新已发布快照(无 published 回退主表初值)
    from cogni_agent.tool_center_service import versioning as ver

    snaps = await ver.latest_published_snapshots(db, "tool", [t.id for t in tools])
    meta = await _list_meta(db, "tool", tools)
    return {"items": [
        {**_tool_live_item(t, snaps.get(t.id), names), "published_count": published.get(t.id, 0),
         **meta[t.id]}
        for t in tools]}


# ── 上传给智能体读的文件 ─────────────────────────────────────────────

@router.post("/files")
async def upload_agent_file(file: UploadFile = File(...),
                            conversation_id: str | None = Form(default=None),
                            user: CurrentUser = Depends(get_current_user)):
    """上传一份文件供智能体读取(Excel / CSV / Word / PDF / 图片,≤10MB)。

    上传时就解析一次并把结果记在行上 —— 传完立刻知道能不能读。
    等到对话里才发现读不了的话,用户已经在等回答了,而那时他多半会觉得是智能体不行。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.tool_center_service import agent_files

    # 上传是写操作:占存储、并且这份文件会被智能体当成事实来回答问题。
    # 只读角色不该往里塞东西 —— 不然「谁传的这份数据」在审计上就说不清了。
    perm.assert_can_write(user.role, "上传文件")
    content = await file.read()
    # 景区取自登录态,**不接受前端传** —— 允许指定的话,一个景区的人可以把文件
    # 挂到别的景区名下,而那份文件之后会被那个景区的人读到
    return await agent_files.save(
        user_id=user.user_id, scenic_id=user.scenic_id or None,
        file_name=file.filename or "unnamed", content=content,
        conversation_id=conversation_id)


@router.get("/files")
async def list_agent_files(conversation_id: str | None = None, limit: int = 50,
                           user: CurrentUser = Depends(get_current_user)):
    from cogni_agent.tool_center_service import agent_files

    return {"items": await agent_files.list_files(
        user_id=user.user_id, role=user.role, scenic_id=user.scenic_id or None,
        conversation_id=conversation_id, limit=limit)}


@router.delete("/files/{file_id}")
async def delete_agent_file(file_id: str, user: CurrentUser = Depends(get_current_user)):
    from cogni_agent.common import permissions as perm
    from cogni_agent.tool_center_service import agent_files

    # 删除不可逆:景区操作员不能删(与其它资源同一口径)。
    # 跨景区的那道判定在 agent_files.delete 里 —— 那里才拿得到这行的归属景区。
    perm.assert_can_delete(user.role, "上传的文件")
    await agent_files.delete(file_id=file_id, user_id=user.user_id, role=user.role,
                             scenic_id=user.scenic_id or None)
    return {"ok": True}


class DataApiSyncRequest(BaseModel):
    update_existing: bool = Field(default=False, description="已存在的同接口工具也按文档刷新(会覆盖手工改过的描述)")


@router.post("/tools/sync-data-api")
async def sync_data_api(req: DataApiSyncRequest,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """按随服务打包的《数据指标接口文档》同步 HTTP 工具:缺的新建,已有的默认不动。

    启动时已经自动补过一次;这里给运营一个手动入口 —— 比如删错了想补回来,
    或者文档更新后想把已有工具刷新成最新定义(update_existing)。
    这批工具是全平台共用的(visibility=public),所以只有集团角色能同步。
    """
    if not _can_write(user.role) or user.role not in GROUP_ROLES:
        raise Forbidden("数据接口工具全平台共用,只有集团角色(且非只读)能同步")
    from cogni_agent.tool_center_service import data_api_seed as seed

    cfg = get_settings().get("tool_center", "data_api_seed", default={}) or {}
    res = await seed.sync_data_api_tools(db, owner_user_id=user.user_id,
                                         base_url=cfg.get("base_url") or None,
                                         update_existing=req.update_existing)
    from cogni_agent.governance_event_bus_service import platform_secrets as _ps

    await _ps.refresh()
    res["key_configured"] = bool(_ps.get("data_platform_key"))
    return res


@router.get("/tools/builtin")
async def list_builtin_tools(user: CurrentUser = Depends(get_current_user)):
    """系统内置工具清单(只读)。

    **为什么要单独露出来**:内置工具不落库、不走版本、不需要配置,页面上因此一个都看不见 ——
    于是运营会照着自己的需要再建一个同样功能的工具(「数值计算」这种),
    填参数、发版本、再分发,而系统里本来就有一个更稳的。更糟的是自己建的那个
    每改一次都要重新发版,忘了发版就是「改了没生效」。

    这里返回的东西不可编辑也不需要编辑:它们对所有技能自动可用,永远是最新的。
    """
    from cogni_agent.tool_center_service import builtin_tools as bi

    _ = user
    # 平台密钥**配没配**(只给是/否,永远不给值)。同步脚本据此决定能不能把工具的
    # 明文密钥换成占位符:这边没配就换,已经能用的工具会立刻变成 401。
    from cogni_agent.governance_event_bus_service import platform_secrets as _ps

    await _ps.refresh()
    return {"secrets_configured": {k: bool(v) for k, v in _ps.all_values().items()},
            "items": [
        {"name": name,
         "description": spec["description"],
         "parameters": list(((spec.get("schema") or {}).get("properties") or {}).keys()),
         "required": list(((spec.get("schema") or {}).get("required") or [])),
         "schema": spec.get("schema") or {},
         "group": _BUILTIN_GROUPS.get(name, "其他")}
        for name, spec in bi.BUILTIN.items()],
        "note": "内置工具免配置、免发版、对所有技能默认可用,且始终是最新实现;"
                "不要为这些能力再建一份同名工具 —— 自建的那份每改一次都要重新发版"}


# 分组只影响页面上的展示顺序与归类,不参与任何执行逻辑
_BUILTIN_GROUPS = {
    "sys_now": "时间", "sys_date_range": "时间", "sys_date_shift": "时间", "sys_align_period": "时间",
    "sys_table_query": "数据分析", "sys_table_stats": "数据分析", "sys_table_compare": "数据分析",
    "sys_table_pivot": "数据分析", "sys_table_outliers": "数据分析", "sys_table_trend": "数据分析",
    "sys_table_search": "数据分析", "sys_datasets": "数据分析",
    "sys_calc": "计算", "sys_metric_delta": "计算", "sys_json_peek": "计算",
    "sys_chart": "图表",
    "sys_read_file": "文件",
    "sys_supplier_ids": "票务前置",
    "sys_quickbi_link": "降级兜底",
}


class ApiDocParseRequest(BaseModel):
    markdown: str = Field(description="接口文档原文(Markdown)。直接把那份文档整段贴进来即可")
    default_base_url: str = Field(default="", description="文档里只写了 URI 没写域名时用这个")


@router.post("/tools/from-api-doc/parse")
async def parse_api_doc(req: ApiDocParseRequest, user: CurrentUser = Depends(get_current_user)):
    """把一份 Markdown 接口文档解析成工具定义(**只解析,不落库**)。

    为什么值得单独一步:解析结果要先给人看一眼 —— 哪些参数被认成必传、
    数据体被认在哪一层、认证 header 有没有从 curl 里取到。
    看过再保存,比保存完再去猜「为什么模型老是传错参数」省事得多。
    """
    from cogni_agent.tool_center_service.apidoc import ApiDocParseError, from_markdown

    try:
        doc = from_markdown(req.markdown, default_base_url=req.default_base_url)
    except ApiDocParseError as exc:
        raise ValidationError(str(exc)) from exc
    return {**doc.to_dict(), "type_config": doc.to_type_config(),
            "preview_schema": doc.request_schema()}


class FieldTableRequest(BaseModel):
    markdown: str = Field(description="一段 Markdown 字段表;贴一整份接口文档也认(会自动挑出对应那一节)")
    kind: str = Field(default="request", description="request=请求参数(读「必传」列) / response=返回参数")


@router.post("/tools/parse-fields")
async def parse_field_table(req: FieldTableRequest, user: CurrentUser = Depends(get_current_user)):
    """把一段 Markdown 字段表解析成字段列表(**只解析,不落库**)。

    给工具编辑页的「参数定义」「响应字段说明」用:运维手上是一份接口文档,
    最自然的操作是把那一节整段贴进来,而不是在表格里一行一行点「加字段」——
    嵌套结构(`data[].profile[].uri`)那种,点到第十行就没人愿意继续了。

    用的是解析整份文档的同一套表格解析器,不另写一份 —— 两份解析器迟早对不上,
    而对不上的表现是「预览看着对、存进去不对」,最难查。
    """
    from cogni_agent.tool_center_service.apidoc import parse_fields

    kind = (req.kind or "request").lower()
    if kind not in ("request", "response"):
        raise ValidationError("kind 只能是 request 或 response")
    return parse_fields(req.markdown, kind=kind)


class ApiDocToolRequest(ApiDocParseRequest):
    name: str = Field(default="", description="工具名;留空取文档标题")
    description: str = Field(default="", description="给模型看的说明;留空取文档里的概述")
    visibility: str | None = None
    scenic_id: str | None = Field(default=None, description="归属景区;留空=集团级")
    # 给参数加扩展声明:{参数名: {"x-param-source": {...}} 或 {"x-ctx-default": "$ctx.scenicId"}}。
    # 接口文档里写不出「这个参数该由系统自动取」,这层语义只能由导入方补上。
    param_annotations: dict[str, dict] = Field(
        default_factory=dict,
        description="参数扩展声明,合并进对应参数的 schema;只接受 x- 开头的键")


def _annotate_params(type_config: dict, annotations: dict[str, dict]) -> dict:
    """服务端入口:共用 apidoc.annotate_params,把解析错误转成 400。"""
    from cogni_agent.tool_center_service.apidoc import ApiDocParseError, annotate_params

    try:
        return annotate_params(type_config, annotations)
    except ApiDocParseError as exc:
        raise ValidationError(str(exc)) from exc


@router.post("/tools/from-api-doc")
async def create_tool_from_api_doc(req: ApiDocToolRequest,
                                   user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    """按一份 Markdown 接口文档创建 http 工具(并自动发布 v1)。

    **为什么不让人写 JSON Schema**:JSON Schema 是给机器看的,人写起来啰嗦、
    读起来更啰嗦,而运维手上现成的东西恰恰是一份接口文档。让人手工翻译一遍,
    既慢又容易译错 —— 译错的表现还特别隐蔽:模型照着错的 schema 传参,
    接口回一个业务错误,看起来像「接口有问题」,其实是参数名少了个字母。

    顺带一提:文档里那张**返回参数表**不是可有可无的。结果压缩层靠它知道
    「数据体在哪一层、每一列是什么」,才谈得上按列筛选与统计;没有它,
    上千行的返回只能原样截断 —— 模型看到半截 JSON,然后一本正经地作答。
    """
    from cogni_agent.tool_center_service.apidoc import ApiDocParseError, from_markdown

    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能创建工具")
    try:
        doc = from_markdown(req.markdown, default_base_url=req.default_base_url)
    except ApiDocParseError as exc:
        raise ValidationError(str(exc)) from exc

    tool = Tool(id=str(uuid.uuid4()), name=(req.name or doc.title)[:64],
                owner_user_id=user.user_id, scenic_id=req.scenic_id or user.scenic_id,
                visibility=req.visibility or _default_visibility(user), active=True)
    db.add(tool)
    await db.flush()
    from cogni_agent.tool_center_service import versioning as ver

    snapshot = {
        "description": req.description or doc.summary or f"{doc.method} {doc.uri}",
        "type": "http", "type_config": _annotate_params(doc.to_type_config(), req.param_annotations),
        "risk_level": "low",
    }
    # base_version_id 是必填关键字参数(新资源没有基线 → None)。之前漏传,
    # 这个接口每次都是 TypeError → 500,「按接口文档新建」从来没成功过。
    draft = await ver.derive_version(db, "tool", tool.id, base_version_id=None, snapshot=snapshot,
                                     created_by=user.user_id, note="按接口文档创建")
    await ver.publish_version(db, draft.id)
    await db.commit()
    return {"id": tool.id, "name": tool.name, "doc": doc.to_dict(),
            "warnings": doc.warnings,
            "message": f"已创建工具「{tool.name}」并发布 v1;"
                       f"接下来把它绑到 Skill 上就能被智能体调用"}


@router.post("/tools")
async def create_tool(req: ToolCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能创建工具")
    if req.type == "quickbi":
        # Quick BI 工具引用的连接/资产/文件必须是创建者自己能访问的(否则等于借工具越权使用他人连接)
        from cogni_agent.tool_center_service.quickbi.tool_type import validate_config as _qbi_validate

        await _qbi_validate(user, req.type_config or {})
    # v1.2(0051):Tool 与 Skill/Agent 同构 —— 主表仅身份目录(name/owner/scenic/
    # visibility/active),内容(description/type/type_config/risk_level)一律落版本快照。
    # 工具是叶子资源:无发布门禁、无指派语义,为保留「创建即生效」体验(技能 refresh-pins
    # 需要工具已有 published 版本),创建后自动派生草稿并发布 v1 —— 内容从此进版本体系,
    # 可追溯/回滚;后续编辑一律走草稿 + 手动发布(update_tool / POST versions/{id}/publish)。
    tool = Tool(
        name=req.name, active=True,
        owner_user_id=user.user_id, scenic_id=user.scenic_id,
        visibility=req.visibility or _default_visibility(user),
    )
    db.add(tool)
    await db.flush()
    from cogni_agent.tool_center_service import versioning as ver

    snapshot = {
        "description": req.description or "",
        "type": req.type,
        "type_config": req.type_config or {},
        "risk_level": req.risk_level or "low",
    }
    draft = await ver.derive_version(db, "tool", tool.id, base_version_id=None,
                                     snapshot=snapshot, created_by=user.user_id)
    await ver.publish_version(db, draft.id)
    await db.commit()
    names = await _resolve_user_names(db, [tool.owner_user_id])
    return _tool_dict(tool, snapshot, names)


@router.get("/tools/{tool_id}")
async def get_tool(tool_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool or tool.deleted_at:
        raise NotFound("工具不存在")
    ok, _ = await test_permission(db, tool_id, user.user_id, user.role, user.scenic_id, "use")
    if not ok:
        raise Forbidden("无权限查看该工具")
    # 收敛双轨(推荐方案):工具发布历史统一从统一版本表读取,旧表 tc_tool_versions 停止读写
    from cogni_agent.tool_center_service import versioning as ver
    ver_items = await ver.list_versions(db, "tool", tool_id)
    names = await _resolve_user_names(db, (tool.owner_user_id, *(i.get("created_by") for i in ver_items)))
    pub = await ver.latest_published_version(db, "tool", tool_id)
    return {**_tool_live_item(tool, pub.snapshot if pub else None, names), "versions": [
        {"id": i.get("id"),
         "version": f"v{i['version']}" if i.get("version") is not None else "草稿",
         "status": i.get("status"),
         "published_by": i.get("created_by"),
         "published_by_name": names.get(i.get("created_by"), ("", ""))[0],
         "published_by_phone": names.get(i.get("created_by"), ("", ""))[1],
         "created_at": i.get("published_at") or i.get("created_at")}
        for i in ver_items
    ]}


@router.put("/tools/{tool_id}")
async def update_tool(tool_id: str, req: ToolUpdateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool or tool.deleted_at:
        raise NotFound("工具不存在")
    if tool.locked and not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise Forbidden("工具已锁定,仅创建者或 admin 可编辑")
    if not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise _deny_manage(user, "工具")
    # v1.1.10 起:编辑 = 保存草稿,不再直改主表内容(内容全在版本快照)。
    # v1.2(0051):visibility 是身份/目录列(主表),与 skill/agent 一致**直写主表**——
    # 它不在工具内容键(adapter accepted_keys 会过滤),落快照等于白改。
    # 内容键(type_config/description/risk_level)改动落草稿快照(无草稿先派生、有草稿则
    # 覆盖保存),发布走 POST /versions/{id}/publish → G1 漂移源消除。
    if req.visibility is not None:
        if user.role not in GROUP_ROLES and req.visibility in ("group", "public"):
            raise Forbidden("景区角色只能在 private/scenic 间选择可见范围")
        tool.visibility = req.visibility
    snapshot: dict = {}
    if req.type_config is not None:
        live = await _tool_live_snapshot(db, tool_id)
        if (live or {}).get("type") == "quickbi":
            from cogni_agent.tool_center_service.quickbi.tool_type import validate_config as _qbi_validate

            await _qbi_validate(user, req.type_config)
        snapshot["type_config"] = req.type_config
    if req.description is not None:
        snapshot["description"] = req.description
    if req.risk_level is not None:
        snapshot["risk_level"] = req.risk_level
    if snapshot:
        from cogni_agent.tool_center_service import versioning as ver
        draft = await ver.latest_draft(db, "tool", tool_id)
        if draft is not None:
            await ver.update_version(db, draft.id, snapshot=snapshot, updated_by=user.user_id)
        else:
            await ver.derive_version(db, "tool", tool_id, base_version_id=None,
                                     snapshot=snapshot, created_by=user.user_id)
    await db.commit()
    # v1.2:响应取最新草稿快照视角(刚保存的内容;无草稿仅主表身份),同 skill/agent update
    from cogni_agent.tool_center_service import versioning as ver
    draft = await ver.latest_draft(db, "tool", tool_id)
    names = await _resolve_user_names(db, [tool.owner_user_id])
    return _tool_dict(tool, draft.snapshot if draft else None, names)


async def _tool_live_snapshot(db: AsyncSession, tool_id: str) -> dict | None:
    """工具「当前对外内容」快照:最新已发布优先;仅草稿(未发布/迁移孤儿)退回草稿。
    主表已无内容列(0051),凡需要回显工具内容的管理操作一律经此取快照。"""
    from cogni_agent.tool_center_service import versioning as ver

    pub = await ver.latest_published_version(db, "tool", tool_id)
    if pub is not None:
        return pub.snapshot or {}
    draft = await ver.latest_draft(db, "tool", tool_id)
    return (draft.snapshot or {}) if draft is not None else None


async def _tool_binders(db: AsyncSession, tool_id: str) -> list[str]:
    """返回引用该 tool 的 skill 名称(主表 tool_ids + skill 版本快照 tools),空列表 = 可删。"""
    from cogni_agent.tool_center_service import versioning as ver

    skills = (await db.execute(select(Skill))).scalars().all()
    names = {s.name for s in skills if tool_id in (s.tool_ids or [])}
    rows = (await db.execute(
        select(ver.ResourceVersion).where(ver.ResourceVersion.resource_type == "skill")
    )).scalars().all()
    name_map = {s.id: s.name for s in skills}
    for r in rows:
        pins = (r.snapshot or {}).get("tools") or []
        if any(isinstance(p, dict) and p.get("tool_id") == tool_id for p in pins):
            names.add(name_map.get(r.resource_id, r.resource_id))
    return sorted(names)


@router.delete("/tools/{tool_id}")
async def delete_tool(tool_id: str, force: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除工具 = 软删除归档(deleted_at)。有 Skill 绑定时默认拒绝;force=true 自动解除绑定后归档。
    Skill 版本快照为不可变历史,不改写;旧版本引用的已删工具在运行时自然解析失败。"""
    tool = await db.get(Tool, tool_id)
    if not tool or tool.deleted_at:
        raise NotFound("工具不存在")
    if not _can_remove(tool.owner_user_id, tool.scenic_id, user):
        raise _deny_remove(user, "工具")
    binders = await _tool_binders(db, tool_id)
    if binders and not force:
        raise Forbidden(f"工具「{tool.name}」已被 Skill 绑定:{', '.join(binders)},请先解除绑定再删除(或勾选强制删除自动解除)")
    if force and binders:
        # 强制删除:解除引用(不级联删绑定方)——从 Skill 主表 tool_ids 移除该工具。
        # v1.1.10:tool_usage 主表列已删(0050),孤儿 usage 只可能存在于版本快照
        # (不可变历史不改写,删除后运行时按已删工具解析失败,与注释一致)。
        for s in (await db.execute(select(Skill))).scalars():
            if tool_id in (s.tool_ids or []):
                s.tool_ids = [tid for tid in (s.tool_ids or []) if tid != tool_id]
    tool.active = False  # 停用,运行时/授权立即失效
    tool.deleted_at = datetime.now(timezone.utc)  # 软删除归档
    # 级联清理:该工具的共享授权记录(ResourceGrant),避免残留指向已删除工具的授权
    await db.execute(delete(ResourceGrant).where(
        ResourceGrant.resource_type == "tool", ResourceGrant.resource_id == tool_id))
    await db.commit()
    return {"ok": True, "archived": True}


@router.post("/tools/{tool_id}/toggle")
async def toggle_tool(tool_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise NotFound("工具不存在")
    if not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise Forbidden("无权限操作该工具")
    tool.active = not tool.active
    await db.commit()
    snap = await _tool_live_snapshot(db, tool_id)
    names = await _resolve_user_names(db, [tool.owner_user_id])
    return _tool_dict(tool, snap, names)


@router.post("/tools/{tool_id}/lock")
async def toggle_lock_tool(tool_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise NotFound("工具不存在")
    if not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise Forbidden("无权限操作该工具")
    tool.locked = not tool.locked
    await db.commit()
    snap = await _tool_live_snapshot(db, tool_id)
    names = await _resolve_user_names(db, [tool.owner_user_id])
    return _tool_dict(tool, snap, names)


class GrantRequest(BaseModel):
    grantee_type: str  # user / scenic_all
    grantee_id: str
    permission: str = "use"


@router.post("/tools/{tool_id}/share")
async def share_tool(tool_id: str, req: GrantRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise NotFound("工具不存在")
    if not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise Forbidden("无权限共享该工具")
    grant = ResourceGrant(resource_type="tool", resource_id=tool_id, grantee_type=req.grantee_type,
                          grantee_id=req.grantee_id, permission=req.permission)
    db.add(grant)
    await db.commit()
    return {"ok": True, "grant_id": grant.id}


@router.get("/tools/{tool_id}/grants")
async def list_grants(tool_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """授权列表(含被授权人姓名/账号)。与「共享授权」卡片联动:前端据此渲染
    已授权列表,并在搜索候选时排除已授权用户。"""
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise NotFound("工具不存在")
    if not _can_manage(tool.owner_user_id, tool.scenic_id, user):
        raise Forbidden("无权限查看该工具授权")
    rows = (await db.execute(
        select(ResourceGrant)
        .where(ResourceGrant.resource_type == "tool", ResourceGrant.resource_id == tool_id)
        .order_by(ResourceGrant.created_at.desc())
    )).scalars().all()
    names = await _resolve_user_names(db, (r.grantee_id for r in rows if r.grantee_type == "user"))
    return {"items": [
        {"grant_id": r.id, "grantee_type": r.grantee_type, "grantee_id": r.grantee_id,
         "grantee_name": names.get(r.grantee_id, ("", ""))[0] if r.grantee_type == "user" else r.grantee_id,
         "grantee_phone": names.get(r.grantee_id, ("", ""))[1] if r.grantee_type == "user" else "",
         "permission": r.permission, "created_at": r.created_at}
        for r in rows
    ]}


@router.delete("/tools/{tool_id}/grants/{grant_id}")
async def delete_grant(tool_id: str, grant_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """回收授权:删 tc_grants 行,恢复未授权状态。"""
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise NotFound("工具不存在")
    # 回收授权也是删除动作:景区操作员不能做
    if not _can_remove(tool.owner_user_id, tool.scenic_id, user):
        raise _deny_remove(user, "工具授权")
    grant = await db.get(ResourceGrant, grant_id)
    if grant is None or grant.resource_id != tool_id:
        return {"ok": True, "deleted": False}
    await db.delete(grant)
    await db.commit()
    return {"ok": True, "deleted": True}


@router.get("/tools/users/search")
async def search_tool_users(keyword: str | None = None, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """共享授权下拉的用户搜索源。与用户中心 /admin/users/paged 的区别:
    权限放宽到「管理员/工具开发者」——group_developer 有工具管理权(见 _can_manage),
    若搜不了人,授权下拉对工具开发者就是死的。非超管只搜本组织。"""
    if user.role not in ("super_admin", "group_admin", "group_developer"):
        raise Forbidden("仅管理员或工具开发者可搜索用户")
    from cogni_agent.user_center_service.users import list_users_page

    scenic_id = None if user.role == "super_admin" else user.scenic_id
    items, _ = await list_users_page(db, keyword=keyword, scenic_id=scenic_id, is_active=True, page=1, page_size=20)
    return {"items": [
        {k: it[k] for k in ("id", "name", "account", "phone", "role") if k in it}
        for it in items
    ]}


class DirectDebugRequest(BaseModel):
    params: dict = {}


@router.post("/tools/{tool_id}/test")
async def test_tool(tool_id: str, req: DirectDebugRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """在线调试面板——通过与「对话」页完全相同的 tool 直调路径跑一次真实调用(含 HITL/护栏),
    不是另一套独立实现,保证调试结果与线上行为一致。"""
    from cogni_agent.control_plane_service.queue import create_task

    task = await create_task(
        scenic_id=user.scenic_id, source="internal",
        payload={"user_id": user.user_id, "role": user.role, "message": __import__("json").dumps(req.params, ensure_ascii=False)},
        executor_type="tool", executor_id=tool_id,
        user_id=user.user_id,
    )
    return {"task_id": task["id"], "status": task["status"]}


async def _resolve_user_names(db: AsyncSession, user_ids) -> dict[str, tuple[str, str]]:
    """user_id -> (name, phone)。批量联表用户中心,给详情/列表展示用,避免 N+1。

    App 端用户主要用手机号登录,手机号比系统账号辨识度更高,展示一律用手机号。"""
    from cogni_agent.user_center_service.models import User

    ids = list(dict.fromkeys(uid for uid in user_ids if uid))
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name, User.phone).where(User.id.in_(ids)))).all()
    return {r[0]: (r[1] or "", r[2] or "") for r in rows}


async def _list_meta(db: AsyncSession, resource_type: str | None, rows) -> dict[str, dict]:
    """列表卡片的「最近修改时间 / 修改人 / 归属」。

    - 最近修改 = 该资源所有版本(草稿 / 已发布 / 已下线)里 updated_at 最大的那一行,
      修改人取那一行的 updated_by(0087 起保存、发布、下线都会写);
      一个版本都没有(或 MCP 这类不走版本的)退回主表 created_at + 创建者;
    - 归属 = 所属景区名(scenic_id 为空 = 集团级)+ 创建者姓名;
    - 用户名、景区名都是批量查,列表再长也只多 3 条 SQL。
    """
    from cogni_agent.user_center_service.models import SysScenic
    from cogni_agent.tool_center_service import versioning as ver

    rows = list(rows)
    ids = [r.id for r in rows]
    latest: dict[str, tuple] = {}
    if resource_type and ids:
        vrows = (await db.execute(
            select(ver.ResourceVersion.resource_id, ver.ResourceVersion.updated_at,
                   ver.ResourceVersion.updated_by, ver.ResourceVersion.created_by)
            .where(ver.ResourceVersion.resource_type == resource_type,
                   ver.ResourceVersion.resource_id.in_(ids))
        )).all()
        for rid, at, by, cby in vrows:
            if at is not None and (rid not in latest or at > latest[rid][0]):
                latest[rid] = (at, by or cby or "")
    user_ids = [r.owner_user_id for r in rows] + [v[1] for v in latest.values()]
    names = await _resolve_user_names(db, user_ids)
    scenic_ids = list({r.scenic_id for r in rows if getattr(r, "scenic_id", None)})
    scenic_names: dict[str, str] = {}
    if scenic_ids:
        scenic_names = {sid: n for sid, n in (await db.execute(
            select(SysScenic.scenic_id, SysScenic.scenic_name)
            .where(SysScenic.scenic_id.in_(scenic_ids)))).all()}
    out: dict[str, dict] = {}
    for r in rows:
        at, by = latest.get(r.id, (r.created_at, r.owner_user_id))
        sid = getattr(r, "scenic_id", None)
        out[r.id] = {
            "updated_at": at, "updated_by": by or "",
            "updated_by_name": (names.get(by) or ("", ""))[0] if by else "",
            "owner_name": (names.get(r.owner_user_id) or ("", ""))[0],
            # 归属:景区名;集团级资源(scenic_id 为空)统一叫「集团」
            "scenic_name": (scenic_names.get(sid) or sid) if sid else "集团",
        }
    return out


def _tool_dict(t: Tool, snapshot: dict | None = None, names: dict | None = None) -> dict:
    """工具返回结构。v1.2(0051):Tool 与 Skill/Agent 同构 —— 内容一律以版本快照为
    权威(description/type/type_config/risk_level 只存在于草稿/发布快照,主表无列),
    snapshot=None(无任何版本)时内容给空默认,不再回退主表;身份/目录字段仍来自主表。"""
    snap = snapshot or {}
    d = {
        "id": t.id, "name": t.name,
        "description": snap.get("description") or "",
        "type": snap.get("type") or "",
        "type_config": snap.get("type_config") or {},
        "risk_level": snap.get("risk_level") or "low",
        "active": t.active, "locked": t.locked,
        "owner_user_id": t.owner_user_id, "scenic_id": t.scenic_id, "visibility": t.visibility,
        "source_mcp_server_id": t.source_mcp_server_id, "created_at": t.created_at,
    }
    if names and t.owner_user_id in names:
        d["owner_name"], d["owner_phone"] = names[t.owner_user_id]
    # 已被系统内置工具取代的历史行(「时间对齐」「数值计算」这类手建的 type=builtin)。
    # 它们从建出来那天起就调不通 —— type=builtin 是按**工具名**去内置注册表找函数,
    # 中文名一个都找不到。列表上标出来,运维才知道这一行可以下线了;
    # 不标的话它看起来和别的工具一模一样,还带着「ACTIVE 启用」的绿标。
    from cogni_agent.tool_center_service import builtin_tools as _bi

    _sup = _bi.superseded_by(t.name) if d["type"] == "builtin" else None
    if _sup:
        d["superseded_by"] = _sup
        d["superseded_note"] = (f"这项能力已内置为 {_sup},所有技能默认可用、无需绑定。"
                                f"这一行本身调不通(内置工具按英文名注册),可以下线了 —— "
                                f"已经绑了它的技能会自动改用内置的那一份,不会因此断掉。")
    return d


def _tool_live_item(t: Tool, snapshot: dict | None, names: dict | None = None) -> dict:
    """同 skill/agent 语义:列表/详情「当前对外内容」= 指定版本快照(通常为最新已发布);
    snapshot=None(无任何版本)时仅主表身份 + 空内容。"""
    return _tool_dict(t, snapshot, names)


# ───────────────────────────── Skill ─────────────────────────────

class SkillCreateRequest(BaseModel):
    name: str
    # 能力一句话概述(人写,面向意图识别路由器的能力清单)。留空时运行时回退 system_prompt 前120字
    description: str | None = None
    tool_usage: dict = {}
    tool_ids: list[str] = []
    suggested_mode: str = "L2"
    knowledge_sources: list[str] = []
    constraints: dict = {}
    visibility: str | None = None
    prompt: str = ""


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tool_ids: list[str] | None = None
    # {tool_id: "什么时候用它"} —— 与 tool_ids 就近维护,解析时自动合成进系统提示
    tool_usage: dict | None = None
    suggested_mode: str | None = None
    knowledge_sources: list[str] | None = None
    constraints: dict | None = None
    visibility: str | None = None
    prompt: str | None = None  # 更新 Prompt(写草稿版本,发布时才分配版本号)


async def _visible_skills(db: AsyncSession, user: CurrentUser) -> list[Skill]:
    is_group = user.role in GROUP_ROLES
    conditions = [Skill.owner_user_id == user.user_id, Skill.visibility == "public"]
    if is_group:
        conditions.append(Skill.visibility == "group")
    if user.scenic_id:
        conditions.append((Skill.visibility == "scenic") & (Skill.scenic_id == user.scenic_id))
    # 列表顺序必须稳定:无 ORDER BY 时 PostgreSQL 按物理堆顺序返回,行 UPDATE(如发布/改状态)
    # 后位置会变,导致每次刷新顺序不同。统一按创建时间倒序 + id 兜底,保证确定性。
    stmt = (
        select(Skill)
        .where(or_(*conditions), Skill.deleted_at.is_(None))
        .order_by(Skill.created_at.desc(), Skill.id.desc())
    )
    return list((await db.execute(stmt)).scalars())


@router.get("/skills")
async def list_skills(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    skills = await _visible_skills(db, user)
    # 15 号文档:列表不再携带 default_version;「已发布 N 版」徽标由 published 计数推导
    published = await _published_version_counts(db, [s.id for s in skills], resource_type="skill")
    # v1.1.8:列表内容 = 最新已发布快照(无 published 回退主表初值)
    from cogni_agent.tool_center_service import versioning as ver

    snaps = await ver.latest_published_snapshots(db, "skill", [s.id for s in skills])
    # 0074:技能也能按用户分发,列表带上「已指派 M 个用户」徽标(与 Agent 同口径)
    assigned = await _agent_assigned_counts(db, [s.id for s in skills], resource_type="skill")
    meta = await _list_meta(db, "skill", skills)
    return {"items": [
        {**_skill_live_item(s, snaps.get(s.id)), "published_count": published.get(s.id, 0),
         "assigned_count": assigned.get(s.id, 0), **meta[s.id]}
        for s in skills]}


@router.post("/skills")
async def create_skill(req: SkillCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能创建 Skill")
    # v1.1.10:主表已无内容列(0050,同 agent description 0045)——创建时填写的
    # description/tool_usage/constraints 等直接派生首草稿进版本快照,
    # 「创建即进入编辑态」,与编辑保存(update_skill)同语义;发布后即线上内容。
    skill = Skill(
        name=req.name, tool_ids=req.tool_ids or [], visibility=req.visibility or _default_visibility(user),
        owner_user_id=user.user_id, scenic_id=user.scenic_id or "",
    )
    db.add(skill)
    await db.flush()
    from cogni_agent.tool_center_service import versioning as ver

    snap: dict = {}
    for field in ("description", "tool_usage", "suggested_mode", "knowledge_sources", "constraints"):
        val = getattr(req, field)
        if val is not None:
            snap[field] = val
    if req.tool_ids:
        snap["tool_ids"] = req.tool_ids  # binds 旧键,derive 时归一化为 tools pin
    if req.prompt:
        snap["prompt"] = req.prompt
    draft = await ver.derive_version(db, "skill", skill.id, base_version_id=None,
                                     snapshot=snap, created_by=user.user_id)
    return _skill_live_item(skill, draft.snapshot, prompt=req.prompt)



@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    skill = await db.get(Skill, skill_id)
    if not skill or skill.deleted_at:
        raise NotFound("Skill 不存在")
    # 列表按可见范围筛了,详情一直没筛 —— 拿到 id 就能读别的景区技能的完整 prompt
    _assert_visible(user, skill.owner_user_id, skill.scenic_id, skill.visibility, "Skill")
    # 详情内容 = 最新已发布快照,无 published 回退主表初值 + tc_prompts 旧值兜底
    from cogni_agent.tool_center_service import versioning as ver

    pub = await ver.latest_published_version(db, "skill", skill_id)
    prompt = (await db.execute(
        select(Prompt).where(Prompt.skill_id == skill_id).order_by(Prompt.created_at.desc()).limit(1)
    )).scalars().first()
    return _skill_live_item(skill, pub.snapshot if pub else None,
                            prompt=prompt.content if prompt else "")


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: str, req: SkillUpdateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    skill = await db.get(Skill, skill_id)
    if not skill or skill.deleted_at:
        raise NotFound("Skill 不存在")
    if not _can_manage(skill.owner_user_id, skill.scenic_id, user):
        raise _deny_manage(user, "Skill")
    # 收敛双轨(13 号文档 Phase 1 B1):编辑 = 保存草稿,内容改动落统一版本表草稿,
    # 不再直改主表、不再直写 tc_prompts;发布(POST /versions/{id}/publish)只冻结
    # published 快照,不再物化主表(15 号文档:快照即运行唯一数据源)。status 载荷属
    # G3 旧发布路径,已随 Phase 4 移除,载荷不再直改主表。
    # v1.1.10:内容键纯快照(主表列已删 0050),编辑/回显一律以草稿快照为准。
    from cogni_agent.tool_center_service import versioning as ver

    draft = await ver.latest_draft(db, "skill", skill_id)
    snapshot: dict = {}
    for field in ("name", "description", "suggested_mode", "knowledge_sources", "constraints"):
        val = getattr(req, field)
        if val is not None:
            snapshot[field] = val
    if req.visibility is not None:
        if user.role not in GROUP_ROLES and req.visibility in ("group", "public"):
            raise Forbidden("景区角色只能在 private/scenic 间选择可见范围")
        snapshot["visibility"] = req.visibility
    if req.prompt is not None:
        snapshot["prompt"] = req.prompt
    if req.tool_ids is not None:
        snapshot["tool_ids"] = req.tool_ids
        # 解绑工具时同步清掉它的使用说明(前端也会清,后端兜一道):以绑定集为准,
        # usage 只保留仍在绑定内的说明,否则孤儿说明会被合成进系统提示。
        # 底座 = 请求自带 usage,缺省时取当前草稿快照的 usage(主表无列可取)。
        keep = set(req.tool_ids)
        if req.tool_usage is not None:
            base_usage = req.tool_usage
        elif draft is not None:
            base_usage = (draft.snapshot or {}).get("tool_usage") or {}
        else:
            base_usage = {}
        snapshot["tool_usage"] = {k: v for k, v in base_usage.items() if k in keep}
    elif req.tool_usage is not None:
        snapshot["tool_usage"] = req.tool_usage
    # 发布/下线语义统一由版本操作(POST /versions/{id}/publish + offline)承载;
    # 主表 status 已废弃(0043 删列),旧 status 载荷字段随之一并移除。
    if snapshot:
        if draft is not None:
            await ver.update_version(db, draft.id, snapshot=snapshot, updated_by=user.user_id)
        else:
            await ver.derive_version(db, "skill", skill_id, base_version_id=None,
                                     snapshot=snapshot, created_by=user.user_id)
    await db.commit()
    # v1.1.10:响应取最新草稿快照视角(刚保存的内容;无草稿仅主表身份)
    draft = await ver.latest_draft(db, "skill", skill_id)
    return _skill_live_item(skill, draft.snapshot if draft else None)


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: str, force: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除 Skill = 软删除归档(deleted_at),保留 prompts 与全部 ResourceVersion 版本记录(发布物不可变)。
    默认有任一绑定/引用即拒绝;force=true 自动解除绑定(不级联删绑定方)后归档。"""
    skill = await db.get(Skill, skill_id)
    if not skill or skill.deleted_at:
        raise NotFound("Skill 不存在")
    if not _can_remove(skill.owner_user_id, skill.scenic_id, user):
        raise _deny_remove(user, "Skill")
    from cogni_agent.tool_center_service import versioning as ver

    # ① 被 Agent 绑定(主表 bound_skill_ids + agent 版本快照 bound_skills)
    agents = (await db.execute(select(Agent))).scalars().all()
    binders = {a.name for a in agents if skill_id in (a.bound_skill_ids or [])}
    rows = (await db.execute(
        select(ver.ResourceVersion).where(ver.ResourceVersion.resource_type == "agent")
    )).scalars().all()
    name_map = {a.id: a.name for a in agents}
    for r in rows:
        pins = (r.snapshot or {}).get("bound_skills") or []
        if any(isinstance(p, dict) and p.get("skill_id") == skill_id for p in pins):
            binders.add(name_map.get(r.resource_id, r.resource_id))
    # ② Skill 自身绑定的 tools:主表 tool_ids + skill 版本快照 tools
    tools = (await db.execute(select(Tool))).scalars().all()
    tool_map = {t.id: t.name for t in tools}
    bound = {tool_map.get(tid, tid) for tid in (skill.tool_ids or [])}
    sv_rows = (await db.execute(
        select(ver.ResourceVersion).where(
            ver.ResourceVersion.resource_type == "skill",
            ver.ResourceVersion.resource_id == skill_id,
        )
    )).scalars().all()
    for r in sv_rows:
        pins = (r.snapshot or {}).get("tools") or []
        for p in pins:
            if isinstance(p, dict) and p.get("tool_id"):
                bound.add(tool_map.get(p["tool_id"], p["tool_id"]))
    # ③ 固定问题引用(app_question_bank.skill_id,APP 业务表)
    # 23 号:框架层不直接摸业务表,走业务层(gateway_service.question_bank)暴露的
    # 只读查询做守卫——原「被编排组合引用」守卫随组合层(0055 删表)一并移除。
    from cogni_agent.gateway_service.question_bank import skills_referenced_by_questions

    refs = (await skills_referenced_by_questions(db, {skill_id})).get(skill_id) or []
    if not force:
        if binders:
            raise Forbidden(f"Skill「{skill.name}」已被 Agent 绑定:{', '.join(sorted(binders))},请先解除绑定再删除(或勾选强制删除自动解除)")
        if bound:
            raise Forbidden(
                f"Skill「{skill.name}」已绑定 {len(bound)} 个工具:{', '.join(sorted(bound))},"
                f"请先在技能编辑页解除工具绑定再删除(或勾选强制删除自动解除)")
        if refs:
            active_n = sum(1 for _, _, act in refs if act)
            raise Forbidden(
                f"Skill「{skill.name}」被 {len(refs)} 个固定问题引用(其中启用中 {active_n} 个),"
                f"请先在「APP 运营 · 问题库」改绑或清空执行技能再删除(或勾选强制删除自动解绑)")
    if force:
        # 强制删除:解除引用(不级联删绑定方)——主表解绑即可;
        # Agent/Skill 版本快照为不可变历史,不改写;问题行解绑但不删除(见下)。
        for a in agents:
            if skill_id in (a.bound_skill_ids or []):
                a.bound_skill_ids = [sid for sid in (a.bound_skill_ids or []) if sid != skill_id]
        # 问题行 skill_id 置空(行本身保留):承载 theme_code/param_template/预热档位等
        # 运营配置,是业务资产;解绑后退化为走 Triager 动态链路,功能仍可用。
        from cogni_agent.gateway_service.question_bank import unbind_questions_from_skill

        await unbind_questions_from_skill(db, skill_id)
    # 软删除归档:保留 prompts 与全部 ResourceVersion 版本记录(发布物不可变,审计/回滚/成本归因)
    skill.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "archived": True}


def _skill_dict(s: Skill) -> dict:
    # v1.1.10:主表仅身份/门面 + tool_ids 绑定键兜底;内容键(description/tool_usage/
    # constraints 等)只在版本快照,由 _skill_live_item 快照覆盖注入。
    return {
        "id": s.id, "name": s.name, "tool_ids": s.tool_ids,
        "owner_user_id": s.owner_user_id, "scenic_id": s.scenic_id, "visibility": s.visibility,
        "created_at": s.created_at,
    }


# v1.1.8 对齐 agent 语义:列表/详情「当前对外内容」= 最新已发布版本快照,
# 无 published 回退主表初值。update_skill 编辑全落草稿快照,主表内容列
# (description/constraints/tool_usage/tool_ids 等)是创建初值不随编辑更新 ——
# 展示/路由口径须以快照为准(与发布校验读草稿快照的修订配套)。
_SKILL_LIVE_KEYS = ("description", "tool_usage", "knowledge_sources", "constraints", "suggested_mode")


def _skill_live_item(s: Skill, snapshot: dict | None, prompt: str = "") -> dict:
    item = _skill_dict(s)
    item["prompt"] = prompt  # tc_prompts 旧表值兜底,新编辑落快照 prompt
    if not snapshot:
        return item
    for k in _SKILL_LIVE_KEYS:
        if k in snapshot and snapshot[k] is not None:
            item[k] = snapshot[k]
    # 绑定:快照 tools pin 列表 → 主表键 tool_ids(纯 id 列表,前端同字段消费)
    binds = snapshot.get("tools")
    if binds:
        item["tool_ids"] = [p["tool_id"] for p in binds if isinstance(p, dict) and p.get("tool_id")]
    if snapshot.get("prompt"):
        item["prompt"] = snapshot["prompt"]
    return item


# ───────────────────────────── Agent ─────────────────────────────


def _validate_history_rounds(intent_config: dict | None) -> None:
    """校验 intent_config.history_rounds(v1.1.7 多轮会话历史注入深度)。

    存在时必须为 0-5 的整数(0 是有效值 = 显式关闭注入);未配置/None 合法
    (语义 = 0,不注入)。越界/类型非法抛 ValueError -> FastAPI 422。
    """
    if not intent_config:
        return
    raw = intent_config.get("history_rounds")
    if raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 5:
        raise ValueError(
            "intent_config.history_rounds 必须是 0-5 的整数"
            f"(0 = 不注入多轮历史,1-5 = 注入最近 N 轮);当前值 {raw!r}"
        )


class AgentCreateRequest(BaseModel):
    name: str
    description: str | None = None
    avatar_url: str | None = None
    persona_desc: str | None = None
    tone_style: str | None = None
    opening_line: str | None = None
    bound_skill_ids: list[str] = []
    # v1.2(19 号 D4-①):多技能协作模式收进 agent(默认 chain;未绑定/单技能时无意义)
    composition_mode: Literal["chain", "dag", "pool"] = "chain"
    visibility: str | None = None
    rate_limit_qps: int = 5
    rate_limit_daily: int = 10000
    # v1.1.5 意图识别 agent 化:一个 Agent 一个意图识别。
    # 结构:{"enabled": bool, "mode": "always|side_effects|off", "model": str|None,
    #       "prompt": str|None, "history_rounds": int|None}
    #       # 27 号评审:recheck_before_clarify 已移除;history_rounds=多轮历史注入深度,0-5,缺省 0
    intent_config: dict | None = None

    @field_validator("intent_config")
    @classmethod
    def _validate_intent_history_rounds(cls, v: dict | None) -> dict | None:
        _validate_history_rounds(v)
        return v


class AgentUpdateRequest(BaseModel):
    description: str | None = None
    persona_desc: str | None = None
    tone_style: str | None = None
    opening_line: str | None = None
    bound_skill_ids: list[str] | None = None
    composition_mode: Literal["chain", "dag", "pool"] | None = None
    visibility: str | None = None
    rate_limit_qps: int | None = None
    rate_limit_daily: int | None = None
    # v1.1.5 意图识别 agent 化:intent_config 为 dict,更新时整体覆盖(传 None 不更新)
    intent_config: dict | None = None

    @field_validator("intent_config")
    @classmethod
    def _validate_intent_history_rounds(cls, v: dict | None) -> dict | None:
        _validate_history_rounds(v)
        return v


async def _visible_agents(db: AsyncSession, user: CurrentUser) -> list[Agent]:
    is_group = user.role in GROUP_ROLES
    conditions = [Agent.owner_user_id == user.user_id, Agent.visibility == "public"]
    if is_group:
        conditions.append(Agent.visibility == "group")
    if user.scenic_id:
        conditions.append((Agent.visibility == "scenic") & (Agent.scenic_id == user.scenic_id))
    agents = list((await db.execute(
        select(Agent).where(or_(*conditions), Agent.deleted_at.is_(None))
        .order_by(Agent.created_at.desc(), Agent.id.desc())
    )).scalars())

    # 版本指派隐含可见性:被指派给该用户/其景区/全局的 agent 也进入列表。
    # 已指派 = 该客户必须能用这个 agent,否则「钉了版本却看不到执行目标」逻辑说不过去。
    assigned_ids = await _assigned_agent_ids(db, user)
    if assigned_ids:
        seen = {a.id for a in agents}
        extra = list((await db.execute(select(Agent).where(
            Agent.id.in_(assigned_ids), Agent.deleted_at.is_(None)
        ).order_by(Agent.created_at.desc(), Agent.id.desc()))).scalars())
        agents.extend(a for a in extra if a.id not in seen)
    return agents


async def _assigned_agent_ids(db: AsyncSession, user: CurrentUser) -> set[str]:
    """对用户生效的版本指派涉及的全部 agent id(15 号文档 D9-A:分发收敛为仅 user 维度,
    无 scenic/global;用于 list_agents 把「不可见但已指派」的 agent 补进可见列表)。"""
    from cogni_agent.tool_center_service import versioning as ver

    rows = (await db.execute(
        select(ver.ResourceAssignment.agent_id).where(
            ver.ResourceAssignment.resource_type == "agent",
            ver.ResourceAssignment.scope_type == ver.SCOPE_USER,
            ver.ResourceAssignment.scope_id == user.user_id,
        )
    )).scalars().all()
    return set(rows)


@router.get("/agents")
async def list_agents(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    agents = await _visible_agents(db, user)
    # 15 号文档 §5.3 分发中心徽标:「已发布 N 版 / 已指派 M 个用户」
    published = await _published_version_counts(db, [a.id for a in agents], resource_type="agent")
    assigned = await _agent_assigned_counts(db, [a.id for a in agents])
    # v1.1.8 列表内容 = 最新已发布版本快照(详情左树可看其它版本),无 published 回退主表初值
    from cogni_agent.tool_center_service import versioning as ver

    snaps = await ver.latest_published_snapshots(db, "agent", [a.id for a in agents])
    meta = await _list_meta(db, "agent", agents)
    return {"items": [
        {**_agent_live_item(a, snaps.get(a.id)),
         "published_count": published.get(a.id, 0),
         "assigned_count": assigned.get(a.id, 0), **meta[a.id]} for a in agents]}


@router.get("/agents/by-user/{target_user_id}")
async def list_agents_by_user(target_user_id: str, user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """人视角分发(15 号文档 G4/D4-A):从「用户」出发查看其可见的全部 Agent,
    及每个 Agent 对这位用户的解析状态 ——
    `assigned`(已指派版本) / `not_distributed`(已发布但未分发,红标) / `no_published`(尚未发布)。

    候选范围 = 该用户可见的全部 Agent(owner / public / 所在景区 / 组内),另含「不可见但已指派」;
    使用者必为管理员(入口仅管理端分发中心人视角)。
    """
    target = await _assert_can_view_user(db, user, target_user_id)
    from cogni_agent.tool_center_service import versioning as ver

    # 1) 目标用户可见的 agents(语义对齐 _visible_agents,但以目标用户而非当前登录者为准)
    conditions = [Agent.owner_user_id == target_user_id, Agent.visibility == "public"]
    if target.role in GROUP_ROLES:
        conditions.append(Agent.visibility == "group")
    if target.scenic_id:
        conditions.append((Agent.visibility == "scenic") & (Agent.scenic_id == target.scenic_id))
    agents = list((await db.execute(
        select(Agent).where(or_(*conditions), Agent.deleted_at.is_(None))
        .order_by(Agent.created_at.desc(), Agent.id.desc())
    )).scalars())
    # 2) 该用户的全部分发指派(15 号 D9-A:scope 恒 user)
    assigns = (await db.execute(
        select(ver.ResourceAssignment).where(
            ver.ResourceAssignment.resource_type == "agent",
            ver.ResourceAssignment.scope_type == ver.SCOPE_USER,
            ver.ResourceAssignment.scope_id == target_user_id,
        )
    )).scalars().all()
    by_agent: dict[str, ver.ResourceAssignment] = {a.agent_id: a for a in assigns}
    # 3) 把「已指派但按可见性不可见」的 agent 也补进候选(防漏配)
    if assigns:
        known = {a.id for a in agents}
        extra = (await db.execute(select(Agent).where(
            Agent.id.in_(by_agent.keys()), Agent.deleted_at.is_(None)
        ))).scalars().all()
        agents.extend(a for a in extra if a.id not in known)
    # 4) 指派引用的版本号(一次 in_ 查询,避免 N+1)
    version_map: dict[str, int] = {}
    if by_agent:
        ver_rows = (await db.execute(select(ver.ResourceVersion).where(
            ver.ResourceVersion.id.in_([a.version_id for a in by_agent.values()])
        ))).scalars().all()
        version_map = {v.id: v.version for v in ver_rows}
    # 5) 已发布版本数:区分「已发布未分发(红标可指派)」与「尚未发布」
    published = await _published_version_counts(db, [a.id for a in agents], resource_type="agent")
    items = []
    for ag in agents:
        asg = by_agent.get(ag.id)
        if asg is not None:
            state = "assigned"
            extra = {"assigned_version_id": asg.version_id,
                     "assigned_version": version_map.get(asg.version_id),
                     "assignment_id": asg.id, "note": asg.note}
        elif published.get(ag.id, 0) > 0:
            state = "not_distributed"
            extra = {}
        else:
            state = "no_published"
            extra = {}
        items.append({
            "agent_id": ag.id, "name": ag.name, "avatar_url": ag.avatar_url,
            "visibility": ag.visibility, "scenic_id": ag.scenic_id, "active": ag.active,
            "published_count": published.get(ag.id, 0), "state": state, **extra,
        })
    return {"user": {"id": target.id, "name": target.name, "account": target.account,
                     "phone": target.phone, "role": target.role, "scenic_id": target.scenic_id},
            "items": items}


@router.post("/agents")
async def create_agent(req: AgentCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能创建 Agent")
    agent = Agent(
        name=req.name, avatar_url=req.avatar_url, persona_desc=req.persona_desc,
        tone_style=req.tone_style, opening_line=req.opening_line, bound_skill_ids=req.bound_skill_ids,
        composition_mode=req.composition_mode,
        visibility=req.visibility or _default_visibility(user), active=True,
        rate_limit_qps=req.rate_limit_qps, rate_limit_daily=req.rate_limit_daily,
        intent_config=req.intent_config,
        owner_user_id=user.user_id, scenic_id=user.scenic_id,
    )
    db.add(agent)
    await db.flush()
    # v1.1.9:主表已无 description 列(0045)——创建时填写的可编辑内容(description 等)
    # 直接派生首草稿进版本快照,「创建即进入编辑态」,与编辑保存(update_agent)同语义;
    # 未发布前列表/详情 description 为空(仅 published 快照才有),发布后即线上内容。
    from cogni_agent.tool_center_service import versioning as ver

    snap: dict = {}
    for field in ("description", "persona_desc", "tone_style", "opening_line",
                  "rate_limit_qps", "rate_limit_daily",
                  "intent_config", "composition_mode"):
        val = getattr(req, field)
        if val is not None:
            snap[field] = val
    if req.bound_skill_ids:
        snap["bound_skill_ids"] = req.bound_skill_ids
    draft = await ver.derive_version(db, "agent", agent.id, base_version_id=None,
                                     snapshot=snap, created_by=user.user_id)
    return _agent_live_item(agent, draft.snapshot)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    agent = await db.get(Agent, agent_id)
    if not agent or agent.deleted_at:
        raise NotFound("Agent 不存在")
    _assert_visible(user, agent.owner_user_id, agent.scenic_id, agent.visibility, "Agent")
    # v1.1.8 详情打开默认 = 最新已发布版本内容(header/首次派生草稿的基准);看历史切左树版本
    from cogni_agent.tool_center_service import versioning as ver

    snap = (await ver.latest_published_snapshots(db, "agent", [agent_id])).get(agent_id)
    return _agent_live_item(agent, snap)


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: AgentUpdateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent or agent.deleted_at:
        raise NotFound("Agent 不存在")
    if agent.locked and not _can_manage(agent.owner_user_id, agent.scenic_id, user):
        raise Forbidden("Agent 已锁定,仅创建者或 admin 可编辑")
    if not _can_manage(agent.owner_user_id, agent.scenic_id, user):
        raise _deny_manage(user, "Agent")
    # 收敛双轨(13 号文档 Phase 1 C1):编辑 = 保存草稿,内容改动落统一版本表草稿,
    # 不再直改主表;发布(POST /versions/{id}/publish)只冻结 published 快照,
    # 不再物化主表(15 号文档)。
    # v1.1.9:description 是纯版本化内容(主表列已删,0045)——与 persona/tone 等同为
    # 可编辑内容进草稿快照,随发布/回滚;不再直改主表,主表也无处兜底。
    # bound_skill_ids 走快照绑定,派生时 normalize 为 pin 格式。
    snapshot: dict = {}
    for field in ("description", "persona_desc", "tone_style", "opening_line",
                  "rate_limit_qps", "rate_limit_daily",
                  "intent_config", "composition_mode"):
        val = getattr(req, field)
        if val is not None:
            snapshot[field] = val
    if req.bound_skill_ids is not None:
        snapshot["bound_skill_ids"] = req.bound_skill_ids
    if req.visibility is not None:
        if user.role not in GROUP_ROLES and req.visibility in ("group", "public"):
            raise Forbidden("景区角色只能在 private/scenic 间选择可见范围")
        snapshot["visibility"] = req.visibility
    if snapshot:
        from cogni_agent.tool_center_service import versioning as ver
        draft = await ver.latest_draft(db, "agent", agent_id)
        if draft is not None:
            await ver.update_version(db, draft.id, snapshot=snapshot, updated_by=user.user_id)
        else:
            await ver.derive_version(db, "agent", agent_id, base_version_id=None,
                                     snapshot=snapshot, created_by=user.user_id)
    await db.commit()
    # v1.1.9:响应取最新草稿快照视角(description 等刚保存的内容;无草稿仅主表身份)
    from cogni_agent.tool_center_service import versioning as ver

    draft = await ver.latest_draft(db, "agent", agent_id)
    return _agent_live_item(agent, draft.snapshot if draft else None)


@router.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise NotFound("Agent 不存在")
    if not _can_manage(agent.owner_user_id, agent.scenic_id, user):
        raise Forbidden("无权限操作该 Agent")
    agent.active = not agent.active
    await db.commit()
    return _agent_live_item(agent, None)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, force: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """删除 Agent = 软删除归档(deleted_at),保留全部 ResourceVersion 版本记录(发布物不可变)。
    有指派或进行中任务时默认拒绝;force=true 自动解除指派后归档。
    进行中的任务(executor 指向该 agent)无论是否强制都不允许删除,防止运行中途定义消失导致崩溃。"""
    agent = await db.get(Agent, agent_id)
    if not agent or agent.deleted_at:
        raise NotFound("Agent 不存在")
    if not _can_remove(agent.owner_user_id, agent.scenic_id, user):
        raise _deny_remove(user, "Agent")
    from cogni_agent.tool_center_service import versioning as ver

    # 运行时引用保护:存在进行中/未结束的任务(executor 指向该 agent)时禁止删除
    from cogni_agent.control_plane_service.models import ACTIVE_STATUSES, Task

    running = (await db.execute(
        select(Task).where(Task.executor_id == agent_id, Task.status.in_(ACTIVE_STATUSES))
    )).scalars().all()
    if running:
        raise Forbidden(
            f"Agent「{agent.name}」有 {len(running)} 个进行中的任务,"
            f"请先等待任务结束或到任务中心终止后再删除")
    # 分发中心指派(15 号文档:仅 user 维度)
    assigns = (await db.execute(
        select(ver.ResourceAssignment).where(
            ver.ResourceAssignment.resource_type == "agent",
            ver.ResourceAssignment.agent_id == agent_id)
    )).scalars().all()
    if assigns and not force:
        scopes = ", ".join(f"用户 {a.scope_id}" for a in assigns)
        raise Forbidden(f"Agent「{agent.name}」已向 {len(assigns)} 个用户分发版本,请先在分发中心解除指派再删除(或勾选强制删除自动解除)")
    if force and assigns:
        for a in assigns:
            await db.delete(a)
    # 软删除归档:保留 ResourceVersion 版本记录(发布物不可变,审计/回滚/成本归因);
    # API Key 仅移除该 agent 的授权,不删除 Key 本身(同一 Key 可能同时允许多个 Agent)
    agent.deleted_at = datetime.now(timezone.utc)
    keys = (await db.execute(select(ApiKey))).scalars().all()
    for k in keys:
        if agent_id in (k.agent_allowed_ids or []):
            k.agent_allowed_ids = [aid for aid in (k.agent_allowed_ids or []) if aid != agent_id]
    await db.commit()
    return {"ok": True, "archived": True}


def _agent_dict(a: Agent) -> dict:
    """主表字典。description 无主表列(v1.1.9 删列,0045),由 _agent_live_item 从快照补键。"""
    return {
        "id": a.id, "name": a.name, "avatar_url": a.avatar_url,
        "persona_desc": a.persona_desc, "tone_style": a.tone_style, "opening_line": a.opening_line,
        "bound_skill_ids": a.bound_skill_ids, "visibility": a.visibility, "active": a.active,
        # v1.2(19 号 D4-①):协作模式主表值(快照优先,_agent_live_item 覆盖)
        "composition_mode": a.composition_mode,
        "locked": a.locked, "rate_limit_qps": a.rate_limit_qps, "rate_limit_daily": a.rate_limit_daily,
        # v1.1.5 意图识别 agent 化:随 agent 列表/详情一起返回,管理端可配置
        "intent_config": a.intent_config,
        "owner_user_id": a.owner_user_id, "scenic_id": a.scenic_id, "created_at": a.created_at,
    }


# v1.1.8 列表/详情「当前线上内容」= 最新已发布版本快照。
# v1.1.9:description 主表列已删除(0045),description 只从快照取(未发布 = None);
# persona/绑定/限流/意图等主表列仅兜底初值,发布 N 版后不再变化。
# 语义:编辑=草稿、发布冻结快照;列表始终代表「当前对外内容」,看历史在详情左树切版本。
_AGENT_LIVE_KEYS = (
    "description", "persona_desc", "tone_style", "opening_line",
    "rate_limit_qps", "rate_limit_daily",
    "intent_config",
    "composition_mode",
)


def _agent_live_item(a: Agent, snapshot: dict | None) -> dict:
    """主表字典 + 最新已发布快照内容覆盖(身份/门面字段 name/avatar/visibility 不走快照)。

    description 主表列已删(0045):无快照 / 快照缺键时为 None(未发布 agent 描述为空,
    属预期);快照有值(草稿/已发布)时随快照,与运行时(API Key / home)口径一致。
    """
    item = _agent_dict(a)
    item["description"] = (snapshot or {}).get("description")
    if not snapshot:
        return item
    for k in _AGENT_LIVE_KEYS:
        if k in snapshot and snapshot[k] is not None:
            item[k] = snapshot[k]
    # 绑定:快照 pin 格式 bound_skills → 主表键 bound_skill_ids(纯 id 列表,前端同字段消费)
    binds = snapshot.get("bound_skills")
    if binds:
        item["bound_skill_ids"] = [
            p["skill_id"] for p in binds
            if isinstance(p, dict) and p.get("skill_id")
        ]
    return item


async def _published_version_counts(db: AsyncSession, resource_ids: list[str],
                                    resource_type: str = "agent") -> dict[str, int]:
    """批量统计各资源已发布版本数(15 号文档:无默认版本概念,「已发布 N 版」徽标
    由 published 版本计数推导,列表/详情不再携带 default_version)。"""
    if not resource_ids:
        return {}
    from cogni_agent.tool_center_service import versioning as ver

    rows = (await db.execute(
        select(ver.ResourceVersion.resource_id, func.count())
        .where(
            ver.ResourceVersion.resource_type == resource_type,
            ver.ResourceVersion.resource_id.in_(resource_ids),
            ver.ResourceVersion.status == ver.STATUS_PUBLISHED,
        )
        .group_by(ver.ResourceVersion.resource_id)
    )).all()
    return {rid: n for rid, n in rows}


async def _agent_assigned_counts(db: AsyncSession, resource_ids: list[str],
                                 resource_type: str = "agent") -> dict[str, int]:
    """批量统计各资源已指派用户数(分发中心「已指派 M 个用户」徽标)。

    0074:resource_type 可为 agent 或 skill —— 技能也进按用户分发体系。"""
    if not resource_ids:
        return {}
    from cogni_agent.tool_center_service import versioning as ver

    rows = (await db.execute(
        select(ver.ResourceAssignment.agent_id, func.count())
        .where(ver.ResourceAssignment.resource_type == resource_type,
               ver.ResourceAssignment.agent_id.in_(resource_ids))
        .group_by(ver.ResourceAssignment.agent_id)
    )).all()
    return {aid: n for aid, n in rows}


# ───────────────────────────── MCP ─────────────────────────────

class McpCreateRequest(BaseModel):
    name: str
    url: str
    auth_type: str = "none"
    visibility: str | None = None


@router.get("/mcp-servers")
async def list_mcp_servers(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    # 列表顺序必须稳定:无 ORDER BY 时 PostgreSQL 按物理堆顺序返回,行 UPDATE(如 ping 更新
    # connect_status)后位置会变。统一按创建时间倒序 + id 兜底,与其它工具中心列表一致。
    # 可见范围:这是工具中心里唯一一个从来没筛过的列表 —— tools/skills/agents 都筛了,
    # 只有 MCP 服务端是全表返回,于是景区账号能看到全平台的 MCP 地址、鉴权方式与归属
    from cogni_agent.common import permissions as perm

    stmt = select(McpServer)
    if not perm.is_group_role(user.role):
        stmt = stmt.where(or_(McpServer.owner_user_id == user.user_id,
                              McpServer.visibility == "public",
                              perm.scenic_read_clause(user.role, user.scenic_id, McpServer.scenic_id)))
    rows = (await db.execute(
        stmt.order_by(McpServer.created_at.desc(), McpServer.id.desc())
    )).scalars().all()
    meta = await _list_meta(db, None, rows)
    return {"items": [{**_mcp_dict(m), **meta[m.id]} for m in rows]}


@router.post("/mcp-servers")
async def create_mcp_server(req: McpCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增 MCP Server")
    server = McpServer(
        name=req.name, url=req.url, auth_type=req.auth_type, connect_status="pending",
        owner_user_id=user.user_id, scenic_id=user.scenic_id, visibility=req.visibility or _default_visibility(user),
    )
    db.add(server)
    await db.commit()
    return _mcp_dict(server)


@router.post("/mcp-servers/{server_id}/discover")
async def discover_mcp_tools(server_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """M5-07/08:按 MCP 协议发现该 Server 的工具并同步为 tc_tools 记录。

    几个刻意的处理:
    - 导入的工具 `type="mcp"`,**不是 http**。MCP 工具必须通过 JSON-RPC tools/call
      调用,当成 http 工具导入的话调用时会退化成"对 MCP 端点发任意 HTTP 请求",
      服务端根本不认。
    - **重复发现是幂等的**:按 (来源 Server, 远端工具名) 匹配已有记录做更新,而不是
      每次都新插一批。否则点几次「发现工具」就会攒出一堆同名工具,而工具名在租户内
      是唯一的,第二次直接撞唯一索引失败。
    - 远端已删除的工具标记为 `active=False` 而不是物理删除:它可能已经被某个 Skill
      绑定,直接删会让那个 Skill 的解析出现"工具不存在"的告警且无从追溯。
    """
    server = await db.get(McpServer, server_id)
    if not server:
        raise NotFound("MCP Server 不存在")
    if not _can_manage(server.owner_user_id, server.scenic_id, user):
        raise Forbidden("无权管理该 MCP Server")

    from cogni_agent.tool_center_service.mcp.client import McpError
    from cogni_agent.tool_center_service.mcp.protocol import _resolve_mcp_auth

    auth_header = await _resolve_mcp_auth(db, server)
    try:
        discovered = await discover_tools(server.url, auth_header=auth_header)
    except McpError as exc:
        server.connect_status = "failed"
        await db.commit()
        raise Forbidden(f"MCP Server 连接失败:{exc}") from exc

    existing = (await db.execute(
        select(Tool).where(Tool.source_mcp_server_id == server_id)
    )).scalars().all()
    from cogni_agent.tool_center_service import versioning as ver

    # v1.2(0051):主表无内容列 —— remote_tool_name 存在版本快照的 type_config 里,
    # 匹配键须从快照取(老工具若有草稿/发布快照;纯孤儿无快照回退 name)。
    by_remote_name: dict[str, Tool] = {}
    for t in existing:
        snap = await _tool_live_snapshot(db, t.id) or {}
        tc = snap.get("type_config") or {}
        by_remote_name[tc.get("remote_tool_name") or t.name] = t

    imported, updated = [], []
    seen: set[str] = set()
    for spec in discovered:
        remote_name = spec["name"]
        seen.add(remote_name)
        type_config = {
            "server_id": server_id,
            "remote_tool_name": remote_name,
            # MCP 的 inputSchema 本身就是标准 JSON Schema,直接作为参数规范使用
            "input_schema": spec.get("input_schema") or {"type": "object", "properties": {}},
        }
        new_snap = {"description": spec.get("description", ""), "type": "mcp",
                    "type_config": type_config, "risk_level": "low"}
        found = by_remote_name.get(remote_name)
        if found is not None:
            cur = (await _tool_live_snapshot(db, found.id)) or {}
            # 内容没变(远端定义一致)不空发版;变了则覆盖草稿并发布新版本 —— MCP 工具
            # 的内容由远端定义驱动,同步即"覆盖+自动发布"(无人工编辑/发布门禁)。
            if (cur.get("description"), cur.get("type_config"), cur.get("type")) != \
                    (new_snap["description"], new_snap["type_config"], new_snap["type"]):
                draft = await ver.latest_draft(db, "tool", found.id)
                if draft is not None:
                    await ver.update_version(db, draft.id, snapshot=new_snap, updated_by=user.user_id)
                else:
                    draft = await ver.derive_version(db, "tool", found.id, base_version_id=None,
                                                     snapshot=new_snap, created_by=user.user_id)
                await ver.publish_version(db, draft.id)
                updated.append(found)
            found.active = True
            continue
        tool = Tool(
            # 工具名在租户内唯一,加 Server 名前缀降低撞名概率(两个 MCP Server
            # 各有一个 search 工具是很常见的)
            name=f"{server.name}_{remote_name}"[:64],
            active=True, owner_user_id=user.user_id, scenic_id=server.scenic_id,
            visibility=server.visibility, source_mcp_server_id=server_id,
        )
        db.add(tool)
        await db.flush()
        # 与 create_tool 同构:内容落草稿快照并自动发布 v1(叶子工具创建即生效)
        draft = await ver.derive_version(db, "tool", tool.id, base_version_id=None,
                                         snapshot=new_snap, created_by=user.user_id)
        await ver.publish_version(db, draft.id)
        imported.append(tool)

    disabled = []
    for remote_name, tool in by_remote_name.items():
        if remote_name not in seen and tool.active:
            tool.active = False   # 远端已删除:停用而不是物理删除(可能已被 Skill 绑定)
            disabled.append(tool)

    server.connect_status = "connected"
    await db.commit()
    return {
        "imported_count": len(imported), "updated_count": len(updated), "disabled_count": len(disabled),
        "tool_ids": [t.id for t in imported],
    }


@router.post("/mcp-servers/{server_id}/ping")
async def ping_mcp_server(server_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """连通性自检(握手 + 列一次工具),并把结果写回 connect_status。"""
    server = await db.get(McpServer, server_id)
    if not server:
        raise NotFound("MCP Server 不存在")

    from cogni_agent.tool_center_service.mcp.client import ping
    from cogni_agent.tool_center_service.mcp.protocol import _resolve_mcp_auth

    ok, detail = await ping(server.url, auth_header=await _resolve_mcp_auth(db, server))
    server.connect_status = "connected" if ok else "failed"
    await db.commit()
    return {"ok": ok, "detail": detail, "connect_status": server.connect_status}


def _mcp_dict(m: McpServer) -> dict:
    return {
        "id": m.id, "name": m.name, "url": m.url, "auth_type": m.auth_type, "connect_status": m.connect_status,
        "active": m.active, "locked": m.locked, "owner_user_id": m.owner_user_id, "scenic_id": m.scenic_id,
        "visibility": m.visibility, "created_at": m.created_at,
    }


# ══════════════════════════════════════════════════════════════════════════
# 统一版本管理:Tool / Skill / Agent 共用同一套接口
# ══════════════════════════════════════════════════════════════════════════
class VersionWriteRequest(BaseModel):
    snapshot: dict = {}
    note: str = ""
    base_version_id: str | None = None


# 路径刻意设计成不会歧义:
#   列表/创建 -> /resources/{type}/{id}/versions   (四段)
#   单版本操作 -> /versions/{version_id}/xxx        (三段)
# 早先两者都是三段(/versions/{a}/{b}),FastAPI 按声明顺序匹配,
# `POST /versions/ver_x/publish` 被 `POST /versions/{type}/{id}` 抢先吃掉,
# 报一个莫名其妙的 422「body Field required」。靠调整声明顺序能绕过,
# 但那很脆弱——以后再加一条路由又会撞。结构上消除歧义更可靠。
@router.get("/resources/{resource_type}/{resource_id}/versions")
async def list_resource_versions(resource_type: str, resource_id: str,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """列出某个资源的全部版本(倒序)。"""
    from cogni_agent.tool_center_service import versioning as ver

    # 同路径的 POST 判了归属,GET 没判 —— 版本快照里是完整的 prompt / type_config
    # (接口地址、Quick BI 连接),等于把别的景区的资源内容整本翻出来
    await _assert_resource_visible(db, resource_type, resource_id, user)
    items = await ver.list_versions(db, resource_type, resource_id)
    available, reason = await ver.is_resource_available(db, resource_type, resource_id)
    return {"items": items, "available": available, "unavailable_reason": reason}


@router.post("/resources/{resource_type}/{resource_id}/versions")
async def create_resource_version(resource_type: str, resource_id: str, req: VersionWriteRequest,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """派生一个 draft 版本(打开编辑区)。

    v1.1.10 幂等化:每资源同时最多一个草稿(derive_version 拒绝重复草稿)——而
    create_skill 已改为「创建即派生首草稿」,新建 skill 点「列表一键发布」时草稿已存在,
    直接复用现有草稿返回(不覆盖内容),避免前端发布流程报「已存在未发布的草稿」。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_manage(db, resource_type, resource_id, user)
    existing = await ver.latest_draft(db, resource_type, resource_id)
    if existing is not None:
        return {"id": existing.id, "version": existing.version, "status": existing.status,
                "reused": True}
    row = await ver.derive_version(db, resource_type, resource_id,
                                   base_version_id=req.base_version_id, snapshot=req.snapshot,
                                   created_by=user.user_id, note=req.note)
    return {"id": row.id, "version": row.version, "status": row.status, "reused": False}


@router.put("/versions/{version_id}")
async def edit_resource_version(version_id: str, req: VersionWriteRequest,
                                user: CurrentUser = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    """保存草稿(编辑区)。

    **只允许编辑 draft**:保存仅更新内容,版本号不变、不留痕、不影响任何客户 ——
    「编辑保存与版本无关」,只有「发布」才对外。
    **已发布的版本不能直接改**(那等于绕过版本管理改线上行为),请基于它派生新草稿再改。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user)

    row = await ver.get_version(db, version_id)
    if row.status != ver.STATUS_DRAFT:
        raise Forbidden(
            f"当前状态为「{row.status}」,不能直接编辑。"
            f"请先通过「基于它新建编辑」派生一个草稿再改,"
            f"改完发布即可,原版本不受影响。")
    updated = await ver.update_version(db, version_id, snapshot=req.snapshot, note=req.note or None,
                                     updated_by=user.user_id)
    label = f"v{updated.version}" if updated.version is not None else "草稿"
    return {"id": updated.id, "version": updated.version, "status": updated.status,
            "message": f"{label} 已保存(未发布,不影响任何客户)。"}


@router.post("/versions/{version_id}/precheck")
async def precheck_resource_version(version_id: str, user: CurrentUser = Depends(get_current_user),
                                    db: AsyncSession = Depends(get_db)):
    """发布预检(dry-run):不改变任何状态,只做依赖全链路解析。

    推荐方案:发布前对绑定资源做 agent->skill->tool 闭包解析,返回依赖树与问题清单;
    version:null 的模糊 pin 会预览将固化为哪个版本号。正式发布内部会做同样校验,
    预检不过会被拦截。供前端「发布预检」按钮调用。
    """
    from cogni_agent.tool_center_service import versioning as ver

    return await ver.precheck_version(db, version_id)


@router.post("/versions/{version_id}/publish")
async def publish_resource_version(version_id: str, user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    """发布草稿:draft -> published,快照冻结。

    推荐方案发布语义(15 号文档:无默认版本概念):
    - 发布预检:依赖闭包解析,拦截 DRAFT / 不存在 / 已下线,version:null 自动固化;
    - 版本号在发布时分配(草稿不占号),生成 locked_deps 依赖锁;
    - 发布仅产生 published 快照,不再写主表、不再隐含「默认/生效」语义;
      零 user 指派时返回黄条 hint(引导去分发中心按用户指派)。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user, removing=False)

    # G3 收敛(13 号文档 Phase 4):评测门禁(M5-15)自旧 POST /agents/{id}/publish
    # 迁移到 agent 版本发布统一路径 —— 发布 agent 草稿前真实跑一遍该 agent 的评测
    # 用例集,通过率低于阈值即阻断(未登记用例默认放行);skill/tool 发布不设门禁。
    # 旧主表直改发布(publish_agent / 主表 status)已删除,此为唯一发布门禁点。
    draft = await ver.get_version(db, version_id)
    if draft.resource_type == "agent":
        from cogni_agent.governance_event_bus_service.evals import run_eval_gate

        gate = await run_eval_gate(draft.resource_id, "v0", "candidate", target_type="agent",
                                   scenic_id=user.scenic_id, user_id=user.user_id)
        if not gate["passed"]:
            raise ValidationError("发布被评测门禁阻断:" + "；".join(gate["blocking_reasons"]))

    result = await ver.publish_version(db, version_id, updated_by=user.user_id)
    return {"ok": True, "version": result["version"],
            "hint": result["hint"], "assigned_users": result.get("assigned_users", 0),
            "locked_deps": result.get("locked_deps")}


@router.post("/versions/{version_id}/offline")
async def offline_resource_version(version_id: str, user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    """下线某个 published 版本。仅影响该版本自己。

    被指派 / 被 pin(版本级引用) -> 禁止下线(15 号文档:无默认版本概念,不再有「默认不可下线」
    拦截);全部 published 下线后该资源才整体不可用。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user, removing=False)

    return await ver.offline_version(db, version_id, updated_by=user.user_id)


@router.post("/versions/{version_id}/refresh-pins")
async def refresh_version_pins(version_id: str, user: CurrentUser = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """一键刷新依赖:把草稿快照里所有绑定 pin 更新为各绑定资源的最新 published。

    只允许草稿(published 快照已冻结,不可变)。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user, removing=False)

    return await ver.refresh_pins(db, version_id)





@router.post("/versions/{version_id}/rollback")
async def rollback_resource_version(version_id: str, user: CurrentUser = Depends(get_current_user),
                                    db: AsyncSession = Depends(get_db)):
    """回滚:基于历史 published 版本派生新草稿并重新发布(15 号文档:无「设为默认」语义,
    旧版本本身冻结不改;回滚后的发布与普通发布一致,对谁生效仍由 user 指派决定)。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user, removing=False)

    result = await ver.rollback_to(db, version_id, updated_by=user.user_id)
    return {"ok": True, "version": result["version"], "message": result["message"]}


@router.delete("/versions/{version_id}")
async def discard_resource_version(version_id: str, user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    """放弃编辑:丢弃草稿(编辑区),删除未发布的草稿。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_version_manage(db, version_id, user, removing=True)

    return await ver.discard_draft(db, version_id)


@router.get("/resources/{resource_type}/{resource_id}/pin-options")
async def resource_pin_options(resource_type: str, resource_id: str,
                               version_id: str | None = None,
                               user: CurrentUser = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    """编辑区版本下拉数据:每个绑定资源 + 其全部版本(draft/published 都可选,标注状态)。

    v1.1.5 版本规则:未发布版本也可被选到(编辑/预览/联调),只有已发布的才能
    生产生效 / 被钉版本 pin(门禁在 publish 预检与指派校验,这里不设限)。

    - 不传 `version_id`:基于主表(默认版本)编辑,绑定取自主表当前内容;
    - 传 `version_id`:基于该版本快照的绑定列表。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_visible(db, resource_type, resource_id, user)

    return await ver.pin_options(db, resource_type, resource_id, version_id=version_id)


class AssignmentCreateRequest(BaseModel):
    """分发指派请求(15 号文档:仅 user 维度):把某用户钉到该 agent 的某个 published 版本。"""
    user_id: str             # 被指派用户;重复指派同一用户 = 切换版本
    version_id: str
    note: str = ""


@router.get("/agents/{agent_id}/assignments")
async def list_agent_assignments(agent_id: str, user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """指派列表:该 agent 的所有用户指派(用户 / 版本 / 时间 / 操作人)。"""
    from cogni_agent.tool_center_service import versioning as ver

    # 指派表里是「谁、哪个景区、钉在哪个版本」——增删都判了归属,查一直没判
    await _assert_resource_visible(db, "agent", agent_id, user)
    return await ver.list_assignments(db, agent_id)


@router.post("/agents/{agent_id}/assignments")
async def upsert_agent_assignment(agent_id: str, req: AssignmentCreateRequest,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """创建 / 覆盖用户指派:同一 (agent, user) 重复指派 = 切换版本(15 号文档 D9-A 仅 user)。

    目标版本必须已 published,且用户对该版本可见(§6.3:指派即授权)。
    """
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_manage(db, "agent", agent_id, user)

    return await ver.upsert_assignment(
        db, agent_id, req.user_id, req.version_id,
        created_by=user.user_id, note=req.note)


@router.delete("/agents/{agent_id}/assignments/{assignment_id}")
async def remove_agent_assignment(agent_id: str, assignment_id: str,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """解除指派:该用户不再拥有该 agent 的指定版本(15 号文档 fail-closed:
    解除后用户调用将报 agent_not_distributed,不再回落任何默认版本)。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_manage(db, "agent", agent_id, user, removing=True)

    return await ver.remove_assignment(db, assignment_id)


# ── Skill 分发(0074):技能与 Agent 同构地按用户指派 ────────────────────
# 为什么技能也要独立分发:APP 端有「直接点一个技能提问」的入口(POST /app/chat 带
# skill_id),这条路径不经过任何 agent,版本与授权就无处可依 —— 挂靠 agent 快照的
# pin 只能解决「agent 里绑的那些技能」。独立指派后两条路径口径一致:
# 有指派用指派版本,无指派 fail-closed(超管豁免)。

@router.get("/skills/{skill_id}/assignments")
async def list_skill_assignments(skill_id: str, user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """指派列表:该技能的所有用户指派(用户 / 版本 / 时间 / 操作人)。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_visible(db, "skill", skill_id, user)
    return await ver.list_assignments(db, skill_id, resource_type="skill")


@router.post("/skills/{skill_id}/assignments")
async def upsert_skill_assignment(skill_id: str, req: AssignmentCreateRequest,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """创建 / 覆盖用户指派:同一 (skill, user) 重复指派 = 切换版本。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_manage(db, "skill", skill_id, user)

    return await ver.upsert_assignment(
        db, skill_id, req.user_id, req.version_id,
        created_by=user.user_id, note=req.note, resource_type="skill")


@router.delete("/skills/{skill_id}/assignments/{assignment_id}")
async def remove_skill_assignment(skill_id: str, assignment_id: str,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """解除指派:该用户不再能直接调用该技能(fail-closed;仍可经绑定它的 Agent 间接用到)。"""
    from cogni_agent.tool_center_service import versioning as ver

    await _assert_resource_manage(db, "skill", skill_id, user, removing=True)

    return await ver.remove_assignment(db, assignment_id)


@router.get("/skills/by-user/{target_user_id}")
async def list_skills_by_user(target_user_id: str, user: CurrentUser = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    """人视角分发(技能):从「用户」出发看其可见的全部 Skill 及对这位用户的状态 ——
    `assigned` / `not_distributed`(已发布未分发) / `no_published`(尚未发布)。

    与 `/agents/by-user/{id}` 同构,前端「按用户管理」一屏里两块并列。
    """
    target = await _assert_can_view_user(db, user, target_user_id)
    from cogni_agent.tool_center_service import versioning as ver

    conditions = [Skill.owner_user_id == target_user_id, Skill.visibility == "public"]
    if target.role in GROUP_ROLES:
        conditions.append(Skill.visibility == "group")
    if target.scenic_id:
        conditions.append((Skill.visibility == "scenic") & (Skill.scenic_id == target.scenic_id))
    skills = list((await db.execute(
        select(Skill).where(or_(*conditions), Skill.deleted_at.is_(None))
        .order_by(Skill.created_at.desc(), Skill.id.desc())
    )).scalars())

    assigns = (await db.execute(
        select(ver.ResourceAssignment).where(
            ver.ResourceAssignment.resource_type == "skill",
            ver.ResourceAssignment.scope_type == ver.SCOPE_USER,
            ver.ResourceAssignment.scope_id == target_user_id,
        )
    )).scalars().all()
    by_skill: dict[str, ver.ResourceAssignment] = {a.agent_id: a for a in assigns}
    if assigns:
        known = {s.id for s in skills}
        extra = (await db.execute(select(Skill).where(
            Skill.id.in_(by_skill.keys()), Skill.deleted_at.is_(None)
        ))).scalars().all()
        skills.extend(s for s in extra if s.id not in known)

    version_map: dict[str, int] = {}
    if by_skill:
        ver_rows = (await db.execute(select(ver.ResourceVersion).where(
            ver.ResourceVersion.id.in_([a.version_id for a in by_skill.values()])
        ))).scalars().all()
        version_map = {v.id: v.version for v in ver_rows}
    published = await _published_version_counts(db, [s.id for s in skills], resource_type="skill")
    snaps = await ver.latest_published_snapshots(db, "skill", [s.id for s in skills])
    items = []
    for sk in skills:
        asg = by_skill.get(sk.id)
        if asg is not None:
            state = "assigned"
            extra = {"assigned_version_id": asg.version_id,
                     "assigned_version": version_map.get(asg.version_id),
                     "assignment_id": asg.id, "note": asg.note}
        elif published.get(sk.id, 0) > 0:
            state = "not_distributed"
            extra = {}
        else:
            state = "no_published"
            extra = {}
        snap = snaps.get(sk.id) or {}
        items.append({
            "skill_id": sk.id, "name": sk.name, "description": snap.get("description"),
            "visibility": sk.visibility, "scenic_id": sk.scenic_id,
            "published_count": published.get(sk.id, 0), "state": state, **extra,
        })
    return {"user": {"id": target.id, "name": target.name, "account": target.account,
                     "phone": target.phone, "role": target.role, "scenic_id": target.scenic_id},
            "items": items}
