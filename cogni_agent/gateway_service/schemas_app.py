"""v1.0 APP 端 Pydantic 请求/响应模型。

与 routes_app.py 内联的 login/refresh/me 模型不同，本文件收纳 config 和
competitor-requests 两个新模块的 Schema，避免 routes_app.py 越写越长。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


# ═══════════════════════════════════════════════════════════════
# 用户配置存储（文档 05）
# ═══════════════════════════════════════════════════════════════

CONFIG_TYPE_WHITELIST = {"ota_credentials", "workbench"}


class ConfigResponse(BaseModel):
    config_type: str
    config: dict
    updated_at: str | None


class ConfigTypeItem(BaseModel):
    config_type: str
    updated_at: str | None


class ConfigTypesResponse(BaseModel):
    types: list[ConfigTypeItem]


class SaveConfigResponse(BaseModel):
    ok: bool = True
    updated_at: str


# ═══════════════════════════════════════════════════════════════
# 竞品需求提交（文档 06）
# ═══════════════════════════════════════════════════════════════

VALID_STATUSES = {"pending", "reviewing", "accepted", "rejected"}


class CompetitorRequestCreate(BaseModel):
    """竞品需求提交请求体，只含 schema_version + extra，具体字段由 extra 承载。"""
    schema_version: int = Field(default=1, ge=1)
    extra: dict = Field(default_factory=dict)


class CompetitorRequestListItem(BaseModel):
    id: str
    schema_version: int
    extra: dict
    status: str
    created_at: str


class CompetitorRequestListResponse(BaseModel):
    items: list[CompetitorRequestListItem]
    total: int
    page: int
    page_size: int


class CompetitorRequestDetailResponse(BaseModel):
    id: str
    schema_version: int
    extra: dict
    status: str
    created_at: str
    updated_at: str


# ═══════════════════════════════════════════════════════════════
# v1.1 APP API: Agent Home / Chat / 会话 / 消息（阶段二）
# ═══════════════════════════════════════════════════════════════


# ── 2A.1: /api/app/agent/home ──

class GuideCard(BaseModel):
    """引导卡片：在欢迎页展示 5 条主题推荐。"""
    id: str = ""                      # 卡片唯一标识（overview/passenger_flow/ticket/public_opinion/ota）
    title: str
    subtitle: str = ""                # 副标题（卡片头部展示）
    question: str
    icon: str = "sparkles"            # 前端图标名
    theme_code: str = ""              # 主题标识（passenger_flow/ticket/...）
    # 20 号 P1:固定入口点击后 chat 需携带的问题库 id(问题库为空回退静态数据时为空串)
    question_id: str = ""
    # 主题解读问题的 issue_id(该景区已配 Quick BI 主题解读时有值)。
    # **前端优先用它发起** —— 只发 question_id 的话,这张卡会走通用对话,
    # 回一句「请告诉我您需要查询什么」,而用户点的明明是「客群解读」。
    issue_id: str = ""
    # 该主题解读是否已绑 Quick BI(false = 点了只会走通用对话,运营需要去后台生成)
    quickbi_ready: bool = False
    # 20 号:数据新鲜度温和标识(P3 预热后有值;无缓存为 None)
    generated_at: str | None = None  # 缓存生成时间(ISO)
    is_today: bool | None = None     # generated_at 日期 == 今天(后端算好,前端免时区逻辑)


class OverviewInsightItem(BaseModel):
    """概览页各主题核心指标解读概要（异常分色）。"""
    theme: str
    metric: str                       # 指标名（如 在园人数/最大承载数）
    value: str                        # 当前值（如 8523/20000）
    level: str                        # danger / warning / normal


class SelectorQuestionRef(BaseModel):
    """三级选择器第三级：指标叶子节点(方案 A)。

    name = 指标名(问题行 metric_name,如 实时人数/承载率;空则 app 端回退 text);
    id   = 该指标对应的唯一问题(question_id),点击后 chat 携带锁定;
    text = 完整问题原文(发问内容,chat 的 message)。
    """
    id: str
    text: str
    name: str | None = None  # 指标名(第三列展示;空 = 回退问题原文)


class SelectorMetricItem(BaseModel):
    """三级选择器第二级：维度(19 号起由 dimension_name 承载;metrics 字段名为 11 号历史遗留)。"""
    name: str
    questions: list[str] = []         # 兼容老前端(纯文本列表)
    items: list[SelectorQuestionRef] = []  # 20 号 P1:第三级叶子(指标名+question_id)


class SelectorThemeItem(BaseModel):
    """三级选择器第一级：主题。"""
    theme: str
    metrics: list[SelectorMetricItem]


class AppTargetItem(BaseModel):
    """可调用目标(0074):Agent 或 Skill,前端据 target_type 决定 chat 传哪个字段。

    version:该用户被指派到的版本号(agent 为指派版本,skill 为技能指派版本);
    source:assigned = 管理员按用户指派的(fail-closed 体系里唯一可执行来源),
            visible  = 仅可见(超管等豁免角色可直接调用,普通账号调用会被拦)。
    """
    target_type: str                 # agent / skill
    target_id: str
    name: str
    description: str | None = None
    opening_line: str | None = None
    version: int | None = None
    source: str = "assigned"


class AgentHomeResponse(BaseModel):
    agent_id: str | None
    agent_name: str | None
    agent_description: str | None
    agent_opening_line: str | None
    guide_cards: list[GuideCard]
    followed_questions: list[AppFollowedQuestionItem]
    selector_data: list[SelectorThemeItem]  # 三级选择器（主题→维度→指标,点击指标锁 question_id）
    overview_insights: list[OverviewInsightItem] = []  # 概览页各主题核心指标解读概要
    # 0074:当前默认目标 + 该用户可调用的全部目标(Agent / Skill)。
    # agent_id 保留是为了不打断存量前端;新代码读 target_type + target_id。
    target_type: str | None = None   # agent / skill
    target_id: str | None = None
    targets: list[AppTargetItem] = []


# ── 2A.6: 试试问我问题列表 ──

class AppGuideQuestionItem(BaseModel):
    id: str
    theme: str            # 主题中文名（客流/票务/舆情/OTA直销）
    question: str


class AppGuideQuestionListResponse(BaseModel):
    items: list[AppGuideQuestionItem]
    total: int
    page: int
    page_size: int


# ── 2A.7: 看板 AI 解读结论概要 ──

class AppPanelInsightMetricItem(BaseModel):
    """彩色指标条(≤4):name=维度中文名(客流/票务/舆情/OTA直销),metric/value=该维现状,
    level=异常分色。与 agent/home overview_insights 同源结构。"""
    name: str
    metric: str = ""
    value: str = ""
    level: str = "normal"             # danger / warning / normal


class AppPanelInsightResponse(BaseModel):
    theme: str
    summary: str                      # 命中缓存 = 完整解读报告(answer);未命中 = 空串(纯读缓存,不走实时)
    sentence: str | None = None       # 一句话总结(命中缓存 = cache.summary;未命中 = None)
    level: str                        # danger / warning / normal
    metrics: list[AppPanelInsightMetricItem] = []      # 彩色指标条(≤4,概览四维兜底)
    updated_at: str
    question_id: str = ""             # 该主题【主题解读】问题库 id(点击解读卡片→chat 携带;无库行时为空串)
    question: str = ""                # 该主题【主题解读】问题内容(前端展示/发出消息;无库行时为空串)
    # 该主题解读问题的 issue_id(景区已配 Quick BI 主题解读时有值)。
    # **前端点「解析」优先带它** —— 只带 question_id 会走通用对话,回一句
    # 「请告诉我您需要查询什么」,而用户点的明明是这个主题的解读
    issue_id: str = ""
    quickbi_ready: bool = False       # false = 该景区还没把这个主题解读绑到 Quick BI
    # 报告的生成状态。报告一条要 3~10 分钟,前端要能分清「还没开始 / 正在生成 / 已经好了」——
    # 三种状态显示同一个空白卡片的话,用户只会以为坏了。
    #   ready     已有结果,summary 就是报告正文(预热跑好的,直出)
    #   pending   已经在后台生成了,等几分钟再来
    #   none      还没配 Quick BI,或没有可用连接 —— 点了也出不来
    report_state: str = "none"
    source: str = ""                  # prewarm_cache / quickbi_report,排查时知道这份结果是哪来的


# ── 2A.2: POST /api/app/chat ──

class AppAskDateContext(BaseModel):
    """问数日期上下文（04 智能问数方案 §4.2：date 上下文注入）。

    APP 端选择日期区间后随问题一起提交，作为默认时间范围注入执行上下文，
    供 16 个数字大脑工具（startTime/endTime 等参数）与图表标题使用。
    全部字段可选：未传时由 LLM 自行判断/询问，或取工具默认范围。
    """
    start_date: str | None = None   # 起始日期，格式 YYYY-MM-DD
    end_date: str | None = None     # 结束日期，格式 YYYY-MM-DD
    compare_start: str | None = None  # 对比期起始日期（同比/环比）
    compare_end: str | None = None    # 对比期结束日期


class AppChatRequest(BaseModel):
    """发起对话。目标二选一:agent_id(默认)或 skill_id(0074 技能直调)。

    两者都传时以 skill_id 为准(更具体);都不传、也没给 issue_id 则 422 ——
    与其猜一个目标,不如让调用方明确说出来,否则「答非所问」排查起来要翻半天日志。
    """
    agent_id: str | None = Field(default=None, description="目标 agent id(与 skill_id 二选一)")
    skill_id: str | None = Field(default=None, description="目标 skill id(与 agent_id 二选一;"
                                                           "按用户分发,未分发报 skill_not_distributed)")
    # 带 issue_id 时可以留空,用问题配置里的原文(前端不必把问题文案再抄一遍)
    message: str = Field(default="")
    conversation_id: str | None = None  # 多轮会话 id（续聊时传入）
    date: AppAskDateContext | None = None  # 问数日期上下文（v1.1 智能问数 Phase B）
    # 19 号 S3:固定问题结构化标记(照 date 字段先例,全部可选=老版本零破坏)。
    # question_id:固定入口(卡片/选择器/试试问我)必带,命中后跳过 LLM 意图识别;自由输入不传。
    question_id: str | None = Field(default=None, max_length=36)
    # 前后端改造 §3:问题列表(businessIssueList)里 businessClass=local_system 那条的 id。
    # 传了它就不用再填 agent_id/skill_id/message —— 问题配置里都有,服务端带出来,
    # 省得前端把同一份配置抄两遍。Quick BI 那一类请改发 /api/app/quickbi/ask。
    issue_id: str | None = Field(default=None, max_length=48)

    @property
    def target_type(self) -> str:
        return "skill" if self.skill_id else "agent"

    @property
    def target_id(self) -> str | None:
        return self.skill_id or self.agent_id

    @model_validator(mode="after")
    def _require_target(self):
        # 带 issue_id 时目标由问题配置带出(可能是 agent 也可能是 skill),这里不强求
        if not self.agent_id and not self.skill_id and not self.issue_id:
            raise ValueError("缺少目标:agent_id / skill_id / issue_id 至少要有一个")
        if not (self.message or "").strip() and not self.issue_id:
            raise ValueError("提问内容不能为空(或改用 issue_id 发起固定问题)")
        return self
    # source 请求参数已于 2026-09-09 废弃:全链路无消费方(仅透传 SSE 展示,不落库不统计),
    # 且枚举收紧导致 insight 等面板入口 422。App 端遗留 source=insight 等值被 pydantic 忽略不报错。


class AppPrewarmHitResult(BaseModel):
    """预热缓存直出结果(20 号 P4):与实时执行同构——answer=完整报告,
    structured=cards/charts 数据,前端免开 SSE 直接渲染。

    一个景区一份数据:服务端按 token 所属景区 + 当日行匹配后下发,前端不做匹配。
    no_data=true 表示「该景区未配置 OTA 账号」的无数据说明(OTA 主题专属,
    未进 LLM,answer 即原因文案,前端走无数据态渲染而非图表)。
    """
    answer: str
    structured: dict | None = None
    data_as_of: str | None = None   # 统计口径日(ISO 日期),前端标注"X日数据"
    generated_at: str | None = None
    period: str | None = None       # 取数范围(24号:time_period 枚举,前端展示取数窗口,如 yesterday/today)
    is_today: bool | None = None
    no_data: bool = False           # true=无数据说明(如景区未配置 OTA 账号)


class AppChatResponse(BaseModel):
    task_id: str = ""               # 缓存直出时为空(前端据此判断免开 SSE)
    conversation_id: str
    hit_prewarm: bool = False       # 20 号 P4:true = 走预热缓存直出(未建任务)
    prewarm_result: AppPrewarmHitResult | None = None
    # 0074:这一轮实际打给谁。前端原样存下来,续聊/重发时回传同一个目标即可,
    # 不必自己推断——固定问题命中技能时,目标会从 agent 切到 skill。
    target_type: str = "agent"      # agent / skill
    target_id: str | None = None


# ── 2A.3: GET /api/app/chat/{task_id}/stream —— 无请求体，复用已有 SSE 事件模型 ──


# ── 2A.4: 会话管理 ──

class AppConversationListItem(BaseModel):
    id: str
    title: str
    executor_type: str
    executor_id: str
    turn_count: int
    created_at: datetime
    updated_at: datetime
    # 0074:这条会话打的是谁(与 executor_* 同值,字段名与 chat/home 对齐)。
    # 前端续聊时把它原样回传给 POST /chat 即可,不必自己记。
    target_type: str = "agent"
    target_id: str | None = None


class AppConversationListResponse(BaseModel):
    items: list[AppConversationListItem]
    total: int


# ── 2A.5: 消息操作 ──

class AppMessageFeedbackRequest(BaseModel):
    message_id: str = Field(...)
    # None 表示取消（删除该条反馈）；like/dislike 为正常点赞/踩
    feedback_type: str | None = Field(default=None, pattern="^(like|dislike)$")


class AppFollowQuestionRequest(BaseModel):
    question_text: str = Field(..., min_length=1, max_length=1024)
    source_message_id: str | None = None


class AppFollowedQuestionItem(BaseModel):
    id: str
    question_text: str
    source_message_id: str | None
    created_at: str
    # 关注问题点回去时该怎么发起:有 issue_id 就带它(问题可能配在 Quick BI 上),
    # 只发文本的话会走通用对话 —— 表现成「关注的问题点开回答失败/答非所问」
    issue_id: str = ""
    business_class: str = ""          # quick_bi / local_system;空 = 没对上问题配置


class AppFollowedQuestionListResponse(BaseModel):
    items: list[AppFollowedQuestionItem]
    total: int
