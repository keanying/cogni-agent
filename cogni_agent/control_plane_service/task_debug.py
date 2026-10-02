# -*- coding: utf-8 -*-
"""一次智能体执行的完整诊断包(非 Quick BI 链路)。

做给「跑出问题了,把执行过程整个发给研发」这件事用的 —— Quick BI 那边早就有
`/runs/{id}/debug`,自己这条链一直没有,排障只能靠截图,而截图里恰恰缺最要紧的东西:
**每一步的提示词、工具入参、结果摘要**。

现场撞过一次典型的:分诊判对了(数据查询 → we),也列出了缺失参数 period/start/end,
然后一个工具都没调就去生成文字。光看界面只知道「它没查数」,看不出是谁没接这一棒。
这份导出把六段拼在一起,一次说清:

  1. 任务本身      问的是什么、谁问的、哪个执行目标、最后什么状态
  2. 版本溯源      跑的是哪个 Agent / Skill 的哪一版(跑错版本是最难自查的一类)
  3. 执行链时间线  意图分诊 → 模式判定 → 工具调用(入参/耗时/结果摘要)→ 生成
  4. 提示词        每一步真正发给模型的 system + history
  5. 用量与成本    按步拆开,能看出是哪一步吃掉了上下文
  6. 最终回答

**凭证不进导出**:这份东西本来就是要发出去的。脱敏复用 Quick BI 那套三道防线
(键名 / 文本里的裸露写法 / 已知凭证字面值),不另写一份 —— 两份脱敏迟早有一份漏。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from cogni_agent.control_plane_service.models import ACTIVE_STATUSES
from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import NotFound

# 步骤的中文名。界面上叫什么,导出里就叫什么 —— 两边对不上时,
# 人拿着导出去对界面会先怀疑自己看错了。
STEP_LABELS = {
    "version_trace": "版本溯源", "intent_check": "意图分诊", "mode_decision": "模式判定",
    "orchestration_plan": "编排计划", "tool_call": "工具调用", "tool_call_skipped": "工具跳过",
    "tool_disabled": "工具摘除", "stall_finalize": "空转收尾", "knowledge_retrieval": "知识检索", "knowledge_search": "知识检索",
    "generation": "内容生成", "drift_check": "目标漂移自检", "guardrail": "护栏",
    "approval": "人工审批", "error": "错误",
}


def _secret_literals() -> set[str]:
    """已知的凭证字面值 —— 脱敏的第三道。

    只靠键名和正则会漏:有些配置把密钥放在看不出是密钥的字段里。
    """
    from cogni_agent.config import get_settings

    lits: set[str] = set()
    s = get_settings()
    for path in (("quickbi", "api_key"), ("quickbi", "api_secret"), ("quickbi", "user_token"),
                 ("tool_center", "builtin", "quickbi_link", "api_key"),
                 ("user_center", "jwt_secret")):
        try:
            v = s.get(*path, default="")
        except Exception:  # noqa: BLE001 - 取不到就算了,不能因为脱敏取值失败就不导出
            v = ""
        if isinstance(v, str) and len(v) >= 6:
            lits.add(v)
    return lits


def _mask(value: Any) -> Any:
    from cogni_agent.tool_center_service.quickbi.trace import mask

    return mask(value, _secret_literals())



def _timing_breakdown(timeline: list[dict]) -> list[str]:
    """「时间花在哪」一张表:分诊 / 每次内容生成(排队·思考·吐字)/ 工具。

    执行链逐条看很难看出大头 —— 5877db72 一共 186 秒,四次内容生成占了 158 秒,
    而工具加起来不到 20 秒;不汇总的话,人会先去怀疑工具慢。
    并行的工具按「一批取最长」计,不然 4 个并发各 5 秒会被算成 20 秒。
    """
    rows: list[tuple[str, float, str]] = []
    gens = 0
    tool_ms = 0.0
    batch: list[float] = []

    def _flush():
        nonlocal tool_ms, batch
        if batch:
            tool_ms += max(batch)
            batch = []

    for e in timeline:
        step = e.get("step")
        llm = e.get("llm") if isinstance(e.get("llm"), dict) else None
        if step != "tool_call":
            _flush()
        if step == "drift_check" and llm and llm.get("total_ms"):
            rows.append(("目标漂移自检", float(llm["total_ms"]),
                         ("判定偏离:" + str(e.get("note") or "")[:40] + "(未拦截,照常执行)") if e.get("on_track") is False else "在轨"))
        elif step == "intent_check" and llm and llm.get("total_ms"):
            rows.append(("意图分诊", float(llm["total_ms"]), f"输入 {llm.get('prompt_tokens')} / 输出 {llm.get('completion_tokens')} tokens"))
        elif step == "generation" and llm and llm.get("total_ms"):
            gens += 1
            what = ("→ 调工具 " + "、".join(map(str, e.get("will_call_tools") or []))) if e.get("will_call_tools") else "→ 出回答"
            rows.append((f"内容生成 #{gens} {what}", float(llm["total_ms"]),
                         f"排队 {llm.get('queue_ms')} · 思考 {llm.get('think_ms')} · 吐字 {llm.get('output_ms')} ms;"
                         f"输入 {llm.get('prompt_tokens')} / 输出 {llm.get('completion_tokens')} tokens,"
                         f"思考 {llm.get('reasoning_chars')} 字,消息 {llm.get('messages')} 条"))
        elif step == "tool_call" and e.get("real_duration_ms") is not None:
            if e.get("parallel"):
                batch.append(float(e["real_duration_ms"]))
            else:
                _flush()
                tool_ms += float(e["real_duration_ms"])
    _flush()
    if tool_ms:
        rows.append(("工具调用(并行按批取最长)", tool_ms, ""))
    if not rows:
        return []
    # 首尾事件之间的真实跨度 - 已统计的 = 没被任何一步认领的时间(写库、排队、建图、审核……)
    ts = [float(e["ts"]) for e in timeline if isinstance(e.get("ts"), (int, float))]
    span = (max(ts) - min(ts)) * 1000 if len(ts) > 1 else 0.0
    counted = sum(r[1] for r in rows)
    if span > counted + 500:
        rows.append(("其它(步骤之间的空隙:写库 / 内容审核 / 建图等)", span - counted, "首尾事件跨度减去上面各项"))
    total = sum(r[1] for r in rows) or 1.0
    out = ["## 耗时拆解", "", "| 阶段 | 耗时(秒) | 占比 | 说明 |", "| --- | --- | --- | --- |"]
    for name, ms, note in rows:
        out.append(f"| {name} | {ms / 1000:.1f} | {ms / total * 100:.0f}% | {note} |")
    out.append("")
    out.append("_排队 = 发出请求到首个分片(上下文越长越大);思考 = 推理模型出正文前的思考;"
               "吐字 = 输出正文。老任务没有这些字段时只统计工具。_")
    out.append("")
    return out


def _step_details(timeline: list[dict]) -> list[str]:
    """逐步明细:每次内容生成的上下文构成、思考开头 / 结尾、决定调用的工具与参数;分诊的输入规模。

    执行链里每步只有一行,这里把能解释「为什么慢 / 为什么这么调」的东西全摊开。
    """
    out: list[str] = []
    n = 0
    for e in timeline:
        d = e.get("detail") if isinstance(e.get("detail"), dict) else None
        if not d:
            continue
        if e.get("step") == "intent_check":
            out += [f"### 意图分诊 · {e.get('wall_ms') or e.get('wall') or ''}", "",
                    f"- 问题 {d.get('goal_chars')} 字;带入会话历史 {d.get('history_rounds')} 轮({d.get('history_chars')} 字);"
                    f"可选技能 {d.get('skills_offered')} 个",
                    "- 问题改写:" + ("已采用" if d.get("rewritten_goal_used") else
                                   "改写成了空目标,已丢弃、改用用户原话" if d.get("rewritten_goal_rejected") else "未改写"), ""]
            continue
        if e.get("step") != "generation":
            continue
        n += 1
        llm = e.get("llm") or {}
        ctx = d.get("context") or {}
        out += [f"### 内容生成 #{n}(第 {d.get('round')} 步)· 开始 {d.get('started_at')} · 共 {d.get('wall_ms')} ms", ""]
        out.append(f"- 模型 {llm.get('model') or '-'};结束原因 {d.get('finish_reason') or '-'};可用工具 {d.get('tools_offered')} 个")
        out.append(f"- 耗时:排队 {llm.get('queue_ms')} / 思考 {llm.get('think_ms')} / 输出 {llm.get('output_ms')} ms")
        out.append(f"- 上下文:{llm.get('messages')} 条消息 {ctx.get('messages_by_role')},系统提示 {llm.get('system_chars')} 字,"
                   f"全部 {llm.get('prompt_chars')} 字;其中工具结果 {ctx.get('tool_result_chars')} 字;"
                   f"历史 {ctx.get('history_messages')} 条(截短 {ctx.get('history_answers_clipped')} 条)")
        out.append(f"- 输入 {llm.get('prompt_tokens')} / 输出 {llm.get('completion_tokens')} tokens;思考 {llm.get('reasoning_chars')} 字,正文 {llm.get('output_chars')} 字")
        if d.get("tool_calls"):
            out.append("- 决定调用:")
            for tc in d["tool_calls"]:
                out.append(f"    - `{tc.get('name')}` {tc.get('args')}")
        if d.get("reasoning_head"):
            out += ["", "思考(开头):", "", "```text", d["reasoning_head"], "```"]
            if d.get("reasoning_tail"):
                out += ["", "思考(结尾):", "", "```text", d["reasoning_tail"], "```"]
        if d.get("output_preview"):
            out += ["", "输出(开头):", "", "```text", d["output_preview"], "```"]
        out.append("")
    return (["## 逐步明细", ""] + out) if out else []


def _capabilities_from_prompts(prompts) -> dict:
    """从分诊提示词里把「能力清单」那一段抠出来。

    它不单独落库,但模型确实看到了它 —— 而「模型到底有哪些工具可用」是排障的第一问。
    抠不出来就返回空,不猜。
    """
    import json as _json
    import re as _re

    for p in prompts:
        try:
            hist = _json.loads(p.history or "[]")
        except (ValueError, TypeError):
            continue
        for msg in hist if isinstance(hist, list) else []:
            text = (msg or {}).get("content") or ""
            if "当前智能体的能力清单" not in text:
                continue
            block = text.split("当前智能体的能力清单:", 1)[1]
            block = block.split("已收集的上下文参数", 1)[0].strip()
            skills = _re.findall(r"^- (\S+):", block, _re.M)
            tools: list[str] = []
            for line in block.splitlines():
                if "可用工具:" in line:
                    tools += [t.strip() for t in line.split("可用工具:", 1)[1].split(",") if t.strip()]
            return {"raw": block[:4000], "skills": skills, "tools": sorted(set(tools))}
    return {}


# 系统内置工具:它们每个技能都有(resolver 统一注入),所以「只剩这些」
# 等于「一个业务工具都没挂上」。
_BUILTIN_PREFIX = "sys_"


def _self_checks(timeline: list, prompts, usage, result: dict, status: str = "") -> list[dict]:
    """几条一眼能看出的问题。**只报能确证的**,不猜。

    导出是给人读的。把线索摆在最前面,比让人从几百行里自己找强 ——
    尤其这份常常是转给别人看的,对方没有上下文。

    **任务还没跑完时,大半条目根本不能判。** timeline / usage / answer 都是收尾时
    才落库的,执行中导出一律是空的 —— 而空的会让下面这几条同时报「整轮没有调用任何工具」
    「没有生成步骤也没有回答」「没有用量记录」。转发出去的人只会得到一个结论:
    这任务彻底坏了。实际上它正跑得好好的,已经调了十几次工具。
    所以在终态之前,这些依赖完整执行链的判断**一条都不出**,只说明「还在跑」。
    """
    out: list[dict] = []
    running = (status or "").upper() in ACTIVE_STATUSES
    if running:
        out.append({"level": "info", "title": f"任务还在执行中(状态 {status})",
                    "detail": "执行链、用量、最终回答都是**收尾时**才落库的,这会儿导出必然是空的 ——"
                              "这不代表任务有问题。要看实时过程请回操练台,那边走的是 SSE 流;"
                              "等任务结束后再导出一次,这份文件才是完整的。"})
        return out
    steps = [e.get("step") for e in timeline]
    intent_ev = next((e for e in timeline if e.get("step") == "intent_check"), {})

    caps = _capabilities_from_prompts(prompts)
    tools = caps.get("tools") or []
    if tools and all(t.startswith(_BUILTIN_PREFIX) for t in tools):
        out.append({"level": "error", "title": "本次没有任何业务工具可用",
                    "detail": f"能力清单里只有系统内置工具({'、'.join(tools)})—— 它们每个技能都有。"
                              "也就是说这个技能绑定的业务工具一个都没挂上:要么没绑,"
                              "要么绑了但被跳过(未发布 / pin 的版本下线 / 对当前账号不可见)。"
                              "没有取数工具,模型只能凭上下文说话,答得再像也不是查出来的。"})

    if "tool_call" not in steps:
        why = "能力清单里没有业务工具" if (tools and all(t.startswith(_BUILTIN_PREFIX) for t in tools)) \
            else "模型自己决定不调"
        out.append({"level": "warn", "title": "整轮没有调用任何工具",
                    "detail": f"意图是「{intent_ev.get('intent') or '未知'}」,但执行链里没有 tool_call。"
                              f"最可能的原因:{why}。"})

    if intent_ev.get("degraded"):
        out.append({"level": "error", "title": "意图分诊未生效(降级全量执行)",
                    "detail": intent_ev.get("degraded_reason")
                              or "分诊失败,本轮不裁剪、不澄清、不抽参数。"})

    if not any(e.get("step") == "generation" for e in timeline) and not result.get("answer"):
        out.append({"level": "warn", "title": "没有生成步骤也没有回答",
                    "detail": "任务可能在进入生成之前就结束了。"})

    gen_prompts = [p for p in prompts if p.step == "generation"]
    if "generation" in steps and not gen_prompts:
        out.append({"level": "warn", "title": "生成步骤没有提示词记录",
                    "detail": "看不到「模型生成回答时到底收到了什么」——"
                              "多轮记忆里带了什么、技能提示词长什么样,都无从判断。"})

    if not usage:
        out.append({"level": "warn", "title": "没有用量记录",
                    "detail": "这一轮可能没真正调用模型(比如命中缓存或直接复用了上一轮结果)。"})
    return out


async def task_debug(task_id: str, *, user_id: str | None, role: str,
                     scenic_id: str | None) -> dict:
    """组装诊断包。按「谁能看这条任务」过滤,与运行记录同一口径。"""
    from cogni_agent.common import permissions as perm
    from cogni_agent.control_plane_service.models import LLMPromptLog, Task
    from cogni_agent.governance_event_bus_service.cost import LlmUsage

    async with get_sessionmaker()() as db:
        task = await db.get(Task, task_id)
        if task is None:
            raise NotFound(f"任务不存在:{task_id}")
        # 集团角色看全部;景区角色只看本景区(与任务列表同一口径)
        if not perm.is_group_role(role) and task.scenic_id and task.scenic_id != scenic_id:
            raise NotFound(f"任务不存在或无权查看:{task_id}")

        prompts = (await db.execute(
            select(LLMPromptLog).where(LLMPromptLog.task_id == task_id)
            .order_by(LLMPromptLog.id))).scalars().all()
        usage = (await db.execute(
            select(LlmUsage).where(LlmUsage.task_id == task_id)
            .order_by(LlmUsage.created_at))).scalars().all()

    result = task.result or {}
    payload = task.payload or {}
    timeline = result.get("timeline") or []

    return {
        "task": _mask({
            "id": task.id, "status": task.status,
            "executor_type": task.executor_type, "executor_id": task.executor_id,
            "agent_id": task.agent_id, "conversation_id": task.conversation_id,
            "scenic_id": task.scenic_id, "user_id": task.user_id, "source": task.source,
            "question": payload.get("message") or "",
            "question_id": payload.get("question_id"),
            "retry_count": task.retry_count,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }),
        # 入参里可能有用户填的业务参数,一并给出来(脱敏);message 上面已单列
        "payload": _mask({k: v for k, v in payload.items()
                          if not k.startswith("_") and k != "message"}),
        "timeline": [_mask(e) for e in timeline],
        "prompts": [_mask({
            "step": p.step, "step_label": STEP_LABELS.get(p.step, p.step),
            "model": p.model, "prompt_tokens": p.prompt_tokens,
            "system_prompt": p.system_prompt or "",
            "history": _safe_json(p.history),
            "at": p.created_at.isoformat() if p.created_at else None,
        }) for p in prompts],
        "usage": [{
            "model": u.model, "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens, "cost_cny": float(u.cost_cny or 0),
            "at": u.created_at.isoformat() if u.created_at else None,
        } for u in usage],
        # 本次模型**真正看到**的技能与工具。这份清单不落库,但它就在分诊提示词里 ——
        # 而它恰恰是排障时第一个要看的东西:现场那次「分诊判对了却没查数」,
        # 根因就是能力清单里一个业务工具都没有,只剩 6 个系统内置工具。
        "capabilities": _capabilities_from_prompts(prompts),
        # 一眼能看出的问题。导出是给人读的 —— 把线索摆出来,比让人从几百行里自己找强。
        "checks": _self_checks(timeline, prompts, usage, result, task.status),
        "answer": _mask(result.get("answer") or ""),
        "error": _mask(result.get("error") or ""),
        "captured": {"timeline": len(timeline), "prompts": len(prompts), "usage": len(usage)},
    }


def _safe_json(text: str | None):
    if not text:
        return []
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


_ORDERED_FIELDS = (
    ("intent", "意图"), ("action", "动作"), ("reason", "判断"),
    ("target_skills", "技能调用"), ("params_filled", "已抽取参数"),
    ("missing", "缺失参数"), ("rewritten_goal", "问题改写"), ("plan", "执行计划"),
    ("mode", "模式"), ("provenance", "版本溯源"),
    ("code", "诊断码"), ("message", "说明"), ("ref", "关联对象"),
    ("args", "入参"), ("summary", "结果摘要"), ("findings", "要点"),
    ("predicted_gaps", "预判缺参"), ("error", "错误"), ("degraded_reason", "降级原因"),
)


def _fmt(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def task_debug_markdown(b: dict) -> str:
    """渲染成一段能直接贴出去的 Markdown。

    排障的人要的是能读的东西,不是一坨 JSON —— 尤其这份常常是转给别人看的。
    """
    t = b.get("task") or {}
    cap = b.get("captured") or {}
    L: list[str] = []

    L.append(f"# 智能体执行详情 · {t.get('executor_type') or 'agent'}")
    L.append("")
    L.append(f"- 任务 ID:`{t.get('id')}`　状态:**{t.get('status')}**")
    L.append(f"- 执行目标:{t.get('executor_type')} / `{t.get('executor_id')}`")
    if t.get("conversation_id"):
        L.append(f"- 会话:`{t.get('conversation_id')}`(多轮记忆按它挂)")
    L.append(f"- 景区:{t.get('scenic_id') or '(集团级)'}　发起人:{t.get('user_id') or '-'}"
             f"　来源:{t.get('source')}")
    L.append(f"- 时间:{t.get('created_at')} → {t.get('updated_at')}")
    L.append("")
    L.append("## 问题")
    L.append("")
    L.append(f"> {t.get('question') or '(空)'}")
    if b.get("payload"):
        L.append("")
        L.append(f"附带参数:`{json.dumps(b['payload'], ensure_ascii=False)}`")

    checks = b.get("checks") or []
    if checks:
        L.append("")
        L.append("## 值得注意")
        L.append("")
        for c in checks:
            icon = "❌" if c.get("level") == "error" else "⚠️"
            L.append(f"- {icon} **{c.get('title')}** —— {c.get('detail')}")

    caps = b.get("capabilities") or {}
    if caps:
        L.append("")
        L.append("## 本次可用的技能与工具")
        L.append("")
        L.append(f"- 技能:{'、'.join(caps.get('skills') or []) or '(无)'}")
        L.append(f"- 工具:{'、'.join(caps.get('tools') or []) or '(无)'}")
        L.append("")
        L.append("<details><summary>模型看到的原文</summary>")
        L.append("")
        L.append("```text")
        L.append(caps.get("raw") or "")
        L.append("```")
        L.append("")
        L.append("</details>")

    L.append("")
    L.append("## 执行链")
    L.append("")
    if not b.get("timeline"):
        # 空时间线有两种完全不同的原因,别把它们说成同一句话:
        # 任务还在跑(执行链收尾才落库)vs 真的没进执行图就挂了。
        # 之前一律写后者,于是一份跑得好好的任务被导成「疑似启动即失败」。
        if str((b.get("task") or {}).get("status") or "").upper() in ACTIVE_STATUSES:
            L.append(f"_任务还在执行中(状态 {(b.get('task') or {}).get('status')})——"
                     f"执行链在收尾时才落库,这会儿是空的属于正常。实时过程请在操练台看。_")
        else:
            L.append("_时间线为空 —— 任务可能在进入执行图之前就失败了(看下面的错误)。_")
    for i, e in enumerate(b.get("timeline") or [], 1):
        step = e.get("step") or "?"
        label = STEP_LABELS.get(step, step)
        head = f"**{i}. {label}**"
        if e.get("tool_display_name") or e.get("tool_name"):
            head += f" · {e.get('tool_display_name') or e.get('tool_name')}"
        if e.get("kind"):
            head += f" · {e['kind']}"
        if e.get("real_duration_ms") is not None:
            head += f" · {e['real_duration_ms']}ms"
        elif e.get("duration_ms") is not None:
            head += f" · {e['duration_ms']}ms"
        if e.get("wall_ms") or e.get("wall"):
            head += f" · {e.get('wall_ms') or e['wall']}"
        L.append(head)
        # 先按已知字段排个顺序(分诊那几项最值钱:判断依据 / 挂了哪些技能 / 缺哪些参数),
        # **然后把剩下的字段全部打出来**。
        #
        # 原先这里是个白名单,认识的才打 —— 于是 resolve_provenance 那一行是空的,
        # 解析期告警(「绑定的工具已不存在,已跳过」这类)也一个字都看不到。
        # 诊断导出恰恰不能这么写:最要紧的线索往往就在你没预料到的那个字段里。
        shown = {"step", "ts", "wall", "wall_ms", "source_type", "tool_name", "tool_display_name",
                 "kind", "real_duration_ms", "duration_ms", "parallel", "degraded", "llm", "detail", "io"}
        # 细节字段(v3.60):一行人话放在这里,完整展开在下面「逐步明细」
        _llm = e.get("llm") if isinstance(e.get("llm"), dict) else None
        if _llm:
            seg = (f"排队 {_llm.get('queue_ms')} · 思考 {_llm.get('think_ms')} · 输出 {_llm.get('output_ms')} ms"
                   if "queue_ms" in _llm else f"{_llm.get('total_ms')} ms")
            L.append(f"    - 模型:{_llm.get('model') or '-'} · {seg} · "
                     f"输入 {_llm.get('prompt_tokens')} / 输出 {_llm.get('completion_tokens')} tokens")
        _io = e.get("io") if isinstance(e.get("io"), dict) else None
        if _io:
            L.append(f"    - 发出 {_io.get('started_at') or '-'}"
                     + (" · 并行" if e.get("parallel") else "")
                     + (f" · HTTP {_io['http_status']}" if _io.get("http_status") is not None else "")
                     + f" · 返回 {_io.get('result_chars')} 字 → 给模型 {_io.get('sent_to_model_chars')} 字"
                     + ("(已压缩)" if _io.get("compressed") else "")
                     + (f" · 按「{(_io.get('fanout') or {}).get('label')}」逐个调用 {(_io.get('fanout') or {}).get('count')} 次"
                        if _io.get("fanout") else ""))
        for key, name in _ORDERED_FIELDS:
            v = e.get(key)
            if v in (None, "", [], {}):
                continue
            shown.add(key)
            L.append(f"    - {name}:{_fmt(v)}")
        for key in e:
            if key in shown:
                continue
            v = e.get(key)
            if v in (None, "", [], {}):
                continue
            L.append(f"    - {key}:{_fmt(v)}")
        if e.get("degraded"):
            L.append("    - ⚠️ 意图分诊未生效,本轮全量执行(不裁剪、不澄清)")
        L.append("")

    L.extend(_timing_breakdown(b.get("timeline") or []))
    L.extend(_step_details(b.get("timeline") or []))

    L.append("## 每一步发给模型的提示词")
    L.append("")
    if not b.get("prompts"):
        L.append("_没有提示词记录。可能是这次没走到 LLM,或提示词日志未开启。_")
    for p in b.get("prompts") or []:
        L.append(f"### {p.get('step_label')} · {p.get('model') or '-'}"
                 f"（prompt_tokens={p.get('prompt_tokens')}）")
        L.append("")
        L.append("```text")
        L.append((p.get("system_prompt") or "")[:8000])
        L.append("```")
        hist = p.get("history")
        if hist:
            L.append("")
            L.append("历史消息:")
            L.append("")
            L.append("```json")
            L.append(json.dumps(hist, ensure_ascii=False, indent=2)[:8000])
            L.append("```")
        L.append("")

    L.append("## 用量与成本")
    L.append("")
    if b.get("usage"):
        L.append("| 模型 | 输入 | 输出 | 成本(元) | 时间 |")
        L.append("| --- | --- | --- | --- | --- |")
        for u in b["usage"]:
            L.append(f"| {u['model']} | {u['prompt_tokens']} | {u['completion_tokens']} "
                     f"| {u['cost_cny']:.4f} | {u['at']} |")
    else:
        L.append("_没有用量记录。_")

    L.append("")
    L.append("## 最终回答")
    L.append("")
    L.append(b.get("answer") or "_(空)_")
    if b.get("error"):
        L.append("")
        L.append("## 错误")
        L.append("")
        L.append(f"```\n{b['error']}\n```")

    L.append("")
    L.append(f"---\n\n采集量:时间线 {cap.get('timeline', 0)} 条 · "
             f"提示词 {cap.get('prompts', 0)} 条 · 用量 {cap.get('usage', 0)} 条。"
             f"凭证已脱敏,可直接转发。")
    return "\n".join(L)
