"""M5-04/06:Skill(工具×Prompt×约束)与 Prompt 版本。

注:原 M5-05「编排组合」(tc_skill_compositions) 已由 23 号文档删除——
APP 固定问题改为问题行直接绑定 skill_id,agent 路径编排走 Agent.composition_mode。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class Skill(Base):
    __tablename__ = table_name("tc_skills")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    # v1.1.10:内容键全部纯快照化 —— description/tool_usage/suggested_mode/
    # knowledge_sources/constraints 主表列已删(迁移 0050),只存于版本快照
    # (adapter extra_keys,随草稿/发布存在);主表不再承载。同 agent description
    # (0045) 模式:编辑=草稿、发布冻结快照,主表仅身份/门面 + 绑定键 tool_ids 兜底。
    tool_ids: Mapped[list] = mapped_column(JSON, default=list)  # 绑定键(纯 id 数组,快照 tools pin 为权威)
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None=集团级资源(与 Tool/Agent 一致)
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    # 主表 status 已废弃(0043 删列):发布唯一权威在版本表(published 快照)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 软删除标记(删除保护加固):删除 = 置 deleted_at 归档,保留版本记录与 prompts 历史。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Prompt(Base):
    """Skill 的提示词内容(历史遗留表:prompt 已内嵌 skill 版本快照 extra_keys,发布不再写回此表)。"""

    __tablename__ = table_name("tc_prompts")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    skill_id: Mapped[str] = mapped_column(String(36), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
