"""
统一 API 响应格式与错误码体系。

外部 AI API 标准化请求入口的核心规范：
- 所有接口统一返回 ApiResponse 信封
- 错误码集中定义，方便调用方统一处理
- 分页请求/响应标准化
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════
# 统一响应信封
# ═══════════════════════════════════════════════════════════════

class ApiResponse(BaseModel, Generic[T]):
    """所有 API 接口统一返回格式。"""
    code: int = Field(default=0, description="业务状态码，0=成功，非0=错误（见 ErrorCode）")
    message: str = Field(default="success", description="人类可读的描述信息")
    data: T | None = Field(default=None, description="响应数据体")
    trace_id: str | None = Field(default=None, description="全链路追踪ID，便于排障")

    @classmethod
    def ok(cls, data: T = None, message: str = "success", trace_id: str | None = None) -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data, trace_id=trace_id)

    @classmethod
    def fail(cls, code: int, message: str, data: T = None, trace_id: str | None = None) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=data, trace_id=trace_id)


# ═══════════════════════════════════════════════════════════════
# 错误码体系（业务层，非 HTTP 状态码）
# ═══════════════════════════════════════════════════════════════

class ErrorCode:
    """错误码常量，外部调用方可据此做分支处理。"""
    # 通用 1xxx
    SUCCESS = 0
    UNKNOWN = 1000
    INVALID_PARAM = 1001
    MISSING_PARAM = 1002
    RATE_LIMITED = 1003

    # 鉴权 2xxx
    UNAUTHORIZED = 2000          # 未认证
    FORBIDDEN = 2001             # 无权限
    API_KEY_INVALID = 2002       # API Key 无效
    API_KEY_EXPIRED = 2003       # API Key 已过期
    API_KEY_REVOKED = 2004       # API Key 已吊销
    AGENT_NOT_ALLOWED = 2005     # 该 Key 无权调用此 Agent

    # 用户登录认证细分 2006-2011（v1.0 新增，与 API Key 错误码区分）
    AUTH_BAD_CREDENTIALS = 2006      # 账号/密码错误
    AUTH_TOKEN_EXPIRED = 2007        # Token 已过期（可触发刷新）
    AUTH_TOKEN_INVALID = 2008        # Token 无效/被篡改（需重新登录）
    AUTH_CREDENTIALS_CHANGED = 2009  # 凭据变更（密码已修改，旧 Token 失效）
    AUTH_ACCOUNT_DISABLED = 2010     # 账号已被禁用
    AUTH_TOKEN_REVOKED = 2011        # Token 已被强制失效（管理员踢下线）

    # 资源 3xxx
    NOT_FOUND = 3000
    AGENT_NOT_FOUND = 3001
    AGENT_DISABLED = 3002
    TASK_NOT_FOUND = 3003

    # 执行 4xxx
    TASK_FAILED = 4000
    GUARDRAIL_TRIPPED = 4001
    EXECUTION_TIMEOUT = 4002
    LLM_ERROR = 4003
    RESOLVE_FAILED = 4004        # 解析执行目标失败（能力定义不存在/无权限/覆盖参数不合法）
    BUDGET_EXCEEDED = 4005       # 当日预算超限
    OUTPUT_BLOCKED = 4006        # 生成内容未通过安全审核
    QUICKBI_ERROR = 4100         # Quick BI 智能小Q 调用失败(配置/鉴权/业务错误码/超时,message 含原因)

    # 系统 5xxx
    INTERNAL_ERROR = 5000
    DB_ERROR = 5001
    REDIS_ERROR = 5002

    # 错误码 → 人类可读消息
    MESSAGES: dict[int, str] = {
        0: "success",
        1000: "未知错误",
        1001: "参数无效",
        1002: "缺少必填参数",
        1003: "请求过于频繁，请稍后再试",
        2000: "未认证，请提供有效的凭证",
        2001: "无权限访问该资源",
        2002: "API Key 无效",
        2003: "API Key 已过期",
        2004: "API Key 已被吊销",
        2005: "该 API Key 无权调用此 Agent",
        2006: "账号/手机号或密码错误",
        2007: "Token 已过期，请刷新",
        2008: "Token 无效，请重新登录",
        2009: "密码已修改，请重新登录",
        2010: "账号已被停用，请联系管理员",
        2011: "会话已被管理员终止",
        3000: "资源不存在",
        3001: "Agent 不存在",
        3002: "Agent 已禁用",
        3003: "任务不存在",
        4000: "任务执行失败",
        4001: "触发安全护栏，请求被拒绝",
        4002: "任务执行超时",
        4003: "LLM 调用失败",
        4004: "解析执行目标失败",
        4005: "当日预算已用尽",
        4006: "生成内容未通过安全审核",
        5000: "服务器内部错误",
        5001: "数据库错误",
        5002: "缓存服务不可用",
    }

    @classmethod
    def message(cls, code: int) -> str:
        return cls.MESSAGES.get(code, cls.MESSAGES[1000])


# ═══════════════════════════════════════════════════════════════
# SSE 错误事件统一规范（错误码统一）
# ═══════════════════════════════════════════════════════════════

class TaskErrorKind:
    """SSE error 事件的 kind 常量（机器可读），与 ErrorCode 数字码一一映射。

    铁律：任何向客户端推送 error 事件的地方（executor / routes_chat / routes_app 等）
    必须用 task_error() 构造 data，禁止散落裸 dict 或魔法字符串——这样 error event
    与 ApiResponse 共享同一套 ErrorCode，调用方按 code 分支、按 message 展示、
    按 kind 精确识别细分场景。kind 值保持稳定（会落库/被前端依赖），新增场景先在此登记。
    """
    INTERNAL_ERROR = "internal_error"      # 执行器未捕获异常（兜底）
    TASK_NOT_FOUND = "task_not_found"      # 任务不存在/无权访问
    RESOLVE_FAILED = "resolve_failed"      # 解析执行目标失败（业务性：能力不存在/无权限/参数不合法）
    RESOLVE_ERROR = "resolve_error"        # 解析执行目标时发生意外异常
    AGENT_NOT_DISTRIBUTED = "agent_not_distributed"  # 15 号文档:Agent 未对当前账号分发,无隐式默认版本可回落
    # Skill 未对当前账号分发。这个 kind 原先**没有登记在这里**,于是 executor 的
    # `kind = exc.code if exc.code in KIND_TO_CODE else RESOLVE_FAILED` 把它降级成了
    # 通用的 resolve_failed —— 前端好几处按 skill_not_distributed 写的引导文案
    # (分发中心的跳转提示)从来没被触发过,用户只看到一句笼统的「解析失败」。
    SKILL_NOT_DISTRIBUTED = "skill_not_distributed"
    BUDGET_EXCEEDED = "budget_exceeded"    # 当日预算超限
    GUARDRAIL_TRIPPED = "guardrail_tripped"  # 触发安全护栏
    OUTPUT_BLOCKED = "output_blocked"      # 输出内容未通过审核
    TASK_TIMEOUT = "task_timeout"          # 执行超时
    UNEXPECTED_ERROR = "unexpected_error"  # 任务执行过程意外失败
    STREAM_ERROR = "stream_error"          # SSE 事件流异常（网关侧）
    FAILED = "failed"                      # 兜底：未知/未标记具体类别的任务失败


# kind → ErrorCode 数字码。一个数字码可对应多个 kind（细粒度差异保留在 kind 上）
KIND_TO_CODE: dict[str, int] = {
    TaskErrorKind.INTERNAL_ERROR: ErrorCode.INTERNAL_ERROR,        # 5000
    TaskErrorKind.TASK_NOT_FOUND: ErrorCode.TASK_NOT_FOUND,        # 3003
    TaskErrorKind.RESOLVE_FAILED: ErrorCode.RESOLVE_FAILED,        # 4004
    TaskErrorKind.RESOLVE_ERROR: ErrorCode.INTERNAL_ERROR,         # 5000
    TaskErrorKind.AGENT_NOT_DISTRIBUTED: ErrorCode.RESOLVE_FAILED,  # 4004（细分 kind，同码）
    TaskErrorKind.SKILL_NOT_DISTRIBUTED: ErrorCode.RESOLVE_FAILED,  # 4004（同上）
    TaskErrorKind.BUDGET_EXCEEDED: ErrorCode.BUDGET_EXCEEDED,      # 4005
    TaskErrorKind.GUARDRAIL_TRIPPED: ErrorCode.GUARDRAIL_TRIPPED,  # 4001
    TaskErrorKind.OUTPUT_BLOCKED: ErrorCode.OUTPUT_BLOCKED,        # 4006
    TaskErrorKind.TASK_TIMEOUT: ErrorCode.EXECUTION_TIMEOUT,       # 4002
    TaskErrorKind.UNEXPECTED_ERROR: ErrorCode.TASK_FAILED,         # 4000
    TaskErrorKind.STREAM_ERROR: ErrorCode.INTERNAL_ERROR,          # 5000
    TaskErrorKind.FAILED: ErrorCode.TASK_FAILED,                   # 4000
}


def task_error(kind: str, message: str | None = None) -> dict:
    """构造标准化的 SSE error 事件 data（与 ApiResponse 共用 ErrorCode 数字码）。

    data = {kind, code, message}：
    - kind:    机器可读的细分场景标识（稳定，可落库/被前端依赖）
    - code:    与 ApiResponse.code 同一套 ErrorCode 数字码，调用方按 code 分支
    - message: 传入的具体原因；未传则用 ErrorCode 标准文案（Message 格式统一：标准文案: 具体原因）
    """
    code = KIND_TO_CODE.get(kind, ErrorCode.UNKNOWN)
    return {
        "kind": kind,
        "code": code,
        "message": message or ErrorCode.message(code),
    }


# ═══════════════════════════════════════════════════════════════
# 分页规范
# ═══════════════════════════════════════════════════════════════

class PageRequest(BaseModel):
    """分页请求参数。"""
    page: int = Field(default=1, ge=1, description="页码，从1开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页条数，最大100")


class PageResponse(BaseModel, Generic[T]):
    """分页响应体。"""
    items: list[T] = Field(default_factory=list)
    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1)
    page_size: int = Field(default=20)
    total_pages: int = Field(default=0)

    @classmethod
    def of(cls, items: list[T], total: int, page: int, page_size: int) -> "PageResponse[T]":
        total_pages = (total + page_size - 1) // page_size if total > 0 else 0
        return cls(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)
