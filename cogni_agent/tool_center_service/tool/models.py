"""工具注册表(Tool 三类型:http/db/python)+ G-AUTH 授权表。工具版本统一存 tc_resource_versions(旧表 tc_tool_versions 已停用)。

v1.2(0051):Tool 与 Skill/Agent 同构 —— 主表仅身份目录,内容一律存版本快照。
description/type/type_config/risk_level 原主表列已随 0051 删除,只存在于
tc_resource_versions 的草稿/发布快照里(extra_keys),随版本发布/回滚;任何逻辑
(解析/授权/执行/展示)都从版本快照取,与 tc_skills(0050)/tc_agents(0045) 同构。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class Tool(Base):
    __tablename__ = table_name("tc_tools")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64), index=True)
    # v1.2(0051):描述/类型/执行配置/风险分级已全部快照化(见文件头),此处不再有内容列。
    active: Mapped[bool] = mapped_column(Boolean, default=True)         # 启用/禁用
    locked: Mapped[bool] = mapped_column(Boolean, default=False)        # 锁定后仅创建者/admin可编辑删除
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None=集团级资源
    visibility: Mapped[str] = mapped_column(String(16), default="group")     # private/scenic/group/public
    source_mcp_server_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 从 MCP 发现导入时标记来源
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 软删除标记(删除保护加固):删除 = 置 deleted_at 归档,列表/详情/更新均过滤。
    # active 仍为「启用/停用」语义(toggle),与删除归档分离。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 工具名在**租户范围内**唯一,而不是全局唯一。
    #
    # 原来是 name unique=True(全局唯一),这在多租户平台上是个硬伤:武夷山景区先建了
    # 一个叫「查询订单」的工具,厦门景区就再也不能建同名工具了——两个租户的命名空间被
    # 强行打通,与架构里"景区 = 租户"的隔离前提直接冲突。
    #
    # 用 coalesce(scenic_id,'') 而不是普通的联合唯一约束:Postgres 默认认为 NULL 之间
    # 互不相等,直接写 UniqueConstraint(scenic_id, name) 的话,两个集团级工具
    # (scenic_id 都是 NULL)同名照样能插进去,等于集团级这一层完全没约束住。
    #
    # 删除保护加固(0035):唯一性只约束**未删除**的行——部分唯一索引
    # (WHERE deleted_at IS NULL)。软删归档行不再占用 (scenic_id, name),
    # 删除后同租户可重建同名工具(行业做法:deleted_at 参与唯一索引)。
    __table_args__ = (
        Index(
            "uq_tc_tools_scope_name",
            text("coalesce(scenic_id, '')"),
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class ResourceGrant(Base):
    """G-AUTH-03/08:细粒度授权表,resource_type 通用于 tool/skill/mcp/agent。"""

    __tablename__ = table_name("tc_grants")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    resource_type: Mapped[str] = mapped_column(String(16))  # tool / skill / mcp / agent
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    grantee_type: Mapped[str] = mapped_column(String(16))   # user / scenic_all
    grantee_id: Mapped[str] = mapped_column(String(64))     # user_id / scenic_id
    permission: Mapped[str] = mapped_column(String(16), default="use")  # use/edit/manage
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolCredential(Base):
    """凭证密文保管,调用时解密注入,前端/Agent 不可见明文。

    §10.1 M0(实施记录 02 任务 1):凭证归属从「按工具一条」扩展为「按
    (tool_id, user_id, scope) 三维」。v0.1.4 收紧:user_id **必须非空**,凭证一律
    归属具体用户,不存在共享凭证(NULL 行仅存量遗留,运行时视为无效,迁移 0023 清理)。
    scope 缺省为 "default"。唯一约束用表达式索引处理 NULL(同 Tool 的
    uq_tc_tools_scenic_id_name 先例),保证「同一 tool × 同一归属 × 同一场景」只有一行。
    """

    __tablename__ = table_name("tc_credentials")
    __table_args__ = (
        Index(
            "uq_tc_credentials_tool_user_scope",
            "tool_id",
            text("coalesce(user_id, '')"),
            "scope",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tool_id: Mapped[str] = mapped_column(String(36), index=True)
    # 凭证归属用户,业务层必填;平台不保存共享凭证(NULL 为存量遗留,0023 清理)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 凭证使用场景标签,缺省 "default"
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    cipher_text: Mapped[str] = mapped_column(String(1024))
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
