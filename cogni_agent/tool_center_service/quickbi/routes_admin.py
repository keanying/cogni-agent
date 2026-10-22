"""工具中心 · Quick BI 管理端接口(/api/admin/quickbi/*,JWT)。

供「工具中心 · Quick BI」页面与操练台验证使用。所有执行类接口都支持
stream=true(SSE,事件定义见 events.py)与 stream=false(等待完成返回运行记录)。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel, Field

from cogni_agent.common.errors import Forbidden, ValidationError
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.tool_center_service.quickbi import service as svc
from cogni_agent.tool_center_service.quickbi.http_helpers import collect, guard, primed, read_upload, sse

router = APIRouter(prefix="/admin/quickbi", tags=["工具中心 · Quick BI"])


def actor_of(user: CurrentUser) -> svc.Actor:
    # 超管种子账号的 scenic_id 是空串,统一归一为 None(= 集团级)
    return svc.Actor(user_id=user.user_id, scenic_id=user.scenic_id or None, role=user.role, source="console")


# ── 概览 ─────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(user: CurrentUser = Depends(get_current_user)):
    """能力清单、三个业务场景、环境变量连接、文件限制。"""
    return svc.overview()


# ── 连接 ─────────────────────────────────────────────────────────────

class ConnectionReq(BaseModel):
    name: str | None = None
    server_domain: str | None = None
    api_key: str | None = None
    api_secret: str | None = Field(default=None, description="None=不修改;空串=清空")
    user_token: str | None = Field(default=None, description="None=不修改;空串=清空")
    use_trial: bool | None = None
    default_locale: str | None = None
    is_default: bool | None = None
    api_enabled: bool | None = None
    active: bool | None = None
    visibility: str | None = None
    scenic_id: str | None = Field(default=None, description="绑定景区(取自景区主数据 /admin/scenics/catalog);"
                                                           "空串=解绑回集团级。仅集团角色可改")


class SkillConfigPaste(BaseModel):
    """直接粘贴 Quick BI 控制台「一键复制 skill 配置」的文本(key: value 每行一个)。"""
    text: str
    name: str | None = None
    connection_id: str | None = None
    is_default: bool | None = True
    scenic_id: str | None = Field(default=None, description="绑定景区(取自景区主数据);空=集团级。仅集团角色可设")


@router.get("/connections")
async def list_connections(user: CurrentUser = Depends(get_current_user)):
    views = await svc.list_connections(actor_of(user))
    # 0075:带上分配人数 —— 0 表示该连接尚未启用白名单(按景区/可见性判定)
    counts = await svc.grant_counts([v.id for v in views if not v.virtual])
    return {"items": [{**svc.connection_dict(v),
                       "granted_count": counts.get(v.id, 0),
                       "restricted": counts.get(v.id, 0) > 0} for v in views]}


# ── 连接分配(0075:Quick BI 进分发中心) ──────────────────────────────────

class GrantReq(BaseModel):
    user_id: str
    note: str = ""


@router.get("/connections/{connection_id}/grants")
async def list_connection_grants(connection_id: str, user: CurrentUser = Depends(get_current_user)):
    """该连接分配给了哪些人。`restricted=false` = 还没启用白名单。"""
    return await svc.list_grants(actor_of(user), connection_id)


@router.post("/connections/{connection_id}/grants")
async def grant_connection(connection_id: str, req: GrantReq,
                           user: CurrentUser = Depends(get_current_user)):
    """把连接分配给某个用户。**第一条分配会让该连接转为白名单制**(其他人从此不可见)。"""
    return await svc.grant_connection(actor_of(user), connection_id, req.user_id, req.note)


@router.delete("/connections/{connection_id}/grants/{grant_id}")
async def revoke_connection_grant(connection_id: str, grant_id: str,
                                  user: CurrentUser = Depends(get_current_user)):
    """解除分配。解除最后一条 = 该连接回到按景区/可见性判定。"""
    return await svc.revoke_grant(actor_of(user), grant_id)


@router.get("/grants/by-user/{target_user_id}")
async def list_grants_by_user(target_user_id: str, user: CurrentUser = Depends(get_current_user)):
    """人视角:该用户对每个 Quick BI 连接的状态(granted / not_granted / open)。"""
    return await svc.grants_by_user(actor_of(user), target_user_id)


@router.post("/connections")
async def create_connection(req: ConnectionReq, user: CurrentUser = Depends(get_current_user)):
    if not req.name:
        raise ValidationError("连接名称不能为空")
    v = await svc.save_connection(actor_of(user), req.model_dump())
    return svc.connection_dict(v)


@router.post("/connections/paste")
async def paste_skill_config(req: SkillConfigPaste, user: CurrentUser = Depends(get_current_user)):
    """解析「一键复制 skill 配置」文本(支持 key: value / key：value / key=value)并保存为连接。"""
    import re

    data: dict[str, Any] = {}
    for line in req.text.splitlines():
        m = re.match(r"\s*[-*]?\s*([A-Za-z_]+)\s*[::=]\s*(.*?)\s*$", line)
        if m and m.group(1).lower() in ("api_key", "api_secret", "user_token", "server_domain"):
            data[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
    if not data:
        raise ValidationError("没有识别到 api_key / api_secret / user_token / server_domain")
    data["name"] = req.name or ("Quick BI" if not req.connection_id else None)
    data["is_default"] = req.is_default
    data["use_trial"] = not (data.get("api_key") and data.get("api_secret"))
    if req.scenic_id is not None:
        # 粘贴建连接时就能选景区 —— 否则建完还得再进编辑改一次,而这一步最容易被忘
        data["scenic_id"] = req.scenic_id
    v = await svc.save_connection(actor_of(user), data, req.connection_id)
    return {**svc.connection_dict(v), "parsed_keys": sorted(k for k in data if data[k] not in (None, ""))}


@router.put("/connections/{connection_id}")
async def update_connection(connection_id: str, req: ConnectionReq, user: CurrentUser = Depends(get_current_user)):
    v = await svc.save_connection(actor_of(user), req.model_dump(), connection_id)
    return svc.connection_dict(v)


@router.get("/connections/{connection_id}/transfer-plan")
async def connection_transfer_plan(connection_id: str, user: CurrentUser = Depends(get_current_user)):
    """删除前先算一笔账:这条连接名下有哪些东西要转、可以转给谁。

    界面点「删除」先调它 —— 有资产就先走转移,而不是删完才发现一批看板全废了。
    """
    return await svc.connection_transfer_plan(actor_of(user), connection_id)


class TransferReq(BaseModel):
    to_connection_id: str = Field(description="接手的连接。资产能不能打开由它决定(一条连接 = 一套 Quick BI 凭证)")
    owner_user_id: str | None = Field(default=None,
                                      description="可选:把资产/文件的归属人也改成这个人。纯记账,不影响能不能打开")


@router.post("/connections/{connection_id}/transfer")
async def transfer_connection(connection_id: str, req: TransferReq,
                              user: CurrentUser = Depends(get_current_user)):
    """把这条连接名下的**数据资产 / 上传文件 / 问题**整体转到另一条连接。

    会话与运行记录不转:那是「当时用哪条连接跑的」,改了就不是史实了。
    """
    return await svc.transfer_connection(actor_of(user), connection_id, req.to_connection_id,
                                         owner_user_id=req.owner_user_id)


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, force: bool = False,
                            user: CurrentUser = Depends(get_current_user)):
    """删除连接。还被问题/资产引用时默认拒绝(并说清被谁引用了);
    `force=true` 强制删除,同时把指着它的问题上的连接清空 —— 之后按景区自动重挑,
    而不是留下一个指着已删除连接的死 id。"""
    return await svc.delete_connection(actor_of(user), connection_id, force=force)


@router.post("/connections/{connection_id}/default")
async def set_default(connection_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.set_default(actor_of(user), connection_id)
    return {"ok": True}


@router.post("/connections/{connection_id}/check")
async def check_connection(connection_id: str, user: CurrentUser = Depends(get_current_user)):
    """连通性自检:验证签名 + user_token,并返回可问数数据集数量。失败不抛错,返回 ok=false 与原因。"""
    return await svc.check_connection(actor_of(user), connection_id)


@router.get("/connections/{connection_id}/cubes")
async def list_cubes(connection_id: str, keyword: str | None = None, user: CurrentUser = Depends(get_current_user)):
    """当前 user_token 可问数的数据集(实时从 Quick BI 拉取)。"""
    return {"items": await guard(svc.list_cubes(actor_of(user), connection_id, keyword))}


# ── 数据资产:仪表板 / 数据集 ─────────────────────────────────────────────

class DashboardReq(BaseModel):
    connection_id: str | None = None
    url: str = Field(description="仪表板地址 / 数据门户地址 / pageId")
    name: str | None = None
    # 绑定景区:提问时按景区找该景区的看板,所以登记时就要说清楚这块看板属于谁。
    # 留空 = 集团级资产,所有景区都能用。
    scenic_id: str | None = None


class DatasetReq(BaseModel):
    connection_id: str | None = None
    cube_id: str
    name: str | None = None
    scenic_id: str | None = None
    source_asset_id: str | None = Field(default=None, description="从哪块仪表板拉出来登记的")


@router.post("/dashboards/parse")
async def parse_dashboard(req: DashboardReq, user: CurrentUser = Depends(get_current_user)):
    """预览仪表板结构(不保存):图表、字段、查询控件、引用数据集、候选问题。"""
    return await guard(svc.parse_dashboard(actor_of(user), req.connection_id, req.url))


@router.get("/assets")
async def list_assets(connection_id: str | None = None, kind: str | None = None,
                      scenic_id: str | None = None,
                      user: CurrentUser = Depends(get_current_user)):
    return {"items": await svc.list_assets(actor_of(user), connection_id, kind, scenic_id=scenic_id)}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str, user: CurrentUser = Depends(get_current_user)):
    return svc.asset_dict(await svc.get_asset(actor_of(user), asset_id), full=True)


@router.post("/assets/dashboards")
async def register_dashboard(req: DashboardReq, user: CurrentUser = Depends(get_current_user)):
    return await guard(svc.register_dashboard(actor_of(user), req.connection_id, req.url, req.name,
                                             scenic_id=req.scenic_id))


@router.post("/assets/datasets")
async def register_dataset(req: DatasetReq, user: CurrentUser = Depends(get_current_user)):
    return await guard(svc.register_dataset(actor_of(user), req.connection_id, req.cube_id, req.name,
                                           scenic_id=req.scenic_id,
                                           source_asset_id=getattr(req, "source_asset_id", None)))


@router.get("/assets/{asset_id}/cubes")
async def board_cubes(asset_id: str, user: CurrentUser = Depends(get_current_user)):
    """这块仪表板里用到哪些数据集(以及哪些已经登记过了)。

    比「拉取整条连接下的全部 cube」准:一条连接上挂着两块板、几十个 cube,
    人得自己认哪个属于哪块板 —— 认错了就是拿 A 板的集去问 B 板的问题,答得出来,还不报错。
    """
    return await guard(svc.board_cubes(actor_of(user), asset_id))


class DatasetBatchReq(BaseModel):
    connection_id: str | None = None
    cubes: list[dict] = Field(default_factory=list, description="要登记的数据集,每项 {cube_id, name?}")
    all: bool = Field(default=False, description="一键登记全部:不传 cubes,实时拉该连接下全部可问数数据集")
    scenic_id: str | None = Field(default=None, description="归属景区;不传则跟着来源仪表板走")
    source_asset_id: str | None = Field(default=None,
                                        description="这批集是从哪块仪表板拉出来的。记下来之后,"
                                                    "分组与「删板连带删集」都按它算,不再靠 meta.cubes 反查")


@router.post("/assets/datasets/batch")
async def register_datasets(req: DatasetBatchReq, user: CurrentUser = Depends(get_current_user)):
    """批量登记数据集(一块板动辄十几二十个,一个一个点既慢又容易漏)。

    已登记的自动跳过而不是整批失败 —— 多选时勾到一两个已登记的很正常。
    """
    return await guard(svc.register_datasets(actor_of(user), req.connection_id, req.cubes,
                                             scenic_id=req.scenic_id,
                                             source_asset_id=req.source_asset_id, all_live=req.all))


@router.delete("/assets/datasets/bulk")
async def clear_datasets(connection_id: str | None = None, user: CurrentUser = Depends(get_current_user)):
    """清空这条连接下已登记的数据集(仪表板与上传文件不动)。还被问题引用的按条跳过。"""
    return await guard(svc.clear_datasets(actor_of(user), connection_id))


class AssetUpdateReq(BaseModel):
    name: str | None = Field(default=None, description="显示名称;不传 = 不改")
    scenic_id: str | None = Field(
        default=None,
        description="归属景区;空串 = 解绑回集团级(所有景区共用);不传 = 不改。"
                    "改的是同一行(asset_id 不变),所以指着这个资产的问题一条都不用动")
    category: str | None = Field(default=None, description="自定义分类;空串 = 清掉分类(未分类);不传 = 不改")


class AssetCategoryReq(BaseModel):
    ids: list[str] = Field(description="要设分类的资产 id(一次可多个)")
    category: str = Field(default="", description="分类名;空串 = 清掉分类")


@router.put("/assets/{asset_id}")
async def update_asset(asset_id: str, req: AssetUpdateReq,
                       user: CurrentUser = Depends(get_current_user)):
    """改已登记资产的名称 / 归属景区。

    没有这个接口的话,改归属只能「删了重登记」—— 而删掉的那一刻,所有指着这个
    asset_id 的问题全部失效(发起时报「资产不存在」),运营根本不会把这件事
    和刚才那次删除联系起来。
    """
    return await guard(svc.update_asset(actor_of(user), asset_id,
                                        name=req.name, scenic_id=req.scenic_id, category=req.category))


@router.post("/assets/category")
async def set_assets_category(req: AssetCategoryReq, user: CurrentUser = Depends(get_current_user)):
    """批量设分类:在数据集列表里勾几个、输入分类回车即可(不需要先建分类)。逐条按改资产的权限判。"""
    actor = actor_of(user)
    items = []
    for aid in dict.fromkeys(req.ids):
        items.append(await guard(svc.update_asset(actor, aid, category=req.category)))
    return {"items": items, "category": (req.category or "").strip() or None}


@router.post("/assets/{asset_id}/refresh")
async def refresh_asset(asset_id: str, user: CurrentUser = Depends(get_current_user)):
    return await guard(svc.refresh_asset(actor_of(user), asset_id))


@router.get("/assets/{asset_id}/delete-plan")
async def asset_delete_plan(asset_id: str, user: CurrentUser = Depends(get_current_user)):
    """删这个资产会连带删掉什么:它下面的数据集、打在它上面的问题。

    界面点删除先调它 —— 一句「确定删除吗」不说清会连带删掉 17 个集、12 条问题,
    人按下确定时并不知道自己同意了什么。
    """
    return await svc.asset_delete_plan(actor_of(user), asset_id)


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str, cascade: bool = False, force: bool = False,
                       user: CurrentUser = Depends(get_current_user)):
    """删除已登记的数据资产,**连带删掉它下面的数据集与问题**(APP 与管理端随即都不再显示)。

    下面还有东西时,不带 `cascade=true` 会被拒绝并说清会连带删掉几个集、几条问题;
    先调 `/delete-plan` 拿清单给人看,确认后再带 `cascade=true` 执行。
    `force` 是 `cascade` 的旧名,保留兼容。
    """
    return await svc.delete_asset(actor_of(user), asset_id, cascade=cascade, force=force)


class AdoptReq(BaseModel):
    questions: list[dict] | None = None


@router.post("/assets/{asset_id}/adopt-questions")
async def adopt_questions(asset_id: str, req: AdoptReq, user: CurrentUser = Depends(get_current_user)):
    """把仪表板候选问题(或传入的问题列表)批量保存为固定问题。"""
    return {"items": await svc.adopt_suggestions(actor_of(user), asset_id, req.questions)}


# ── 上传文件 ─────────────────────────────────────────────────────────

@router.post("/files")
async def upload_file(file: UploadFile = File(...), connection_id: str | None = Form(default=None),
                      user: CurrentUser = Depends(get_current_user)):
    """上传 xls/xlsx/csv/doc/docx(≤5MB)。表格文件会立即上传到 Quick BI 问数并本地转 Markdown 供解读。"""
    name, content = await read_upload(file)
    return await guard(svc.save_file(actor_of(user), connection_id or None, name, content))


@router.get("/files")
async def list_files(connection_id: str | None = None, user: CurrentUser = Depends(get_current_user)):
    return {"items": await svc.list_files(actor_of(user), connection_id)}


@router.get("/files/{file_id}/download")
async def download_file(file_id: str, user: CurrentUser = Depends(get_current_user)):
    """下载上传文件原文(文档解析产出的汇总 Excel 也在这里下)。"""
    from urllib.parse import quote

    f = await svc._load_file(actor_of(user), file_id)
    return Response(content=f.content or b"", media_type=svc.fs.MIME.get(svc.fs.suffix(f.file_name),
                                                                      "application/octet-stream"),
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.file_name)}"})


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.delete_file(actor_of(user), file_id)
    return {"ok": True}


# ── 执行(操练台直连) ─────────────────────────────────────────────────

class RunReq(BaseModel):
    connection_id: str | None = None
    capability: str | None = Field(default=None, description="空或 auto = 按问题意图自动路由;"
                                                             "dataset_qa 问数 / insight 解读 / "
                                                             "dashboard_qa 报表 / report 报告")
    question: str = ""
    target: dict = Field(default_factory=dict)
    locale: str | None = None
    stream: bool = True
    scenic_id: str | None = Field(default=None, description="本次提问归属的景区;不传按调用者所属景区")
    fixed_question_id: str | None = Field(default=None, description="按固定问题发起:其余字段留空即沿用该条配置")
    session_id: str | None = Field(default=None, description="归属的会话;传了这一轮才会出现在该会话的历史里")
    # 操练台里问就是要看现在的结果;回放加速缓存还会让这一轮不进会话历史(没有新的运行记录)
    use_cache: bool = Field(default=False, description="true = 允许回放固定问题的加速缓存")


@router.post("/run")
async def run(req: RunReq, user: CurrentUser = Depends(get_current_user)):
    agen = svc.run_stream(actor_of(user), connection_id=req.connection_id, capability=req.capability,
                          question=req.question, target=req.target, locale=req.locale,
                          mode="fixed" if req.fixed_question_id else "direct",
                          report_async=not req.stream, scenic_id=req.scenic_id,
                          fixed_question_id=req.fixed_question_id, session_id=req.session_id,
                          use_cache=req.use_cache)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor_of(user), c["run_id"]) if c["run_id"] else c


# ── 场景一:固定问题 ─────────────────────────────────────────────────

class FixedReq(BaseModel):
    connection_id: str | None = None
    title: str | None = None
    question: str | None = None
    capability: str | None = None
    target: dict | None = None
    locale: str | None = None
    category: str | None = None
    domain: str | None = Field(default=None, description="数据域:票务/财务/舆情/客群/核销/营销/流量/画像…,可填新值;空串=清空")
    ask_type: str | None = Field(default=None, description="请求类别:qa 问数 / insight 解读 / report 报告;不填按能力推导")
    model_instruction: str | None = Field(
        default=None,
        description="口径说明:真正发给模型的那段约束(取哪个时间范围、什么算异常、答多长、"
                    "先结论还是先数据)。与「问题」分开是因为按钮上的文案要短,"
                    "而模型需要的恰恰是那些啰嗦的约束 —— 发起时它会**追加**在问题后面,"
                    "不替换用户的原话。传空串 = 清空")
    # 三级标签(0077):只传编码,中文名由后端按标签表回填。三级要么全传、要么全不传;
    # 传空串三件套 = 把这条问题从 APP 问题树上摘下来(管理端仍可用)。
    topic_id: str | None = Field(default=None, description="主题编码,取自 /admin/app-ops/dimension-labels")
    dimension_id: str | None = Field(default=None, description="维度编码")
    metric_id: str | None = Field(default=None, description="指标编码;问题最终挂在指标下面")
    scenic_id: str | None = Field(default=None, description="归属景区(取自景区主数据 /admin/scenics/catalog);"
                                                           "空串=集团级(全景区通用)。仅集团角色可改")
    sort_order: int | None = None
    active: bool | None = None
    visibility: str | None = None
    # 加速(预热):后台定时提前跑好,前台/APP 再问这条问题时直接回放,不必现跑。
    # 两个分钟数留空 = 跟随全局默认(刷新 30 / 存储 60),不写死是为了改默认值时
    # 这些问题能跟着走;存储时间必须 >= 刷新时间,否则两次预热之间会有空窗。
    prewarm_enabled: bool | None = Field(default=None, description="是否开启加速(预热)")
    prewarm_interval_minutes: int | None = Field(
        default=None, description="刷新时间(分钟):每隔多久后台重新跑一遍;留空=全局默认 30")
    prewarm_ttl_minutes: int | None = Field(
        default=None, description="存储时间(分钟):跑出来的结果多久内可以直接回放;留空=全局默认 60")


class PrewarmReq(BaseModel):
    scenic_id: str | None = Field(default=None, description="给哪个景区预热;不传用问题绑定的景区")


class FixedRunReq(BaseModel):
    params: dict = Field(default_factory=dict, description="问题里 {{变量}} 的取值")
    target: dict | None = None
    stream: bool = True
    scenic_id: str | None = Field(default=None, description="换个景区问同一条固定问题;不传沿用问题绑定的景区")
    # 管理台上点「运行 / 重新问」就是要看现在的结果:默认不读缓存,并且先把这条问题的缓存清掉 ——
    # 否则改完口径再问,APP 上回放的还是之前那份
    use_cache: bool = Field(default=False, description="true = 允许回放加速缓存;默认 false = 清除缓存后现跑")


class BatchRunReq(BaseModel):
    ids: list[str]
    params: dict = Field(default_factory=dict)


@router.get("/fixed-questions")
async def list_fixed(connection_id: str | None = None, category: str | None = None, active: bool | None = None,
                     keyword: str | None = None, domain: str | None = None, ask_type: str | None = None,
                     scenic_id: str | None = None, topic_id: str | None = None,
                     dimension_id: str | None = None, metric_id: str | None = None,
                     unlabeled: bool | None = None,
                     prewarm: bool | None = Query(None, description="true=已开加速 / false=未开"),
                     last_status: str | None = Query(None, description="最近运行:success/failed/running;"
                                                                        "none=从未运行"),
                     user: CurrentUser = Depends(get_current_user)):
    """固定问题列表。`unlabeled=true` 单独筛「还没挂三级标签」的 —— 这些问题
    在 APP 的问题树上没有挂载点,前台看不到,是最常见的一类「配了没生效」。
    keyword 同时匹配标题、问题原文与问题 ID。"""
    return {"items": await svc.list_fixed(actor_of(user), connection_id=connection_id, category=category,
                                          active=active, keyword=keyword, domain=domain, ask_type=ask_type,
                                          scenic_id=scenic_id, topic_id=topic_id, dimension_id=dimension_id,
                                          metric_id=metric_id, unlabeled=unlabeled, prewarm=prewarm,
                                          last_status=last_status)}


@router.get("/fixed-questions/queue-stats")
async def queue_stats(user: CurrentUser = Depends(get_current_user)):
    """任务队列水位(排队 / 运行中,按来源分),问题列表右上角常驻展示。全局数,不按景区切。"""
    return await svc.queue_stats()


class BatchOpReq(BaseModel):
    op: str = Field(description="set_interval / copy / insight / delete")
    ids: list[str]
    interval_minutes: int | None = Field(default=None, description="set_interval:刷新时间(分钟)")
    ttl_minutes: int | None = Field(default=None, description="set_interval:存储时间(分钟);"
                                                               "不填 = 原值,小于刷新时间时自动抬到刷新时间")
    prewarm_enabled: bool | None = Field(default=None, description="set_interval:true 一并开启 / "
                                                                   "false 关闭 / 不传 = 不动开关")
    connection_id: str | None = Field(default=None, description="copy:复制到哪条连接(景区跟着连接走)")
    stream: bool = True


@router.post("/fixed-questions/batch-op")
async def batch_op(req: BatchOpReq, user: CurrentUser = Depends(get_current_user)):
    """固定问题批量运维:逐条执行、逐条回报(SSE:start / item / done),一条失败不中断其余。
    非流式把 item 聚成数组返回,供脚本调用。单次最多 50 条。"""
    agen = svc.batch_op(actor_of(user), req.op, req.ids, interval_minutes=req.interval_minutes,
                        ttl_minutes=req.ttl_minutes, prewarm_enabled=req.prewarm_enabled,
                        connection_id=req.connection_id)
    if req.stream:
        return sse(await primed(agen))
    items: list[dict] = []
    done: dict = {}
    async for e in agen:
        if e["type"] == "item":
            items.append(e)
        elif e["type"] == "done":
            done = e
    return {"items": items, **done}


class PromptReq(BaseModel):
    name: str | None = None
    content: str | None = None
    description: str | None = None
    visibility: str | None = None


@router.get("/prompts")
async def list_prompts(keyword: str | None = None, user: CurrentUser = Depends(get_current_user)):
    """口径说明库。挑一条 → 抄进固定问题的「口径说明」(不是引用:改库不改写已配的问题)。"""
    return {"items": await svc.list_prompts(actor_of(user), keyword=keyword)}


@router.post("/prompts")
async def create_prompt(req: PromptReq, user: CurrentUser = Depends(get_current_user)):
    return await svc.save_prompt(actor_of(user), req.model_dump())


@router.put("/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, req: PromptReq, user: CurrentUser = Depends(get_current_user)):
    return await svc.save_prompt(actor_of(user), req.model_dump(), prompt_id)


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.delete_prompt(actor_of(user), prompt_id)
    return {"ok": True}


class ScenicDatasetReq(BaseModel):
    scenic_id: str
    connection_id: str
    cube_id: str
    cube_name: str | None = None
    prewarm_enabled: bool | None = Field(default=None, description="景区级加速预热开关;不传 = 新建默认开、修改不动")


class ScenicPrewarmReq(BaseModel):
    enabled: bool


@router.get("/scenic-datasets")
async def list_scenic_datasets(user: CurrentUser = Depends(get_current_user)):
    """景区数据集映射清单(景区 → 连接 + 数据集 + 预热开关)。"""
    return {"items": await svc.list_scenic_datasets(actor_of(user))}


@router.post("/scenic-datasets")
async def upsert_scenic_dataset(req: ScenicDatasetReq, user: CurrentUser = Depends(get_current_user)):
    """新增 / 修改一个景区的映射(按 scenic_id 覆盖)。只有集团管理角色可改。"""
    return await guard(svc.upsert_scenic_dataset(actor_of(user), scenic_id=req.scenic_id,
                                                 connection_id=req.connection_id, cube_id=req.cube_id,
                                                 cube_name=req.cube_name, prewarm_enabled=req.prewarm_enabled))


@router.put("/scenic-datasets/{scenic_id}/prewarm")
async def set_scenic_dataset_prewarm(scenic_id: str, req: ScenicPrewarmReq,
                                     user: CurrentUser = Depends(get_current_user)):
    return await svc.set_scenic_dataset_prewarm(actor_of(user), scenic_id, req.enabled)


@router.delete("/scenic-datasets/{scenic_id}")
async def delete_scenic_dataset(scenic_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.delete_scenic_dataset(actor_of(user), scenic_id)
    return {"ok": True}


@router.get("/fixed-questions/{fq_id}/thinking")
async def latest_thinking(fq_id: str, user: CurrentUser = Depends(get_current_user)):
    """该问题最近一次成功的**解读/报告类运行**的思考过程(只读)。

    与 /runs 分开:列表出身的运行不带 result(太大),拿它去取必然是空框;
    这里只回一段 reasoning。"""
    return await svc.latest_thinking(actor_of(user), fq_id)


@router.get("/fixed-questions/domains")
async def list_domains(user: CurrentUser = Depends(get_current_user)):
    """数据域候选:预置 8 个 + 现场已经用过的自定义值。前端下拉用,但允许直接填新的。"""
    return {"items": await svc.list_domains(actor_of(user)),
            "presets": svc.PRESET_DOMAINS,
            "ask_types": [{"key": k, "label": v} for k, v in svc.ASK_TYPES.items()]}


@router.get("/catalog")
async def catalog(connection_id: str | None = None, scenic_id: str | None = None,
                  ask_type: str | None = None, domain: str | None = None, include_assets: bool = True,
                  user: CurrentUser = Depends(get_current_user)):
    """与开放平台 /v1/quickbi/catalog 同一份数据,管理端用来预览外部调用方会看到什么。"""
    return await guard(svc.question_catalog(actor_of(user), connection_id=connection_id, scenic_id=scenic_id,
                                            ask_type=ask_type, domain=domain, include_assets=include_assets))


@router.post("/fixed-questions")
async def create_fixed(req: FixedReq, user: CurrentUser = Depends(get_current_user)):
    return await svc.save_fixed(actor_of(user), req.model_dump())


@router.put("/fixed-questions/{fq_id}")
async def update_fixed(fq_id: str, req: FixedReq, user: CurrentUser = Depends(get_current_user)):
    return await svc.save_fixed(actor_of(user), req.model_dump(), fq_id)


@router.delete("/fixed-questions/{fq_id}")
async def delete_fixed(fq_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.delete_fixed(actor_of(user), fq_id)
    return {"ok": True}


@router.post("/fixed-questions/batch-run")
async def batch_run(req: BatchRunReq, user: CurrentUser = Depends(get_current_user)):
    if not req.ids:
        raise ValidationError("ids 不能为空")
    return {"items": await svc.batch_run(actor_of(user), req.ids, params=req.params)}


@router.post("/fixed-questions/{fq_id}/prewarm")
async def prewarm_fixed(fq_id: str, req: PrewarmReq | None = None,
                        user: CurrentUser = Depends(get_current_user)):
    """立刻重新跑一遍并刷新缓存 —— 列表上的「加速」按钮。

    同步等结果(报告类除外,那类创建即返回),这样按钮能直接给出成功/失败,
    而不是「已提交」然后让人自己去别处找结果。
    """
    return await guard(svc.prewarm_now(actor_of(user), fq_id,
                                       scenic_id=(req.scenic_id if req else None)))


@router.post("/fixed-questions/{fq_id}/run")
async def run_fixed(fq_id: str, req: FixedRunReq, user: CurrentUser = Depends(get_current_user)):
    actor = actor_of(user)
    if not req.use_cache and svc.can_clear_cache(actor, await svc.get_fixed(actor, fq_id)):
        # 能管这条问题的人重问 = 先清缓存;管不了的(景区账号问集团级问题)只是这次不读缓存,
        # 不替别的景区把加速结果清掉
        await svc.clear_fixed_cache(actor, fq_id)
    agen = svc.run_fixed(actor, fq_id, params=req.params, target_override=req.target, scenic_id=req.scenic_id,
                         report_async=not req.stream, use_cache=req.use_cache)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor_of(user), c["run_id"]) if c["run_id"] else c


@router.post("/fixed-questions/{fq_id}/clear-cache")
async def clear_fixed_cache(fq_id: str, user: CurrentUser = Depends(get_current_user)):
    """清除这条问题的加速缓存:之前的结果不再回放,下一次提问现跑。"""
    return await svc.clear_fixed_cache(actor_of(user), fq_id)


# ── 场景二:对话 ─────────────────────────────────────────────────────

class SessionReq(BaseModel):
    connection_id: str | None = None
    capability: str = "auto"
    target: dict = Field(default_factory=dict)
    title: str | None = None
    scenic_id: str | None = Field(default=None, description="会话归属的景区;之后每一轮都沿用它")


class MessageReq(BaseModel):
    question: str
    capability: str | None = None
    target: dict | None = None
    stream: bool = True


@router.get("/sessions")
async def list_sessions(user: CurrentUser = Depends(get_current_user)):
    return {"items": await svc.list_sessions(actor_of(user))}


@router.post("/sessions")
async def create_session(req: SessionReq, user: CurrentUser = Depends(get_current_user)):
    return await svc.create_session(actor_of(user), connection_id=req.connection_id, capability=req.capability,
                                    target=req.target, title=req.title, scenic_id=req.scenic_id)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    return await svc.session_detail(actor_of(user), session_id)


@router.delete("/sessions/{session_id}")
async def archive_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    await svc.archive_session(actor_of(user), session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, req: MessageReq, user: CurrentUser = Depends(get_current_user)):
    agen = svc.chat(actor_of(user), session_id, req.question, capability=req.capability, target=req.target,
                    report_async=not req.stream)
    if req.stream:
        return sse(await primed(agen))
    c = await collect(agen)
    return await svc.get_run(actor_of(user), c["run_id"]) if c["run_id"] else c


# ── 场景三:报告 ─────────────────────────────────────────────────────

class ReportReq(BaseModel):
    connection_id: str | None = None
    question: str
    target: dict = Field(default_factory=dict, description="file_ids / file_id / asset_id / page_id / web_search")
    locale: str | None = None
    stream: bool = False


@router.post("/reports")
async def create_report(req: ReportReq, user: CurrentUser = Depends(get_current_user)):
    """stream=false(默认):创建任务后立即返回运行记录(status=running);stream=true:SSE 跟踪到完成。"""
    if req.stream:
        agen = svc.run_stream(actor_of(user), connection_id=req.connection_id, capability="report",
                              question=req.question, target={**req.target, "wait": True}, locale=req.locale,
                              mode="report")
        return sse(await primed(agen))
    return await guard(svc.start_report(actor_of(user), connection_id=req.connection_id, question=req.question,
                                        target=req.target, locale=req.locale))


@router.get("/reports/{run_id}")
async def get_report(run_id: str, refresh: bool = True, user: CurrentUser = Depends(get_current_user)):
    """查询报告;refresh=true 时实时向 Quick BI 拉取一次进度并落库。"""
    if refresh:
        return await guard(svc.refresh_report(actor_of(user), run_id))
    return await svc.get_run(actor_of(user), run_id)


@router.get("/reports/{run_id}/stream")
async def stream_report(run_id: str, user: CurrentUser = Depends(get_current_user)):
    return sse(await primed(svc.stream_report(actor_of(user), run_id)))


# ── 运行记录 ─────────────────────────────────────────────────────────

@router.get("/runs")
async def list_runs(capability: str | None = None, mode: str | None = None, status: str | None = None,
                    session_id: str | None = None, fixed_question_id: str | None = None,
                    q: str | None = Query(None, description="按问题名称搜:问句正文命中,"
                                          "或所属固定问题的标题命中(固定问题存的是渲染后的问句,"
                                          "与标题不是同一段文本,两路都算)"),
                    task_id: str | None = Query(None, description="触发这次运行的任务号(APP 对话 / 执行引擎)"),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    user: CurrentUser = Depends(get_current_user)):
    return await svc.list_runs(actor_of(user), capability=capability, mode=mode, status=status,
                               session_id=session_id, fixed_question_id=fixed_question_id,
                               q=q, task_id=task_id, limit=limit, offset=offset)


@router.post("/runs/abort-all")
async def abort_all_runs(user: CurrentUser = Depends(get_current_user)):
    """一键终止:把可见范围内**所有**「进行中」的运行判死(failed + error_code=aborted)。

    给「列表里卡着一堆 running 没人收」这种情况用的 —— 多数是发版/重启掐断留下的僵尸,
    执行体早就不在了,除了改状态没有别的办法。已收尾的记录不受影响。
    """
    return await svc.abort_runs(actor_of(user))


class AbortRunsReq(BaseModel):
    ids: list[str] | None = Field(default=None, description="只终止这几条;不传 = 全部进行中的")
    stream: bool = True


@router.post("/runs/abort-batch")
async def abort_batch_runs(req: AbortRunsReq, user: CurrentUser = Depends(get_current_user)):
    """批量终止:逐条流式回报(start / item / done),一条失败不中断其余;非流式把 item 聚成数组返回。"""
    agen = svc.abort_runs_stream(actor_of(user), req.ids)
    if req.stream:
        return sse(await primed(agen))
    items: list[dict] = []
    done: dict = {}
    async for e in agen:
        if e["type"] == "item":
            items.append(e)
        elif e["type"] == "done":
            done = e
    return {"items": items, **done}


@router.get("/runs/{run_id}/debug")
async def run_debug(run_id: str, format: str = "json", user: CurrentUser = Depends(get_current_user)):
    """导出这一次运行的完整执行详情(配置脱敏 + 上游原样事件 + 归一化事件 + 结果分区)。

    format=md 时直接返回可粘贴的 Markdown —— 排障时对方要的是能读的东西,
    而不是一坨 JSON。凭证不在导出内容里。
    """
    bundle = await svc.run_debug(actor_of(user), run_id)
    if (format or "").lower() in ("md", "markdown", "text"):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(svc.run_debug_markdown(bundle), media_type="text/markdown; charset=utf-8")
    return bundle


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: CurrentUser = Depends(get_current_user)):
    return await svc.get_run(actor_of(user), run_id)


@router.post("/runs/{run_id}/abort")
async def abort_run(run_id: str, user: CurrentUser = Depends(get_current_user)):
    """终止单条运行。本进程内还在跑的那条会立刻中断(并关掉上游连接);
    已经跑完的返回「无需终止」而不是报错。"""
    return await svc.abort_runs(actor_of(user), run_id)


# ── 一键生成工具(供 Skill / Agent 绑定) ──────────────────────────────

class PublishToolReq(BaseModel):
    name: str
    description: str = ""
    connection_id: str | None = None
    capability: str = "dataset_qa"
    target: dict = Field(default_factory=dict)
    fixed_question_id: str | None = None
    report_wait: bool = False
    max_rows: int = 30
    risk_level: str = "low"
    visibility: str | None = None


class DashboardSkillReq(BaseModel):
    name: str | None = None              # 空 = 「{仪表板名} · 数据查询」
    with_insight: bool = True            # 同时生成一个「解读」工具
    visibility: str | None = None


@router.post("/assets/{asset_id}/skill")
async def generate_dashboard_skill(asset_id: str, req: DashboardSkillReq,
                                   user: CurrentUser = Depends(get_current_user)):
    """仪表板 → 专属查询技能(对齐官方「仪表板技能生成」)。

    生成两样东西:打在这块看板上的 quickbi 工具(问数,可选再加一个解读),和一个绑定了它们的
    技能**草稿** —— 系统提示按官方模板的章节由看板结构拼出(见 skillgen)。发布走技能的正常流程。
    """
    if user.role == "group_viewer":
        raise Forbidden("group_viewer 为只读角色,不能生成技能")
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.tool_center_service.quickbi import skillgen
    from cogni_agent.tool_center_service.routes import (
        SkillCreateRequest, ToolCreateRequest, create_skill, create_tool,
    )

    asset = await svc.get_asset(actor_of(user), asset_id)
    if asset.kind != "dashboard":
        raise ValidationError("只有仪表板能生成查询技能")
    if not (asset.meta or {}).get("charts"):
        raise ValidationError("这块仪表板还没有结构信息(图表 / 度量 / 维度),请先在数据资产里「刷新结构」")
    title = (asset.meta or {}).get("name") or asset.name
    caps = [("qa", "dashboard_qa", "问数")] + ([("insight", "insight", "解读")] if req.with_insight else [])
    tools: dict[str, dict] = {}
    async with get_sessionmaker()() as db:
        for key, capability, label in caps:
            body = ToolCreateRequest(
                name=f"{title} · {label}"[:120], type="quickbi", visibility=req.visibility,
                description=f"「{title}」仪表板{label}:{svc.cap.CAPABILITIES[capability]['desc']}",
                type_config={"connection_id": asset.connection_id, "capability": capability,
                             "target": {"asset_id": asset.id}, "fixed_question_id": "", "report_wait": False,
                             "max_rows": 30})
            tools[key] = await create_tool(body, user=user, db=db)
    spec = skillgen.build_skill(name=asset.name, url=asset.url, page_id=asset.ref_id, meta=asset.meta or {},
                                tool_names={k: t["name"] for k, t in tools.items()})
    usage = {tools["qa"]["id"]: "问这块看板上的具体数据(多少、排名、对比、占比)"}
    if "insight" in tools:
        usage[tools["insight"]["id"]] = "解读这块看板:趋势、异常、归因"
    async with get_sessionmaker()() as db:
        skill = await create_skill(SkillCreateRequest(
            name=(req.name or spec["name"])[:120], description=spec["description"], prompt=spec["prompt"],
            tool_ids=[t["id"] for t in tools.values()], tool_usage=usage, visibility=req.visibility,
        ), user=user, db=db)
        await db.commit()
    return {"skill": skill, "tools": list(tools.values()), "prompt": spec["prompt"]}


@router.post("/tools")
async def publish_tool(req: PublishToolReq, user: CurrentUser = Depends(get_current_user)):
    """把「连接 + 能力 + 目标数据」生成为 type=quickbi 的工具并发布 v1,可直接绑定到 Skill。"""
    if user.role == "group_viewer":
        raise Forbidden("group_viewer 为只读角色,不能创建工具")
    if req.capability not in svc.CAPABILITY_KEYS | {"auto"}:
        raise ValidationError(f"未知能力:{req.capability}")
    actor = actor_of(user)
    conn = await svc.get_connection(actor, req.connection_id)
    if req.fixed_question_id:
        await svc.get_fixed(actor, req.fixed_question_id)
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.tool_center_service.routes import ToolCreateRequest, create_tool

    description = req.description or f"Quick BI {svc.cap.CAPABILITIES.get(req.capability, {}).get('label', '问数')}"
    body = ToolCreateRequest(
        name=req.name, description=description, type="quickbi",
        type_config={"connection_id": conn.id, "capability": req.capability, "target": req.target,
                     "fixed_question_id": req.fixed_question_id or "", "report_wait": req.report_wait,
                     "max_rows": req.max_rows},
        risk_level=req.risk_level, visibility=req.visibility,
    )
    async with get_sessionmaker()() as db:
        return await create_tool(body, user=user, db=db)
