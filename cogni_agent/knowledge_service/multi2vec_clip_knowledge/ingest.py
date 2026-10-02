"""M6-06/07:文档切分嵌入 / 图片 CLIP 嵌入。"""
from __future__ import annotations

import re
import uuid

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.knowledge_service.embeddings import embed_image, embed_text
from cogni_agent.knowledge_service.vector_backends import VectorRecord, get_backend
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import KbChunk, KbDocument

log = get_logger("knowledge_service.ingest")

_CHUNK_SIZE = 500  # 字符数,简单滑窗切分(不依赖分词器,足够覆盖中英文场景)
_CHUNK_OVERLAP = 50


def _split_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


async def ingest_document(scenic_id: str | None, kb_id: str, document_id: str, text: str) -> int:
    """切分文本 -> 调嵌入模型 -> 写入 kb_chunks(chunk_type=text)。返回写入的分块数。
    调用方(routes.py 的文档上传接口)先创建 KbDocument 记录(status=processing),
    这里写完分块后把状态置为 indexed;任一步失败则回退为 failed,不留半成品状态。"""
    async with get_sessionmaker()() as db:
        doc = await db.get(KbDocument, document_id)
        try:
            pending_vectors: list[VectorRecord] = []
            chunks = _split_text(text)
            for chunk_text in chunks:
                vector = await embed_text(chunk_text)
                chunk_id = f"chk_{uuid.uuid4().hex}"
                db.add(KbChunk(
                    id=chunk_id, kb_id=kb_id, scenic_id=scenic_id,
                    chunk_type="text", content_ref=chunk_text, embedding=vector,
                ))
                pending_vectors.append(VectorRecord(
                    chunk_id=chunk_id, kb_id=kb_id, scenic_id=scenic_id, embedding=vector))

            # 先落元数据再写向量库。顺序是有意的:元数据是权威副本,
            # 向量写失败时文档标记为 failed、chunk 仍在库里,可以重建索引;
            # 反过来先写向量的话,元数据失败会在向量库留下查不到出处的孤儿向量。
            if doc:
                doc.status = "indexed"
            await db.commit()
            await _push_vectors(pending_vectors, document_id)
            return len(chunks)
        except Exception:
            log.exception(f"文档 {document_id} 切分/嵌入失败")
            if doc:
                doc.status = "failed"
                await db.commit()
            raise


async def ingest_image(scenic_id: str | None, kb_id: str, document_id: str, file_path: str) -> None:
    """调 CLIP 模型生成图像嵌入 -> 写入 kb_chunks(chunk_type=image)。"""
    async with get_sessionmaker()() as db:
        doc = await db.get(KbDocument, document_id)
        try:
            vector = await embed_image(file_path)
            chunk_id = f"chk_{uuid.uuid4().hex}"
            db.add(KbChunk(
                id=chunk_id, kb_id=kb_id, scenic_id=scenic_id,
                chunk_type="image", content_ref=file_path, embedding=vector,
            ))
            if doc:
                doc.status = "indexed"
            await db.commit()
            await _push_vectors(
                [VectorRecord(chunk_id=chunk_id, kb_id=kb_id, scenic_id=scenic_id, embedding=vector)],
                document_id)
        except Exception:
            log.exception(f"图片文档 {document_id} 嵌入失败")
            if doc:
                doc.status = "failed"
                await db.commit()
            raise


async def _push_vectors(records: list["VectorRecord"], document_id: str) -> None:
    """把向量写入向量后端。

    pgvector 模式下这是个空操作(向量随 kb_chunks 一起落库了);Milvus 模式下才真正写入。
    失败时把文档标记为 failed 并留日志 —— **不静默吞掉**:向量没写进去意味着这份文档
    检索不到,而元数据看起来一切正常,不标出来就会变成"传了文档但永远搜不到"的哑故障。
    """
    if not records:
        return
    try:
        backend = await get_backend()
        await backend.upsert(records)
    except Exception:  # noqa: BLE001
        log.exception(f"文档 {document_id} 的向量写入向量库失败,该文档将无法被检索到")
        from sqlalchemy import update as _update

        from cogni_agent.common.db import get_sessionmaker

        async with get_sessionmaker()() as db2:
            await db2.execute(_update(KbDocument).where(KbDocument.id == document_id)
                              .values(status="failed"))
            await db2.commit()
