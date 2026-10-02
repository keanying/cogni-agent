"""Agent 注册中心:联合 Skill + 人格设定,构成有身份的对话入口。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class Agent(Base):
    __tablename__ = table_name("tc_agents")

    id: Mapped[str] = mapped_column(String(24), primary_key=True, default=lambda: f"agt_{uuid.uuid4().hex[:16]}")
    name: Mapped[str] = mapped_column(String(64))
    # v1.1.9:description 主表列已删除(0045)。描述是版本化内容,只存于版本快照
    # (adapter extra_keys),随草稿/发布存在;主表不再承载。新库 create_all 天然无此列。
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 人格设定(新增于 v3):
    persona_desc: Mapped[str | None] = mapped_column(Text, nullable=True)  # 性格描述,自由文本
    tone_style: Mapped[str | None] = mapped_column(String(32), nullable=True)      # 正式/亲切/简洁/活泼/自定义
    opening_line: Mapped[str | None] = mapped_column(Text, nullable=True)   # 开场白
    bound_skill_ids: Mapped[list] = mapped_column(JSON, default=list)
    # v1.2(19 号文档 D4-①):Agent 内部多技能协作模式,收进 agent 自身配置——
    # chain(固定顺序链,默认)/ dag(固定依赖图,边按绑定顺序派生线性)/ pool(动态可选池)。
    # 运行时读版本快照(发布冻结,编辑改的是草稿),快照缺键回退本列(老 agent 兜底)。
    # tc_skill_compositions 不再参与 agent 路径的编排解析(消灭"技能集合精确相等"静默失配),
    # 组合表保留给不依附 agent 的独立编排(固定问题工作流/复合问题)。
    composition_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="chain")
    visibility: Mapped[str] = mapped_column(String(16), default="group")
    # 主表 status 已废弃(0043 删列):发布唯一权威在版本表(published 快照),停用由 active 承担
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_qps: Mapped[int] = mapped_column(Integer, default=5)
    rate_limit_daily: Mapped[int] = mapped_column(Integer, default=10000)
    # 关联推荐追问已于 24 号定稿删除(0056 迁移删列):问题都由问题库配置,
    # APP 端推荐入口走问题库,不再由 agent 配置预设追问。
    # 意图识别配置(agent 级,v1.1.5):意图识别不是框架级的,而是挂在每个 Agent 下。
    # 一个 Agent 一个意图识别。结构:
    #   {"enabled": bool,                # 是否对本 Agent 开启意图识别(缺省 true)
    #    "mode": "always|side_effects|off",  # 判定策略(缺省跟随框架配置)
    #    "model": str|None,              # 独立分诊模型覆盖(缺省跟随框架配置)
    #    "prompt": str|None,             # 自定义 Triager system prompt(缺省用框架模板)
    #    "history_rounds": int|None}     # v1.1.7:Triager 多轮会话历史注入深度(轮数,
    #                                    # 0-5,缺省 0=不注入,无框架级默认;0 是有效值
    #                                    # =显式关闭,>5 前端校验拒绝/运行时 clamp 到 5)
    #    (27 号评审:recheck_before_clarify 二次确认已移除,不再消费该键)
    intent_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 软删除标记(删除保护加固):删除 = 置 deleted_at 归档,保留版本记录。
    # active 仍为「启用/停用」语义(toggle),与删除归档分离。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiKey(Base):
    __tablename__ = table_name("tc_api_keys")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key_hash: Mapped[str] = mapped_column(String(128))
    prefix_display: Mapped[str] = mapped_column(String(24))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_allowed_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(36))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
