"""
M4-01:L0/L1/L2 运行模式判定链。

判定原则(评审口径):确定性优先于自主性,用能完成任务的最低档。这条原则落地
成两条具体规则,而不是每次都现场用 LLM"猜"一个模式(那本身就会引入新的不确定性):

1. 单 Tool/MCP 直调 -> 恒定 L0(文档 3.2 節"直接调试单个工具",不经过 LLM 推理层)。
2. 单 Skill -> 由管理员在 Skill「基础信息」Tab 登记的 `suggested_mode` 字段决定
   (这本来就是这个字段存在的目的:模式是设计时声明的,不是运行时猜的)。
   Skill 没有登记时,按有没有绑定工具退化:无工具 -> L0,有工具 -> L2。
3. Agent 绑定多个 Skill -> 看 Agent 自身的 composition_mode(23 号:原
   tc_skill_compositions 编排组合表已删除,协作模式收进 agent 自身):
   - chain(固定顺序链)-> L1
   - pool(动态可选池)-> L2
   - 未登记 -> 默认按 Agent.bound_skill_ids 给定的顺序视为固定链,L1
     (没有人显式打开"动态可选池",就不引入不确定性;这一步在 resolver.py 里已经
     算好放进 ExecutionSpec.composition_hint,这里只是把它翻译成 RunMode)。

调用方仍可在「对话」页高级选项里显式传 `overrides.mode` 强制覆盖(比如联调时想
强制某条路径),该值优先级最高。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cogni_agent.config import get_settings
from cogni_agent.runtime_service.engine.resolver import ExecutionSpec


class RunMode(str, Enum):
    L0_DIRECT = "L0"       # 直接调用,单次 LLM 调用(或单 Tool/MCP 直调,不经 LLM)
    L1_WORKFLOW = "L1"     # 固定工作流(DAG),路径设计时定死
    L2_REACT = "L2"        # 单 Agent ReAct,路径运行时动态决定
    # 先规划再执行:动手前一次性把「分几步、每步用什么」想清楚,再按计划走。
    #
    # **与 L2(ReAct)的区别不是谁更强,而是适用场景不同**:
    #   - ReAct 走一步看一步,适合「问了才知道下一步」的探索型任务
    #     (查到数据不对要换个查法、结果出乎意料要改方向)
    #   - Plan-and-Execute 适合多步骤、路径相对确定的任务:
    #     过程可见(五步一开始就全列出来,用户知道还要等几步)、
    #     可中断(看到计划里有「发送通知」才有机会喊停)、
    #     工具选择更稳(把「用哪些工具」从每一轮的决策收敛成一次)
    #
    # 计划不是枷锁:执行中模型仍可偏离(发现数据不对换个查法),
    # 偏离会在时间线留痕但不阻断。
    L4_PLAN_EXECUTE = "L4"
    L3_MULTI_AGENT = "L3"  # 多 Agent 协同(预留,当前版本未启用,遇到时降级按 L1 处理)


@dataclass
class ModeDecision:
    mode: RunMode
    reasons: list[str]
    intent: str  # 供「执行过程展示·意图识别」时间线节点使用的粗粒度意图标签


def _intent_for(mode: RunMode, spec: ExecutionSpec) -> str:
    if spec.direct_tool is not None:
        return "tool_debug"
    if not spec.skill_chain:
        return "open_chat"
    if mode == RunMode.L1_WORKFLOW:
        return "multi_step_workflow"
    if mode == RunMode.L4_PLAN_EXECUTE:
        return "planned_multi_step_task"
    if mode == RunMode.L2_REACT and len(spec.skill_chain) > 1:
        return "multi_skill_dynamic_task"
    return "single_skill_task"


async def decide_mode(spec: ExecutionSpec, override_mode: str | None = None) -> ModeDecision:
    if override_mode:
        try:
            mode = RunMode(override_mode)
        except ValueError:
            mode = RunMode.L2_REACT
        mode = RunMode.L1_WORKFLOW if mode == RunMode.L3_MULTI_AGENT else mode
        reasons = [f"调用方通过高级选项显式指定运行模式 override_mode={override_mode}"]
        return ModeDecision(mode, reasons, _intent_for(mode, spec))

    if spec.direct_tool is not None:
        mode = RunMode.L0_DIRECT
        reasons = ["executor_type 为 tool/mcp,单工具直调场景,恒定走 L0 等价路径(不经 LLM 推理层)"]
        return ModeDecision(mode, reasons, _intent_for(mode, spec))

    if not spec.skill_chain:
        # Agent 未绑定任何 Skill -> 纯人格对话,单次 LLM 调用
        mode = RunMode.L0_DIRECT
        reasons = ["未绑定任何 Skill,退化为纯人格对话,单次 LLM 调用 -> 命中 L0"]
        return ModeDecision(mode, reasons, _intent_for(mode, spec))

    if len(spec.skill_chain) == 1:
        skill = spec.skill_chain[0]
        if spec.suggested_mode in ("L0", "L1", "L2"):
            mode = RunMode(spec.suggested_mode)
            reasons = [f"Skill「{skill.name}」在管理后台登记的运行模式建议值为 {spec.suggested_mode},直接采用"]
            return ModeDecision(mode, reasons, _intent_for(mode, spec))
        if not skill.tools:
            mode = RunMode.L0_DIRECT
            reasons = [f"Skill「{skill.name}」未绑定任何工具,一次 LLM 调用即可解决 -> 命中 L0"]
        else:
            threshold = int(get_settings().get("runtime", "planning", "min_tools", default=3))
            if len(skill.tools) >= threshold:
                mode = RunMode.L4_PLAN_EXECUTE
                reasons = [f"Skill「{skill.name}」绑定了 {len(skill.tools)} 个工具(≥{threshold}),"
                           f"先规划再执行,过程可见且工具选择更稳 -> 命中 L4"]
            else:
                mode = RunMode.L2_REACT
                reasons = [f"Skill「{skill.name}」绑定了 {len(skill.tools)} 个工具,规模不大,"
                           f"按路径运行时决定 -> 命中 L2"]
        return ModeDecision(mode, reasons, _intent_for(mode, spec))

    # 多 Skill(Agent 绑定 >=2 个 Skill)
    #
    # **不再按 composition_mode(chain/dag/pool)决定编排形态。**
    # 那是「设计时把路径定死」的思路:建 Agent 时就要想清楚这几个 Skill 该串行还是并行,
    # 而实际上同一个 Agent 面对不同问题需要的路径本来就不同 ——
    # 「今天客流多少」要一步,「分析 Q2 趋势并给建议」要五步,把它们绑定到同一种编排上,
    # 要么简单问题绕远路,要么复杂问题步子不够。
    #
    # 现在按**任务本身**在两种执行策略之间选:
    #   L2 ReAct           走一步看一步,适合探索型(问了才知道下一步)
    #   L4 Plan-and-Execute 先规划再执行,适合多步骤且路径相对确定的任务
    #
    # 判定依据是「可用工具的规模」——工具越多,ReAct 每轮重新挑工具越容易跑偏,
    # 先规划把「用哪些」收敛成一次决策的收益就越大。
    total_tools = sum(len(sk.tools) for sk in spec.skill_chain)
    threshold = int(get_settings().get("runtime", "planning", "min_tools", default=3))

    if total_tools >= threshold:
        mode = RunMode.L4_PLAN_EXECUTE
        reasons = [
            f"{len(spec.skill_chain)} 个 Skill 共 {total_tools} 个可用工具(≥{threshold}),"
            f"先规划再执行:过程可见、可中断,且把「用哪些工具」从每轮决策收敛成一次 -> 命中 L4"
        ]
    else:
        mode = RunMode.L2_REACT
        reasons = [
            f"{len(spec.skill_chain)} 个 Skill 共 {total_tools} 个可用工具(<{threshold}),"
            f"规模不大、规划的收益抵不过额外一次模型调用的延迟,按 ReAct 动态推进 -> 命中 L2"
        ]
    return ModeDecision(mode, reasons, _intent_for(mode, spec))
