"""Quick BI 集成模块数据表(均带 numeri_core_ 前缀,迁移见 common/schema.py 0066)。

  tc_qbi_connections     Quick BI 连接(server_domain / api_key / 加密的 api_secret、user_token)
  tc_qbi_assets          已登记的数据资产:仪表板(小Q报表)、数据集
  tc_qbi_files           上传的数据文件(≤5MB,原文件 + 各能力的远端 fileId 缓存)
  (固定问题已并入 scenic_agent_issue_detail,见 0079)
  tc_qbi_sessions        对话会话(粘住目标数据,多轮追问)
  tc_qbi_runs            每次调用的运行记录(问数/解读/报告,含结果快照)
  tc_qbi_grants          连接分配(0075):哪个用户能用哪个 Quick BI 连接
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


def _uuid() -> str:
    return str(uuid.uuid4())


class QbiConnection(Base):
    __tablename__ = table_name("tc_qbi_connections")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64))
    server_domain: Mapped[str] = mapped_column(String(255), default="https://bi.aliyun.com")
    api_key: Mapped[str] = mapped_column(String(128), default="")              # AccessId,非机密,列表脱敏展示
    api_secret_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet 密文
    user_token_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet 密文(决定可访问的数据范围)
    use_trial: Mapped[bool] = mapped_column(Boolean, default=False)            # api_key/secret 为空时使用环境变量试用凭证
    default_locale: Mapped[str] = mapped_column(String(8), default="zh_CN")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    api_enabled: Mapped[bool] = mapped_column(Boolean, default=True)           # 是否允许开放平台 /v1/quickbi 调用
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")         # pending / ok / failed
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="group")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class QbiAsset(Base):
    __tablename__ = table_name("tc_qbi_assets")
    __table_args__ = (Index("ix_tc_qbi_assets_conn_kind", "connection_id", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))                  # dashboard / dataset
    name: Mapped[str] = mapped_column(String(128), default="")
    ref_id: Mapped[str] = mapped_column(String(128))               # pageId / cubeId
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)        # 仪表板结构摘要 / 数据集信息
    owner_user_id: Mapped[str] = mapped_column(String(36))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 停用而不是删除:连接没了又转不过去时(ref_id 只在原 Quick BI 工作空间里有效),
    # 删掉会丢登记信息,留着会变成「点了才报错」的死引用。停用后列表里仍看得到、
    # 并写明为什么用不了;换回可用的连接可以重新启用。
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 数据集专用:从哪块仪表板拉出来登记的。删那块板要连带删它,所以这层归属直接记下来 ——
    # 靠 meta.cubes 反查会在「两块板共用一个 cube」和「板刷新结构」这两种情况下算错。
    source_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 0088:自定义分类(运营随手输入,回车即存;不需要先建分类)。数据集列表按它分组展示,
    # 编辑问题时按分类多选 / 全选数据集。空 = 未分类
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())



class QbiFile(Base):
    __tablename__ = table_name("tc_qbi_files")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(String(36), index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)   # ≤5MB,按需上传到 Quick BI
    qa_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # copilot/parse 返回的 fileId
    report_file: Mapped[dict | None] = mapped_column(JSONB, nullable=True)      # uploadReferenceFile 返回
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)            # 小Q解读用的 Markdown 表格
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)                    # 解析出的表结构等
    owner_user_id: Mapped[str] = mapped_column(String(64))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 停用而不是删除:连接没了又转不过去时,删掉会丢登记信息,留着会变成「点了才报错」的死引用。
    # 停用后列表里仍看得到、并写明为什么用不了;换回可用的连接可以重新启用。
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


# QbiFixedQuestion(tc_qbi_fixed_questions)已于 0079 删除。
# 「Quick BI 固定问题」现在就是统一问题表 scenic_agent_issue_detail 里
# business_class='quick_bi' 的那些行(模型见 gateway_service/issue_models.py)。
# 并表的原因:同一件事配两张表,必然出现「管理端建了、APP 前台没有」——
# 前台读统一表、管理端写老表,两边靠人记得同步。现在只有一张表。
# 老 ID 不作废:迁过去的行保留 source_question_id=老表 id,按老 id 仍能查到。



class QbiSession(Base):
    __tablename__ = table_name("tc_qbi_sessions")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(128), default="")
    capability: Mapped[str] = mapped_column(String(24), default="auto")   # auto = 按意图路由
    target: Mapped[dict] = mapped_column(JSONB, default=dict)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="console")    # console / api
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class QbiRun(Base):
    __tablename__ = table_name("tc_qbi_runs")
    __table_args__ = (Index("ix_tc_qbi_runs_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(String(36))
    capability: Mapped[str] = mapped_column(String(24))
    mode: Mapped[str] = mapped_column(String(16), default="direct")        # direct / fixed / chat / report / tool
    question: Mapped[str] = mapped_column(Text, default="")
    target: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")     # running / success / failed
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 64 而不是 36:0079 之后这里存的是统一问题表的 issue_id(issue_<32位> 就有 38 字符),
    # 老表的 UUID 才是 36 —— 按 36 建的话每次按固定问题发起都会写不进运行记录(见迁移 0080)
    fixed_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)   # 小Q报告 chatId
    report_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 执行留痕:上游原样事件 + 归一化后的事件(脱敏、限长)。排障时导出给研发看,
    # 没它的话只能靠复述现象 —— 而现象往往正是「看起来不对但说不清哪里不对」。
    debug: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="console")     # console / api / agent
    user_id: Mapped[str] = mapped_column(String(64))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QbiGrant(Base):
    """Quick BI 连接分配(0075):把某个连接分配给某个用户。

    **为什么 Quick BI 不走版本指派表**:Agent/Skill 分发解决的是「这个人用哪一版」,
    而 Quick BI 连接没有版本概念 —— 要解决的是「这个人能不能用这套凭证」。
    硬塞进 tc_agent_assignments 就得给它编一个假版本号,之后每一处读版本的地方都要
    特判,得不偿失。所以单开一张表,但在「分发中心」里与 Agent/Skill 并排展示。

    **默认不收紧**:某个连接一旦有了至少一条分配,它就转成白名单制(没被分配的人
    看不到、用不了);一条都没有的连接维持原有的景区 + 可见性判定。
    这样升级当天不会有人突然用不了 —— 要收紧哪个连接,分配一次就行。
    """

    __tablename__ = table_name("tc_qbi_grants")
    __table_args__ = (Index("uq_tc_qbi_grants_conn_user", "connection_id", "user_id", unique=True),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: f"grt_{uuid.uuid4().hex}")
    connection_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class AgentFile(Base):
    """智能体可读的上传文件(0085)。

    与 QbiFile 分开,是因为两者的约束不一样:QbiFile 要把内容原样发给 Quick BI 的
    OpenAPI,受它 5MB 的限制;这张表只要能被本地解析就行,所以走对象存储、能到 10MB。
    混在一张表里的话,那个 5MB 会被一路带进来 —— 而它跟「智能体读一份报表」毫无关系。
    """

    __tablename__ = table_name("tc_agent_files")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    file_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    # 内容存在哪:minio=对象存储(看 object_key)/ db=数据库(看 content)。
    # **取的时候按它走,不要两边试** —— 靠猜会出现「两边都没有但记录还在」的死文件。
    storage: Mapped[str] = mapped_column(String(16), default="db")
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # 解析结果:table 表格(给句柄)/ doc 文档 / image 图片(给文本)
    parsed_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 解析失败也留痕:回答「为什么读不了这个文件」时要能说清
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    owner_user_id: Mapped[str] = mapped_column(String(64), default="")
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
