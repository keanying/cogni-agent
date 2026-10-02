"""图文混检 + 来源标注。向量检索走可切换后端(pgvector / Milvus)。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.logging import get_logger
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import KbChunk
from cogni_agent.knowledge_service.vector_backends import get_backend

log = get_logger("knowledge_service.retrieve")


async def retrieve(db: AsyncSession, scenic_id: str | None, kb_id: str,
                   query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """
    强隔离原则:scenic_id 精确匹配,或该知识库为集团级(scenic_id 为空)时所有景区可查。

    **租户过滤在后端内部完成**(见 vector_backends.VectorBackend.search 的约定),
    这里不允许、也没有办法跳过它 —— 换成向量库之后,如果指望调用方记得传过滤条件,
    漏一次就是跨租户泄露,而且不会报错,只会安静地把别的景区的资料喂给模型。

    向量库只返回 chunk_id 与相似度,正文与来源标注仍从 Postgres 的 kb_chunks 读 ——
    元数据留在关系库,列表/删除/审计都还是普通 SQL。
    """
    backend = await get_backend()
    hits = await backend.search(kb_id=kb_id, scenic_id=scenic_id,
                                query_embedding=query_embedding, top_k=top_k)
    if not hits:
        return []

    order = {h.chunk_id: i for i, h in enumerate(hits)}
    score_of = {h.chunk_id: h.score for h in hits}
    rows = (await db.execute(
        select(KbChunk).where(KbChunk.id.in_(list(order.keys())))
    )).scalars().all()

    # 二次校验租户归属。向量库的过滤已经做了一遍,这里再核对一次元数据 ——
    # 两套存储之间可能出现不一致(例如向量写入成功但元数据被删、或换了集合却没重建索引),
    # 隔离这种事多校验一次的成本远低于泄露一次的代价。
    safe = []
    for r in rows:
        if r.scenic_id is not None and r.scenic_id != scenic_id:
            log.warning(f"向量检索命中了不属于当前租户的 chunk {r.id}(kb={kb_id}),已丢弃。"
                        f"请检查向量库与元数据是否一致")
            continue
        safe.append(r)

    safe.sort(key=lambda r: order.get(r.id, 1_000_000))
    return [{"id": r.id, "source": r.content_ref, "type": r.chunk_type,
             "score": score_of.get(r.id, 0.0)} for r in safe]
