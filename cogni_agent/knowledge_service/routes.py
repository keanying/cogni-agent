"""知识中心管理接口——数据库知识(数据源/表登记/自定义数据源)、网络知识(搜索引擎配置/
白名单/站点档案)、向量知识(存储配置/知识库/文档/检索测试)。全部是真实实现:表登记走
真实的数据库内省(introspect.py),文档入库走真实的切分+嵌入(ingest.py),检索测试
走真实的向量检索(retrieve.py)。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.db import get_db
from cogni_agent.common.errors import Forbidden, NotFound
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.knowledge_service.database_knowledge import introspect as db_introspect
from cogni_agent.knowledge_service.database_knowledge.catalog import DbCatalog, DbCustomSource, DbDatasource
from cogni_agent.knowledge_service.embeddings import embed_text
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.ingest import ingest_document
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.retrieve import retrieve as vector_retrieve
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import (
    KbChunk, KbDocument, KnowledgeBase, VectorDbConfig, VectorStorageConfig,
)
from cogni_agent.knowledge_service.websearch_knowledge.fetch import WebSiteArchive, WebWhitelist
from cogni_agent.knowledge_service.websearch_knowledge.search import WebSearchConfig
from cogni_agent.tool_center_service.tool.registry import GROUP_ROLES

router = APIRouter(prefix="/admin/knowledge", tags=["知识中心"])


def _can_write(role: str) -> bool:
    from cogni_agent.common import permissions as perm

    return perm.can_create(role)


def _read_clause(user: CurrentUser, column):
    """列表的可见范围:景区角色只看本景区 + 集团级(scope=group / scenic_id 为空)。"""
    from cogni_agent.common import permissions as perm

    return perm.scenic_read_clause(user.role, user.scenic_id, column)


def _assert_read(user: CurrentUser, obj_scenic: str | None, what: str) -> None:
    """详情接口的读门禁 —— 这一层最容易漏:列表筛了、详情没筛,拿到 id 就能读。"""
    from cogni_agent.common import permissions as perm

    perm.assert_can_read(user.role, user.scenic_id, obj_scenic, what)


async def _readable_datasource(db: AsyncSession, user: CurrentUser, ds_id: str) -> DbDatasource:
    """取数据源并判可见 —— 内省/DDL/预览这几条都直连真实数据库,
    漏判的后果不是「多看见一行配置」,而是**把别的景区生产库的表结构和数据读出来**。"""
    ds = await db.get(DbDatasource, ds_id)
    if not ds:
        raise NotFound("数据源不存在")
    if (ds.scope or "") != "group":
        _assert_read(user, ds.scenic_id, "数据源")
    return ds


def _assert_write(user: CurrentUser, obj_scenic: str | None, what: str) -> None:
    """新增/修改:集团只读拒绝;景区角色只能动本景区的(集团级配置能用不能改)。"""
    from cogni_agent.common import permissions as perm

    perm.assert_can_write_obj(user.role, user.scenic_id, obj_scenic, what)


def _assert_delete(user: CurrentUser, obj_scenic: str | None, what: str) -> None:
    """删除:景区操作员不能删;景区角色只能删本景区的。

    这几个删除接口原先**一个角色判定都没有** —— 只读账号拿到 id 就能把数据源停用掉。
    """
    from cogni_agent.common import permissions as perm

    perm.assert_can_delete_obj(user.role, user.scenic_id, obj_scenic, what)


def _default_scope(user: CurrentUser) -> str:
    return "group" if user.role in GROUP_ROLES else "scenic"


# ───────────────────────────── 数据库知识:数据源 ─────────────────────────────

class DatasourceCreateRequest(BaseModel):
    name: str
    db_type: str  # mysql / selectdb / starrocks / clickhouse / postgres
    connection_config: dict
    scope: str | None = None


@router.get("/datasources")
async def list_datasources(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(DbDatasource)
    if user.scenic_id:
        stmt = stmt.where((DbDatasource.scope == "group") | (DbDatasource.scenic_id == user.scenic_id))
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_ds_dict(d) for d in rows]}


@router.post("/datasources")
async def create_datasource(req: DatasourceCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增数据源")
    ds = DbDatasource(
        name=req.name, db_type=req.db_type, connection_config=req.connection_config,
        scope=req.scope or _default_scope(user), scenic_id=None if (req.scope or _default_scope(user)) == "group" else user.scenic_id,
        active=True,
    )
    db.add(ds)
    await db.commit()
    return _ds_dict(ds)


@router.delete("/datasources/{ds_id}")
async def delete_datasource(ds_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ds = await db.get(DbDatasource, ds_id)
    if not ds:
        raise NotFound("数据源不存在")
    _assert_delete(user, ds.scenic_id, "数据源")
    ds.active = False
    await db.commit()
    return {"ok": True}


@router.get("/datasources/{ds_id}/tables")
async def list_datasource_tables(ds_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """M6-02:选择数据源 -> 展示该数据源下的真实表列表(直连数据库内省,不是登记表里已有的那几张)。"""
    ds = await _readable_datasource(db, user, ds_id)
    tables = await db_introspect.list_tables(ds.db_type, ds.connection_config)
    registered_stmt = select(DbCatalog.table_name_ref).where(DbCatalog.datasource_id == ds_id)
    registered = set((await db.execute(registered_stmt)).scalars())
    return {"items": [{"table_name": t, "registered": t in registered} for t in tables]}


@router.get("/datasources/{ds_id}/tables/{table_name}/ddl")
async def get_table_ddl_route(ds_id: str, table_name: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    ds = await _readable_datasource(db, user, ds_id)
    ddl = await db_introspect.get_table_ddl(ds.db_type, ds.connection_config, table_name)
    preview = await db_introspect.preview_table(ds.db_type, ds.connection_config, table_name, limit=100)
    return {"ddl": ddl, "preview": preview}


class RegisterTableRequest(BaseModel):
    table_name: str
    description: str = ""
    columns_doc: dict = {}


@router.post("/datasources/{ds_id}/tables")
async def register_table(ds_id: str, req: RegisterTableRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能登记表")
    ds = await db.get(DbDatasource, ds_id)
    if not ds:
        raise NotFound("数据源不存在")
    entry = DbCatalog(datasource_id=ds_id, table_name_ref=req.table_name, description=req.description, columns_doc=req.columns_doc)
    db.add(entry)
    await db.commit()
    return {"id": entry.id, "table_name_ref": entry.table_name_ref, "description": entry.description}


# ───────────────────────────── 数据库知识:自定义数据源 ─────────────────────────────

class CustomSourceCreateRequest(BaseModel):
    name: str
    base_datasource_id: str
    build_sql: str


@router.get("/custom-sources")
async def list_custom_sources(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # build_sql 是这个景区的取数逻辑,不该给别的景区看见
    rows = (await db.execute(
        select(DbCustomSource).where(_read_clause(user, DbCustomSource.scenic_id)))).scalars().all()
    return {"items": [{"id": c.id, "name": c.name, "base_datasource_id": c.base_datasource_id, "build_sql": c.build_sql} for c in rows]}


@router.post("/custom-sources")
async def create_custom_source(req: CustomSourceCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增自定义数据源")
    if not req.build_sql.strip().lower().startswith("select"):
        raise Forbidden("自定义数据源的视图逻辑只允许 SELECT")
    src = DbCustomSource(name=req.name, base_datasource_id=req.base_datasource_id, build_sql=req.build_sql, scenic_id=user.scenic_id)
    db.add(src)
    await db.commit()
    return {"id": src.id, "name": src.name}


def _ds_dict(d: DbDatasource) -> dict:
    return {"id": d.id, "name": d.name, "db_type": d.db_type, "scope": d.scope, "scenic_id": d.scenic_id, "active": d.active, "created_at": d.created_at}


# ───────────────────────────── 网络知识 ─────────────────────────────

class WebSearchConfigRequest(BaseModel):
    name: str = ""
    provider: str = "bochaai"
    api_key: str | None = None      # 明文只在请求里出现一次,落库前加密;接口永不回显
    base_url: str | None = None     # searxng 自建实例必填
    cache_ttl_seconds: int = 21600
    scope: str | None = None
    active: bool = True


@router.get("/web-search-config")
async def list_web_search_configs(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    rows = (await db.execute(
        select(WebSearchConfig).where(_read_clause(user, WebSearchConfig.scenic_id)))).scalars().all()
    return {"items": [{
        "id": c.id, "name": c.name, "provider": c.provider, "base_url": c.base_url,
        "cache_ttl_seconds": c.cache_ttl_seconds, "scope": c.scope, "active": c.active,
        # 只回传"有没有配密钥",绝不回传密钥本身
        "has_api_key": bool(c.api_key_cipher),
    } for c in rows]}


@router.post("/web-search-config")
async def create_web_search_config(req: WebSearchConfigRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """新增搜索引擎配置。密钥在这里加密落库,之后就不用再去改 .env 和重启服务了。"""
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能配置搜索引擎")
    from cogni_agent.tool_center_service.mcp.vault import encrypt_credential

    cfg = WebSearchConfig(
        name=req.name or req.provider, provider=req.provider,
        api_key_cipher=encrypt_credential(req.api_key) if req.api_key else None,
        base_url=req.base_url, cache_ttl_seconds=req.cache_ttl_seconds,
        scope=req.scope or _default_scope(user), scenic_id=user.scenic_id, active=req.active,
    )
    db.add(cfg)
    await db.commit()
    return {"id": cfg.id, "provider": cfg.provider}


@router.put("/web-search-config/{cfg_id}")
async def update_web_search_config(cfg_id: str, req: WebSearchConfigRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能修改搜索引擎配置")
    from cogni_agent.tool_center_service.mcp.vault import encrypt_credential

    cfg = await db.get(WebSearchConfig, cfg_id)
    if not cfg:
        raise NotFound("配置不存在")
    _assert_write(user, getattr(cfg, "scenic_id", None), "搜索引擎配置")
    cfg.name, cfg.provider = req.name or cfg.name, req.provider
    cfg.base_url, cfg.cache_ttl_seconds, cfg.active = req.base_url, req.cache_ttl_seconds, req.active
    # 留空表示不改密钥——否则每次编辑别的字段都得把密钥重新输一遍
    if req.api_key:
        cfg.api_key_cipher = encrypt_credential(req.api_key)
    await db.commit()
    return {"ok": True}


@router.delete("/web-search-config/{cfg_id}")
async def delete_web_search_config(cfg_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cfg = await db.get(WebSearchConfig, cfg_id)
    if not cfg:
        raise NotFound("配置不存在")
    _assert_delete(user, getattr(cfg, "scenic_id", None), "搜索引擎配置")
    await db.delete(cfg)
    await db.commit()
    return {"ok": True}


@router.post("/web-search-config/{cfg_id}/test")
async def test_web_search_config(cfg_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """真实发一次检索验证配置是否可用。配完密钥能当场验证,而不是等某次对话里悄悄失败。"""
    cfg = await db.get(WebSearchConfig, cfg_id)
    if not cfg:
        raise NotFound("配置不存在")
    from cogni_agent.knowledge_service.websearch_knowledge.search import web_search

    results = await web_search("武夷山 景区 开放时间", top_k=3)
    return {"ok": bool(results), "count": len(results),
            "sample": [{"title": r.get("title"), "url": r.get("url")} for r in results[:3]],
            "hint": "" if results else "没有返回结果。可能是密钥无效、provider 选错,或该 provider 需要 base_url。"}


# ── 向量库后端配置(向量存 Milvus,元数据仍在 Postgres)──

class VectorDbConfigRequest(BaseModel):
    name: str = "默认向量库"
    backend: str = "milvus"          # milvus / pgvector
    uri: str | None = None           # 如 http://10.0.0.5:19530;填了它就不用再填 host/port
    host: str | None = None
    port: int | None = 19530
    token: str | None = None         # Milvus 认证(user:password 或 API Key),加密存储
    db_name: str = "default"
    collection: str = "cogni_kb_chunks"
    dim: int | None = None           # 留空则用 knowledge.embedding_dim
    metric: str = "COSINE"           # COSINE / L2 / IP
    index_type: str = "AUTOINDEX"


def _vector_cfg_public(row) -> dict:
    """回传给前端的配置。**token 只回传有无,不回明文。**"""
    c = dict(row.connection_config or {})
    c.pop("token", None)
    return {"id": row.id, "name": row.name, "backend": row.backend,
            "has_token": bool((row.connection_config or {}).get("token")), **c}


@router.get("/vector-db-configs")
async def list_vector_db_configs(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    rows = (await db.execute(select(VectorDbConfig))).scalars().all()
    return {"items": [_vector_cfg_public(r) for r in rows]}


@router.post("/vector-db-configs")
async def upsert_vector_db_config(req: VectorDbConfigRequest,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    """新增/更新向量库配置。

    当前只保留一条生效配置(检索时取第一条)——多套向量库并存会让"这条知识存在哪"
    变成一个需要额外记账的问题,在没有明确需求前不引入这个复杂度。
    """
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能配置向量库")

    conn = {k: v for k, v in req.model_dump().items()
            if k not in ("name", "backend") and v is not None}
    row = (await db.execute(select(VectorDbConfig))).scalars().first()
    if row is None:
        row = VectorDbConfig(name=req.name, backend=req.backend, connection_config=conn)
        db.add(row)
    else:
        # token 留空表示不修改,否则每次改别的字段都得把密钥重输一遍
        if not conn.get("token"):
            conn["token"] = (row.connection_config or {}).get("token")
        row.name, row.backend, row.connection_config = req.name, req.backend, conn
    await db.commit()

    from cogni_agent.knowledge_service.vector_backends import invalidate_backend_cache

    invalidate_backend_cache()   # 让下一次检索立刻用新配置,不用重启
    return {"id": row.id, "backend": row.backend}


@router.post("/vector-db-configs/test")
async def test_vector_db_config(user: CurrentUser = Depends(get_current_user)):
    """连通性自检:连上向量库、确认集合存在(不存在会自动创建)、回报当前向量条数。

    配完能当场验证,而不是等某次对话里悄悄检索不到东西。
    """
    from cogni_agent.knowledge_service.vector_backends import get_backend

    backend = await get_backend()
    ok, detail = await backend.ping()
    return {"ok": ok, "detail": detail}


class WhitelistCreateRequest(BaseModel):
    domain: str


@router.get("/web-whitelist")
async def list_whitelist(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    # scenic_id 为空 = 集团级通用白名单,所有景区都看得到;景区自己加的只有自己看得到
    rows = (await db.execute(
        select(WebWhitelist).where(_read_clause(user, WebWhitelist.scenic_id)))).scalars().all()
    return {"items": [{"id": w.id, "domain": w.domain, "scenic_id": w.scenic_id} for w in rows]}


@router.post("/web-whitelist")
async def add_whitelist(req: WhitelistCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增白名单")
    entry = WebWhitelist(domain=req.domain, scenic_id=user.scenic_id)
    db.add(entry)
    await db.commit()
    return {"id": entry.id, "domain": entry.domain}


@router.delete("/web-whitelist/{entry_id}")
async def delete_whitelist(entry_id: str, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    entry = await db.get(WebWhitelist, entry_id)
    if not entry:
        raise NotFound("白名单记录不存在")
    _assert_delete(user, entry.scenic_id, "白名单")
    await db.delete(entry)
    await db.commit()
    return {"ok": True}


class SiteArchiveCreateRequest(BaseModel):
    site_url: str
    crawl_interval_hours: int = 24


@router.get("/web-site-archive")
async def list_site_archive(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    rows = (await db.execute(select(WebSiteArchive))).scalars().all()
    return {"items": [{"id": s.id, "site_url": s.site_url, "crawl_interval_hours": s.crawl_interval_hours, "last_crawled_at": s.last_crawled_at} for s in rows]}


@router.post("/web-site-archive")
async def add_site_archive(req: SiteArchiveCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增站点档案")
    entry = WebSiteArchive(site_url=req.site_url, crawl_interval_hours=req.crawl_interval_hours)
    db.add(entry)
    await db.commit()
    return {"id": entry.id, "site_url": entry.site_url}


# ───────────────────────────── 向量知识 ─────────────────────────────

class VectorStorageConfigRequest(BaseModel):
    name: str
    endpoint: str
    bucket: str


@router.get("/vector-storage-configs")
async def list_vector_storage_configs(db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    rows = (await db.execute(select(VectorStorageConfig))).scalars().all()
    return {"items": [{"id": c.id, "name": c.name, "endpoint": c.endpoint, "bucket": c.bucket} for c in rows]}


@router.post("/vector-storage-configs")
async def create_vector_storage_config(req: VectorStorageConfigRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新增对象存储配置")
    cfg = VectorStorageConfig(name=req.name, endpoint=req.endpoint, bucket=req.bucket)
    db.add(cfg)
    await db.commit()
    return {"id": cfg.id, "name": cfg.name}


class KbCreateRequest(BaseModel):
    name: str
    storage_config_id: str | None = None
    vector_db_config_id: str | None = None
    scope: str | None = None
    visibility: str | None = None


@router.get("/bases")
async def list_knowledge_bases(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(KnowledgeBase)
    if user.scenic_id:
        stmt = stmt.where((KnowledgeBase.scope == "group") | (KnowledgeBase.scenic_id == user.scenic_id))
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_kb_dict(k) for k in rows]}


@router.post("/bases")
async def create_knowledge_base(req: KbCreateRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能新建知识库")
    scope = req.scope or _default_scope(user)
    kb = KnowledgeBase(
        name=req.name, storage_config_id=req.storage_config_id, vector_db_config_id=req.vector_db_config_id,
        scope=scope, scenic_id=None if scope == "group" else user.scenic_id, visibility=req.visibility or scope,
    )
    db.add(kb)
    await db.commit()
    return _kb_dict(kb)


def _kb_dict(k: KnowledgeBase) -> dict:
    return {"id": k.id, "name": k.name, "scope": k.scope, "scenic_id": k.scenic_id, "visibility": k.visibility, "created_at": k.created_at}


@router.get("/bases/{kb_id}/documents")
async def list_kb_documents(kb_id: str, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    # 文档行自己没有景区,归属在**父知识库**上 —— 只判文档等于没判
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFound("知识库不存在")
    if (getattr(kb, "scope", "") or "") != "group":
        _assert_read(user, kb.scenic_id, "知识库")
    rows = (await db.execute(select(KbDocument).where(KbDocument.kb_id == kb_id).order_by(KbDocument.created_at.desc()))).scalars().all()
    return {"items": [{"id": d.id, "file_name": d.file_name, "file_type": d.file_type, "status": d.status, "created_at": d.created_at} for d in rows]}


class DocumentIngestRequest(BaseModel):
    file_name: str
    text: str  # 文本原文,直接传内容而不是二进制文件上传(见 README「已知限制」)


@router.post("/bases/{kb_id}/documents")
async def ingest_document_route(kb_id: str, req: DocumentIngestRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """M6-06:文档上传/切分/嵌入——真实调用 embeddings.embed_text() + 写入 kb_chunks,
    不是接口占位。当前接受文本内容直传(而不是二进制文件上传),复杂文件格式的文本抽取
    (PDF/Word 等)不在这次范围内,可以先把抽取好的纯文本传进来。"""
    if not _can_write(user.role):
        raise Forbidden("group_viewer 为只读角色,不能上传文档")
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise NotFound("知识库不存在")
    doc = KbDocument(kb_id=kb_id, file_name=req.file_name, file_type="document", status="processing")
    db.add(doc)
    await db.commit()
    try:
        chunk_count = await ingest_document(kb.scenic_id, kb_id, doc.id, req.text)
    except Exception as exc:  # noqa: BLE001
        return {"id": doc.id, "status": "failed", "error": str(exc)}
    return {"id": doc.id, "status": "indexed", "chunk_count": chunk_count}


class RetrieveTestRequest(BaseModel):
    query: str
    top_k: int = 5


@router.post("/bases/{kb_id}/retrieve-test")
async def retrieve_test(kb_id: str, req: RetrieveTestRequest, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """M6-08:检索测试面板——真实调用嵌入模型 + pgvector 余弦距离检索,返回召回分块 + 来源标注。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise NotFound("知识库不存在")
    query_vec = await embed_text(req.query)
    results = await vector_retrieve(db, kb.scenic_id, kb_id, query_vec, top_k=req.top_k)
    return {"items": results}
