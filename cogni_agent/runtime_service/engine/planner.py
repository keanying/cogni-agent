"""
执行规划(L4 Plan-and-Execute):动手之前先想清楚分几步、每步用什么。

**与 L2 ReAct 的关系是互补,不是替代**:

| | ReAct(L2) | Plan-and-Execute(L4) |
|---|---|---|
| 决策时机 | 每轮重新想下一步 | 动手前一次性想完 |
| 适合 | 探索型(问了才知道下一步) | 多步骤、路径相对确定 |
| 过程可见性 | 只能看到「已经做了什么」 | 一开始就知道「总共几步、现在第几步」 |
| 工具选择 | 每轮都要从全部工具里挑 | 收敛成一次决策 |

**三个实际收益**(不只是显示好看):

1. **过程可见**。ReAct 下用户只看到「正在思考…」,不知道还要多久。有了计划,
   五步一开始就全列出来,做完一个勾一个 —— 这是「等待」和「知道在等什么」的区别。
2. **可中断**。看到计划里有「发送企业微信通知」才有机会说「先别发」。
   走一步看一步的话,等你看到它时消息已经发出去了。
3. **工具选择更稳**。工具多了之后,ReAct 每轮把候选工具丢给模型自己挑,
   步数一多容易跑偏(调了不相干的、或漏掉必要步骤)。

**计划不是枷锁**。执行中模型仍可偏离(发现数据不对要换个查法),计划的作用是给出
主线和可见性,不是把模型锁死;偏离会在时间线留痕但不阻断。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.engine.planner")

KIND_TOOL = "tool"
KIND_SCRIPT = "script"
KIND_KNOWLEDGE = "knowledge"
KIND_ANALYZE = "analyze"
KIND_COMPOSE = "compose"
_KINDS = (KIND_TOOL, KIND_SCRIPT, KIND_KNOWLEDGE, KIND_ANALYZE, KIND_COMPOSE)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class PlanStep:
    id: str
    title: str
    kind: str
    detail: str = ""
    tool_name: str | None = None
    skill_id: str | None = None
    status: str = STATUS_PENDING
    duration_ms: int | None = None
    result_summary: str = ""
    # 这一步发现的异常/要点。由工具自己声明(返回 _findings),引擎不去猜什么算异常
    findings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "kind": self.kind, "detail": self.detail,
            "tool_name": self.tool_name, "skill_id": self.skill_id, "status": self.status,
            "duration_ms": self.duration_ms, "result_summary": self.result_summary,
            "findings": list(self.findings or []),
        }


@dataclass
class ExecutionPlan:
    steps: list[PlanStep] = field(default_factory=list)
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict:
        return {"summary": self.summary, "steps": [s.as_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExecutionPlan":
        d = d or {}
        plan = cls(summary=d.get("summary", ""))
        fields = ("id", "title", "kind", "detail", "tool_name", "skill_id",
                  "status", "duration_ms", "result_summary", "findings")
        for raw in d.get("steps") or []:
            plan.steps.append(PlanStep(**{k: raw[k] for k in fields if k in raw}))
        return plan

    def step_by_tool(self, tool_name: str) -> PlanStep | None:
        """按工具名找到第一个还没执行完的对应步骤。

        找不到不是错误 —— 模型可以偏离计划,那种情况会另外记一条时间线。
        """
        for s in self.steps:
            if s.tool_name == tool_name and s.status in (STATUS_PENDING, STATUS_RUNNING):
                return s
        return None


def _cfg(key: str, default):
    return get_settings().get("runtime", "planning", key, default=default)


_SYSTEM = """你是任务规划员。根据用户请求和当前可用能力,拆出一个简洁的执行计划。

只输出 JSON,不要任何其它内容:
{"summary": "一句话说明整体思路", "steps": [
  {"title": "给人看的一句话", "kind": "tool|script|knowledge|analyze|compose",
   "tool_name": "要调用的工具名(kind 为 tool/script 时必填,必须来自下面的可用工具清单)",
   "detail": "这一步具体做什么"}
]}

