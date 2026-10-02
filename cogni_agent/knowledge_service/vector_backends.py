"""
向量存储后端抽象:pgvector / Milvus 可切换。

**分工**:向量本身存 Milvus(或 pgvector),chunk 的元数据(所属知识库、租户、来源标注、
文本内容)仍然留在 Postgres 的 kb_chunks 表里。这样知识库列表、文档删除、审计追溯这些
都还是普通 SQL,不用为了查一条记录去连向量库;而向量检索走专门的引擎。

**租户隔离是这个模块最要紧的事。** pgvector 时代隔离靠调用方在 SQL 里写
`WHERE scenic_id = ...`;换成向量库之后,如果还指望调用方记得传过滤条件,
漏一次就是跨租户数据泄露 —— 而且这种泄露不会报错,只会安静地把别的景区的资料
喂给模型。所以这里把租户过滤做进 `search()` 内部:**scenic_id 是必填参数,
过滤表达式由后端自己拼**,调用方没有"忘记加"的机会。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("knowledge_service.vector_backend")


@dataclass
class VectorHit:
    chunk_id: str
    score: float


@dataclass
class VectorRecord:
    chunk_id: str
    kb_id: str
    scenic_id: str | None      # None = 集团级知识库
    embedding: list[float]


class VectorBackend(abc.ABC):
    """向量后端接口。新增后端实现这四个方法即可。"""

    @abc.abstractmethod
    async def upsert(self, records: list[VectorRecord]) -> int: ...

    @abc.abstractmethod
    async def search(self, *, kb_id: str, scenic_id: str | None,
                     query_embedding: list[float], top_k: int) -> list[VectorHit]:
        """检索。

        **实现必须同时按 kb_id 与 scenic_id 过滤**:
        - scenic_id 精确匹配(景区自己的知识)
        - 或 scenic_id 为空(集团级知识,所有景区可见)
        这是强隔离约定,不允许任何实现跳过。
        """

    @abc.abstractmethod
    async def delete_by_kb(self, kb_id: str) -> int: ...

    @abc.abstractmethod
    async def ping(self) -> tuple[bool, str]: ...


# ══════════════════════════════════════════════════════════════════════════
# Milvus
# ══════════════════════════════════════════════════════════════════════════
class MilvusBackend(VectorBackend):
    """
    Milvus 后端。

    集合结构(字段名与 kb_chunks 对齐,便于对照排查):
        chunk_id   VARCHAR  主键
        kb_id      VARCHAR  知识库 id
        scenic_id  VARCHAR  租户;集团级知识库写空串 ""(**不能用 null**,见下)
        embedding  FLOAT_VECTOR

    **为什么集团级写空串而不是 null**:Milvus 的标量过滤表达式对 null 的支持在不同版本
    间差异较大,而空串的比较行为在所有版本上都是确定的。宁可在边界处做一次
    None <-> "" 的转换,也不要让隔离条件依赖一个不稳定的语义。
    """

    def __init__(self, cfg: dict):
        self.uri = cfg.get("uri") or f"http://{cfg.get('host', 'localhost')}:{cfg.get('port', 19530)}"
        self.token = cfg.get("token") or ""
        self.db_name = cfg.get("db_name") or "default"
        self.collection = cfg.get("collection") or "cogni_kb_chunks"
        self.dim = int(cfg.get("dim") or get_settings().get("knowledge", "embedding_dim", default=1024))
        self.metric = (cfg.get("metric") or "COSINE").upper()
        self.index_type = cfg.get("index_type") or "AUTOINDEX"
        self._client = None

    def _get_client(self):
        if self._client is None:
            from pymilvus import MilvusClient

            self._client = MilvusClient(uri=self.uri, token=self.token or None, db_name=self.db_name)
            self._ensure_collection(self._client)
        return self._client

    def _ensure_collection(self, client) -> None:
        """集合不存在就建。幂等,可反复调用。"""
        from pymilvus import DataType

        if client.has_collection(self.collection):
            return
        log.info(f"Milvus 集合 {self.collection} 不存在,正在创建(dim={self.dim}, metric={self.metric})")
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
        # 集团级知识库写空串,不写 null(见类文档)
        schema.add_field("scenic_id", DataType.VARCHAR, max_length=64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dim)

        index_params = client.prepare_index_params()
        index_params.add_index("embedding", index_type=self.index_type, metric_type=self.metric)
        # kb_id / scenic_id 建标量索引:每次检索都要按这两个字段过滤,
        # 不建索引在数据量大时会退化成全表扫描
        index_params.add_index("kb_id", index_type="INVERTED")
        index_params.add_index("scenic_id", index_type="INVERTED")
        client.create_collection(self.collection, schema=schema, index_params=index_params)

    @staticmethod
    def _tenant_value(scenic_id: str | None) -> str:
        return scenic_id or ""

    def _filter_expr(self, kb_id: str, scenic_id: str | None) -> str:
        """租户隔离表达式。**只在这里拼一次**,调用方碰不到。

        语义:限定知识库,且(属于本租户 或 属于集团级)。
        """
        tenant = self._tenant_value(scenic_id).replace('"', '\\"')
        kb = kb_id.replace('"', '\\"')
        return f'kb_id == "{kb}" and (scenic_id == "{tenant}" or scenic_id == "")'

    async def upsert(self, records: list[VectorRecord]) -> int:
        import asyncio

        if not records:
            return 0
        rows = [{
            "chunk_id": r.chunk_id, "kb_id": r.kb_id,
            "scenic_id": self._tenant_value(r.scenic_id), "embedding": r.embedding,
        } for r in records]

        def _do():
            client = self._get_client()
            client.upsert(collection_name=self.collection, data=rows)
            return len(rows)

        # pymilvus 是同步库,丢线程池避免阻塞事件循环
        return await asyncio.to_thread(_do)

    async def search(self, *, kb_id: str, scenic_id: str | None,
                     query_embedding: list[float], top_k: int) -> list[VectorHit]:
        import asyncio

        expr = self._filter_expr(kb_id, scenic_id)

        def _do():
            client = self._get_client()
            res = client.search(
                collection_name=self.collection, data=[query_embedding],
                limit=top_k, filter=expr, output_fields=["chunk_id"],
                search_params={"metric_type": self.metric},
            )
            hits: list[VectorHit] = []
            for group in res or []:
                for h in group:
                    entity = h.get("entity") or {}
                    cid = entity.get("chunk_id") or h.get("id")
                    if cid:
                        hits.append(VectorHit(chunk_id=str(cid), score=float(h.get("distance", 0.0))))
            return hits

        return await asyncio.to_thread(_do)

    async def delete_by_kb(self, kb_id: str) -> int:
        import asyncio

        def _do():
            client = self._get_client()
            kb = kb_id.replace('"', '\\"')
            res = client.delete(collection_name=self.collection, filter=f'kb_id == "{kb}"')
            return int((res or {}).get("delete_count", 0)) if isinstance(res, dict) else 0

        return await asyncio.to_thread(_do)

    async def ping(self) -> tuple[bool, str]:
        import asyncio

        def _do():
            client = self._get_client()
            stats = client.get_collection_stats(self.collection)
            return True, (f"连接正常。集合 {self.collection},"
                          f"当前 {stats.get('row_count', '?')} 条向量,维度 {self.dim},度量 {self.metric}")

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"


# ══════════════════════════════════════════════════════════════════════════
# pgvector(保留:便于对比召回效果与回滚)
# ══════════════════════════════════════════════════════════════════════════
class PgVectorBackend(VectorBackend):
    """原有实现。向量和元数据都在 kb_chunks 表里,不需要额外写入。"""

    async def upsert(self, records: list[VectorRecord]) -> int:
        # pgvector 模式下向量随 kb_chunks 一起落库,这里无需额外动作
        return len(records)

    async def search(self, *, kb_id: str, scenic_id: str | None,
                     query_embedding: list[float], top_k: int) -> list[VectorHit]:
        from sqlalchemy import or_, select

        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import KbChunk

        async with get_sessionmaker()() as db:
            stmt = (
                select(KbChunk.id)
                .where(KbChunk.kb_id == kb_id,
                       or_(KbChunk.scenic_id == scenic_id, KbChunk.scenic_id.is_(None)))
                .order_by(KbChunk.embedding.cosine_distance(query_embedding))
                .limit(top_k)
            )
            ids = (await db.execute(stmt)).scalars().all()
        return [VectorHit(chunk_id=i, score=0.0) for i in ids]

    async def delete_by_kb(self, kb_id: str) -> int:
        return 0    # 由 kb_chunks 的级联删除处理

    async def ping(self) -> tuple[bool, str]:
        return True, "pgvector(与元数据同库,无需额外连接)"


# ══════════════════════════════════════════════════════════════════════════
# 后端选择
# ══════════════════════════════════════════════════════════════════════════
_cached: tuple[str, VectorBackend] | None = None


async def get_backend() -> VectorBackend:
    """按「知识中心 · 向量知识」里启用的配置返回后端;没有配置则回落到 config.yaml。

    配置放数据库的意义:换 Milvus 实例、换集合、调维度都在页面上做,不用改文件重启。
    """
    global _cached
    cfg = await _load_config()
    key = f"{cfg.get('backend')}:{cfg.get('uri') or cfg.get('host')}:{cfg.get('collection')}"
    if _cached and _cached[0] == key:
        return _cached[1]

    backend_name = (cfg.get("backend") or "pgvector").lower()
    backend: VectorBackend = MilvusBackend(cfg) if backend_name == "milvus" else PgVectorBackend()
    _cached = (key, backend)
    log.info(f"向量后端:{backend_name}")
    return backend


def invalidate_backend_cache() -> None:
    """管理后台改动向量库配置后调用,让下一次检索用新配置。"""
    global _cached
    _cached = None


async def _load_config() -> dict:
    from sqlalchemy import select

    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import VectorDbConfig

    try:
        async with get_sessionmaker()() as db:
            row = (await db.execute(
                select(VectorDbConfig).order_by(VectorDbConfig.id)
            )).scalars().first()
    except Exception:  # noqa: BLE001 - 库不可用时用文件配置
        row = None
    if row is not None:
        return {"backend": row.backend, **(row.connection_config or {})}
    return dict(get_settings().get("knowledge", "vector_db", default={}) or {"backend": "pgvector"})
