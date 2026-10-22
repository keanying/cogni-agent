"""Quick BI → APP 对话流的桥接(前后端改造 §3:前端只认一个发起接口)。

**为什么要这层桥**:
Quick BI 的一问是它自己端到端完成的(直连上游、自带事件流),而 APP 的对话是
「POST 拿 task_id → GET 订阅事件流」。两套形状不一样,如果让前端按问题类型分流,
前端就得维护两套请求、两套事件解析、两套中断逻辑 —— 而这个区别对用户是不存在的,
他只是点了一个问题。

所以分流放在后端:`POST /api/app/chat` 不管哪类问题都返回 task_id,
Quick BI 那一类由这里起一个后台任务跑,把上游事件**翻译**成 APP 已有的事件
(token / stage / structured / done),推到同一个 task_id 的通道上。
前端零改动,历史回放也照常(这一轮同样落一条 task 行)。

**租约**:任务状态是 RUNNING,巡检会把租约过期的 RUNNING 判为 FAILED
(见 control_plane_service/queue.requeue_expired_leases)。Quick BI 报告能跑几十分钟,
所以这里要像执行引擎一样定期续租,否则跑到一半会被判死。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.control_plane_service.models import Task
from cogni_agent.runtime_service import streaming

log = get_logger("gateway.qbi_bridge")

# 续租周期与租约长度:租约必须明显长于续租周期,否则网络抖一下就被判死。
LEASE_SECONDS = 180
RENEW_SECONDS = 45
# 一次问答最多跑多久(报告类可能很久,但不能无限挂着)
MAX_RUN_SECONDS = 45 * 60


def _echarts_option(chart: dict) -> dict | None:
    """Quick BI 图表(fields + rows)→ ECharts option。

    APP 端图表组件吃的是 option,不是字段表 —— 转换放在服务端,前端就不用为
    Quick BI 单独写一套渲染。转不了的(纯表格、无维度)返回 None,由调用方
    退化成 Markdown 表格,总之不能让用户看到一个空白块。
    """
    fields = chart.get("fields") or []
    rows = chart.get("rows") or []
    if not fields or not rows:
        return None
    dims = [f["name"] for f in fields if f.get("role") != "metric"]
    metrics = [f["name"] for f in fields if f.get("role") == "metric"]
    if not dims or not metrics:
        return None
    x = [str(r.get(dims[0], "")) for r in rows]
    ctype = str(chart.get("chart_type") or "").lower()

    if ctype == "pie" and len(metrics) == 1:
        return {
            "tooltip": {"trigger": "item"},
            "series": [{
                "type": "pie", "radius": ["40%", "70%"],
                "data": [{"name": str(r.get(dims[0], "")), "value": r.get(metrics[0], 0)} for r in rows],
            }],
        }

    series_type = "line" if ctype in ("line", "area") else "bar"
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": metrics} if len(metrics) > 1 else {},
        "xAxis": {"type": "category", "data": x},
        "yAxis": {"type": "value"},
        "series": [{
            "name": m, "type": series_type,
            "smooth": series_type == "line",
            "areaStyle": {} if ctype == "area" else None,
            "data": [r.get(m, 0) for r in rows],
        } for m in metrics],
    }


def _markdown_table(chart: dict, limit: int = 20) -> str:
    """转不成图的就给张表 —— 数据在那儿,总比一个空白块强。"""
    fields = [f["name"] for f in (chart.get("fields") or [])]
    rows = chart.get("rows") or []
    if not fields or not rows:
        return ""
    head = "| " + " | ".join(fields) + " |"
    sep = "| " + " | ".join("---" for _ in fields) + " |"
    body = ["| " + " | ".join(str(r.get(f, "")) for f in fields) + " |" for r in rows[:limit]]
    more = f"\n\n> 共 {len(rows)} 行,这里只列前 {limit} 行" if len(rows) > limit else ""
    title = chart.get("title") or "查询结果"
    return f"\n\n**{title}**\n\n{head}\n{sep}\n" + "\n".join(body) + more + "\n"


async def _renew_lease(task_id: str) -> None:
    """定期续租,直到任务被取消。不续的话巡检会把它判成 FAILED(跑一半没了)。"""
    try:
        while True:
            await asyncio.sleep(RENEW_SECONDS)
            async with get_sessionmaker()() as db:
                await db.execute(sa.update(Task).where(Task.id == task_id).values(
                    lease_until=datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)))
                await db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 续租失败不该让本轮问答崩掉
        log.warning(f"task {task_id} 续租失败:{exc}")


# 在跑的桥接任务。asyncio 只弱引用 Task,不留强引用的话它可能在跑到一半时被 GC 回收
# (与 worker/loops.py 同一个坑);跑完自动移除。
_RUNNING: set[asyncio.Task] = set()


def spawn(task_id: str, agen, *, issue_id: str | None = None) -> asyncio.Task:
    """后台起一个 pump 并持有强引用。"""
    t = asyncio.create_task(pump(task_id, agen, issue_id=issue_id))
    _RUNNING.add(t)
    t.add_done_callback(_RUNNING.discard)
    return t


async def pump(task_id: str, agen, *, issue_id: str | None = None) -> None:
    """把 Quick BI 事件流翻译成 APP 事件流,并在结束时收尾任务。

    翻译口径(左边 Quick BI,右边 APP 已有事件):
      text(channel=data)  → token      正文打字机增量
      reasoning / status / step / sql → stage   思考过程面板(不进正文)
      chart               → structured  图表(转成 ECharts option;转不了退化成 Markdown 表)
      report              → token       报告链接直接写进正文(用户要的就是这个链接)
      usage               → usage
      error               → error
      done                → done        带完整 answer 与 structured,供历史回放
    """
    renew = asyncio.create_task(_renew_lease(task_id))
    answer_parts: list[str] = []
    charts: list[dict] = []
    reasoning_buf = ""
    status_text = ""
    failed_msg: str | None = None

    async def emit(event: str, data: dict) -> None:
        await streaming.publish(task_id, event, data)

    try:
        async with asyncio.timeout(MAX_RUN_SECONDS):
            async for evt in agen:
                et = str(evt.get("type") or "")
                channel = str(evt.get("channel") or "")

                if et == "text" and channel != "thinking":
                    delta = str(evt.get("delta") or "")
                    if delta:
                        answer_parts.append(delta)
                        await emit("token", {"delta": delta})
                    continue

                if et in ("reasoning",) or (et == "text" and channel == "thinking"):
                    # 推理是长文本流,逐字推给「思考过程」面板会刷屏 —— 按行聚合再推
                    reasoning_buf += str(evt.get("delta") or "")
                    while "\n" in reasoning_buf:
                        line, reasoning_buf = reasoning_buf.split("\n", 1)
                        if line.strip():
                            await emit("stage", {"stage": "thinking", "text": line.strip()[:200]})
                    continue

                if et == "status":
                    status_text = str(evt.get("message") or "")
                    if status_text:
                        await emit("stage", {"stage": evt.get("stage") or "running", "text": status_text})
                    continue

                if et in ("step", "trace"):
                    title = str(evt.get("title") or evt.get("message") or "")
                    if title:
                        await emit("stage", {"stage": "step", "text": title[:200]})
                    continue

                if et == "sql":
                    await emit("stage", {"stage": "sql", "text": "已生成查询 SQL"})
                    continue

                if et == "chart":
                    chart = evt.get("chart") or {}
                    charts.append(chart)
                    option = _echarts_option(chart)
                    if option:
                        await emit("structured", {"items": [{"charts": [{"option": option}]}]})
                    else:
                        md = _markdown_table(chart)
                        if md:
                            answer_parts.append(md)
                            await emit("token", {"delta": md})
                    continue

                if et == "report":
                    url = str(evt.get("report_url") or "")
                    if url:
                        line = f"\n\n[打开报告]({url})\n"
                        answer_parts.append(line)
                        await emit("token", {"delta": line})
                    else:
                        await emit("stage", {"stage": "report", "text": "报告生成中(通常需要 3~10 分钟)"})
                    continue

                if et == "usage":
                    await emit("usage", {k: v for k, v in evt.items() if k != "type"})
                    continue

                if et == "error":
                    failed_msg = str(evt.get("message") or "Quick BI 执行失败")
                    continue

                if et == "done":
                    if evt.get("status") == "failed":
                        failed_msg = failed_msg or str(evt.get("error") or "Quick BI 执行失败")
                    continue
    except TimeoutError:
        failed_msg = f"Quick BI 执行超过 {MAX_RUN_SECONDS // 60} 分钟,已停止等待"
    except asyncio.CancelledError:
        failed_msg = "已取消"
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要收尾,不能让任务永远 RUNNING
        log.exception(f"task {task_id} Quick BI 执行异常")
        failed_msg = str(exc)
    finally:
        renew.cancel()
        answer = "".join(answer_parts).strip()
        structured = [{"charts": [{"option": o} for o in
                                  filter(None, (_echarts_option(c) for c in charts))]}] if charts else None
        # Quick BI 什么都没产出时,把这一轮转交给自研智能体,而不是把失败甩给用户。
        #
        # 现场最常见的三种:小Q 判「该问题与数据集无关」、凭证/额度出问题、
        # 以及「跑完了但一个字都没有」。这三种对用户是同一件事 —— 他问了个问题,
        # 什么也没拿到。而我们自己的智能体手里有雷达图指标这类业务工具,
        # 很多时候答得出来。
        #
        # **接在同一个 task_id 上**:APP 那边的 SSE 流已经开着了,
        # 换个任务号等于让它白等一场。execute_task 会往同一个流里继续推。
        _no_data = _looks_like_no_data(answer, charts)
        if ((not answer and not charts) or _no_data) and not _is_cancelled(failed_msg):
            if await _handoff_to_agent(task_id, reason=failed_msg or ("Quick BI 未查到数据" if _no_data else None),
                                       issue_id=issue_id):
                return                      # 智能体接手,收尾与 done 都由它负责
        status = "FAILED" if failed_msg and not answer else "COMPLETED"
        result: dict[str, Any] = {"answer": answer or (failed_msg or ""), "structured": structured}
        if failed_msg:
            result["error"] = failed_msg
        try:
            async with get_sessionmaker()() as db:
                # task_queue 没有 finished_at 列,终态时间看 updated_at(onupdate 自动写)
                await db.execute(sa.update(Task).where(Task.id == task_id).values(
                    status=status, result=result, lease_until=None,
                    updated_at=sa.func.now()))
                await db.commit()
            if issue_id:
                from cogni_agent.gateway_service import issue_service as issues

                async with get_sessionmaker()() as db:
                    await issues.touch_run(db, issue_id, run_id=task_id,
                                           status="success" if status == "COMPLETED" else "failed")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"task {task_id} 收尾写库失败:{exc}")
        if failed_msg and not answer:
            await streaming.publish(task_id, "error", {"message": failed_msg})
        await streaming.publish(task_id, "done", {
            "status": "failed" if status == "FAILED" else "success",
            "answer": answer, "structured": structured, "error": failed_msg,
        })


# 小Q「答了,但等于没答」的说法。只在回答很短、且没有任何带数据的图时才据此判无数据 ——
# 一段正经分析里顺带一句「某渠道暂无数据」不能被当成整轮没数据转走。
_NO_DATA_PHRASES = ("未查询到", "没有查询到", "查询结果为空", "暂无数据", "没有数据", "无数据",
                    "未找到相关数据", "没有找到相关数据", "与数据集无关", "数据为空", "无法回答该问题")
_NO_DATA_MAX_CHARS = 200


def _looks_like_no_data(answer: str, charts: list[dict]) -> bool:
    """Quick BI 跑完了,但回的是「未查询到数据」这类话 —— 对用户和什么都没拿到是一回事。"""
    text = (answer or "").strip()
    if not text:
        return False
    if any(_echarts_option(c) or _markdown_table(c) for c in charts or []):
        return False                      # 有一张带数据的图就不算没数据
    # 解读的「不存在」+ 我们补的说明:这是明确约定的信号,不看长度
    from cogni_agent.tool_center_service.quickbi.capabilities import NO_RELATED_DATA_MARK

    if text.rstrip("。.") == "不存在" or (text.startswith("不存在") and NO_RELATED_DATA_MARK in text):
        return True
    if len(text) > _NO_DATA_MAX_CHARS:
        return False
    return any(p in text for p in _NO_DATA_PHRASES)


async def _latest_published_id(db, resource_type: str, resource_id: str) -> str | None:
    from cogni_agent.tool_center_service import versioning as ver

    v = await ver.latest_published_version(db, resource_type, resource_id)
    return v.id if v is not None else None


def _is_cancelled(failed_msg: str | None) -> bool:
    """用户主动取消的不转交 —— 他要的是停下来,不是换一个人再答一遍。"""
    return bool(failed_msg) and ("取消" in failed_msg or "cancel" in failed_msg.lower())


async def _handoff_to_agent(task_id: str, *, reason: str | None, issue_id: str | None = None) -> bool:
    """把这一轮交给自研智能体 / 技能接着跑。成功接手返回 True。

    **接手的一律跑「最新已发布版本」**(显式钉 executor_version_id)。不钉的话走「按用户指派」,
    而降级这条路上的人多半没被指派过这个智能体/技能 —— 结果是 Quick BI 没答上来,
    转过去又卡在「未分发给当前用户」,两头都落空。

    接手目标的顺序:这条问题自己绑的技能 > 问题绑的智能体 > 这个账号的默认智能体。
    问题上绑了技能,说明运营明确知道这类问题该谁答,比默认智能体再做一次意图路由更准。

    做法是**改写同一行任务**的执行目标再跑一次 execute_task —— 不新建任务:
    新建的话 APP 那边开着的 SSE 流收不到后续事件,用户对着一个转圈等到超时,
    而后台其实已经答完了(答在另一个他看不到的任务里)。

    任何一步不成立就返回 False,由调用方按原来的失败路径收尾 ——
    转交是增强,不能让它把「Quick BI 失败」变成「什么都没有」。
    """
    try:
        async with get_sessionmaker()() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return False
            payload = dict(task.payload or {})
            question = str(payload.get("message") or "").strip()
            if not question:
                return False

            from cogni_agent.gateway_service.routes_app import _default_agent_for
            from cogni_agent.user_center_service.models import User

            user = await db.get(User, payload.get("user_id") or "")
            if user is None:
                return False
            target_type, target_id, version_id = None, None, None
            if issue_id:
                from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

                # issue_id 是业务编号列,不是主键(主键是自增 bigint):不能用 db.get
                issue = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
                    ScenicAgentIssueDetail.issue_id == issue_id))).scalars().first()
                for ttype, tid in (("skill", getattr(issue, "skill_id", None)),
                                   ("agent", getattr(issue, "agent_id", None))):
                    if tid:
                        vid = await _latest_published_id(db, ttype, tid)
                        if vid:
                            target_type, target_id, version_id = ttype, tid, vid
                            # 问题上绑的目标是运营配好的:授权同固定问题(见 executor)
                            payload["handoff_authorized"] = True
                            break
            if target_id is None:
                agent_id = await _default_agent_for(db, user)
                vid = await _latest_published_id(db, "agent", agent_id) if agent_id else None
                if not agent_id or not vid:
                    log.info(f"task {task_id} Quick BI 无结果,但这个账号没有可用(已发布)的智能体,不转交")
                    return False
                target_type, target_id, version_id = "agent", agent_id, vid

            # 目标换成智能体,并记下这一轮是怎么来的 —— 排查时要能一眼看出
            # 「这条是 Quick BI 没答上来转过来的」,而不是以为用户本来就问的智能体
            payload["handoff_from"] = "quickbi"
            payload["handoff_reason"] = (reason or "Quick BI 未返回任何结果")[:300]
            # 目标数据/连接是 Quick BI 的上下文,智能体用不上,留着只会干扰解析
            for k in ("connection_id", "capability", "target_type", "target_id"):
                payload.pop(k, None)
            payload["executor_version_id"] = version_id      # 最新已发布版本(见上)
            await db.execute(sa.update(Task).where(Task.id == task_id).values(
                executor_type=target_type, executor_id=target_id, payload=payload,
                status="RUNNING", lease_until=None, updated_at=sa.func.now()))
            await db.commit()
    except Exception as exc:                # noqa: BLE001 - 转交失败就走原来的失败路径
        log.warning(f"task {task_id} 转交智能体失败,按原路收尾:{type(exc).__name__}: {exc}")
        return False

    # 告诉用户发生了什么。不说的话,他会看到思考面板突然从 Quick BI 跳到另一套步骤,
    # 而正文迟迟不出来 —— 那比直接报错更让人摸不着头脑。
    await streaming.publish(task_id, "stage", {
        "stage": "handoff",
        "text": "Quick BI 没有返回结果,正在转由智能体处理…"})
    try:
        from cogni_agent.runtime_service.engine.executor import execute_task

        await execute_task(task_id)
        return True
    except Exception as exc:                # noqa: BLE001
        log.warning(f"task {task_id} 智能体接手后仍失败:{type(exc).__name__}: {exc}")
        # 智能体也没跑起来:把任务落成失败并给一句说得清的话,
        # 否则它会一直挂在 RUNNING 上,被巡检判死时用户早就走了
        try:
            async with get_sessionmaker()() as db:
                await db.execute(sa.update(Task).where(Task.id == task_id).values(
                    status="FAILED", lease_until=None, updated_at=sa.func.now(),
                    result={"answer": "", "error": f"Quick BI 未返回结果,转由智能体处理后仍未成功:{exc}"}))
                await db.commit()
        except Exception:                   # noqa: BLE001
            pass
        await streaming.publish(task_id, "error", {
            "message": "Quick BI 没有返回结果,转由智能体处理后仍未成功。请换个问法再试"})
        await streaming.publish(task_id, "done", {
            "status": "failed", "answer": "", "structured": None,
            "error": "Quick BI 与智能体都未返回结果"})
        return True                          # 已经自己收过尾了,不要再走原路径
