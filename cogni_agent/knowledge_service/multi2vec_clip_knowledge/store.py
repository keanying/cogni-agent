"""向量知识:对象存储配置 / 向量数据库配置 / 知识库信息 / 文档 / 分块(pgvector,文本+CLIP图像同库)。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name

EMBED_DIM = 768  # 按实际使用的嵌入模型维度调整


class VectorStorageConfig(Base):
    """对象存储配置(存文档/图片原文件,如 MinIO/S3)。"""

    __tablename__ = table_name("kb_vector_storage_configs")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(255))
    bucket: Mapped[str] = mapped_column(String(128))
    access_key_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)


class VectorDbConfig(Base):
    """向量数据库连接配置(pgvector/Milvus 等)。"""

    __tablename__ = table_name("kb_vector_db_configs")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    backend: Mapped[str] = mapped_column(String(16), default="pgvector")  # pgvector / milvus
    connection_config: Mapped[dict] = mapped_column(JSON)


class KnowledgeBase(Base):
    """知识库基础信息。"""

    __tablename__ = table_name("kb_knowledge_bases")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    storage_config_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    vector_db_config_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), default="scenic")  # group / scenic
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="scenic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KbDocument(Base):
    """文档/图片清单,归属某个知识库。"""

    __tablename__ = table_name("kb_documents")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))  # document / image
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing / indexed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KbChunk(Base):
    __tablename__ = table_name("kb_chunks")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # 强隔离(集团级知识库为 None)
    chunk_type: Mapped[str] = mapped_column(String(16))  # text / image
    # Text 而非 String(512):这里存的是**分块原文**,一个 chunk 动辄上千字,
    # 512 字符会被数据库直接截断或报错,而且截断是静默的——检索出来的内容缺一半,
    # 模型基于残缺资料作答,很难发现。
    content_ref: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
