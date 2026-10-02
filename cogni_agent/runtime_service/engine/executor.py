"""
M4-05:统一执行入口——串联解析/判定/护栏/HITL/图执行/标准化输出,worker 认领任务
后唯一调用的函数就是这里的 execute_task()。这是文档 4.1「一次对话请求的执行路径」
的完整落地:

    解析能力定义 + 应用参数覆盖(resolver.py)
      -> 判定 L0/L1/L2(modes.py)
      -> 按模式构建/复用图(graphs.py,真正执行时用同一个 Checkpointer 支持中断恢复)
      -> 执行过程中每一步产生:时间线记录 + trace 日志 + 用量记录(本文件 + streaming.py)
      -> 按标准化事件模型通过 streaming.py 广播(SSE 端点订阅同一个 task_id 即可)
      -> 任务落库(COMPLETED/FAILED/CANCELLED/WAITING_HUMAN),归属当前用户所在景区

HITL 恢复(WAITING_HUMAN -> 审批通过/驳回 -> 重新 QUEUED)与首次执行走的是同一个
函数——resume 只是 payload 里多了 `_resume=True`,读取的 Checkpoint 由
thread_id=task_id 唯一确定,不需要额外的"恢复专用"代码路径。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import ApprovalRejected, ClarificationNeeded, GuardrailTripped
from cogni_agent.common.logging import get_logger, set_trace_context
from cogni_agent.common.schemas import KIND_TO_CODE, TaskErrorKind, task_error
from cogni_agent.config import get_settings
from cogni_agent.control_plane_service.models import SOURCE_PREWARM, Task
from cogni_agent.governance_event_bus_service.events.outbox import emit_event
from cogni_agent.runtime_service import streaming
from cogni_agent.runtime_service.engine import credential_guard
from cogni_agent.runtime_service.engine import hitl
from cogni_agent.runtime_service.engine import intent
from cogni_agent.runtime_service.engine.prompt_log import record_prompt
from cogni_agent.runtime_service.engine.checkpoint import get_checkpointer
from cogni_agent.runtime_service.engine.graphs import (
    OutputBlocked, build_agent_graph, build_dag_graph, build_direct_dispatch_graph,
    build_direct_tool_graph,
)
from cogni_agent.control_plane_service import memory as ctx_memory
from cogni_agent.runtime_service.engine import estimate as estimator
from cogni_agent.runtime_service.engine import guardrails as gr
from cogni_agent.runtime_service.engine import planner
from cogni_agent.tool_center_service.tool import stats as tool_stats
from cogni_agent.runtime_service.engine.modes import RunMode, decide_mode
from cogni_agent.runtime_service.engine.resolver import (
    ResolveError, ResolvedSkill, resolve_execution_spec,
)
from cogni_agent.runtime_service.engine.structured_output import (
    StructuredOutput,
    parse as parse_structured_output,
)
from cogni_agent.runtime_service.llm.budget import check_org_daily_budget

log = get_logger("runtime_service.executor")

_QUERYING_STAGE_TOOL_STEPS = {"tool_call", "tool_start", "tool_executing", "knowledge_search", "knowledge_segment"}

# 模型在无可用工具时会把工具调用文本协议当正文输出(resolver.py 已记录 dev 实测事故,
# 历史上出现 answer 以 <|FunctionCallBegin|> 开头泄漏给用户);正常完成与用户取消
# 都可能在最终/部分回答里残留完整或半截原语。落库与 done/cancelled 广播前统一剥离。
# End 标记兼容带不带斜杠两种写法(平台文本协议的历史差异)。
_FUNC_CALL_BEGIN = "<|FunctionCallBegin|>"
_FUNC_CALL_END_RE = re.compile(r"</?\|FunctionCallEnd\|>")
_FUNC_CALL_ANY_RE = re.compile(r"<\|/?FunctionCall(?:Begin|End)\|>")


def _sanitize_answer(text: str) -> str:
    """剥掉模型误输出的 <|FunctionCallBegin|>...</|FunctionCallEnd|> 工具调用文本原语。

    - 完整原语段(有 End 闭合)整段删除,Begin 前的正常内容保留;
    - 半截原语(取消/中断遗留,无 End 闭合)自 Begin 起至文末整体丢弃;
    - 防御性清理孤立 Begin/End 标记,并收敛剥离造成的多余空行。
    """
    if not text:
        return text
    out = ""
    rest = text
    while True:
        idx = rest.find(_FUNC_CALL_BEGIN)
        if idx < 0:
            out += rest
            break
        out += rest[:idx]
        tail = rest[idx + len(_FUNC_CALL_BEGIN):]
        m = _FUNC_CALL_END_RE.search(tail)
        if m:
            rest = tail[m.end():]  # 完整段:整段丢弃
        else:
            break  # 半截原语(通常在文末):其后内容整体丢弃
    out = _FUNC_CALL_ANY_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


class UserCancelled(Exception):
    """用户通过 cancel 接口主动取消任务——流式循环感知到取消标记后抛出,
    由专门的 except 分支收尾为 CANCELLED(带已生成的部分回答)。"""

# Triager 槽位里"景区 ID"的默认字段别名,与 tools_adapter._SCENIC_PARAM_FIELDS 一致,
# 可用 runtime.tools.scenic_param_fields 覆盖。数字大脑(data.12301dev.com)等接口
# 用驼峰 scenicId,平台统一 snake_case scenic_id,别名全部注入让 Triager 不再误判。
_DEFAULT_SCENIC_SLOT_FIELDS = ("scenic_id", "scene_id", "spot_id", "scenicId", "景区id", "景区ID")


def _fill_time_slots(spec, slots: dict, goal_text: str) -> dict:
    """把句子里的时间说法算成工具要的参数,填进槽位。

    只填**工具自己声明过**的时间参数,且只在槽里还缺的时候填 ——
    用户显式选过的日期(APP 端日期选择器)优先级更高,已经在槽里了,不会被覆盖。
    算不出来就不填:留给模型问,或者留给澄清,总好过填一个猜的日期。
    """
    from cogni_agent.runtime_service.engine import timeslots as _ts

    try:
        schemas = [t.json_schema or {} for sk in (spec.skill_chain or []) for t in (sk.tools or [])]
        wanted = _ts.missing_time_slots(schemas, slots)
        filled = _ts.resolve(goal_text, wanted)
        if filled:
            log.info("时间参数按问题原文算出:%s", filled)
            return {**slots, **filled}
    except Exception:  # noqa: BLE001 - 算不出来不该影响主流程
        log.exception("时间参数解析失败,跳过")
    return slots


def _force_hit_skills(skill_chain: list[ResolvedSkill], goal_text: str) -> list[str]:
    """规则兜底(确定性,非 LLM):Triager 漏挂时,技能声明里的 route_hit_keywords
    命中用户原话即强制挂载该技能,高频技能(如客流分析)绝不因一次分诊误判被裁剪。

    职责边界:本函数只做通用匹配,关键词清单是技能侧配置
    (DB tc_skills.constraints.route_hit_keywords,管理后台可配),不含业务硬编码;
    Triager 判定逻辑(意图分诊)保持框架级不动。调用方仅在 Triager 已裁剪
    (target_skills 非空)时使用,避免把"全量执行"误改造成"裁剪执行"。
    """
    forced: list[str] = []
    for sk in skill_chain:
        if sk.route_hit_keywords and any(kw in goal_text for kw in sk.route_hit_keywords):
            forced.append(sk.name)
    return forced


def _scenic_slots(scenic_id: str | None) -> dict:
    """会话绑定景区的全部字段别名槽位,供 Triager 判定"景区参数已填"。

    平台统一用 snake_case 的 scenic_id,但历史工具(数字大脑域)参数是驼峰 scenicId,
    若已收集槽位里只有 scenic_id,LLM 可能把 scenicId 误判为 missing 反向用户澄清。
    这里把配置声明的所有别名(值相同)一并注入,无论 Triager 面对哪种字段名都判定已填。
    与 tools_adapter 参数级越权校验共用同一份配置,保证校验/槽位口径一致。
    """
    if not scenic_id or not scenic_id.strip():
        return {}
    fields = get_settings().get(
        "runtime", "tools", "scenic_param_fields", default=_DEFAULT_SCENIC_SLOT_FIELDS,
    )
    return {f: scenic_id for f in fields}


def _triage_tools(sk) -> list[str]:
    """Triager 能力清单的工具名:去重后全量给,不限量。

    v1.1.4 起放开限量:工具名很短(中文名自带语义,如 `获取景区舆情数据`),全量展开
    的 token 代价可忽略,却换来路由层对全部能力边界的全局权衡 —— 限量 8 个可能漏掉
    第 9 个真正相关的工具直接导致路由错,省 token 省错了地方。描述与参数细节仍留给
    L2 主模型看完整 schema,两端各司其职。
    """
    seen: list[str] = []
    for t in sk.tools:
        if t.name not in seen:
            seen.append(t.name)
    return seen


def _triage_tool_notes(sk) -> list[str]:
    """Triager 能力清单的工具依赖声明(工具名 + 描述全量)。

    v1.1.2 曾为省 prefill 把工具描述整体砍掉(只留工具名),导致路由层读不到
    「XX 参数必须通过某依赖接口获取,禁止编造」这类依赖声明 —— 模型判了缺失却
    不知道有补齐技能可挂载(conv_d23a24de 事故)。v1.1.3 起受控恢复,曾采用
    「每技能最多 4 条、每条截断 120 字符」的限量方案;实测数字大脑 5 技能全量
    描述约 7300 字符(≈3.6k token),相比限量只多约 2.7k token,Triager 属轻量
    路由调用,全量暴露收益更大:Triager 能看到每条工具完整 description 的依赖
    声明,不再受「前 120 字符是否恰好含关键信息」与「技能绑 4+ 个工具时第 5 个
    描述缺失」两个截断盲区影响。故 v1.1.5 起改为全量暴露。
    """
    notes: list[str] = []
    for t in sk.tools:
        d = (t.description or "").strip()
        if not d:
            continue
        notes.append(f"{t.name}:{d}")
    return notes


def _with_step_durations(timeline: list[dict], *, start_ts: float | None = None) -> list[dict]:
    """给 timeline 事件补每步耗时 duration_ms(相邻事件间隔)。

    引擎在事件产生时已打真实 ts(time.monotonic,见 graphs._tl_ts / intent_check),
    这里统一按 ts 相邻间隔计算:实时 SSE、SSE 回放、落库 timeline 三处口径一致,
    不再出现"实时流错位一帧、落库 duration 全 0"两类失真。无 ts 的旧事件回退为
    收尾遍历时刻近似(与旧行为一致)。只新增字段,不修改原事件内容。
    首事件是时间线起点,自身耗时计 0(不相对全局最早 ts 产生虚高值);
    显式传入 start_ts 时,首事件相对该时刻计算。
    """
    prev = start_ts
    out: list[dict] = []
    for item in timeline:
        if isinstance(item, dict):
            ts = item.get("ts")
            if ts is None:
                # 兼容无 ts 的历史事件:以收尾遍历时刻近似
                ts = time.monotonic()
                item = {**item, "ts": ts}
            if "duration" not in item:
                if prev is None:
                    duration = 0.0  # 时间线起点:自身耗时计 0
                else:
                    duration = round(max(0.0, ts - prev) * 1000, 1)
                item = {**item, "duration": duration}
            prev = ts
        out.append(item)
    return out

# ── 执行活性登记 ──────────────────────────────────────────────────────────
# 租约心跳(worker/loops._heartbeat_until_done)按「最近一次有进展的时间」决定是否续租:
# 只有 execute_task 在真实推进(publish 事件 / 关键步骤完成)时才续租。任务一旦卡死
# (如收尾路径的 DB/Redis 调用挂起),进度不再更新 → 心跳停止续租 → 租约在 lease_seconds
# 内过期,由 lease_patrol_loop 回收为 FAILED;否则「RUNNING 但实际已死」的任务会永远
# 锁死整个会话——前端表现为光标一直闪、消息发不出去。
_TASK_PROGRESS: dict[str, float] = {}


def mark_progress(task_id: str) -> None:
    _TASK_PROGRESS[task_id] = time.monotonic()


def get_last_progress(task_id: str) -> float | None:
    return _TASK_PROGRESS.get(task_id)


def clear_progress(task_id: str) -> None:
    _TASK_PROGRESS.pop(task_id, None)


async def _publish(task_id: str, event: str, data: dict) -> None:
    """发布 SSE 事件并登记执行活性(卡死检测的时间戳来源)。"""
    mark_progress(task_id)
    await streaming.publish(task_id, event, data)


class _FinalizeStepTimeout(Exception):
    """收尾单步超时:任务已中断,状态尽力落 FAILED,前端会收到 error 事件。"""


# 收尾路径单步超时:图执行完后的收尾(读最终状态/计费/会话轮次 bump/落库/发 done)
# 全部是 DB/Redis await,历史上完全无超时保护——任一步挂起任务就永久 RUNNING,
# 会话被锁死(见 _guarded 注释)。20s 对本地 DB/Redis 操作足够宽松,且能兜住挂起。
_FINALIZE_STEP_TIMEOUT = 20


async def _finalize_task_bounded(task_id: str, status: str, result: dict, *, usage_event: dict | None = None) -> bool:
    """带超时的 _finalize_task。返回 False 表示超时(状态可能未落库),调用方继续兜底。"""
    try:
        await asyncio.wait_for(
            _finalize_task(task_id, status, result, usage_event=usage_event),
            timeout=_FINALIZE_STEP_TIMEOUT,
        )
        return True
    except TimeoutError:
        log.error(f"task {task_id} 落库 {status} 超过 {_FINALIZE_STEP_TIMEOUT}s(DB 可能不可用),状态可能未持久化")
        return False


async def _guarded(task_id: str, label: str, coro, timeout: float = _FINALIZE_STEP_TIMEOUT):
    """收尾路径统一保护:任何一步挂起(DB/Redis/外部调用不返回)都转为超时并就地兜底。

    图执行段已有 asyncio.timeout 兜底;但收尾段(aget_state/计费/bump_turn/落库/done)
    完全在它之外,是「RUNNING 永不终结 → 会话锁死 → 前端光标闪」的头号现场。
    这里把每个关键 await 用 wait_for 包住:超时即判定任务卡死,尽力落 FAILED 并抛出
    内部异常中断收尾,由外层捕获后正常关流。
    """
    mark_progress(task_id)
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        log.error(f"task {task_id} 收尾步骤 {label} 超过 {timeout}s,判定任务卡死,自动终止")
        await _finalize_task_bounded(task_id, "FAILED", {
            "error": f"任务收尾步骤 {label} 超时(数据库/事件总线不可用?),已自动终止,请重新发送",
            "kind": TaskErrorKind.TASK_TIMEOUT,
        })
        # 就地广播 error,让前端 SSE 立刻收到终态解锁(不能只靠落库——前端可能没在刷新)
        await _publish(task_id, "error", task_error(
            TaskErrorKind.TASK_TIMEOUT,
            message=f"任务收尾步骤 {label} 超时,已自动终止,请重新发送"))
        raise _FinalizeStepTimeout(label) from None


async def execute_task(task_id: str) -> None:
    """执行一个任务。

    **不变量:这个函数返回时,SSE 流一定已经关闭**(除非任务进入 WAITING_HUMAN——
    那种情况下同一个 task_id 后续还会继续产出事件)。

    以前不满足这个不变量:任务不存在时直接 return、resolve 抛出非 ResolveError 的
    异常时向上冒泡,两种情况都不会 close_stream。前端 EventSource 就一直挂在那里
    等一个永远不会来的事件——操练台表现为"点了执行,然后什么都没有,也不报错"。
    现在用 _guard 兜住:任何路径漏掉的收尾都在这里补上。
    """
    finished = False
    mark_progress(task_id)
    try:
        finished = await _execute_task_inner(task_id)
    except _FinalizeStepTimeout as exc:
        # 收尾单步超时已在 _guarded 内就地落 FAILED + 广播 error,这里只负责关流
        log.warning(f"task {task_id} 收尾中断(步骤 {exc.args[0] if exc.args else '?'}),按已终止处理")
        finished = True
    except Exception as exc:  # noqa: BLE001 - 兜底:绝不让异常带着一个开着的流离开
        log.exception(f"task {task_id} 执行时发生未捕获异常")
        try:
            await _finalize_task(task_id, "FAILED", {"error": f"{type(exc).__name__}: {exc}", "kind": TaskErrorKind.INTERNAL_ERROR})
        except Exception:  # noqa: BLE001 - 落库也失败时至少要把流关掉,否则前端永久等待
            log.exception(f"task {task_id} 标记失败状态时再次异常")
        await _publish(task_id, "error", task_error(
            TaskErrorKind.INTERNAL_ERROR,
            message=f"执行过程发生内部错误:{type(exc).__name__}: {exc}"))
        finished = True
    finally:
        # WAITING_HUMAN 不能关流:审批通过后同一个 task_id 会继续产出事件
        clear_progress(task_id)
        if finished:
            await streaming.close_stream(task_id)

    return finished


async def _execute_task_inner(task_id: str) -> bool:
    """真正的执行逻辑。返回 True 表示"本次已终结,可以关流"。"""
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task is None:
            # 以前这里直接 return,流不关,前端永久等待
            log.error(f"execute_task: task {task_id} 不存在")
            await _publish(task_id, "error", task_error(
                TaskErrorKind.TASK_NOT_FOUND, message="任务不存在或已被清理"))
            return True

        scenic_id = task.scenic_id
        payload = dict(task.payload or {})
        user_id = payload.get("user_id", "")
        role = payload.get("role", "scenic_operator")
        is_resume = bool(payload.get("_resume"))
        set_trace_context(payload.get("trace_id", "-"), scenic_id)

        # B 方案兜底:凭证确认被忽略(approved=False)却带 _resume 入队的(历史遗留/竞态)
        # 不驱动图恢复——否则 agent 在无用户输入时会再次发出同一个「保存凭证」请求,
        # 形成「待确认→超时→忽略→再请求」的自动重试循环。直接终结,交互一次一次来。
        if is_resume and not payload.get("_resume_approved") and payload.get("_credential_confirm"):
            log.info(f"task {task_id} 凭证确认忽略后不应恢复执行,直接终结(B 方案兜底)")
            from cogni_agent.runtime_service.engine.hitl import finalize_credential_confirm

            await finalize_credential_confirm(task_id, "忽略后仍入队恢复,已兜底终结")
            return True

        try:
            spec = await resolve_execution_spec(
                db,
                executor_type=task.executor_type,
                executor_id=task.executor_id,
                scenic_id=scenic_id,
                role=role,
                user_id=user_id,
                overrides=payload.get("overrides") or None,
                # Phase 5(13 号文档):Console 两层版本选择 —— 调用方显式钉到某 published 版本,
                # resolver 按档位 0 解析(优先于指派/默认);无选择时不传,保持原语义。
                executor_version_id=payload.get("executor_version_id"),
                # 19 号 S7:固定问题命中直跳(skill 直达不走 agent 指派链),
                # 等价 agent 指派的授权穿透——否则 APP 景区用户对集团工具失权,
                # skill_no_usable_tools 降级、模型无工具文本模拟调用(dev 实测事故)
                # handoff_authorized:Quick BI 没答上来,转给**这条问题上绑定的**技能/智能体 ——
                # 同样是运营背书过的入口,授权口径与固定问题一致(不再过「按用户分发」那道闸)
                question_authorized=bool(payload.get("question_id") or payload.get("handoff_authorized")),
            )
            await _publish(task_id, "stage", {
                "stage": streaming.STAGE_UNDERSTANDING,
                "text": "正在理解您的问题…",
            })
        except ResolveError as exc:
            # 细粒度错误码(如 15 号文档 agent_not_distributed)透出为细分 kind,
            # 未登记时回落 RESOLVE_FAILED(同数字码 4004,前端只需按 code 分支)。
            kind = exc.code if exc.code in KIND_TO_CODE else TaskErrorKind.RESOLVE_FAILED
            await _finalize_task(task_id, "FAILED", {"error": str(exc), "kind": kind})
            await _publish(task_id, "error", task_error(kind, message=str(exc)))
            return True
        except Exception as exc:  # noqa: BLE001 - 解析层的意外错误(数据脏/配置怪)同样要给前端一个交代
            log.exception(f"task {task_id} 解析执行目标时异常")
            await _finalize_task(task_id, "FAILED",
                                 {"error": f"{type(exc).__name__}: {exc}", "kind": TaskErrorKind.RESOLVE_ERROR})
            await _publish(task_id, "error", task_error(
                TaskErrorKind.RESOLVE_ERROR, message=f"解析执行目标失败:{exc}"))
            return True

        if not is_resume:
            try:
                await check_org_daily_budget(scenic_id, 0)
            except GuardrailTripped as exc:
                await _finalize_task(task_id, "FAILED", {"error": str(exc), "kind": TaskErrorKind.BUDGET_EXCEEDED})
                await _publish(task_id, "error", task_error(TaskErrorKind.BUDGET_EXCEEDED, message=str(exc)))
                return True

        decision = await decide_mode(spec, payload.get("override_mode"))

        # 解析期诊断推到时间线上。这类"降级但没中断"的情况以前只写日志——运维在管理
        # 后台看到的只是"Agent 好像什么都没做",却不知道是它绑定的工具全被权限过滤了、
        # 还是 Prompt 变量没渲染上。让它可见,比让它安静地降级重要得多。
        # v1.1.2:只推 warn/error 级真降级;info 级(如 tool_renamed 函数名规范化)是
        # 每次执行都一样的确定性内部细节,对排查无增量信息,只写日志不刷时间线。
        diagnostic_events: list[dict] = []
        if spec.diagnostics:
            warn_diags = [d.as_dict() for d in spec.diagnostics if d.level != "info"]
            for diag in warn_diags:
                # 这些事件**必须同时进 graph_input 的 timeline 种子**(见下面),否则
                # 只活在 SSE 流里:页面关掉就没了,落库的时间线里一个字都没有,
                # 导出执行详情也看不到。而它们恰恰是「为什么一个工具都没调」的答案 ——
                # 现场任务 6dbbf1c8 就是这样:三个工具全被跳过,告警发了,没人看得见。
                ev = {"step": "resolve_diagnostic", **diag,
                      "ts": time.monotonic(), "wall": datetime.now().strftime("%H:%M:%S")}
                diagnostic_events.append(ev)
                await _publish(task_id, "timeline", ev)
            warn_count = sum(1 for d in spec.diagnostics if d.level == "warn")
            if warn_count:
                log.warning(f"task {task_id} 解析期产生 {warn_count} 条告警")

        # v1.1.6(P0-3 会话溯源):把「实际解析到的版本来源」推时间线——13 号文档 Phase 1/3 后
        # 运行时 100% 走版本快照、主表不可变,12 号 P0-2 的漂移比对接口随之退役(无独立接口
        # 需保留),本事件即唯一观测面:agent/skill/tool 每层实际用到的版本(15 号文档删默认后
        # 枚举收敛为 assigned/explicit_snapshot/pinned_snapshot),排查「配置改了但执行没跟上」
        # 的第一入口,故不随 warn 级过滤,有溯源就推。
        provenance_event = None
        if spec.provenance:
            provenance_event = {"step": "resolve_provenance", "provenance": spec.provenance}
            await _publish(task_id, "timeline", provenance_event)

        checkpointer = await get_checkpointer()
        # 图的选择:单工具直调 / 固定 DAG(架构图 ④「多 Skill 编排:固定DAG」)/ ReAct 形状统一图。
        # 只有"L1 + 显式登记 mode=dag + 确实存在非线性依赖"才走 DAG 图;线性链继续走
        # 原来的 ReAct 图(线性链在那张图里已经能正确表达,没必要为它多引入一条代码路径)。
        use_dag = (
            spec.direct_tool is None
            and decision.mode.value == "L1"
            and spec.composition_hint == "dag"
            and len(spec.skill_chain) > 1
        )
        # 编排直出(19 号 S7.5):固定问题已由「指派 pin 的技能版本」确定取数口径时,
        # 不再让 LLM 去猜该调哪个工具——改为「绑定工具全集并行 + 一次成文」。
        # 是否置标记由网关决定(payload.direct_dispatch):网关那里拿得到技能版本的原始
        # 快照,能不看 ToolSpec 就判断「这个技能绑没绑工具」;而真正调用用的 ToolSpec
        # 仍由 resolver 按同一版本解析产出 —— 一处真相,不是两处各解析一遍。
        _direct_dispatch = (
            bool(payload.get("direct_dispatch"))
            and spec.direct_tool is None
            and bool(spec.skill_chain)
            and bool(spec.skill_chain[0].tools)
        )
        # ── 上下文记忆 ──
        # 把此前提炼出的事实(用户角色/关注周期/偏好)注入本轮。
        # 与 Checkpointer 是两回事:后者存完整消息历史且会被裁剪,
        # 这里存的是很短的结构化事实,裁剪不掉 ——
        # 所以「这个人是销售总监」不会聊着聊着就丢了。
        _memories: list[dict] = []
        try:
            async with get_sessionmaker()() as _mdb:
                _memories = await ctx_memory.recall(
                    _mdb, conversation_id=task.conversation_id,
                    user_id=(payload or {}).get("user_id"))
        except Exception:  # noqa: BLE001 - 记忆读失败不该挡住正常执行
            log.debug("读取上下文记忆失败,本轮不注入", exc_info=True)

        # ── L4:执行规划 ──
        # 动手前把「分几步、每步用什么」想清楚,并**立刻整份推给前端** ——
        # 界面一开始就能列出全部步骤(后面的显示「等待中」),用户知道还有几步。
        #
        # 计划不另建一张图:它作为约束注入 ReAct 循环即可。
        # 另建图意味着两套执行路径要各自维护护栏、审批、断点续跑,
        # 而这些逻辑与「是否有计划」无关 —— 复用一套更不容易长歪。
        exec_plan = planner.ExecutionPlan()
        if decision.mode == RunMode.L4_PLAN_EXECUTE:
            exec_plan = await planner.make_plan(
                goal=payload.get("message", ""), skills=spec.skill_chain,
                model=spec.model_override)
            if exec_plan.steps:
                # 耗时预估:有历史数据用实测均值,没有就用同类默认值并在文案里
                # 注明「首次运行,仅供参考」—— 离谱的预估比不给预估更糟。
                _payload_plan = exec_plan.as_dict()
                try:
                    async with get_sessionmaker()() as _sdb:
                        _by_id = await tool_stats.totals_by_tool(
                            _sdb, days=30, scenic_id=task.scenic_id)
                    _name_of = {t.id: t.name for sk in spec.skill_chain for t in sk.tools}
                    _by_name = {_name_of[k]: v for k, v in _by_id.items() if k in _name_of}
                    _est = estimator.estimate_plan(exec_plan, tool_stats_by_name=_by_name)
                    if _est:
                        _payload_plan["estimate"] = _est.as_dict()
                except Exception:  # noqa: BLE001 - 预估失败不该挡住执行
                    log.debug("耗时预估失败,本次不展示", exc_info=True)
                await streaming.publish(task_id, "plan", _payload_plan)
                log.info(f"task {task_id} 生成 {len(exec_plan.steps)} 步执行计划")
            else:
                # 规划没产出(模型判断一步就能答完,或调用失败)——
                # 退回 ReAct,不要卡在一个空计划上
                log.info(f"task {task_id} 规划为空,按 ReAct 执行")

        # v1.1.6:conversation_id 传给图,供 LLM 提示词日志(llm_prompt_logs)按会话聚合。
        _conv_id = task.conversation_id or task_id
        if spec.direct_tool is not None:
            graph = build_direct_tool_graph(spec, db, checkpointer)
        elif use_dag:
            graph = build_dag_graph(spec, db, checkpointer, conversation_id=_conv_id)
        elif _direct_dispatch:
            graph = build_direct_dispatch_graph(spec, db, checkpointer, conversation_id=_conv_id)
        else:
            graph = build_agent_graph(spec, decision.mode.value, db, checkpointer, conversation_id=_conv_id)
        # thread_id 决定"对话记忆挂在哪儿"。有 conversation_id 时用它,同一会话的
        # 多轮任务共享同一份 Checkpointer 历史;没有则退回 task_id(单轮任务,旧行为)。
        # 32 号:固定问题(question_id 命中,卡片/选择器/预热)= 工作流语义——独立
        # thread(task_id),不继承会话 messages 历史:确定性取数不信任上下文
        # (旧轮工具结果/重复拦截提示会教模型偷懒不调工具,轮 16 实证);参数已由
        # 模板装配期全量生成,跨轮槽位零损失。非固定问题保留会话记忆。
        _is_fixed_q = bool(payload.get("question_id"))
        thread_id = (task_id if _is_fixed_q else None) or task.conversation_id or task_id
        config = {"configurable": {"thread_id": thread_id}}

        # timeline 的 reducer 是 operator.add(整个 thread 内累加)。多轮会话里,
        # 第 2 轮如果直接取全量 timeline,会把第 1 轮的执行过程也算进本轮结果里。
        # 这里先记下进入本轮时已有的长度,收尾时只切出本轮新增的部分。
        # 顺带读出历史会话的参数槽(slots):意图层五机制 ② Slot Filling 需要它做
        # 跨轮参数累积(比如上一轮用户说过景区/时间,这一轮说"那同比呢"就不用再问)。
        timeline_base = 0
        history_slots: dict = {}
        if task.conversation_id:
            try:
                prior = await graph.aget_state(config)
                timeline_base = len((prior.values or {}).get("timeline") or [])
                history_slots = (prior.values or {}).get("slots") or {}
            except Exception:  # noqa: BLE001 - 首轮没有快照是正常情况
                timeline_base = 0

        # ── 执行前意图分诊(Triager,意图层五机制 ①③④⑤)──
        # 放在建图之后:判定为需要澄清时根本不该进执行循环(不花工具调用、不产生副作用)。
        # 澄清**不走 HITL interrupt** —— interrupt 的语义是"任务暂停等审批",会占着租约
        # 堆在审批中心;而澄清是"这一轮正常结束、把球踢回给用户",配合多轮会话记忆,
        # 用户回答后接着走即可。
        # 默认开启;Triager 调用失败(degraded=True)时降级为 L2 全量执行,不裁剪不澄清。
        triage: intent.TriageResult | None = None
        # intent_check 事件:Triager 的判定结果。mode_decision 事件放在 graph_input
        # 初始 timeline 里(随 checkpoint 落库),intent_check 也必须同样入 checkpoint,
        # 否则落库 timeline / 管理后台链路可视化 / session-info 的 _intent_info 都读不到
        # 它(此前真实会话 conv_04f6701ff0324ceda82b47bf113c6786 里 Triager 明明执行了,
        # 展示层 intent 却恒为空,就是这个原因)。
        _intent_check_event: dict | None = None
        # 19 号 S7.5:固定问题参数模板(网关命中时已展开为具体值)。
        # 仅注入已填槽位(模板展开的参数——含基准日期与配置的对比窗口,
        # 全部装配期生成,无运行期默认补全)。
        # 定义在 is_resume 判断之外:审批恢复路径同样按模板注入槽位,放在
        # `if not is_resume` 分支内会让恢复路径引用未定义变量(NameError)。
        question_slots: dict = {k: v for k, v in (payload.get("question_params") or {}).items() if v}
        # 景区级接口参数的提示词段(见 engine/scenic_params):读失败 = 空串,不阻断
        from cogni_agent.runtime_service.engine import scenic_params as _scenic_params

        _scenic_param_block = _scenic_params.prompt_block(await _scenic_params.load(db, scenic_id))
        if not is_resume:
            goal_text = payload.get("message", "")
            has_side_effects = any(
                getattr(t, "risk_level", "low") in ("medium", "high")
                for sk in spec.skill_chain for t in sk.tools
            )
            # ② Slot Filling:优先级 APP date > 问题语义 > 会话 slot > 语义默认。
            # date 是端侧(APP 日期选择器)显式选定的问数窗口,优先级最高。
            _date_ctx = payload.get("date") or {}
            app_slots: dict = {}
            for k, v in _date_ctx.items():
                if v:
                    app_slots[k] = v
            # v1.1.5 意图识别 agent 化:判定策略/分诊模型/分诊 prompt 都由
            # Agent 的 intent_config 决定(一个 Agent 一个意图识别),未配置回退框架默认。
            # 19 号 S7:固定问题直跳且参数模板已备齐基准槽位(question_params)时跳过
            # Triager——路由已确定(组合直达)、参数已有(模板装配期全量生成),这一次 LLM
            # 调用纯属浪费(省掉即降首字延迟)。未配模板的固定问题仍走 Triager(参数判定)。
            if not question_slots and intent.enabled_for([sk.name for sk in spec.skill_chain], has_side_effects,
                                                          intent_config=spec.intent_config):
                # Triager 输入 = 会话 slot ∪ APP date 上下文(APP 显式给的日期最高优先级)。
                # v1.1.7:多轮会话历史注入(设计文档 14 §3/§5) —— Triager 判定前取
                # 「本轮之前最近 history_rounds 轮」对话文本注入,支撑跨轮话题延续
                # (用户回答澄清追问/省略表述,conv_5bd2ad... 话题漂移事故)。
                # 历史来源 = conversations 层 tasks 聚合:clarify 轮不写 checkpoint,
                # graph state.messages 里没有澄清问答,只有会话层完整含每轮 user/assistant。
                # history_rounds 由 Agent intent_config 显式配置(0-5),未配置=0(不注入,
                # 存量单轮隔离行为);取数/组装失败静默降级=不注入,不阻断主流程。
                history_rounds = intent.resolve_history_rounds(spec.intent_config)
                history_text = ""
                if history_rounds > 0 and thread_id:
                    try:
                        from cogni_agent.control_plane_service import conversations as conv_svc

                        turns = await conv_svc.get_recent_turns(
                            thread_id, exclude_task_id=task_id, limit=history_rounds,
                        )
                        history_text = intent.format_history_text(turns)
                        log.info(f"task {task_id} Triager 注入会话历史 {history_rounds} 轮"
                                 f"(text {len(history_text)} 字符)")
                    except Exception:  # noqa: BLE001
                        log.warning(f"task {task_id} 取会话历史失败,本轮 Triager 不注入历史",
                                    exc_info=True)
                _triage_t0 = time.monotonic()
                triage = await intent.triage(
                    goal=goal_text,
                    capabilities=[
                        {
                            "name": sk.name,
                            # v1.1.2:描述驱动路由器 —— 优先用人写的能力概述(description),
                            # 存量 Skill 无该字段时回退 system_prompt 截断,保证老数据不受影响。
                            "description": sk.description or (sk.system_prompt or "")[:120],
                            # v1.1.5:Triager 能力清单 = 技能名 + 描述 + 全量工具名 + 全量工具描述,
                            # 不再注入参数名清单 —— 参数事实只在 params_schema 一处(单一事实来源),
                            # skill 增删工具无需同步任何声明。路由层靠描述与工具边界选技能,参数完整性
                            # 交由 L2 主模型看完整 schema 校验(缺参时执行层澄清,比路由层拿并集参数名
                            # 误判 missing 反问更可靠 —— 并集近似信号做精确判定必然过澄清)。
                            # v1.1.5:tool_notes 全量(工具名+完整描述)让路由层能读到全部依赖声明,
                            # 判出缺失时能自主挂载补齐技能,不再只能反问(描述驱动路由,见 intent.py)。
                            "tools": _triage_tools(sk),
                            "tool_notes": _triage_tool_notes(sk),
                        }
                        for sk in spec.skill_chain
                    ],
                    # 槽位注入景区 ID 全部别名(scenic_id/scenicId/...):Triager 据此判定
                    # 景区参数已填,不把 scenicId 等驼峰字段误判为 missing 反问用户。
                    slots={**history_slots, **app_slots, **_scenic_slots(scenic_id)},
                    model=spec.model_override,
                    # v1.1.5:会话隔离维度进缓存 key —— Triager 判定会裁剪执行链,
                    # 跨会话复用会把上个会话的错误判定(如漏挂客流分析)传染给新会话。
                    # 与 checkpoint 同粒度(thread_id):同会话多轮共享,跨会话不共享。
                    conversation_id=thread_id,
                    # v1.1.5 意图识别 agent 化:把 Agent 级意图识别配置传给 Triager
                    # (分诊 prompt 覆盖/独立模型/二次确认开关),一个 Agent 一个意图识别。
                    intent_config=spec.intent_config,
                    # v1.1.7:多轮会话历史注入(history_text 空串 = 不注入,存量行为)。
                    history_text=history_text,
                    # v1.1.6:Triager 的最终提示词落库(step=intent_check),供提示词质量分析。
                    prompt_sink=lambda sp, up: record_prompt(
                        db, task_id=task_id, conversation_id=thread_id,
                        scenic_id=scenic_id, user_id=user_id,
                        step="intent_check", model=spec.model_override,
                        system_prompt=sp, history=[{"role": "user", "content": up}],
                    ),
                )
                # ④ 规则兜底(确定性,非 LLM):Triager 已裁剪时,技能声明 route_hit_keywords
                # 命中用户原话即强制并入 target_skills —— 高频技能(如客流分析)绝不因一次
                # 分诊误判被裁剪(conv_5a900be0 事故教训)。关键词是技能侧配置,本段只做
                # 通用合并;target_skills 为空(全量执行)时不触发,不会把全量改成裁剪。
                # 置于 event 构造之前:确保「执行链分析」展示的 target_skills 是最终名单。
                if triage.target_skills and not triage.degraded:
                    forced = _force_hit_skills(spec.skill_chain, goal_text)
                    if forced:
                        merged = list(dict.fromkeys([*triage.target_skills, *forced]))
                        if merged != triage.target_skills:
                            log.info(f"task {task_id} 规则兜底强制挂载: {forced} "
                                     f"(goal 命中 route_hit_keywords) -> target_skills={merged}")
                            triage.target_skills = merged
                # (27 号评审:原「L3 x-default-rule 规则补全」已移除——缺省值隐藏本该
                # 暴露的缺参问题;参数装配期生成完整,缺=由下方 L2 required_param_gaps
                # 拦截并暴露,不做任何运行期默认补全。)
                # ── L2 结构化输出 + schema 校验(拦截层)───────────────────────────
                # Triager 输出后,对最终 target_skills 绑定的工具做必填覆盖检查:
                # 「工具必填、但 filled/missing/slots 均未覆盖」= Triager 漏报缺失
                # (intent.required_param_gaps:只按 json_schema.required 判定,resolver 已剥离
                # forced_params / x-param-source 等执行层自动补齐的参数,不会误报)。
                # 拦截动作:并入 params_missing + 告警日志 + event 标注 schema_gap,不让
                # Triager 自信 execute 却缺关键参数时带病进入执行(conv_a5c0d4... 教训)。
                schema_gap: list[str] = []
                if (not triage.degraded and triage.action == intent.ACTION_EXECUTE
                        and triage.target_skills):
                    schema_gap = intent.required_param_gaps(
                        skill_chain=spec.skill_chain,
                        target_skills=triage.target_skills,
                        filled=triage.params_filled,
                        missing=triage.params_missing,
                        slots={**history_slots, **app_slots, **_scenic_slots(scenic_id)},
                    )
                    if schema_gap:
                        log.warning(f"task {task_id} L2 schema 校验拦截:Triager 漏报必填参数 "
                                    f"{schema_gap} -> 并入 missing,交由执行层补齐/澄清")
                        triage.params_missing = list(
                            dict.fromkeys([*triage.params_missing, *schema_gap]))
                _intent_check_event = {
                    "step": "intent_check", "intent": triage.intent, "action": triage.action,
                    "target_skills": triage.target_skills, "missing": triage.params_missing,
                    # L2 schema 校验结果:execute 裁剪模式下 Triager 漏报的必填参数清单
                    # (空 = 校验通过或非校验场景)。前端「执行链分析」据此展示拦截详情。
                    "schema_gap": schema_gap,
                    # params_filled/reply/rewritten_goal:Triager 判定细节,前端「执行链分析」
                    # 据此展示「判断依据 → 结果 → 动作对象」,不再只有一句「直接执行」。
                    "params_filled": triage.params_filled,
                    "reply": triage.reply,
                    "rewritten_goal": triage.rewritten_goal,
                    "reason": triage.reason,
                    # degraded=True:Triager 调用失败降级为 L2 全量执行(executor.py 的
                    # 兜底语义),展示层据此区分「真实判定」与「降级默认」,不再把降级
                    # 误显示为「意图分诊:直接执行」。
                    "degraded": triage.degraded,
                    # 降级**为什么**发生。只说「Triager 不可用」的话,运维面对的是一个
                    # 没有下一步的结论 —— 分诊模型没登记、超时、返回非法 JSON,
                    # 这三种的处理方式完全不同,而界面上只有那一句话。
                    "degraded_reason": getattr(triage, "degraded_reason", ""),
                    # 分诊这一次模型调用花了多久 / 多少 token。以前执行日志里这一步恒为 0.0,
                    # 而它在每一轮都串行地挡在执行前面(任务 5877db72 约 12 秒)
                    "llm": {"total_ms": round((time.monotonic() - _triage_t0) * 1000, 1),
                            "prompt_tokens": triage.prompt_tokens,
                            "completion_tokens": triage.completion_tokens},
                    # 分诊的输入有多大、改写有没有被采用(空目标改写会被丢弃,用用户原话)
                    "detail": {"goal_chars": len(goal_text or ""), "history_rounds": history_rounds,
                               "history_chars": len(history_text or ""),
                               "skills_offered": len(spec.skill_chain),
                               "rewritten_goal_used": bool(triage.rewritten_goal)
                               and not intent.is_empty_goal(triage.rewritten_goal),
                               "rewritten_goal_rejected": bool(triage.rewritten_goal)
                               and intent.is_empty_goal(triage.rewritten_goal)},
                    # 事件产生时刻:与 graphs._tl_ts 同口径,供耗时统计按真实 ts 计算。
                    "ts": time.monotonic(),
                    # 墙钟时间(绝对时间点):前端「执行链分析」展示该判定发生在几点几分。
                    "wall": datetime.now().strftime("%H:%M:%S"),
                }
                await _publish(task_id, "timeline", _intent_check_event)
                if not triage.degraded and triage.action == intent.ACTION_CLARIFY:
                    log.info(f"task {task_id} Triager = clarify,不进入执行循环")
                    result = {
                        "answer": triage.reply,
                        "usage": {"prompt_tokens": triage.prompt_tokens,
                                  "completion_tokens": triage.completion_tokens},
                        "timeline": [{"step": "intent_check", "intent": triage.intent,
                                      "action": "clarify", "reason": triage.reason,
                                      "degraded": triage.degraded}],
                        "needs_clarification": True,
                    }
                    if task.conversation_id:
                        from cogni_agent.control_plane_service import conversations as conv_svc

                        await conv_svc.bump_turn(task.conversation_id)
                    await _finalize_task(task_id, "COMPLETED", result)
                    await _publish(task_id, "done", result)
                    return True
                # ⑤ 按需挂载:target_skills 非空时裁剪 skill_chain(受限工具池替代全链/全量),
                # 再按裁剪后的链重建图。DAG 是显式登记的依赖编排,不参与裁剪。
                if triage.target_skills and not use_dag and not triage.degraded:
                    keep = [sk for sk in spec.skill_chain if sk.name in triage.target_skills]
                    if keep and len(keep) < len(spec.skill_chain):
                        log.info(f"task {task_id} Triager 按需挂载: "
                                 f"{[s.name for s in spec.skill_chain]} -> {[s.name for s in keep]}")
                        spec.skill_chain = keep
                        graph = build_agent_graph(spec, decision.mode.value, db, checkpointer, conversation_id=_conv_id)
                    elif not keep and triage.target_skills:
                        # Triager 返回的技能名一个都没匹配上(可能是工具名/接口名混入或已下架):
                        # 当前行为是回退全量执行,记录 warning 便于排查路由异常。
                        log.warning(f"task {task_id} Triager target_skills 全部未匹配: "
                                    f"target_skills={triage.target_skills} "
                                    f"可用技能={[s.name for s in spec.skill_chain]} -> 回退全量执行")
                # ③ Query Rewriting:多轮省略表述改写为自包含问题
                if triage.rewritten_goal and not intent.is_empty_goal(triage.rewritten_goal):
                    goal_text = triage.rewritten_goal
                # ② 槽位回填:问题语义抽取的参数并入会话槽(随 checkpoint 持久化)。
                # 会话绑定的景区 ID 始终作为已收集槽位注入(含 scenicId 等别名,与
                # Triager 判定口径一致)——Triager 据此判定景区参数已填,不再把景区 ID
                # 列为 missing 去向用户反问。
                history_slots = {**history_slots, **app_slots,
                                 **_scenic_slots(scenic_id), **triage.params_filled}
                # ②' 时间参数**算**出来,不要让模型推。
                #
                # 现场那次:用户问「近一日(即昨天 2026-09-19)景区 X 的数据」,分诊判对了
                # (数据查询 → we),也列出了缺失参数 period/start/end —— 然后一个工具都没调,
                # 直接去生成文字,回了句「如需要近一日的数据请告诉我」。
                # 日期他已经写得不能再清楚,缺的那几个参数本来就是能算的。
                # 这一步纯确定性(复用 sys_date_* 的口径),算得出才填,算不出不动。
                history_slots = _fill_time_slots(spec, history_slots, goal_text)
            else:
                # 跳过 Triager 的两种情形:① 固定问题已装配参数模板(question_slots
                # 非空)——路由已确定、参数已齐全,再跑一次 Triager 纯属浪费(19 号 S7);
                # ② 未启用意图识别。二者按同一优先级合并参数槽:
                # 会话历史 < 模板 < APP date(用户显式选择最高)。
                # 缩进铁律:本 else 必须与上面的 `if not question_slots and ...`(12
                # 空格)配对。此前误写成 8 空格,被 Python 解析成外层 `if not is_resume`
                # 的 else,导致固定问题既不走 Triager、也不补发 intent 事件——前端
                # 「意图识别」节点收不到事件而永久挂起(真实任务 4dfda734 即此症状)。
                history_slots = {**history_slots, **question_slots, **app_slots,
                                 **_scenic_slots(scenic_id)}
                if question_slots:
                    # 跳过 Triager 的固定问题:补合成 intent 事件,前端「意图识别」
                    # 节点展示命中信息而不是空白。必须显式 publish——只塞进
                    # graph_input["timeline"] 的话 SSE 实时流里没有,实时链路照样空白
                    # (与 Triager 分支的 _publish 口径保持一致)。
                    _intent_check_event = {
                        "step": "intent_check", "intent": "fixed_question_hit",
                        "detail": {"question_id": payload.get("question_id"),
                                   "source": payload.get("question_source"),
                                   "template_slots": sorted(question_slots.keys())},
                        "ts": time.monotonic(), "wall": datetime.now().strftime("%H:%M:%S"),
                    }
                    await _publish(task_id, "timeline", _intent_check_event)
        else:
            # is_resume:审批恢复后继续原任务,不重跑 Triager(判定已在首轮完成)。
            goal_text = payload.get("message", "")
            # app_slots 只在 `if not is_resume` 分支内装配,此处不引用它(引用会
            # NameError);且恢复路径走 Command(resume=...),graph_input 不携带 slots。
            history_slots = {**history_slots, **question_slots,
                             **_scenic_slots(scenic_id)}
            if question_slots:
                # 恢复后前端是新的 SSE 连接,同样补发命中事件,免得「意图识别」空白。
                _intent_check_event = {
                    "step": "intent_check", "intent": "fixed_question_hit",
                    "detail": {"question_id": payload.get("question_id"),
                               "source": payload.get("question_source"),
                               "template_slots": sorted(question_slots.keys())},
                    "ts": time.monotonic(), "wall": datetime.now().strftime("%H:%M:%S"),
                }
                await _publish(task_id, "timeline", _intent_check_event)

        if is_resume:
            # resume 值有两种形态,tools_adapter 都认:
            #   bool                                  批准 / 驳回(既有审批不受影响)
            #   {"approved": bool, "params": {...}}    改参数后批准(第三种动作)
            _approved = payload.get("_resume_approved", False)
            _patch = payload.get("_resume_params")
            graph_input = Command(
                resume=({"approved": bool(_approved), "params": _patch} if _patch else _approved))
        else:
            graph_input = {
                "messages": [HumanMessage(content=goal_text)],
                "task_id": task_id,
                "scenic_id": scenic_id,
                "user_id": user_id,
                "role": role,
                "goal": goal_text,
                "exec_plan": exec_plan.as_dict(),
                # 固定问题的「问题描述」:运营填的口径与约束,作为附加系统约束下发。
                # 不替换用户消息 —— 用户在固定问题基础上的追问(「那昨天呢」)必须保留。
                "question_instruction": payload.get("question_instruction") or "",
                # 景区级接口参数(OTA 商户账号等)接在记忆段后面:模型知道这些值已经有了,
                # 不会再向用户索要(任务 5877db72 就是等用户自己报了一遍商户 ID)
                "memory_block": ctx_memory.as_prompt_block(_memories) + _scenic_param_block,
                "mode": decision.mode.value,
                "skill_chain": [s.id for s in spec.skill_chain],
                "skill_idx": 0,
                "skill_step_counts": {},
                "skill_outputs": {},
                "tool_tracker": {},
                "persona": spec.persona,
                "overrides": payload.get("overrides") or {},
                "guardrail": {},
                # 种子里少一类事件,落库的时间线就少一类 —— turn_timeline 只取 graph state,
                # 光 _publish 的事件活不过这一次 SSE 连接。
                "timeline": diagnostic_events
                    + ([provenance_event] if provenance_event else [])
                    + [{"step": "mode_decision", "mode": decision.mode.value, "intent": decision.intent, "reasons": decision.reasons,
                        "ts": time.monotonic(), "wall": datetime.now().strftime("%H:%M:%S")}]
                    + ([_intent_check_event] if _intent_check_event else []),
                "usage": {},
                "pending_interrupt": None,
                "final_answer": None,
                # ── 意图层五机制落地点 ──
                "intent": (triage.intent if triage else "") or "",
                "slots": history_slots,          # ② 会话级参数槽(随 checkpoint 持久化)
                "plan": (triage.plan if triage else []) or [],  # ④ Plan-and-Execute
                # 32 号:执行层 LLM 历史策略——按轮注入对话文本(不含旧轮工具结果
                # 原文,旧轮工具结果是上下文毒源)。固定问题强制 0(纯净上下文,
                # 与 thread 隔离双保险);非固定读 Agent intent_config
                # .exec_history_rounds(默认 2,0-5)。
                "exec_history_rounds": 0 if _is_fixed_q else intent.resolve_exec_history_rounds(
                    spec.intent_config),
            }
            task.status = "RUNNING"
            await db.commit()
            await _publish(
                task_id, "timeline",
                {"step": "mode_decision", "mode": decision.mode.value, "intent": decision.intent, "reasons": decision.reasons,
                 "ts": time.monotonic(), "wall": datetime.now().strftime("%H:%M:%S")},
            )

        interrupted_payload = None
        # 阶段追踪:每个阶段最多发布一次,避免冗余。
        # 初始必须是空集:understanding 在任务开始时已发布,querying/generating 在各自
        # 首次触发时发布。若把三个 stage 都塞进初始集,后两个的发布判断恒为 False,
        # stage 事件永远不会发出——前端等待时只能看到光标(表现为"一直转、内容一次性吐出")。
        _stage_published: set[str] = set()
        # 每步耗时打点的起点:事件产生时已打真实 ts,首事件相对本起点计算
        _timeline_ts = time.monotonic()
        # v1.1.2:整轮执行总耗时的起点(执行分析开始时刻)。收尾处与当前时刻相减即
        # 真实墙钟耗时——不依赖事件累加(时间线可能被裁剪、事件也不保证完整)。
        _exec_t0 = time.monotonic()
        task_timeout = get_settings().get("runtime", "task_timeout_seconds", default=300)
        # 离线预热单独放宽:预热是多轮工具调用的重活,实测 10~16 分钟级,沿用全局
        # 600s 会被成批强杀(失败→补派→重新排队→模型更堵),离线任务也不占在线
        # 等待体验 → 给它更长的墙钟上限(配置项,0/空 = 不单独放宽)。
        if (task.source or "") == SOURCE_PREWARM:
            task_timeout = int(get_settings().get(
                "runtime", "guardrails", "prewarm_task_timeout_seconds", default=1800) or 1800)
        # ── 真取消(用户点「停止」)──
        # 两层感知:① 流式循环里逐 chunk 检查(token 流动期间,毫秒级感知);
        # ② 本 watcher 协程——LLM 长静默生成/工具 HTTP 卡住时没有任何 chunk 到来,
        #    只能靠它在 await 点直接 cancel 执行协程,打断正在进行的 LLM 交互。
        # watcher 自终止(执行协程结束后退出),无需 finally 回收。
        _partial_tokens: list[str] = []   # 已流出的部分回答(取消时落库,历史可见)
        _exec_task = asyncio.current_task()

        async def _watch_user_cancel() -> None:
            while True:
                if streaming.is_user_cancelled(task_id):
                    if _exec_task is not None and not _exec_task.done():
                        _exec_task.cancel()
                    return
                if _exec_task is None or _exec_task.done():
                    return
                await asyncio.sleep(0.5)

        _watch_cancel_task = asyncio.create_task(_watch_user_cancel())

        async def _salvage(result: dict) -> tuple[dict, dict | None]:
            """失败 / 取消收尾时,从最后一个检查点捞回**已经发生的**执行链和用量。

            以前这几条路径只落 {error, kind}:页面上实时看得见的几十步工具调用、
            三十万 token 的消耗,收尾后一条都不剩 —— 导出执行详情写着「整轮没有调用任何工具」
            「没有用量记录」(现场任务 f34e0686:跑了 9 分半、100 次工具调用后熔断),
            而且**这部分消耗从没计入成本账**。检查点在每个节点结束时都已落好,直接读回来即可。
            读不到(检查点存储挂了)就原样返回,不能让收尾因此失败。
            """
            try:
                snap = await asyncio.wait_for(graph.aget_state(config), timeout=_FINALIZE_STEP_TIMEOUT)
                st = (snap.values if snap else None) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning(f"task {task_id} 收尾取检查点失败,执行链/用量无法补录:{exc!r}")
                return result, None
            tl = _with_step_durations((st.get("timeline") or [])[timeline_base:])
            gr_state = st.get("guardrail") or {}
            pt, ct = int(gr_state.get("prompt_tokens", 0) or 0), int(gr_state.get("completion_tokens", 0) or 0)
            out = {**result, "timeline": tl} if tl else dict(result)
            if pt or ct:
                out["usage"] = {**(out.get("usage") or {}), "prompt_tokens": pt, "completion_tokens": ct,
                                "total_tokens": pt + ct}
            usage_ev = None if not (pt or ct) else {
                "scenic_id": scenic_id or "", "user_id": user_id or "", "task_id": task_id,
                "agent_id": task.executor_id if task.executor_type == "agent" else None,
                "model": spec.model_override or "", "prompt_tokens": pt, "completion_tokens": ct,
            }
            return out, usage_ev

        try:
            async with asyncio.timeout(task_timeout):
                async for stream_mode, chunk in graph.astream(graph_input, config=config, stream_mode=["custom", "updates"]):
                    if streaming.is_user_cancelled(task_id):
                        raise UserCancelled(task_id)
                    if stream_mode == "custom":
                        # v1.1.2:agent_node 在流式开始前发的 generation 打点事件,与 token
                        # 增量分流——打点进 timeline(前端「内容生成中…」节点;同时 _publish
                        # → mark_progress 完成活性登记,长生成期间心跳持续续租),token 维持
                        # 打字机输出。
                        if isinstance(chunk, dict) and chunk.get("type") == "generation_start":
                            if streaming.STAGE_GENERATING not in _stage_published:
                                _stage_published.add(streaming.STAGE_GENERATING)
                                await _publish(task_id, "stage", {
                                    "stage": streaming.STAGE_GENERATING, "text": "正在生成回答…"})
                            item = {k: v for k, v in chunk.items() if k != "type"}
                            if "duration" not in item:
                                _item_ts = item.get("ts")
                                if _item_ts is not None:
                                    item = {**item, "duration": round(max(0.0, _item_ts - _timeline_ts) * 1000, 1)}
                                    _timeline_ts = _item_ts
                                else:
                                    _now = time.monotonic()
                                    item = {**item, "duration": round((_now - _timeline_ts) * 1000, 1)}
                                    _timeline_ts = _now
                            await _publish(task_id, "timeline", item)
                            continue
                        # 思考流(30 号):推理模型的 reasoning_content 增量——与 token
                        # 同构打字机,但语义是「模型正在思考」;走 _publish 自动刷新
                        # 心跳活性(长思考期间 90s 无进展巡检不会误回收)。不进历史。
                        if isinstance(chunk, dict) and chunk.get("type") == "reasoning":
                            if streaming.STAGE_GENERATING not in _stage_published:
                                _stage_published.add(streaming.STAGE_GENERATING)
                                await _publish(task_id, "stage", {
                                    "stage": streaming.STAGE_GENERATING, "text": "正在思考…"})
                            await _publish(task_id, "reasoning", chunk)
                            continue
                        if streaming.STAGE_GENERATING not in _stage_published:
                            _stage_published.add(streaming.STAGE_GENERATING)
                            await _publish(task_id, "stage", {
                                "stage": streaming.STAGE_GENERATING, "text": "正在生成回答…"})
                        if isinstance(chunk, str):
                            _partial_tokens.append(chunk)
                        await _publish(task_id, "token", chunk)
                        continue
                    if "__interrupt__" in chunk:
                        interrupted_payload = chunk["__interrupt__"][0].value
                        continue
                    for _node_name, delta in chunk.items():
                        if not isinstance(delta, dict):
                            continue
                        for item in delta.get("timeline") or []:
                            # 检测 querying 阶段:出现工具调用/知识检索步骤时
                            if streaming.STAGE_QUERYING not in _stage_published:
                                step = item.get("step") or ""
                                if step in _QUERYING_STAGE_TOOL_STEPS:
                                    _stage_published.add(streaming.STAGE_QUERYING)
                                    await _publish(task_id, "stage", {
                                        "stage": streaming.STAGE_QUERYING, "text": "正在查询相关知识…"})
                            # 每步耗时:按事件真实 ts 相邻间隔(毫秒),与落库 timeline 口径一致。
                            # 用副本避免污染 checkpoint 里的 timeline;无 ts 的旧事件回退遍历近似。
                            if isinstance(item, dict) and "duration" not in item:
                                _item_ts = item.get("ts")
                                if _item_ts is not None:
                                    item = {**item, "duration": round(max(0.0, _item_ts - _timeline_ts) * 1000, 1)}
                                    _timeline_ts = _item_ts
                                else:
                                    _now = time.monotonic()
                                    item = {**item, "duration": round((_now - _timeline_ts) * 1000, 1)}
                                    _timeline_ts = _now
                            await _publish(task_id, "timeline", item)
                        if delta.get("usage"):
                            await _publish(task_id, "usage", delta["usage"])
        except asyncio.CancelledError:
            # watcher 感知到用户取消标记后 cancel 了本协程——在当前 await 点打断
            # 正在进行的 LLM 交互/工具调用,转用户取消收尾。其它来源的取消
            # (进程停机/任务超时的内部取消已被 timeout 上下文转换)原样上抛。
            if streaming.is_user_cancelled(task_id):
                raise UserCancelled(task_id) from None
            raise
        except UserCancelled:
            # 用户主动停止:终态 CANCELLED,带上已流出的部分回答——下次进会话,
            # 历史消息里这一轮显示为「已停止」+ 部分内容,而不是凭空消失或显示完成。
            partial_answer = _sanitize_answer("".join(_partial_tokens).strip())
            log.info(f"task {task_id} 用户主动取消(已生成 {len(partial_answer)} 字)")
            _res, _uev = await _salvage({
                "answer": partial_answer, "cancelled": True, "reason": "用户主动停止"})
            await _finalize_task(task_id, "CANCELLED", _res, usage_event=_uev)
            # cancelled 终态携带权威部分回答(与落库同口径):实时/断线兜底前端都能
            # 呈现「已停止生成 + answer(半截)」,不依赖本地已渲染 token。
            await _publish(task_id, "cancelled", {"message": "任务已取消", "answer": partial_answer})
            return True
        except ApprovalRejected as exc:
            await _finalize_task(task_id, "CANCELLED", {"reason": str(exc)})
            await _publish(task_id, "cancelled", {"message": str(exc)})
            return True
        except ClarificationNeeded as exc:
            # 执行中发现缺一个只有用户能给的输入(集团账号没指定景区):收尾成一个问句。
            # 结果形状与 Triager clarify 完全一致(needs_clarification=True、answer=问句),
            # 两个前端现成就会把它当成一条普通回答显示,不需要改前端。
            # 状态记 COMPLETED 而不是 FAILED —— 这一轮没有出错,它在等用户补一句话;
            # 记成失败会进告警、进失败率,还会让前端显示红字。
            question = exc.question
            if exc.slot == "scenic_id":
                try:
                    from cogni_agent.gateway_service import scenic_binding as _sb

                    async with get_sessionmaker()() as _cdb:
                        _names = await _sb.scenic_choices(_cdb)
                    if _names:
                        question += "\n\n可选的景区有:" + "、".join(_names) + (
                            " 等" if len(_names) >= 8 else "")
                except Exception:  # noqa: BLE001 - 取不到候选列表也照样要问
                    log.warning(f"task {task_id} 取景区候选失败,澄清问句不带候选")
            log.info(f"task {task_id} 需要澄清({exc.slot}):{exc.detail}")
            result = {
                "answer": question,
                "timeline": [{"step": "intent_check", "action": "clarify",
                              "reason": exc.detail, "missing": [exc.slot] if exc.slot else [],
                              "degraded": False}],
                "needs_clarification": True,
                "clarify_slot": exc.slot,
            }
            if task.conversation_id:
                from cogni_agent.control_plane_service import conversations as conv_svc

                await conv_svc.bump_turn(task.conversation_id)
            await _finalize_task(task_id, "COMPLETED", result)
            await _publish(task_id, "done", result)
            return True
        except GuardrailTripped as exc:
            _res, _uev = await _salvage({"error": str(exc), "kind": TaskErrorKind.GUARDRAIL_TRIPPED})
            await _finalize_task(task_id, "FAILED", _res, usage_event=_uev)
            await _publish(task_id, "error", task_error(TaskErrorKind.GUARDRAIL_TRIPPED, message=str(exc)))
            return True
        except OutputBlocked as exc:
            # 输出侧内容审核拦截:任务判失败,并且**不把被拦截的内容回传给前端**——
            # 否则拦截就失去意义了。同时记一条审计事件供合规追溯。
            log.warning(f"task {task_id} 输出被内容审核拦截:{exc}")
            # 被拦的是回答内容;执行链与用量照常补录(不含回答文本)
            _res, _uev = await _salvage({"error": str(exc), "kind": TaskErrorKind.OUTPUT_BLOCKED})
            await _finalize_task(task_id, "FAILED", _res, usage_event=_uev)
            async with get_sessionmaker()() as audit_db:
                await emit_event(
                    audit_db, event_type="com.zhilv.moderation.output_blocked",
                    scenic_id=scenic_id or "", data={"task_id": task_id, "reason": str(exc)},
                )
            await _publish(task_id, "error", task_error(
                TaskErrorKind.OUTPUT_BLOCKED, message="生成内容未通过安全审核,已拦截"))
            return True
        except TimeoutError:
            # asyncio.timeout 超时抛 TimeoutError。必须排在通用 except Exception 之前,
            # 否则会被后者吞掉,错误信息就变成一句没头没尾的空字符串。
            msg = (f"任务执行超过 {task_timeout} 秒仍未完成,已强制终止。"
                   f"常见原因:模型服务地址不可达 / 响应过慢,或某个工具接口卡住。"
                   f"请到「系统设置 · 模型与预算」检查模型地址与 API Key。")
            log.error(f"task {task_id} 超时({task_timeout}s),已强制终止")
            _res, _uev = await _salvage({"error": msg, "kind": TaskErrorKind.TASK_TIMEOUT})
            await _finalize_task(task_id, "FAILED", _res, usage_event=_uev)
            await _publish(task_id, "error", task_error(TaskErrorKind.TASK_TIMEOUT, message=msg))
            return True
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都不应让 worker 循环挂掉
            # str(exc) 可能带内部细节(异常类名/任务ID/连接串片段),不直接出网:
            # 详细堆栈进服务端日志(log.exception),对外只给稳定文案 + trace 侧可查的任务ID。
            log.exception(f"task {task_id} 执行异常: {exc!r}")
            _res, _uev = await _salvage({
                "error": "任务执行遇到内部错误，请稍后重试；若持续失败请联系管理员并提供任务 ID。",
                "kind": TaskErrorKind.UNEXPECTED_ERROR})
            await _finalize_task(task_id, "FAILED", _res, usage_event=_uev)
            await _publish(task_id, "error", task_error(
                TaskErrorKind.UNEXPECTED_ERROR,
                message="任务执行遇到内部错误，请稍后重试；若持续失败请联系管理员。"))
            return True

        if interrupted_payload is not None:
            # v1.1.1:挂起期间不再提前脱敏 payload.message——脱敏锚点后移到
            # 「保存凭证成功/用户忽略」(mark_handled → pop_payload_mask,收尾统一处理)。
            # 否则保存失败(format_error)时模型看不到原文无法自纠(框架能力测试 01 文档);
            # 挂起中的明文仅存于内存缓存,确认/忽略后由 _execute_credential_save 落库或清理。
            if interrupted_payload.get("tool_type") == "credential_save":
                # E 方案(v0.1.5):凭证保存确认从「管理员审批中心」改为「对话内用户确认」——
                # 不落 gov_approvals,改向对话 SSE 流发 credential_confirm 事件(不含明文),
                # 用户点「确认保存/忽略」后经 POST /admin/chat/{task_id}/credential-confirm
                # 恢复任务:approved→_execute_credential_save 取回明文落库;
                # 未批准→tools_adapter 跳过保存并清理明文缓存。
                _payload = dict(task.payload or {})
                _credential_confirm = {
                    "task_id": task_id,
                    "tool_id": interrupted_payload.get("tool_id"),
                    "tool_name": interrupted_payload.get("tool_name"),
                    "target_tool_id": (interrupted_payload.get("params") or {}).get("target_tool_id"),
                    "scope": (interrupted_payload.get("params") or {}).get("scope"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                _payload["_credential_confirm"] = _credential_confirm
                task.payload = _payload
                task.status = "WAITING_HUMAN"
                task.version = (task.version or 0) + 1
                await db.commit()
                await _publish(task_id, "credential_confirm", _credential_confirm)
                log.info(f"task {task_id} 等待对话内用户确认保存凭证: {_credential_confirm.get('tool_name')}")
                return False  # 不关流:确认后同一 task_id 会继续产出事件
            approval_id = await hitl.request_approval(task_id, scenic_id, user_id, interrupted_payload)
            await _publish(task_id, "waiting_human", {"approval_id": approval_id, **interrupted_payload})
            return False  # 不关流:审批恢复后同一个 task_id 会继续产出事件

        final_snapshot = await _guarded(task_id, "final_state", graph.aget_state(config))
        final_state = final_snapshot.values
        # 只保留本轮新增的时间线(多轮会话下 timeline 是整个 thread 累加的)
        # 收尾计划:analyze / compose 这类不是工具调用,不会在 tools 节点被回写。

        # 不处理的话界面会一直停在「2/4 步」,而任务其实早就做完了。

        _final_plan = planner.ExecutionPlan.from_dict(final_state.get("exec_plan"))

        if _final_plan.steps and planner.finalize(_final_plan):

            await streaming.publish(task_id, "timeline",

                                    {"step": "plan_update", "plan": _final_plan.as_dict()})
        turn_timeline = (final_state.get("timeline") or [])[timeline_base:]
        # 每步耗时:收尾统一重算(相邻事件间隔,毫秒),保证落库回放与 SSE 展示一致。
        # checkpoint 里的 timeline 保持原样(不写入 duration,避免污染历史数据)。
        turn_timeline = _with_step_durations(turn_timeline)
        # v1.1.2:整轮执行总耗时 = 执行分析开始/结束两个时刻相减(墙钟真实耗时,毫秒),
        # 随 done 事件下发,前端「执行链分析」汇总展示。不用事件 duration 累加——时间线
        # 可能被裁剪(如多技能场景的部分 info 提示不展示)、事件也不保证完整,累加不可靠。
        total_duration_ms = int((time.monotonic() - _exec_t0) * 1000)
        # v1.1 智能问数 Phase B:解析 final_answer 中的结构化块,剥离后作为纯文本展示,
        # 结构化数据(cards/charts/insight/recommend)在 done 前广播为独立 SSE 事件。
        raw_answer = _extract_final_answer(final_state)
        # 收尾轮模型没产出内容时兜底——任务 249488c8 的教训:工具取数失败被摘除、
        # 步数耗尽后模型输出空,任务却 COMPLETED,用户看到一片空白(成本照扣)。
        # 宁可给一句「没取到 + 原因」,也不能留空(与 no_fabrication 同源:
        # 不编造数字,但必须交代发生了什么)。
        parsed_output = parse_structured_output(raw_answer)
        # sys_chart 在服务端配好的图并进来一起广播。
        # 两条来源是互补的:模型自己写 ```json``` 结构化块(它得把数字重抄一遍),
        # 和 sys_chart 直接从留存数据里取数配图(数字不经模型的手)。
        # 后者放在前面 —— 它的数一定和取回来的那份一致,该先给用户看。
        _tool_charts = _charts_from_tools(task_id)
        if _tool_charts:
            parsed_output.charts = _tool_charts + list(parsed_output.charts or [])
        # 剥离模型误输出的工具调用文本原语(<|FunctionCallBegin|>...):resolver.py 已记录
        # 泄漏事故,此处对最终 answer 统一清洗后再落库/广播(历史与 done 事件同口径)。
        answer = _sanitize_answer(parsed_output.answer)
        # 兜底判空必须放在 _sanitize_answer **之后**:任务 5f906283 里模型末轮输出的正是
        # 工具调用原语,raw_answer 非空(旧判空被绕过)却被剥离成空串 -> COMPLETED 但用户
        # 看到一片空白。按清洗后的文本判空,这条路径才兜得住。
        if not answer.strip():
            answer = _fallback_answer(turn_timeline)
        guardrail = final_state.get("guardrail") or {}

        prompt_tokens = guardrail.get("prompt_tokens", 0)
        completion_tokens = guardrail.get("completion_tokens", 0)
        # 计费后置(与行业主流一致):真实成本由 cost_subscriber 在订阅 usage 事件时
        # 计算落库(见 governance_event_bus_service/cost.py)。收尾只做纯内存估算供前端
        # 展示——复用 _model_cfg 的 30s TTL 配置缓存(任务执行中必然已命中,零 DB),
        # 单价解析只读进程内缓存,绝不触发计费相关表查询。
        cost_cny = await _guarded(task_id, "cost_compute", _estimate_cost_cny(spec.model_override or "", prompt_tokens, completion_tokens))
        # 模型/网关在响应里自带的成本(USD)→CNY(纯内存换算,非计价)。模型没返回计费
        # 时保持 None,前端据此只展示本地估算、不渲染"模型侧费用"对比。
        from cogni_agent.runtime_service.llm.cost import usd_to_cny

        model_cost_usd = guardrail.get("model_cost_usd") or 0.0
        model_cost_cny = round(usd_to_cny(model_cost_usd), 4) if model_cost_usd else None
        # 按模型归集的计费返回明细:calls/returned_cost/missing 让前端能提示
        # "某模型 N 次调用未返回对方计费字段",与本地估算的差异来源一目了然。
        from cogni_agent.runtime_service.llm.client import _model_cfg

        cost_status_items = sorted((guardrail.get("model_cost_status") or {}).items())
        # model_cost_status 的 key 是不可变的模型清单 id(对用户不可读),这里为每个 key
        # 解析出可读展示名(db 清单的 name;静态配置 key 即 name)。解析失败(如模型已被
        # 从清单删除)时用 key 兜底,不阻塞收尾。
        model_display_names: dict[str, str] = {}
        for mk, _info in cost_status_items:
            try:
                _key, cfg = await _guarded(task_id, "model_cfg", _model_cfg(mk))
                model_display_names[mk] = cfg.get("name") or _key
            except _FinalizeStepTimeout:
                raise  # 超时=卡死,必须中断收尾,不能被这里的兜底吞掉
            except Exception:  # noqa: BLE001 - 单个模型解析失败不阻塞收尾(_guarded 超时已整体兜底)
                model_display_names[mk] = mk
        cost_report = [
            {
                "model": mk,
                "name": model_display_names.get(mk, mk),
                "calls": info.get("calls", 0),
                "returned_cost": info.get("returned_cost", 0),
                "missing": info.get("calls", 0) - info.get("returned_cost", 0),
                "cost_usd": round(info.get("cost_usd", 0.0), 6),
                "cost_cny": round(usd_to_cny(info.get("cost_usd", 0.0)), 4) if info.get("cost_usd") else None,
            }
            for mk, info in cost_status_items
        ]

        # 引用溯源:从 timeline 聚合本次任务所有来源明细(带 [n] 编号),与回答里
        # [n] 标注一一对应,随 done 事件下发,前端渲染引用列表供用户核验。
        # 两类来源:knowledge_retrieval(知识检索块)与 tool_call(工具调用结果,
        # 来源=工具展示名,type=tool_call)——工具型链路同样做到来源可追溯。
        citations: list[dict] = []
        for ev in turn_timeline or []:
            if isinstance(ev, dict) and ev.get("step") == "knowledge_retrieval":
                citations.extend(ev.get("sources") or [])
            elif isinstance(ev, dict) and ev.get("step") == "tool_call" and ev.get("source_type") == "tool_call":
                tool_label = ev.get("tool_display_name") or ev.get("tool_name") or "工具调用"
                citations.append({
                    "ref": f"[{tool_label}]",
                    "source": tool_label,
                    "type": "tool_call",
                    "url": "",
                })
        seen_cites: set[tuple[str, str, str]] = set()
        dedup_citations: list[dict] = []
        for c in citations:
            key = (str(c.get("ref", "")), str(c.get("source", "")), str(c.get("type", "")))
            if key in seen_cites:
                continue
            seen_cites.add(key)
            dedup_citations.append(c)

        result = {
            "answer": answer,
            "mode": final_state.get("mode"),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_cny": cost_cny,
                "model_cost_cny": model_cost_cny,
                "cost_report": cost_report,
                # v1.1.2:整轮执行总耗时(毫秒),前端直接展示,无需前端累加。
                "total_duration_ms": total_duration_ms,
                # 本轮**实际生效**的 Token 预算(0 = 不限制)。必须由后端下发:
                # 前端自己猜一个数画进度条,就会出现「17,587 / 8,000 接近上限」
                # 这种把人吓一跳、实际根本没超的假告警。来源见 spec.token_budget。
                "token_budget": gr.resolve_token_budget(spec.token_budget),
            },
            "timeline": turn_timeline,
            "citations": dedup_citations,
        }
        # v1.1 智能问数 Phase B:结构化结果随 result 落库 + done 事件下发。
        # SSE 结构化事件(cards/chart/insight/recommend)是瞬态流、不持久化,会话历史与
        # 断线重连无法回放图表;结构化并入 task.result 后,历史接口(list_turns /
        # list_turns_page)据此透出,APP 上滑加载历史可完整回放卡片/图表/解读/关联推荐。
        if parsed_output.cards or parsed_output.charts or parsed_output.insight or parsed_output.recommends:
            result["structured"] = {
                "cards": parsed_output.cards or None,
                "charts": parsed_output.charts,
                "insight": parsed_output.insight,
                "recommends": parsed_output.recommends or None,
            }
        usage_event = {
            "scenic_id": scenic_id or "",
            "user_id": user_id or "",
            "task_id": task_id,
            "agent_id": task.executor_id if task.executor_type == "agent" else None,
            "model": spec.model_override or "",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            # 计费后置:事件只带原始事实(token/模型/USD 明细),不携带算好的 cost_cny,
            # 真实成本由 cost_subscriber 按最新单价计算落库(见 governance_event_bus_service/cost.py)。
            "cost_report": cost_report,
        }
        if task.conversation_id:
            # 只在成功完成时计数:失败/取消的轮次不该让会话看起来"聊了很多"
            from cogni_agent.control_plane_service import conversations as conv_svc

            await _guarded(task_id, "bump_turn", conv_svc.bump_turn(task.conversation_id))
        await _guarded(task_id, "finalize", _finalize_task(task_id, "COMPLETED", result, usage_event=usage_event))
        # v1.1 智能问数 Phase B:结构化事件(cards→chart→insight→recommend)在 done 前广播。
        # 每个事件独立超时兜底,失败不阻塞 done(前端刷新仍能看到完整回答)。
        await _publish_structured_events(task_id, parsed_output)
        # done 事件:结果已落库 COMPLETED,这里即使超时(Redis 不可用)也不该把任务标失败,
        # 只记日志——前端刷新即可看到已完成的回答。
        try:
            await asyncio.wait_for(_publish(task_id, "done", result), timeout=_FINALIZE_STEP_TIMEOUT)
        except TimeoutError:
            log.error(f"task {task_id} done 事件发布超时(Redis 不可用?),结果已落库,前端刷新即可看到")
        return True


async def _estimate_cost_cny(model_key: str, prompt_tokens: int, completion_tokens: int) -> float:
    """纯内存成本估算(计费后置后收尾只展示估算值)。

    复用 _model_cfg 的 30s TTL 配置缓存解析模型配置(任务执行中必然已命中,零 DB
    查询),单价解析只读进程内单价缓存;真实成本由 cost_subscriber 在订阅 usage
    事件时按最新单价计算落库(见 governance_event_bus_service/cost.py),二者允许
    估算与账单的微小差异(行业惯例:前端展示估算,账单后置)。
    """
    cfg: dict | None = None
    model_id = model_key or ""
    try:
        if model_key:
            from cogni_agent.runtime_service.llm.client import _model_cfg

            _key, cfg = await _model_cfg(model_key)
            model_id = (cfg or {}).get("model_id") or model_key
    except Exception as exc:  # noqa: BLE001 - 估算兜底不因模型配置解析失败而中断
        log.warning(f"cost 估算:解析模型配置失败({model_key}): {exc}")
    from cogni_agent.runtime_service.llm.cost import estimate_cost_cny

    return estimate_cost_cny(model_id, prompt_tokens, completion_tokens, cfg)


def _extract_final_answer(state: dict) -> str:
    if state.get("final_answer"):
        return state["final_answer"]
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage) and message.content:
            return message.content
    return ""


def _fallback_answer(timeline: list[dict]) -> str:
    """收尾轮模型没产出内容时的兜底文案:按时间线给出可读归因。

    空回答是比报错更糟的体验——任务 COMPLETED、成本照扣,而用户看到一片空白
    (任务 249488c8:工具取数失败被摘除、步数耗尽,4 轮 generation 273s 后零输出)。
    这里不编造任何数字,只交代「没取到 + 为什么」。
    """
    disabled = sorted({
        str(t.get("tool_display_name") or t.get("tool_name") or "工具")
        for t in (timeline or []) if t.get("step") == "tool_disabled"
    })
    failed = sorted({
        str(t.get("tool_display_name") or t.get("tool_name") or "工具")
        for t in (timeline or []) if t.get("step") == "tool_call" and t.get("error")
    })
    if disabled or failed:
        names = "、".join((disabled or failed)[:3])
        return (
            f"本次没能取到有效数据:取数工具「{names}」在本次任务中未能返回可用结果,"
            f"已自动停止重试以避免无效等待。请稍后重试;若持续失败,请核查该数据接口"
            f"的可用性与参数配置。"
        )
    return "本次未能生成有效回答(模型未返回内容)。请重试,或换个问法再试一次。"


async def _publish_structured_events(task_id: str, structured: StructuredOutput) -> None:
    """v1.1 智能问数 Phase B:发布结构化问数事件(cards → chart → insight → recommend)。

    在 done 之前按「数据卡片 → 图表 → 解读 → 关联推荐」顺序广播,APP 端据此渲染
    卡片与图表。单个事件发布超时/失败只记日志,由 done 事件兜底,不阻塞收尾。
    """

    async def _pub(event: str, data: dict) -> None:
        try:
            await asyncio.wait_for(_publish(task_id, event, data), timeout=_FINALIZE_STEP_TIMEOUT)
        except TimeoutError:
            log.warning(f"task {task_id} 事件 {event} 发布超时,跳过(Redis 不可用?)")

    if structured.cards:
        await _pub(streaming.EVENT_CARDS, {"items": structured.cards})
    for chart in structured.charts:
        await _pub(streaming.EVENT_CHART, chart)
    if structured.insight:
        await _pub(streaming.EVENT_INSIGHT, structured.insight)
    if structured.recommends:
        await _pub(streaming.EVENT_RECOMMEND, {"items": structured.recommends})


def _charts_from_tools(task_id: str) -> list[dict]:
    """取出本任务里 sys_chart 配好的图。取不到不影响收尾 —— 最坏是少一张图。"""
    try:
        from cogni_agent.runtime_service.engine import databuf

        return databuf.charts(task_id)
    except Exception:                       # noqa: BLE001
        return []


def _clear_task_datasets(task_id: str) -> None:
    """清掉本任务留存的数据集。清不掉也不能连累收尾 —— 收尾失败比留一份内存严重得多。"""
    try:
        from cogni_agent.runtime_service.engine import databuf

        databuf.clear(task_id)
    except Exception:                       # noqa: BLE001
        pass


async def _finalize_task(task_id: str, status: str, result: dict, *, usage_event: dict | None = None) -> None:
    # 任务留存的那几份明细数据(databuf)到此为止:它是业务明细,不该在内存里留过夜。
    # 放在最前面 —— 下面任何一条早退路径都不该把它漏掉(databuf 自己有 TTL 兜底,
    # 但那是兜底,不是清理时机)。
    _clear_task_datasets(task_id)
    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task is None:
            credential_guard.cleanup_task(task_id)
            return
        # §10.3 动作3(v1.1.1):保存凭证**成功 / 用户忽略**后 → Task.payload.message 同步脱敏。
        # 锚点后移(不再以"模型发出调用"为准):失败(format_error/error)路径保留原文,
        # 模型在对话内可自纠;成功路径在 _execute_credential_save 已 mark_handled,
        # 收尾时 pop_payload_mask 消费,回放接口 list_turns 只见脱敏占位。
        if credential_guard.pop_payload_mask(task_id):
            payload = dict(task.payload or {})
            if payload.get("message"):
                payload["message"] = credential_guard.PLACEHOLDER_MESSAGE
                task.payload = payload
        # 真取消终态保护:cancel 接口已把状态置 CANCELLED 后,执行协程若仍自然跑完
        # (取消标记未及时被感知的竞态窗口),不允许把终态改回 COMPLETED/FAILED——
        # 否则「点了停止,历史里却显示已完成」。保留 CANCELLED,仅补写 result(部分
        # 回答)供会话历史回看。
        if task.status == "CANCELLED" and status != "CANCELLED":
            log.info(f"task {task_id} 收尾时发现已被用户取消,保留 CANCELLED 终态,仅补写部分结果")
            result = {**result, "cancelled": True, "reason": "用户主动停止"}
            # 取消终态不携带 error:取消是正常业务终态,异常串/内部ID 不该随历史回看出网
            result.pop("error", None)
            task.result = result
        else:
            task.status = status
            task.result = result
        task.version = (task.version or 0) + 1
        if usage_event:
            await emit_event(db, event_type="com.zhilv.llm.usage", scenic_id=usage_event.get("scenic_id", ""), data=usage_event)
        await emit_event(db, event_type="com.zhilv.task.state_changed", scenic_id=task.scenic_id or "", data={"task_id": task_id, "to": task.status})
        await db.commit()
        # 任务终结:清理该任务的明文缓存与触发标志(WAITING_HUMAN 不终结,不清理,
        # 否则审批恢复后取不到明文)。用户取消标记同步清理(终态已落,watcher 已无意义)。
        credential_guard.cleanup_task(task_id)
        streaming.clear_user_cancelled(task_id)



