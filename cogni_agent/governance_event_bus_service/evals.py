"""
M7-06/07:评测回归集与发布门禁——真实实现:用例真的会通过执行引擎跑一遍
(create_task + execute_task,和「对话」页走的是同一条链路),不是接口占位。

关于"基线版本 vs 候选版本对比"的简化说明:Skill/Agent 当前没有完整的历史快照
机制(只有 Tool 有 ToolVersion、Skill 的 Prompt 有版本,Skill/Agent 整体定义
本身不是版本化实体),所以这里的"评测运行"是对**当前线上定义**跑一遍全部用例,
而不是真的拿两个历史快照做 A/B。baseline_version/candidate_version 字段保留
用于展示,但评测执行的目标是同一个 target_id 的当前状态。这个限制在 README 里
如实注明,不假装做了版本对比。

判定方式是关键词命中率(轻量启发式),不是 LLM-as-judge——避免又引入一层
新的不确定性(评测本身还需要被信任),命中率这种确定性规则更适合做"门禁"用途。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("governance_event_bus_service.evals")


class EvalCase(Base):
    """M7-06:测试用例——绑定到具体 Skill 或 Agent,包含测试输入、期望行为(关键词命中规则)。"""

    __tablename__ = table_name("gov_eval_cases")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(16))  # skill / agent
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    test_input: Mapped[str] = mapped_column(Text)   # 评测输入可能是一整段长文本
    expected_keywords: Mapped[list] = mapped_column(JSON, default=list)  # 命中任一即视为通过的关键词列表
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalRun(Base):
    """M7-06/07:一次评测运行里,某一条用例的执行结果——多条 EvalRun 共享同一个 batch_id
    构成一份「运行报告」。"""

    __tablename__ = table_name("gov_eval_runs")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    candidate_version: Mapped[str] = mapped_column(String(16), default="current")
    actual_output: Mapped[str] = mapped_column(Text, default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)   # 堆栈可能很长
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def _run_single_case(case: EvalCase, *, scenic_id: str | None, user_id: str) -> dict:
    """真实跑一次:建任务 -> 等执行引擎跑完(与对话页同一条 execute_task 链路)-> 按
    expected_keywords 判定 pass/fail。"""
    import time

    from cogni_agent.control_plane_service.queue import create_task, get_task
    from cogni_agent.runtime_service.engine.executor import execute_task

    started = time.perf_counter()
    task = await create_task(
        scenic_id=scenic_id, source="internal",
        payload={"message": case.test_input, "user_id": user_id, "role": "scenic_operator"},
        executor_type=case.target_type, executor_id=case.target_id,
        user_id=user_id,
    )
    try:
        await execute_task(task["id"])
    except Exception as exc:  # noqa: BLE001 - 评测用例本身执行失败也要记为一条失败结果,不能让整批评测中断
        return {"actual_output": "", "passed": False, "prompt_tokens": 0, "completion_tokens": 0,
                "latency_ms": int((time.perf_counter() - started) * 1000), "error": str(exc)}

    row = await get_task(task["id"], scenic_id=scenic_id)
    result = row.get("result") or {}
    answer = result.get("answer", "")
    usage = result.get("usage", {})
    passed = row["status"] == "COMPLETED" and (
        not case.expected_keywords or any(kw in answer for kw in case.expected_keywords)
    )
    return {
        "actual_output": answer, "passed": passed,
        "prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": None if row["status"] == "COMPLETED" else f"任务未成功完成:{row['status']}",
    }


async def run_eval_batch(target_type: str, target_id: str, *, scenic_id: str | None, user_id: str, case_ids: list[str] | None = None) -> dict:
    """M7-06:运行报告——对指定 Agent/Skill 的(全部或指定的)用例集跑一遍,返回聚合指标。"""
    batch_id = f"evb_{uuid.uuid4().hex}"
    async with get_sessionmaker()() as db:
        stmt = select(EvalCase).where(EvalCase.target_type == target_type, EvalCase.target_id == target_id)
        if case_ids:
            stmt = stmt.where(EvalCase.id.in_(case_ids))
        cases = list((await db.execute(stmt)).scalars())

    if not cases:
        return {"batch_id": batch_id, "total": 0, "passed": 0, "pass_rate": None, "cases": []}

    results = []
    for case in cases:
        outcome = await _run_single_case(case, scenic_id=scenic_id, user_id=user_id)
        async with get_sessionmaker()() as db:
            db.add(EvalRun(
                batch_id=batch_id, case_id=case.id, target_type=target_type, target_id=target_id,
                actual_output=outcome["actual_output"], passed=outcome["passed"],
                prompt_tokens=outcome["prompt_tokens"], completion_tokens=outcome["completion_tokens"],
                latency_ms=outcome["latency_ms"], error=outcome["error"],
            ))
            await db.commit()
        results.append({"case_id": case.id, "case_name": case.name, **outcome})

    passed_count = sum(1 for r in results if r["passed"])
    return {
        "batch_id": batch_id, "total": len(results), "passed": passed_count,
        "pass_rate": passed_count / len(results), "cases": results,
    }


async def run_eval_gate(target_id: str, baseline_version: str, candidate_version: str, *, target_type: str = "agent", scenic_id: str | None = None, user_id: str = "system") -> dict:
    """M7-07:发布门禁核心逻辑——真实跑一遍该 Agent/Skill 绑定的评测用例集,按
    governance.eval_gate_min_pass_rate 阈值判定放行/阻断。没有登记任何用例时默认放行
    (没有用例可跑,不应该反过来阻断发布)。"""
    min_pass_rate = get_settings().get("governance", "eval_gate_min_pass_rate", default=0.8)
    report = await run_eval_batch(target_type, target_id, scenic_id=scenic_id, user_id=user_id)
    if report["total"] == 0:
        log.info(f"{target_id} 未登记评测用例,发布门禁默认放行")
        return {"passed": True, "blocking_reasons": [], "report": report}
    if report["pass_rate"] < min_pass_rate:
        return {
            "passed": False,
            "blocking_reasons": [f"评测通过率 {report['pass_rate']:.1%} 低于门禁阈值 {min_pass_rate:.1%}({report['passed']}/{report['total']} 通过)"],
            "report": report,
        }
    return {"passed": True, "blocking_reasons": [], "report": report}
