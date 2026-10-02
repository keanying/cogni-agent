"""景区智能体问题体系(前后端改造 §1/§2):问题详情表 + 三级维度标签表。

**为什么要新开这两张表**:
问题原先散在两处 —— `app_question_bank`(APP 自己的问题库)与
`tc_qbi_fixed_questions`(Quick BI 固定问题)。两边字段、可见性、发起方式都不同,
前台要展示「这个景区能问什么」就得自己去合并两份数据、还要判断该调哪个接口,
于是出现「后台配了 Quick BI 固定问题,APP 里一条都看不到」这种事。

现在统一到 `scenic_agent_issue_detail`:一行就是一个可点击的问题,
`business_class` 说明它该怎么发起(quick_bi 直连 Quick BI / local_system 走对话引擎),
前台拿一份数据就够了。两张旧表不动(存量逻辑仍在用),由迁移把数据灌进新表,
新链路只读新表。

**三级标签**(`public_problem_dimension_label`)是问题的挂载点:主题 → 维度 → 指标。
做成独立表而不是问题表里的三个自由文本列,是因为它要被**维护**(知识中心里增删改)、
要被**复用**(多个问题挂同一个指标)、还要保证「同名三元组只有一条」——
这三件事在自由文本上都做不到。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name

# 业务渠道类别:决定这条问题**怎么发起**,前台据此选接口。
BUSINESS_CLASS_QUICK_BI = "quick_bi"        # 直连 Quick BI(POST /api/app/quickbi/ask)
BUSINESS_CLASS_LOCAL = "local_system"       # 走平台对话引擎(POST /api/app/chat)
BUSINESS_CLASSES = (BUSINESS_CLASS_QUICK_BI, BUSINESS_CLASS_LOCAL)


class PublicProblemDimensionLabel(Base):
    """公共问题维度标签:主题(topic) → 维度(dimension) → 指标(metric) 三级。

    一行 = 一个完整的三级路径(而不是三张表)。这样做的理由很实际:
    前台的选择器是「主题-维度-指标」一路选下来的,取数时最常见的查询就是
    「给我全部路径」再在内存里分组;拆三张表要连查三次、还要处理孤儿节点,
    而这套标签总量是几十行的量级,不值当。

    唯一约束有两道:名称三元组、编码三元组都不允许重复 ——
    名称重复会让运营在下拉里看到两个一模一样的选项(选哪个都对不上),
    编码重复则会让前台按编码分组时把两条路径混成一条。
    """

    __tablename__ = table_name("public_problem_dimension_label")
    __table_args__ = (
        UniqueConstraint("topic", "dimension", "metric", name="uq_dim_label_names"),
        UniqueConstraint("topic_id", "dimension_id", "metric_id", name="uq_dim_label_codes"),
        Index("idx_dim_label_topic", "topic_id", "dimension_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)          # 主题中文名,如 客流
    topic_id: Mapped[str] = mapped_column(String(64), nullable=False)       # 主题编码,如 visitorFlow
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)      # 维度中文名,如 在园人员
    dimension_id: Mapped[str] = mapped_column(String(64), nullable=False)   # 维度编码,如 inParkVisitor
    metric: Mapped[str] = mapped_column(String(64), nullable=False)         # 指标中文名,如 实时人数
    metric_id: Mapped[str] = mapped_column(String(64), nullable=False)      # 指标编码,如 liveVisitorCnt
    # 展示顺序:同一维度下的指标按它排;相同则按 id。运营调顺序不必删了重建。
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下架开关:停用的标签不进前台选择器,但已挂在它下面的问题不会丢(仍可追溯)。
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    create_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                  onupdate=func.now())


class ScenicAgentIssueDetail(Base):
    """景区智能体问题详情:一行 = 前台一个可点击的问题。

    **两类问题共用一张表**,靠 `business_class` 区分:
      - `quick_bi`     发给 Quick BI(需要 connection_id + capability + target);
      - `local_system` 走平台对话引擎(需要 agent_id / skill_id 之一,或都不给走意图路由)。

    **connection_id 是什么**(前后端改造文档里点名要确认的):
    它就是「操练台上那个 Quick BI 链接下拉」选中的那一项 —— `tc_qbi_connections.id`,
    一套 Quick BI 访问凭证(服务器地址 + api_key/secret + user_token)。
    文档里另写的 `quickbiLinkId` 与它是同一个东西,所以表里只存一列,
    对外返回时两个字段名都给(见 businessIssueList),前端用哪个都行。

    **target 是什么**:Quick BI 的目标数据,沿用 `tc_qbi_fixed_questions.target` 的 JSONB
    结构(键可选 asset_id / page_id / cube_id / cube_name / file_id / dashboard)。
    但 JSON 不适合展示,所以额外冗余两列:
      - `target_data_id`   Quick BI 侧的数据编码(仪表板 pageId / 数据集 cubeId);
      - `target_data_name` 数据集/仪表板名称,前台直接显示「选了哪份数据」。
    冗余是有意的:名称改了不影响已发出的问题,而每次展示都去 Quick BI 查一次名字既慢又脆。
    """

    __tablename__ = table_name("scenic_agent_issue_detail")
    __table_args__ = (
        UniqueConstraint("issue_id", name="uq_issue_detail_issue_id"),
        Index("idx_issue_scenic_active", "scenic_id", "active"),
        Index("idx_issue_label", "topic_id", "dimension_id", "metric_id"),
        Index("idx_issue_business_class", "business_class"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 业务主键:对外一律用它(自增 id 只是物理主键,不进接口)。形如 issue_3f2a...
    issue_id: Mapped[str] = mapped_column(String(48), nullable=False,
                                          default=lambda: f"issue_{uuid.uuid4().hex[:16]}")
    # 归属景区;为空 = 全景区通用(集团级模板,所有景区都能看到)
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # ── 发起方式 ────────────────────────────────────────────────────────
    business_class: Mapped[str] = mapped_column(String(16), nullable=False, default=BUSINESS_CLASS_LOCAL)
    # 以下三列只有 quick_bi 才有值,local_system 一律为空
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ask_type: Mapped[str | None] = mapped_column(String(16), nullable=True)      # qa/insight/dashboard/report
    capability: Mapped[str | None] = mapped_column(String(24), nullable=True)    # dataset_qa/dashboard_qa/insight/report/file_qa
    target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    target_data_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_data_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 以下两列只有 local_system 才用:指定执行目标;都为空 = 按 APP 默认 Agent + 意图路由
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    skill_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ── 挂载的三级标签(冗余名称,避免列表接口每次连表) ──────────────────
    topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dimension: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dimension_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 问题本身 ────────────────────────────────────────────────────────
    title: Mapped[str] = mapped_column(String(255), nullable=False)   # 列表上显示的短标题
    issue: Mapped[str] = mapped_column(Text, nullable=False)          # 真正发出去的问题原文
    # 给模型的口径说明(不是给人看的介绍):取哪个时间范围、用哪个工具、什么算异常、答多长。
    # 与 issue 分开是因为按钮要短、而模型需要的恰恰是那些啰嗦的约束。
    model_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 参数模板:{参数名: 带占位符的值},发起前由代码展开({{today}}/{{yesterday}} 等),
    # 省掉让模型去推日期这一步(推错了很难发现)。
    param_template: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    time_period: Mapped[str | None] = mapped_column(String(32), nullable=True)  # today/yesterday/week_to_date…

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── 归属与可见性(0079:Quick BI 固定问题整体迁进来后必须要的几列) ──────
    # 原先这几列长在 tc_qbi_fixed_questions 上。那张表 0079 已删除 ——
    # 「Quick BI 固定问题」和「APP 问题」本来就是同一件事的两种配法,
    # 各存一张表就会出现两边字段不同、可见性判定不同、前台得自己合并的老问题。
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)   # 创建者
    # 可见范围:private 仅自己 / scenic 本景区 / group 集团内 / public(景区 API Key 也可调用)
    # server_default 是必须的:create_all 建表时只有 Python 默认值,库里这列就没有 DEFAULT,
    # 于是任何没显式写 visibility 的 INSERT(比如迁移 0076 的存量搬运)都会撞 NOT NULL。
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="group",
                                            server_default="group")
    # 数据域:票务/财务/舆情/客群…… 自由文本,允许现场新增(业务分类总会长出新的一类)
    domain: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)        # 管理端自定义分组
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)           # 语种,缺省跟随连接

    # ── 执行留痕(最后一次跑成什么样,运营自检用) ─────────────────────────
    # ── 加速(预热) ──────────────────────────────────────────────────
    # Quick BI 的问数/解读慢的要几十秒,报告要几分钟。而同一个景区的同一条固定问题,
    # 一天里会被很多人点很多次 —— 每次现跑既让人干等,也把上游额度和 token 白白烧掉。
    # 开了加速之后,后台按 interval 提前跑好,前台再点直接回放。
    # **没有单独的缓存表**:结果本来就整份存在 tc_qbi_runs 里,
    # 「缓存」= 这条问题在这个景区最近一次成功的运行且在 ttl 之内。
    # server_default 不能省:迁移 0076/0079 是裸 INSERT ... SELECT(不走 ORM),
    # 列里没列到 prewarm_enabled。只写 Python 侧 default 的话,建表时这列是
    # 「NOT NULL 且没有默认值」,那几条存量迁移会直接撞上非空约束 ——
    # 而撞上的时机是**别人升级老库的时候**,不是我们这边。
    prewarm_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"))
    prewarm_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prewarm_ttl_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # success/failed/running
    last_run_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 溯源:这行是从哪迁来的(app_question_bank.id 或 tc_qbi_fixed_questions.id)。
    # 迁移不是一次性的——存量表还在用,出问题要能对回去。
    source_question_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    create_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                  onupdate=func.now())
