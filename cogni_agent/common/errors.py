"""统一异常类型,便于网关层/中间件统一捕获转 HTTP 状态码 + 写审计。"""


class ZhilvError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "", *, status_code: int | None = None,
                 error_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.error_code = error_code  # 可选的自定义 numeric 错误码，覆盖 CODE_MAP 映射


class Unauthorized(ZhilvError):
    status_code = 401
    code = "unauthorized"


class Forbidden(ZhilvError):
    """G-ROLE-04:越权访问统一走这个异常,由中间件记入审计(event=UnauthorizedAccess)。"""

    status_code = 403
    code = "forbidden"


class ValidationError(ZhilvError):
    """入参不合法(400)。

    与 Forbidden(403)区分开:403 是"你没资格做这件事",400 是"这件事本身填错了"。
    以前缺这个类型,像「新密码至少 8 位」这类校验只能借 Forbidden 抛出,
    调用方看到 403 会以为是权限问题,白白排查一圈。
    """

    status_code = 400
    code = "validation_error"


class NotFound(ZhilvError):
    status_code = 404
    code = "not_found"


class RateLimited(ZhilvError):
    """G-API-08:开放平台限流命中。"""

    status_code = 429
    code = "rate_limited"


class GuardrailTripped(ZhilvError):
    """M4-06:护栏熔断(步数/工具次数/Token 预算超限)。"""

    status_code = 422
    code = "guardrail_tripped"


class ApprovalRejected(ZhilvError):
    """M4-07:高风险动作 HITL 审批被驳回——按功能清单语义,整条任务标记 CANCELLED。"""

    status_code = 409
    code = "approval_rejected"


class ClarificationNeeded(ZhilvError):
    """执行到一半发现缺一个**只能由用户给**的关键输入(典型:集团账号没指定景区)。

    与 Triager 的 clarify 是同一件事,只是发现得晚一些 —— Triager 在执行前看的是
    「这句话缺什么」,而「需不需要景区」要等模型真的选中一个按景区取数的接口才知道。
    提前按「技能里有没有这类工具」一刀切会误伤:技能挂了 20 个取数接口,
    用户问的却是「今天几号」,这时反问「查哪个景区」只会让人莫名其妙。

    不用 interrupt:这不是审批,不需要把图挂起等回调 —— 直接收尾成一个问句,
    用户下一轮回答时按新消息正常走一遍就行(和 Triager clarify 同一种收尾方式)。
    """

    status_code = 200
    code = "clarification_needed"

    def __init__(self, question: str, *, slot: str = "", detail: str = "") -> None:
        super().__init__(question)
        self.question = question
        self.slot = slot            # 缺的是哪一项(scenic_id …),前端/统计用
        self.detail = detail        # 给排查的人看的:哪个工具的哪个参数触发的


class ResolveFailed(ZhilvError):
    """M4-02:能力定义解析失败(资源不存在/无权限/覆盖参数不合法)。"""

    status_code = 400
    code = "resolve_failed"


class ApiKeyInvalid(ZhilvError):
    """API Key 无效（格式错误/不存在/已吊销/已过期）。"""

    status_code = 401
    code = "api_key_invalid"


class AgentNotAllowed(ZhilvError):
    """该 API Key 无权调用指定 Agent（未授权/Agent 已禁用）。"""

    status_code = 403
    code = "agent_not_allowed"
