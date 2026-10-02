"""前置参数处理:端无关契约与共用规则。

链路定位——前置参数处理是「用户提问」与「建任务」之间独立的一环:

    用户提问 → 【前置参数处理】 → 建任务 → LLM 意图识别 → LLM 回答组装

它只解决一个问题:**让参数在进 LLM 之前就被凑齐**,从而避免「意图已定、技能已选,
却因为缺一个参数反过来问用户」的二次往返(每反问一次 = 多一轮 LLM + 多一次等待)。

产物 ParamContext 是各端适配层(如 app_pre_params.py)的唯一输出,也是本层与
执行期(runtime_service)之间唯一的数据契约:
  - params     : 已确定性解析出的参数;
  - unresolved : 解析不出来的参数名。入口据此 fail-fast(明确报错/给出选项),
                 而不是丢给 LLM 去反问用户。

端无关原则(重要):本文件不得出现任何端专属概念(Request / APP body / API Key)。
新增一个端 = 新增一个「端适配层」文件并复用本文件的规则,而不是复制一套逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注用,避免把 HTTP 概念引入运行期依赖
    from cogni_agent.gateway_service.deps import CurrentUser


@dataclass
class ParamContext:
    """端无关的标准上下文(前置参数处理的产物)。

    端无关字段(scenic_id/user_id/role)由本文件的共用规则算出;
    端专属字段(如 APP 的 date/question_id)由各端适配层填。
    """

    source: str                        # 端标识,取值见 control_plane_service.models.SOURCE_*
    scenic_id: str | None              # 执行上下文景区;None = 集团级账号或用户未绑定景区
    user_id: str | None
    role: str
    message: str
    date: dict | None = None           # 问数日期上下文(APP 端日期选择器传入)
    question_id: str | None = None     # 固定问题入口标记(卡片/选择器/试试问我)
    params: dict = field(default_factory=dict)            # 已确定性解析出的参数
    unresolved: list[str] = field(default_factory=list)   # 未解析参数名(入口 fail-fast 依据)


def resolve_scenic_id(conv: Any, user: "CurrentUser") -> str | None:
    """执行上下文景区:读会话上的景区。

    会话上的值在入口处已经按产品口径归一过(scenic_binding.resolve_turn_scenic):
    景区账号 = 当前归属(改过归属的旧会话也跟着改),集团账号 = 消息点名 > 会话此前指定。
    这里只读不判 —— 同一个规则在两处各写一遍,迟早对不上。
    无会话(一次性任务)则用当前景区。
    """
    if conv is not None:
        return conv.scenic_id
    return user.scenic_id


def initial_unresolved(scenic_id: str | None) -> list[str]:
    """入口侧唯一能立刻判定的缺参:景区没解析出来。

    注意:scenic_id=None 在管理端是合法的(集团级账号,不归属具体景区),是否致命
    由各端自己决定。故此处只如实记录,不改变任何行为。
    """
    return [] if scenic_id else ["scenic_id"]


def default_context(*, source: str, user: "CurrentUser", message: str,
                    conv: Any = None) -> ParamContext:
    """未注册端的兜底上下文:只算端无关部分,不解析任何端专属字段。"""
    scenic_id = resolve_scenic_id(conv, user)
    return ParamContext(
        source=source, scenic_id=scenic_id, user_id=user.user_id, role=user.role,
        message=message, unresolved=initial_unresolved(scenic_id),
    )