要求:
- **3 到 6 步**。拆太细没人看,拆太粗等于没规划。
- tool_name **必须**来自下面的清单,不要编造。清单里没有的能力就不要写进计划。
- 涉及数据处理时,优先安排一步脚本做清洗/计算,而不是让模型自己心算几千条记录。
- 最后一步固定 kind=compose(整理成给用户的答复)。
- detail 要具体。「分析数据」是废话,「计算月环比与同比,检测 3σ 以外异常值」才有用。
- 一步就能答完的请求,返回空 steps 数组,summary 说明原因。"""


async def make_plan(*, goal: str, skills: list, model: str | None) -> ExecutionPlan:
    """生成执行计划。任何异常都退化成空计划 —— 规划失败不该阻断执行,
    大不了退回 ReAct。"""
    from cogni_agent.runtime_service.llm import client as llm_client

    tools: list[str] = []
    lines: list[str] = []
    for sk in skills:
        names = [t.name for t in sk.tools]
        tools.extend(names)
        lines.append(f"- 技能「{sk.name}」:{(sk.system_prompt or '')[:100]}"
                     + (f"\n  可用工具:{'、'.join(names)}" if names else "\n  (无工具)"))
    if not tools:
        return ExecutionPlan(summary="没有可用工具,直接作答")

    user = (f"用户请求:\n{goal}\n\n当前能力:\n" + "\n".join(lines)
            + f"\n\n可用工具清单(tool_name 只能从中选):{'、'.join(dict.fromkeys(tools))}")

    try:
        result = await llm_client.complete_collected(
            model, [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            max_tokens=800, temperature=0.2,
        )
    except Exception:  # noqa: BLE001
        log.exception("规划调用失败,退回无计划执行")
        return ExecutionPlan()

    raw = (result.content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"规划返回的不是合法 JSON,退回无计划执行:{raw[:200]}")
        return ExecutionPlan(prompt_tokens=result.prompt_tokens,
                             completion_tokens=result.completion_tokens)

    plan = ExecutionPlan(summary=str(data.get("summary") or ""),
                         prompt_tokens=result.prompt_tokens,
                         completion_tokens=result.completion_tokens)
    known = set(tools)
    for raw_step in (data.get("steps") or [])[: int(_cfg("max_steps", 6))]:
        kind = str(raw_step.get("kind") or KIND_ANALYZE)
        if kind not in _KINDS:
            kind = KIND_ANALYZE
        tool_name = raw_step.get("tool_name") or None
        # 模型偶尔会编一个不存在的工具名。**不能直接采信** —— 计划里写着不存在的工具,
        # 执行时必然对不上,而用户看到的是「计划里有这一步却一直不执行」。
        # 降级成分析步骤并留痕,比丢掉整个计划好。
        if tool_name and tool_name not in known:
            log.warning(f"规划里出现未知工具 {tool_name!r},已降级为分析步骤")
            kind, tool_name = KIND_ANALYZE, None
        plan.steps.append(PlanStep(
            id=f"ps_{uuid.uuid4().hex[:8]}",
            title=str(raw_step.get("title") or "执行步骤")[:60],
            kind=kind, detail=str(raw_step.get("detail") or "")[:200],
            tool_name=tool_name,
        ))

    # 没有收尾步骤就补一个:前端据此显示「正在整理答复」,而不是最后一步做完就空着
    if plan.steps and plan.steps[-1].kind != KIND_COMPOSE:
        plan.steps.append(PlanStep(id=f"ps_{uuid.uuid4().hex[:8]}",
                                   title="整理最终答复", kind=KIND_COMPOSE,
                                   detail="把前面各步的结果汇总成面向用户的回答"))
    return plan


def finalize(plan: ExecutionPlan) -> bool:
    """任务结束时收尾计划状态。返回是否有变更。

    analyze / compose 这类步骤不是工具调用,不会在 tools 节点被回写 ——
    不处理的话界面会一直停在「2/4 步」而任务其实早就做完了。
    还挂着工具却没跑的标记为**跳过**:如实反映比谎称完成好。
    """
    changed = False
    for st in plan.steps:
        if st.status != STATUS_PENDING:
            continue
        st.status = STATUS_SKIPPED if st.tool_name else STATUS_DONE
        changed = True
    return changed
