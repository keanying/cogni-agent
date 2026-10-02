"""
M4:上下文窗口治理——ReAct 循环里最容易被忽视、但一定会在生产上炸的一环。

两个独立的增长源,必须分别治理:

1. **消息条数增长**:每一轮 agent 节点都把完整历史重发给模型。max_steps=25 的循环
   意味着最后一轮要重发前 24 轮的全部内容。
2. **单条消息体积**:一个返回 500 行的数据库工具,`json.dumps` 之后可能就是几万字符,
   一条就能吃掉半个上下文窗口。

治理策略刻意选择"确定性裁剪"而不是"LLM 摘要压缩":
- 摘要压缩要额外的 LLM 调用(更慢、更贵),而且摘要本身可能丢关键信息(比如工具
  返回的具体 ID),在 Agent 场景里丢 ID 往往直接导致后续步骤失败。
- 确定性裁剪的行为完全可预测,出问题时容易复现和解释。

保留策略:system 提示 + 最初的用户目标 + 最近 N 轮,中间部分替换为一条占位说明。
"最初的用户目标"必须保留——它是漂移检测和最终收敛的锚点,裁掉它模型会忘记在干嘛。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.engine.context")

def _cfg(key: str, default):
    return get_settings().get("runtime", "context", key, default=default)


def estimate_tokens(text: str) -> int:
    """启发式 token 估算(中英混合),用于上下文预算控制,不引入外部 tokenizer 依赖。

    估算口径:中文/全角宽字符约 1 token/字,ASCII 约 0.3 token/字符
    (主流开源模型中文约 1 token/字、英文约 4 字符/token)。误差方向偏保守(略高估),
    宁可多裁一点,也不让超长上下文在厂商侧被截断或报错。
    """
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)
    ascii_n = len(text) - wide
    return max(1, int(wide + ascii_n * 0.3))


# 工具返回的压缩/截断已拆到 toolresult.py(纯函数,不依赖 langchain)。
# 这里重新导出:`ctx.compress_or_truncate(...)` / `ctx.truncate_tool_result(...)`
# 这些既有写法在 graphs.py 里到处都是,不改调用点。
from cogni_agent.runtime_service.engine.toolresult import (  # noqa: E402
    _TRUNCATION_NOTE,
    _summarize_json,
    _summarize_list,
    compress_or_truncate,
    truncate_tool_result,
)

__all_toolresult__ = ("compress_or_truncate", "truncate_tool_result",
                      "_summarize_json", "_summarize_list", "_TRUNCATION_NOTE")

def trim_messages(messages: list[BaseMessage], *, keep_recent: int | None = None,
                  max_tokens: int | None = None) -> list[BaseMessage]:
    """裁剪对话历史(条数上限 + 估算 token 上限双治理)。

    保留:
      - 全部 SystemMessage(人格/技能提示/防御提示/纠偏提示,都是行为约束,不能丢)
      - 第一条 HumanMessage(原始目标锚点)
      - 最近 keep_recent 条消息

    条数裁剪后,若估算 token 仍超 max_tokens(默认 runtime.context.max_context_tokens),
    再从旧到新二次裁剪——防止"条数不多但某条消息特别大"(如大段工具结果)把上下文撑爆。

    中间被裁掉的部分用一条 SystemMessage 占位说明,让模型知道"这里有过程被省略了",
    而不是让它误以为自己刚开始干活。

    关键约束:AIMessage(带 tool_calls)和其后对应的 ToolMessage 必须成对出现,
    OpenAI 协议不允许孤立的 tool 消息。因此裁剪边界会自动向前对齐到一个安全位置。
    """
    n_keep = keep_recent if keep_recent is not None else _cfg("keep_recent_messages", 20)
    max_tok = max_tokens if max_tokens is not None else _cfg("max_context_tokens", 0)

    if n_keep > 0 and len(messages) > n_keep:
        systems = [m for m in messages if isinstance(m, SystemMessage)]
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]
        if len(non_system) > n_keep:
            first_human = next((m for m in non_system if isinstance(m, HumanMessage)), None)
            cut = _align_cut_boundary(non_system, len(non_system) - n_keep)
            if cut > 0:
                dropped = cut - (1 if first_human is not None and non_system.index(first_human) < cut else 0)
                kept_tail = non_system[cut:]
                out: list[BaseMessage] = list(systems)
                if first_human is not None and first_human not in kept_tail:
                    out.append(first_human)
                if dropped > 0:
                    out.append(SystemMessage(content=f"[上下文裁剪] 为控制上下文长度,此处省略了 {dropped} 条较早的执行过程消息。"))
                out.extend(kept_tail)
                messages = out

    if max_tok > 0 and len(messages) > 2:
        messages = _trim_to_token_budget(messages, max_tok)

    log.debug(f"上下文裁剪:keep_recent={n_keep}, max_tokens={max_tok} -> {len(messages)} 条")
    return messages


def _trim_to_token_budget(messages: list[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    """token 级二次兜底:按估算 token 上限从旧到新裁剪非 System 消息。

    只丢"较早的执行过程";SystemMessage(行为约束)与第一条 HumanMessage(目标锚点)
    始终保留。裁剪边界对齐到安全位置(不拆散 AIMessage(tool_calls)+ToolMessage 对)。
    单条消息本身就超限的极端情况,由 truncate_tool_result / max_tool_result_chars 兜底。
    """

    def _estimate(msgs: list[BaseMessage]) -> int:
        return sum(estimate_tokens(str(getattr(m, "content", ""))) for m in msgs)

    systems = [m for m in messages if isinstance(m, SystemMessage)]
    non_system = [m for m in messages if not isinstance(m, SystemMessage)]
    first_human = next((m for m in non_system if isinstance(m, HumanMessage)), None)

    while len(non_system) > 2:
        current: list[BaseMessage] = list(systems)
        if first_human is not None and first_human not in non_system:
            current.append(first_human)
        current.extend(non_system)
        if _estimate(current) <= max_tokens:
            break
        cut = _align_cut_boundary(non_system, 1)
        if cut <= 0:
            break
        non_system = non_system[cut:]

    out: list[BaseMessage] = list(systems)
    if first_human is not None and first_human not in non_system:
        out.append(first_human)
    if len(out) < len(messages):
        out.append(SystemMessage(content="[上下文裁剪] 为控制上下文长度,此处省略了部分较早的执行过程消息。"))
    out.extend(non_system)
    return out


def _align_cut_boundary(messages: list[BaseMessage], cut: int) -> int:
    """把裁剪点向后推,直到它不会把 AIMessage(tool_calls) 和它的 ToolMessage 拆散。

    两种会产生孤儿 tool 消息的情况(OpenAI 协议下厂商一律 400)都要处理:
    1. 保留区开头是 ToolMessage:它对应的 AIMessage 已在丢弃区 → 向后推,把这一整组
       tool 消息也丢进丢弃区;
    2. 丢弃区结尾是 AIMessage(tool_calls):它对应的 ToolMessage 留在保留区 → 向后推,
       把后面的 ToolMessage 一并丢弃,保证 tool 调用与响应始终成对出现/成对消失。
    """
    idx = cut
    # 情况1:保留区开头不能是 ToolMessage(其 AIMessage 已在丢弃区)
    while idx < len(messages) and isinstance(messages[idx], ToolMessage):
        idx += 1
    # 情况2:丢弃区结尾不能是 AIMessage(tool_calls)(其 ToolMessage 在保留区)
    if (
        idx > 0
        and isinstance(messages[idx - 1], AIMessage)
        and (messages[idx - 1].additional_kwargs or {}).get("tool_calls_raw")
    ):
        while idx < len(messages) and isinstance(messages[idx], ToolMessage):
            idx += 1
    return idx


def to_litellm_message(message: BaseMessage) -> dict:
    """langchain 消息对象 -> OpenAI 协议消息 dict。

    **这里是曾经出过严重问题的地方,改动请务必谨慎**:
    早期版本的实现直接返回 `{"role":"assistant","content":...}`,把 AIMessage 上挂着的
    工具调用信息丢掉了。结果是 ReAct 循环第二轮发给模型的消息里,`role:"tool"` 的
    结果消息前面没有任何带 `tool_calls` 的 assistant 消息 —— OpenAI 协议明确要求
    tool 消息必须是对前一条 assistant.tool_calls 的响应,厂商(OpenAI/DeepSeek/通义/
    vLLM 兼容层)一律直接返回 400。也就是说:只要工具真的被调用过一次,第二轮就必挂,
    L1/L2 在真实模型上完全不可用,而假模型服务器不校验消息结构所以测不出来。
    """
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}

    if isinstance(message, AIMessage):
        raw_calls = (message.additional_kwargs or {}).get("tool_calls_raw")
        if raw_calls:
            return {
                "role": "assistant",
                # 带 tool_calls 时 content 传 None 而不是空串:部分厂商对空串更严格
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": tc.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc.get("name") or "", "arguments": tc.get("arguments") or "{}"},
                    }
                    for i, tc in enumerate(raw_calls)
                ],
            }
        return {"role": "assistant", "content": message.content or ""}

    if isinstance(message, ToolMessage):
        msg = {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id}
        if getattr(message, "name", None):
            msg["name"] = message.name  # 少数厂商会校验 name 与 tool_calls 对应
        return msg

    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}

    return {"role": "user", "content": str(getattr(message, "content", message))}
