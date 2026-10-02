"""19 号文档:APP 问题库 / 指标定义 / 预热缓存三表(固定问题工作流的数据层)。

设计要点(文档 19《APP 固定问题工作流实施定稿》;编排组合层已由 23 号删除):
- app_question_bank 用 dimension_name 吸收原三级选择器(selector 是问题的展示
  分组属性,不是独立实体),同一问题只维护一行,避免两处漂移;
- skill_id 直接指向 tc_skills:非空 = 命中后直跳该技能(跳过 Triager),
  空 = 不干预,走 Triager 动态链路(原 route_mode=auto/workflow 由本列派生);
- app_metric_def 独立保留:它是被引用方(问题库 metric_code/概览分色/预警判断
  都指向它),UNIQUE(theme_code, metric_code) 约束在独立表上才成立;
- app_prewarm_cache:(question_id, scenic_id, biz_date) 唯一,每日每问题一行
  (占位→收割 UPDATE),预热结果不长期存 task_queue(表膨胀/查询低效,见 18 号 §2.6)。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name
from cogni_agent.common.time import today_cst


class AppQuestionBank(Base):
    """APP 问题库(固定问题主表)。

    question_text 是归一化精确匹配的主键文本(与预热命中共用同一归一化函数)。
    skill_id 非空 = 命中后直跳该技能(跳过 Triager);skill_id 为空 = 不干预,
    走 Triager 动态链路。
    """

    __tablename__ = table_name("app_question_bank")
    __table_args__ = (
        Index("idx_qbank_theme", "theme_code", "active"),
        Index("idx_qbank_text", "question_text"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # None=全景区通用模板
    theme_code: Mapped[str] = mapped_column(String(32), nullable=False)  # passenger_flow/ticket/public_opinion/ota_direct_sales
    question_text: Mapped[str] = mapped_column(String(512), nullable=False)  # 问题原文(匹配主键)
    # 「问题描述」:**真正发给大模型的那段说明**,不是给人看的介绍。
    #
    # 与 question_text 的分工必须分清:
    #   question_text      前台按钮上的一句话,给**用户**看 —— 「今日入园人数」
    #   model_instruction  告诉**模型**该怎么办 —— 取哪个时间范围、用哪个工具、
    #                      口径是什么、什么情况下要提示异常、答复要多长
    #
    # 为什么必须分成两个字段:前台按钮要短(长了排不下、用户也不看),
    # 而模型需要的恰恰是那些啰嗦的约束。共用一个字段的话,
    # 要么按钮变成一大段话,要么模型拿不到口径只能猜 ——
    # 「今日入园人数」它不知道是按闸机去重还是按票数、是自然日还是运营日。
    model_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_code: Mapped[str | None] = mapped_column(String(64), nullable=True)  # → app_metric_def.metric_code
    # 三级选择器第三级「指标」展示名(方案 A:主题→维度→指标)。一个指标=一个叶子问题=该行,
    # 点击锁 question_id。运营在问题维护里填(如 实时人数/承载率);空则 app 端回退问题原文。
    # 注意与 app_metric_def.metric_name(全局指标定义的阈值名)区分:本列是选择器叶子展示口径。
    metric_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenario: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 日常巡检/应急响应/经营分析
    time_period: Mapped[str] = mapped_column(String(32), nullable=False)  # today/yesterday/week_to_date/...
    priority: Mapped[str] = mapped_column(String(4), nullable=False, default="P1")  # P0/P1/P2
    # 非 NULL = 该问题作为选择器第三级节点展示(第二级=dimension_name),吸收 app_selector_map
    dimension_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 问题级图表返回形态标注(22 号文档「图表返回问题标识」):auto=不干预(LLM 自由输出
    # cards/charts)/card=数据卡片为主/line/bar/pie/radar=固定图表形态。纯运营标注,
    # 运行时(graphs/网关/预热)一律不消费、不据此注入提示词——图表引导须内置于绑定
    # 技能自身提示词;标注唯一用途 = 运营自检「该问题要的形态,绑定技能是否教了怎么出」
    # (question_bank 图表引导自检 + scripts/check_question_chart_guidance.py)。
    return_format: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # 23 号:直接绑定执行技能(原 composition_id → 组合 → skill_ids_ordered[0] 恒等转发层已删)。
    # 非空 = 命中后直跳该技能(跳过 Triager);空 = 走 Triager 动态链路(等价原 route_mode=auto)。
    skill_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # v1.2 参数模板(19 号 S7.5):{参数名: 带占位符的值},命中后代码展开注入 slots,
    # 替代 Triager 的 LLM 日期推导。占位符:{{today}}/{{yesterday}}/{{now}}/{{week_start}}/
    # {{month_start}}/{{tomorrow}}/{{today_start}}/{{yesterday_start}}/{{yesterday_end}} 等。
    # 相对参数(对比期)不复刻——填了基准后由 L3 x-default-rule 规则引擎自动补全(复用)。
    param_template: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 指定执行 Agent(空=APP 端默认)
    prewarm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prewarm_point: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 如 "00:30";实时类不预热
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)  # 预热调度工作字段
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # priority 主序,同 P 按此序
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 下架开关
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppMetricDef(Base):
    """APP 指标定义:各主题核心指标 + 黄/红预警阈值。

    被 app_question_bank.metric_code 引用,也是 L2 工作流生成时异常分色的规则来源
    (迁移自 insight_service.py 硬编码的分色阈值)。
    """

    __tablename__ = table_name("app_metric_def")
    __table_args__ = (
        UniqueConstraint("theme_code", "metric_code", name="uq_metric_theme_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    theme_code: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False)  # inpark_ratio/refund_rate/...
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 黄色预警(如 ≥75%)
    danger_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 红色预警(如 ≥90%)
    time_node: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 小时/昨日/今日
    update_strategy: Mapped[str] = mapped_column(String(16), nullable=False)  # realtime / daily_0am
    priority: Mapped[str] = mapped_column(String(4), nullable=False, default="P1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppPrewarmCache(Base):
    """APP 预热缓存(22 号复盘后按业务日留存,18 号 §4.2 演进)。

    每「景区×问题×业务日」一行:初派报告任务时即建当日占位行(answer=NULL 锁位),
    任务 COMPLETED 收割时 UPDATE 补全——每天每问题必有一条当日记录。
    OTA 问题(theme_code=ota_direct_sales)在景区无 OTA 账号时不派 LLM(零 token):
    预热侧跳过该景区,APP 直出侧实时判定后直接返回「未配置账号」原因文案。
    消费端只认当日行(biz_date=今天 且 answer 非空):今天未出=空态(等预热填充),
    绝不回退昨日行。历史按日留存,保留 _RETENTION_DAYS(7) 天后由 CronJob 清理。
    biz_date=业务日(任务创建日,东八区);data_as_of=数据时点;time_period 仅作
    LLM 参数透传,与 biz_date 解耦。
    """

    __tablename__ = table_name("app_prewarm_cache")
    __table_args__ = (
        # 25 号:唯一键加 ota_account 维度;27 号:再加 skill_version 维度
        # (表达式索引,COALESCE 让 NULL 与具体值共存互不冲突;对应 0057/0059
        # 迁移的 DROP 旧索引 + 建表达式唯一索引)
        Index(
            "uq_prewarm_q_scenic_acct_ver_date",
            "question_id", "scenic_id",
            text("coalesce(ota_account, '')"), text("coalesce(skill_version, -1)"),
            "biz_date",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    question_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)  # → app_question_bank.id
    scenic_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # 空串=集团级(无景区语义问题)
    # 25 号:OTA 数据按「景区×账号」拆分(账号=platforms.*.username,决定数据范围)。
    # NULL=账号无关行(客流/票务/舆情主题;概览的基础版,未绑账号用户回退);
    # 非 NULL=该账号专属行(OTA 主题;概览的账号版)。唯一键见下方表达式索引。
    ota_account: Mapped[str | None] = mapped_column(String(128))
    # 27 号:生成该行所用技能的指派版本号(版本随用户指派,消费严格匹配;
    # NULL=旧数据/未记,按 -1 桶进唯一键,不匹配任何期望版本,自然淘汰)
    skill_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biz_date: Mapped[date] = mapped_column(Date, nullable=False, index=True,
                                           default=today_cst)  # 业务日=任务创建日(北京时间)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)  # 溯源:当日该问题的初派报告任务(摘要重试计数挂其 payload)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL=当日占位行(报告任务未收割);task.result.answer
    structured: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # cards/charts/insight/recommends
    data_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)  # 数据时点
    # ── 20 号文档:二次加工派生列(落库时生成,卡片消费;chat 直出只读 answer/structured) ──
    summary: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 卡片一句话(60字左右)
    overview_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 摘要技能从主题报告摘录的本主题关键指标(分色),panel/insight 主题级 level 兜底
    enrich_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 二次加工任务防重标记
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SysParamDef(Base):
    """系统参数定义(0090,系统设置 · 参数配置)。

    一行 = 一个可以在 Tool / MCP / Skill 里用 `{{参数标识}}` 引用的参数(如 OTA 商户账号)。
    值分两层:这里的 default_values 是系统默认,景区在 SysParamValue 里覆盖。
    一个景区可以有多个值(多个商户账号),multi_mode 决定执行时怎么用:
    每个值各调一次再合并 / 作为数组一次传入 / 逗号拼接一次传入。
    解析与注入见 runtime_service/engine/scenic_params.py。
    """

    __tablename__ = table_name("sys_param_def")
    __table_args__ = (UniqueConstraint("param_key", name="uq_sys_param_def_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    param_key: Mapped[str] = mapped_column(String(64), nullable=False)               # 参数标识,{{param_key}} 引用
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")      # 显示名
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")       # 说明
    multi_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="single")  # single/fanout/list/join
    default_values: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # 系统默认 [{value,label}]
    arg_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)      # 命中即自动填的工具入参名
    fill_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="fill")  # fill / force
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 敏感:不进提示词
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SysParamValue(Base):
    """参数的景区取值(0090,景区配置 · 接口参数):一行 = 一个景区对一个参数的覆盖。

    没有这一行 = 该景区用系统默认值。values 可有多个,各带备注(如「携程主账号」「美团」)。
    """

    __tablename__ = table_name("sys_param_value")
    __table_args__ = (UniqueConstraint("param_key", "scenic_id", name="uq_sys_param_value"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    param_key: Mapped[str] = mapped_column(String(64), nullable=False)               # 参数标识
    scenic_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)   # 景区 ID
    values: Mapped[list] = mapped_column(JSON, nullable=False, default=list)         # [{value,label}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AppScenicConfig(Base):
    """25 号:景区配置(承载量与景区级参数,APP 运营后台维护)。

    挂景区维度不挂用户(与用户模型演进解耦)。核心列强校验(承载量),
    后期新配置先落 extra(带 schema_version),稳定后提升核心列——与
    竞品需求表同一模式。消费(预热参数注入/insight 分色)为后续批次,
    当前纯配置管理:未配置 = 维持现状,绝不猜默认值。
    """

    __tablename__ = table_name("app_scenic_config")
    __table_args__ = (
        UniqueConstraint("scenic_id", name="uq_app_scenic_config_scenic"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scenic_id: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 日最大承载量(人次/天,>0)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # 扩展配置(带 schema_version)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 最近编辑人(user_id)
