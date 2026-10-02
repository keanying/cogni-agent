"""
M4:执行引擎运行时状态(LangGraph StateGraph 的 State Schema)。

这是 L0/L1/L2 三种运行模式共用的唯一状态定义——三种模式的区别不在状态结构上,
而在"图怎么连、bound_tools 怎么算"上(见 graphs.py 顶部说明)。状态里的字段
全部可被 Checkpointer(见 checkpoint.py)持久化,HITL 中断/恢复靠的就是这份
状态的完整快照,不需要额外设计单独的"断点续跑"表。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ToolSpecDict(TypedDict, total=False):
    """resolver.py 解析出的工具规格,序列化为 dict 存进 state 以便跨节点/跨恢复复用。"""

    id: str
    name: str
    description: str
    type: str  # http / db / python / knowledge
    risk_level: str  # low / medium / high
    json_schema: dict  # 提供给模型 function-calling 的 parameters schema
    allow_override: bool
    forced_params: dict  # 参数覆盖机制:运行时覆盖值,执行前强制合并进最终参数(见 resolver.py)


class ResolvedSkillDict(TypedDict, total=False):
    """一个 Skill 在本次运行中的完整可执行上下文(prompt 已渲染、工具已鉴权过滤)。"""

    id: str
    name: str
    system_prompt: str
    tools: list[ToolSpecDict]
    knowledge_sources: list[str]
    max_steps: int
    max_tool_calls: int


class GuardrailCounters(TypedDict, total=False):
    """M4-06 护栏计数器,同时承载 M4-09 幻觉/格式错误的自愈重试计数。"""

    steps: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    format_retries: int
    hallucination_retries: int
    drift_checks: int


def _last_write_wins(_left: Any, right: Any) -> Any:
    """默认字典类字段用"整体替换"语义——写入节点每次都返回完整新值,不做局部合并。"""
    return right


class RunState(TypedDict):
    """LangGraph 图的顶层状态。"""

    # ── 会话消息(langchain_core 消息对象,借助 add_messages reducer 自动去重/追加) ──
    messages: Annotated[list[AnyMessage], add_messages]

    # ── 任务身份与归属(供工具鉴权/审计/成本记账使用) ──
    task_id: str
    scenic_id: str | None
    user_id: str
    role: str

    # ── 原始目标,供漂移检测节点比对 ──
    goal: str
    # 固定问题的「问题描述」:运营配置的口径与约束,拼进系统提示。
    # 与 goal(用户这轮说的话)分开 —— 它是附加约束,不是用户消息。
    question_instruction: Annotated[str, _last_write_wins]
    # 上下文记忆拼成的「已知上下文」段(来自 cp_context_memory)。
    # 与 Checkpointer 的消息历史不同:它很短、不会被上下文裁剪掉。
    memory_block: Annotated[str, _last_write_wins]

    # ── 运行模式判定结果 ──
    mode: Literal["L0", "L1", "L2"]

    # ── L1/L2 统一的技能推进指针 ──
    # L0: skill_chain 为空
    # L1(固定编排 chain): skill_chain 是多个 skill_id 的严格顺序
    # L2(动态编排 pool / 单 Skill): skill_chain 只有一个"虚拟技能"条目(见 resolver.py)
    skill_chain: list[str]
    skill_idx: int
    skill_step_counts: Annotated[dict[str, int], _last_write_wins]
    # DAG 编排:每个 Skill 的产物,层间靠它传递;L0/L1线性/L2 下为空
    skill_outputs: Annotated[dict[str, str], _last_write_wins]
    # 工具调用轨迹:重复检测 + 连续失败统计。随 checkpoint 持久化,
    # 这样 HITL 暂停恢复后仍然记得"哪个工具已经坏了、哪个调用重复过"
    tool_tracker: Annotated[dict, _last_write_wins]

    # ── Agent 人格设定(仅 executor_type=agent 时非空) ──
    persona: Annotated[dict | None, _last_write_wins]

    # ── 参数覆盖机制解析结果(见 resolver.py resolve_execution_spec) ──
    overrides: Annotated[dict, _last_write_wins]

    # ── 护栏计数器 ──
    guardrail: Annotated[GuardrailCounters, _last_write_wins]

    # ── 执行过程时间线(对应「对话」页的执行过程展示:意图识别→模式判定→知识检索→工具调用→生成) ──
    timeline: Annotated[list[dict], operator.add]

    # ── 消耗展示(Token 用量,累计) ──
    usage: Annotated[dict, _last_write_wins]

    # ── 待批准动作快照:节点在调用 interrupt() 前写入,供 executor.py 落 Approval 记录 ──
    pending_interrupt: Annotated[dict | None, _last_write_wins]

    # ── 最终答案(L0/L1/L2 收敛后的自然语言结果) ──
    final_answer: str | None

    # ── 意图层(04 智能问数方案 §4.0):
    # ① Triager 意图路由结果 ② 会话级参数槽(Slot Filling,conversation 维度随 checkpoint 持久化)
    # ③ 多轮改写后的自包含问题(rewritten_goal) ④ Triager 给出的分步执行计划 ──
    intent: Annotated[str, _last_write_wins]
    slots: Annotated[dict, _last_write_wins]
    plan: Annotated[list[str], _last_write_wins]
    # L4 执行计划(planner 生成的结构化步骤)。**与上面的 plan 是两回事**:
    # plan 是 Triager 给出的分步提示(字符串列表,进提示词引导模型);
    # exec_plan 是带状态/耗时/结果的结构化步骤表,驱动前端「执行计划」展示。
    # 同名会让 Triager 的分步提示被静默覆盖 —— 那种 bug 表现为「模型突然不按步骤走了」,
    # 极难定位。
    exec_plan: Annotated[dict, _last_write_wins]
    # 32 号:执行层 LLM 历史策略——按轮注入对话文本(0=纯净上下文,固定问题
    # 强制 0;非固定 Agent intent_config.exec_history_rounds,默认 2)。agent_node
    # 构造 LLM 上下文时消费,checkpoint 全量 messages 不再作为历史来源。
    exec_history_rounds: Annotated[int, _last_write_wins]
