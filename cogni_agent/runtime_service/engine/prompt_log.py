"""LLM 提示词日志记录助手。

v1.1.6:「会话信息」技能(cogni-agent-session-info)需要能查到当前会话每次大模型调用时
LLM **真正收到的最终提示词**,并据此分析提示词是否存在业务无关噪音 / 内部信息泄漏。

engine 各节点在真正发出调用前把最终 messages 沉淀为一行 ``llm_prompt_logs``:

- system_prompt:完整的 system 层提示词(主 prompt + extra_systems 拼接),完整保留 ——
  「系统提示词有没有噪音」是分析的核心;
- history:user / assistant / tool 消息逐条截断 —— 控制体积,同时噪音类注入
  (监督员提醒、工具禁用计数、调试信息等)通常出现在消息开头/结尾,截断不影响发现。

设计原则:**记录失败绝不影响主流程**,任何异常吞掉只记日志。
"""
from __future__ import annotations

import json
from typing import Any

from cogni_agent.common.logging import get_logger
from cogni_agent.control_plane_service.models import LLMPromptLog

log = get_logger("runtime_service.engine.prompt_log")

# 单条历史消息截断长度:分析噪音看的是开头/结尾(注入提醒都在两端),足够覆盖。
_HISTORY_MSG_CAP = 300
# 保留的历史消息条数:再往前是已被 trim 的老轮次,不完整,分析价值低。
_HISTORY_MSG_MAX = 40
# system 提示词整体长度上限(Text 无硬限制,截断仅为防异常超长注入)。
_SYSTEM_CAP = 20000


def _trunc(text: Any, cap: int) -> str:
    s = "" if text is None else str(text)
    if len(s) <= cap:
        return s
    return s[:cap] + f"…[截断,原长 {len(s)}]"


def _serialize_history(messages: list[Any]) -> list[dict]:
    """把 langchain BaseMessage 列表(或 litellm dict 列表)归一成
    [{"role", "content"}] 的 JSON 数组,逐条截断。"""
    out: list[dict] = []
    for m in messages[-_HISTORY_MSG_MAX:]:
        if isinstance(m, dict):
            role = m.get("role") or m.get("type") or "unknown"
            content = m.get("content", "")
        else:
            role = getattr(m, "type", "unknown")
            content = getattr(m, "content", "")
        if isinstance(content, list):  # tool_calls 等结构化 content
            content = json.dumps(content, ensure_ascii=False, default=str)
        out.append({"role": role, "content": _trunc(content, _HISTORY_MSG_CAP)})
    return out


async def record_prompt(
    db: Any,
    *,
    task_id: str,
    conversation_id: str | None,
    scenic_id: str | None,
    user_id: str | None,
    step: str,
    model: str | None = None,
    system_prompt: str = "",
    history: list[Any] | None = None,
    prompt_tokens: int | None = None,
) -> None:
    """把一次 LLM 调用的最终提示词落库(随主事务提交,不单独 commit)。

    history: 传 langchain BaseMessage 列表或已序列化的 [{"role","content"}...]。
    记录失败只记日志,绝不抛给调用方。
    """
    if db is None:
        return
    try:
        if history:
            first = history[0]
            if not isinstance(first, dict):
                history = _serialize_history(history)
        db.add(LLMPromptLog(
            task_id=task_id,
            conversation_id=conversation_id,
            scenic_id=scenic_id,
            user_id=user_id,
            step=step,
            model=model,
            prompt_tokens=prompt_tokens,
            system_prompt=_trunc(system_prompt, _SYSTEM_CAP) or None,
            history=json.dumps(history or [], ensure_ascii=False),
        ))
        await db.flush()
    except Exception:  # noqa: BLE001 - 记录失败绝不能影响主流程
        log.exception("记录 LLM 提示词日志失败(task=%s step=%s)", task_id, step)
