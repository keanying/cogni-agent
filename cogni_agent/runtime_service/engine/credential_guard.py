"""§10.3 凭证脱敏守卫:明文只在图内内存短暂停留,不落 checkpoint/日志/审批表/回放。

模型输出「保存凭证」工具调用时,agent_node 在把 AIMessage 写入 state(→checkpoint)
**之前**剥除 username/password,明文暂存本模块的内存缓存;tools_node 执行该工具
(HITL 审批通过后)时再取出使用。缓存 key 用 task_id + call_id 唯一化,多任务并发
互不干扰;任务终结时由 executor 清理。

设计原则(§10.3, v1.1.1 调整):脱敏锚点 = 「保存凭证**成功 / 用户明确忽略**」,而不是
「模型发出保存凭证调用」。

- v1.1.1 之前的锚点是"模型一发出调用就擦除本轮 HumanMessage / 脱敏 payload.message"。
  实测发现:保存调用参数校验失败(format_error)时,上下文里的用户消息已被擦成占位符,
  模型看不到账号密码、无法自行纠错,只能反复索要凭证,形成死循环(框架能力测试 01 文档)。
- 因此把擦除时机后移:**成功保存 / 用户忽略 → mark_handled**,由 tools_node 擦除本轮
  HumanMessage、executor 收尾脱敏 payload.message;**失败(format_error/error)不置位**,
  保留原文,模型下一轮能看到账号密码自行纠错。
- 明文在"发出调用"时仍立即剥离暂存(防泄漏),只是**擦除时机**延后到成功之后。

已知边界:进程重启会丢失内存缓存——WAITING_HUMAN 期间服务重启后,审批通过恢复执行时
取不到明文,保存凭证返回「已过期」错误并引导用户重新提供(安全降级,不泄露);
保存失败的任务,payload.message 与 checkpoint 会保留用户原文(失败态,不会进入
已成功的历史),这是"让模型可自纠"与"零明文"之间的取舍,后续可在收尾时按需收紧。

当前实现假设 asyncio 单线程事件循环,模块级 dict 的读写无 await 间隙,天然线程安全。
"""

from __future__ import annotations

# §10.3 脱敏占位:回放 / 上下文 / Task.payload.message 统一使用的占位文本
PLACEHOLDER_MESSAGE = "[已提供账号凭证,已安全保存,不回显]"

# 暂存模型输出中的凭证明文:key = f"{task_id}:{call_id}"
_PENDING: dict[str, dict] = {}
# v1.1.1:保存凭证「已处理」(成功保存 / 用户忽略)标记,按消费方拆成两个独立集合,
# 分别由 graphs(tools_node 擦 HumanMessage)与 executor(收尾脱敏 payload.message)
# 各消费一次,互不抢占。失败(format_error/error)不得置位——保留原文供模型自纠。
_MSG_ERASE: set[str] = set()
_PAYLOAD_MASK: set[str] = set()


def stash_key(task_id: str, call_id: str) -> str:
    return f"{task_id}:{call_id}"


def stash_credential_args(key: str, args: dict) -> None:
    """agent_node 剥离后暂存明文(不入 state/checkpoint)。"""
    _PENDING[key] = dict(args)


def pop_credential_args(key: str) -> dict | None:
    """tools_node 审批通过后取出明文;取不到(缓存丢失)返回 None,安全降级。"""
    return _PENDING.pop(key, None)


def peek_credential_args(key: str) -> dict | None:
    """v1.1.2(B 方案):非破坏性取回明文,供执行层校验/重复判定恢复完整参数。

    与 pop_credential_args 的区别:peek 不删除缓存。真正消费明文仍发生在
    _execute_credential_save(保存执行)时的一次 pop,保证「一次调用只消费一次」;
    进程重启缓存丢失时返回 None,由调用方安全降级(校验自然失败,引导模型重新提供)。
    """
    return _PENDING.get(key)


def mark_handled(task_id: str) -> None:
    """保存凭证**成功**或**用户明确忽略保存**后调用。

    同时置位两个标记:
    - _MSG_ERASE     → tools_node 消费,擦除本轮 HumanMessage 原文;
    - _PAYLOAD_MASK  → executor 收尾消费,把 Task.payload.message 同步为占位符。

    失败(format_error/error)不得调用——模型需要保留原文自行纠错。
    """
    _MSG_ERASE.add(task_id)
    _PAYLOAD_MASK.add(task_id)


def pop_msg_erase(task_id: str) -> bool:
    """tools_node 消费:保存成功/忽略后擦除本轮 HumanMessage 原文。

    注意:set.discard() 返回 None,必须显式判断,否则 `if pop_msg_erase(...)` 永远为假。
    """
    if task_id in _MSG_ERASE:
        _MSG_ERASE.discard(task_id)
        return True
    return False


def pop_payload_mask(task_id: str) -> bool:
    """executor 收尾消费:同步把 Task.payload.message 脱敏为占位符。

    注意:set.discard() 返回 None,必须显式判断,否则 `if pop_payload_mask(...)` 永远为假。
    """
    if task_id in _PAYLOAD_MASK:
        _PAYLOAD_MASK.discard(task_id)
        return True
    return False


def cleanup_task(task_id: str) -> None:
    """任务终结时清理该任务的全部残留(明文缓存 + 脱敏标记)。"""
    _MSG_ERASE.discard(task_id)
    _PAYLOAD_MASK.discard(task_id)
    prefix = f"{task_id}:"
    for key in [k for k in _PENDING if k.startswith(prefix)]:
        _PENDING.pop(key, None)
