"""M3-01/06:任务表,状态机 + 乐观锁 version。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name

# M3-06 Task 状态枚举(字符串存储,不用数据库 ENUM 以便后续加状态不用改表结构)
# 「未结束」的状态集合:多轮会话据此判断上一轮跑完没有(WAITING_HUMAN 也算未结束——
# 审批还没处理,这一轮的执行链条并没有断,只是暂停了)。
ACTIVE_STATUSES = ("CREATED", "QUEUED", "RUNNING", "WAITING_HUMAN")
TASK_STATES = ["CREATED", "QUEUED", "RUNNING", "WAITING_HUMAN", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]

# Task.source 取值 = 「任务由哪个端发起」。它同时是前置参数处理
# (gateway_service/pre_params/)的端分发键:各端的身份来源与请求结构不同,
# 但解析规则共用,所以按端分发适配层而不是各写一套。
SOURCE_INTERNAL = "internal"   # 管理端/操练台   gateway_service/routes_chat.py
SOURCE_APP = "app"             # APP 前端        gateway_service/routes_app.py
SOURCE_OPENAPI = "openapi"     # 开放平台        gateway_service/routes_openai.py
SOURCE_PREWARM = "prewarm"     # 定时预热        cogni_agent/cronjobs/prewarm.py


class Task(Base):
    __tablename__ = table_name("task_queue")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # 强隔离字段,None=集团级任务
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)  # 发起账号;同一 scenic 可被多账号绑定,任务按账号归因;None=兼容历史任务
    executor_type: Mapped[str] = mapped_column(String(16), default="agent")  # agent/skill/tool/mcp,对应「对话」页执行目标选择器
    executor_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # 兼容字段,executor_type=agent 时等于 executor_id
    # 多轮会话:同一个 conversation_id 下的任务共享 LangGraph Checkpointer 的
    # thread,从而拥有连续的对话记忆。为空则是一次性任务(旧行为,每次都是新话题)。
    conversation_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_INTERNAL)  # 取值见文件头 SOURCE_* 常量
    status: Mapped[str] = mapped_column(String(16), default="CREATED", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)  # 乐观锁
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Conversation(Base):
    """
    多轮会话。

    为什么需要一张表而不是让前端自己攥着一个 id:
    - 前端「操练台」原本的会话列表是纯前端状态,刷新就没了;
    - 会话要能按租户/用户列出、要有标题、要知道绑定的执行目标;
    - 更重要的是**并发控制**:同一个会话同时跑两个任务会让 Checkpointer 的
      thread 状态互相覆盖,必须能查到"这个会话现在有没有任务在跑"。

    thread 语义:conversation.id 直接作为 LangGraph 的 thread_id,对话历史由
    Checkpointer 持久化,这张表只存会话本身的元信息,不重复存消息。
    """

    __tablename__ = table_name("conversations")

    # 48 而不是 36:id 带 conv_ 前缀(conv_ + 32 位 hex = 37),前缀能让人一眼区分
    # 会话 id 与任务 id,排查问题时很有用,值得多留几个字符。
    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: f"conv_{uuid.uuid4().hex}")
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    # 会话创建时绑定的执行目标。允许中途切换(切换会记一条诊断),但记录下初始目标
    # 便于在会话列表里展示"这是跟谁的对话"。
    executor_type: Mapped[str] = mapped_column(String(16), default="agent")
    executor_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128), default="")  # 取首条用户消息前若干字
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LLMPromptLog(Base):
    """LLM 调用日志:记录每次大模型调用时 LLM **真正收到的最终提示词**。

    供「会话信息技能」拉取后做提示词质量分析(是否存在业务无关噪音/内部信息泄漏)。
    同一会话同一任务的每次 LLM 调用(intent_check / generation / drift_check)各记一行:

    - system_prompt:完整的 system 层提示词(主 prompt + extra_systems 拼接),用于分析
      「系统提示词有没有业务噪音」;
    - history:user / assistant / tool 消息的 JSON 数组(每条截断),用于分析上下文注入。

    设计取舍:落库的是「最终发出的 messages」而不是中间态;history 逐条截断控制体积
    (分析噪音看的是开头与结构,200~300 字符足够);记录失败绝不影响主流程。
    """

    __tablename__ = table_name("llm_prompt_logs")

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    scenic_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)  # 强隔离字段,None=集团级任务
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step: Mapped[str] = mapped_column(String(24))  # intent_check / generation / drift_check
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AppFollowedQuestion(Base):
    """APP 端用户关注的问题。用户对某条回答中的问题点"关注"以跟踪后续。"""

    __tablename__ = table_name("app_followed_questions")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    question_text: Mapped[str] = mapped_column(String(1024))
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppMessageFeedback(Base):
    """APP 端消息反馈(like/dislike)。"""

    __tablename__ = table_name("app_message_feedback")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    feedback_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
