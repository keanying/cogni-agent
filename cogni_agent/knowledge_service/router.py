"""M6-01:知识路由——按 Skill 配置的可用知识源,多源并发检索 + 融合排序。"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger

log = get_logger("knowledge_service.router")


async def route_and_retrieve(scenic_id: str, query: str, enabled_sources: list[str]) -> list[dict]:
    """enabled_sources 取值:"database" / "websearch" / "vector",来自 Skill 的 knowledge_sources 配置。
    三类知识源各自独立失败不影响其它——任何一路检索异常都在各自的 _search_* 里兜住,
    绝不能因为其中一个知识源挂了就让整轮对话的知识检索全部失败。"""
    tasks = []
    if "vector" in enabled_sources:
        tasks.append(_search_vector(scenic_id, query))
    if "database" in enabled_sources:
        tasks.append(_search_database(scenic_id, query))
    if "websearch" in enabled_sources:
        tasks.append(_search_web(scenic_id, query))
    results = await asyncio.gather(*tasks) if tasks else []
    merged = [item for sub in results for item in sub]
    # 跨源精排:向量检索只保证「召回」不保证「排序最优」,三路拼接后交给 rerank 层
    # (heuristic 字符相似度 / LLM 打分)做统一相关性排序,把真正相关的片段顶到前面。
    # rerank 内部幂等且安全:provider=none / 异常时原样返回,不会比未启用时更差。
    try:
        from cogni_agent.knowledge_service.rerank import rerank_results

        merged = await rerank_results(query, merged)
    except Exception:  # noqa: BLE001 - 精排失败不能拖垮整轮知识检索
        log.exception("跨源精排失败,按原序返回")
    return merged


async def _accessible_kb_ids(scenic_id: str | None) -> list[str]:
    from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import KnowledgeBase

    async with get_sessionmaker()() as db:
        stmt = select(KnowledgeBase.id).where(
            or_(KnowledgeBase.scope == "group", KnowledgeBase.scenic_id == scenic_id)
        )
        return list((await db.execute(stmt)).scalars())


async def _search_vector(scenic_id: str, query: str) -> list[dict]:
    from cogni_agent.knowledge_service.embeddings import embed_text
    from cogni_agent.knowledge_service.multi2vec_clip_knowledge.retrieve import retrieve

    try:
        kb_ids = await _accessible_kb_ids(scenic_id)
        if not kb_ids:
            return []
        query_vec = await embed_text(query)
    except Exception:  # noqa: BLE001 - 未配置嵌入模型/embedding 调用失败时,向量知识降级为空结果
        log.exception("向量知识检索失败(可能未配置 knowledge.embedding_model)")
        return []

    async with get_sessionmaker()() as db:
        results = await asyncio.gather(*[retrieve(db, scenic_id, kb_id, query_vec, top_k=5) for kb_id in kb_ids])
    merged = [item for sub in results for item in sub]
    for item in merged:
        item["knowledge_type"] = "vector"
    return merged


async def _search_database(scenic_id: str, query: str) -> list[dict]:
    from cogni_agent.knowledge_service.database_knowledge.catalog import DbCatalog, DbDatasource
    from cogni_agent.knowledge_service.database_knowledge.text2sql import run_readonly_query, text_to_sql

    async with get_sessionmaker()() as db:
        ds_stmt = select(DbDatasource).where(
            DbDatasource.active.is_(True), or_(DbDatasource.scope == "group", DbDatasource.scenic_id == scenic_id)
        )
        datasources = list((await db.execute(ds_stmt)).scalars())
        if not datasources:
            return []
        out: list[dict] = []
        for ds in datasources:
            catalog_stmt = select(DbCatalog).where(DbCatalog.datasource_id == ds.id)
            tables = list((await db.execute(catalog_stmt)).scalars())
            if not tables:
                continue
            context = "\n\n".join(
                f"表 {t.table_name_ref}:{t.description}\n字段释义:{t.columns_doc}" for t in tables
            )
            try:
                sql = await text_to_sql(query, context)
                result = await run_readonly_query(ds.id, sql)
                out.append({"source": ds.name, "knowledge_type": "database", "sql": sql, "rows": result.get("rows", [])[:20]})
            except Exception:  # noqa: BLE001 - 单个数据源生成/执行失败不影响其它数据源
                log.exception(f"数据库知识检索失败(datasource={ds.id})")
                continue
        return out


async def _search_web(scenic_id: str, query: str) -> list[dict]:
    from cogni_agent.knowledge_service.websearch_knowledge.fetch import WebWhitelist, is_domain_whitelisted
    from cogni_agent.knowledge_service.websearch_knowledge.search import web_search

    results = await web_search(query)
    if not results:
        return []
    async with get_sessionmaker()() as db:
        wl_stmt = select(WebWhitelist.domain).where(or_(WebWhitelist.scenic_id.is_(None), WebWhitelist.scenic_id == scenic_id))
        whitelist = list((await db.execute(wl_stmt)).scalars())
    if whitelist:
        results = [r for r in results if r.get("url") and is_domain_whitelisted(r["url"], whitelist)]
    for r in results:
        r["knowledge_type"] = "websearch"
    return results
