"""
M1-05/06 + 文档 1.2「标准化输出」的流式旁路——worker 进程里跑执行图,
gateway 进程里的 SSE 请求订阅同一个 task_id 的事件流,两边靠这个 broker 解耦。

统一事件模型(对应「对话」页规格):
- "timeline"      执行过程时间线节点(意图识别/模式判定/知识检索/工具调用/生成)
- "reasoning"     LLM 思考过程增量(30 号,推理模型 reasoning_content)——灰色
                  思考区打字机;只当轮展示不进历史(推理模型的思考是一次性的,
                  回传下轮会污染上下文);普通模型无此事件(优雅降级)
- "token"         生成内容的增量 token(流式打字机效果)
- "usage"         每轮消耗展示(prompt/completion 拆分,累计值)
- "stage"         执行阶段切换(understanding→querying→generating),APP 端展示进度条
- "waiting_human" 触发 HITL,任务转 WAITING_HUMAN,前端应引导去审批中心
- "credential_confirm" E 方案(v0.1.5):请求保存凭证,任务挂起等待「对话内用户确认」,
                      data 只含工具名/场景(不含明文),前端渲染确认卡片;用户确认/
                      忽略后经确认端点恢复,同一 SSE 流继续产出后续事件
- "done"          任务完成,data 里带最终 result(关联推荐追问已删,24号/0056,推荐入口走问题库)
- "error"         任务失败(护栏熔断/解析失败/工具执行异常等)

支持断线续传:事件带自增 id,SSE 端点把 `Last-Event-ID` 请求头透传进 subscribe()
的 last_id 参数即可从断点继续,不会丢事件也不会重复推送已消费的部分。

后端二选一:
- Redis 已启用(config.yaml: redis.enabled) -> Redis Streams,天然支持多进程/多 Worker
  部署下 gateway 和 worker 不在同一进程的场景(演进路径见 README「模块化单体」)。
- 未启用 Redis(本地开发) -> 进程内内存队列,足够单进程一键启动场景使用。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from cogni_agent.common.logging import get_logger
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from cogni_agent.config import get_settings

# Redis 短暂不可用时的重连间隔。设小一点:SSE 正连着,用户在等
_RECONNECT_DELAY = 2.0

log = get_logger("runtime_service.streaming")

TERMINAL_EVENTS = {"done", "error", "waiting_human", "cancelled"}
_STREAM_PREFIX = "zhilv:stream:task:"
_MAX_BUFFER = 2000
_IDLE_TTL_SECONDS = 1800  # 内存后端:任务结束后缓冲区保留时长,供短暂断线重连读取

# ── v1.1 阶段二/2C: 执行阶段枚举 ──
STAGE_UNDERSTANDING = "understanding"
STAGE_QUERYING = "querying"
STAGE_GENERATING = "generating"

# ── v1.1 智能问数 Phase B: 结构化问数事件(04 方案 §4.4) ──
# APP 问数场景下,LLM 输出中的结构化结果块(见 engine/structured_output.py)会被拆成
# 以下 4 个事件,在 done 之前按 cards → chart → insight → recommend 顺序广播:
# - "cards"     数据卡片(回复结构第一层):data={"items": [{label, value, unit, trend}]}
# - "chart"     图表(ECharts option spec,由 chart_templates.py 模板填充):
#               data={"title", "type", "option"} —— APP 端渲染,不传图片
# - "insight"   数据解读:data={"level": normal|warning|danger, "content"}
# - "recommend" 关联推荐追问:data={"items": [{question, theme}]}
EVENT_CARDS = "cards"
EVENT_CHART = "chart"
EVENT_INSIGHT = "insight"
EVENT_RECOMMEND = "recommend"


# ── 用户主动取消(真取消)注册表 ────────────────────────────────────
# cancel 接口只改 DB 状态 + 广播事件时,执行协程(LLM 调用/工具循环)并不知道,会继续
# 跑到自然结束:白烧 token,且收尾把 CANCELLED 覆盖成 COMPLETED("点了停止但历史显示
# 已完成")。这里补一个进程内标记:取消入口 mark → executor 的流式循环/watcher 感知 →
# 协作式中断。
# 模块化单体(API + worker 同进程)下天然生效;未来拆分多实例时需换成 Redis 标记
# (与事件流同一个 Redis,加一个 SETEX key 即可),接口语义不变。
_user_cancelled: set[str] = set()


def mark_user_cancelled(task_id: str) -> None:
    """取消入口调用:登记该任务被用户主动取消。"""
    _user_cancelled.add(task_id)


def is_user_cancelled(task_id: str) -> bool:
    return task_id in _user_cancelled


def clear_user_cancelled(task_id: str) -> None:
    _user_cancelled.discard(task_id)


async def wait_user_cancelled(task_id: str, poll: float = 0.5) -> bool:
    """阻塞直到该任务被标记取消(供 executor 的 watcher 协程 await)。

    返回 True 即取消;未取消时永不返回(由调用方 cancel 掉)。轮询而非事件:
    取消是低频操作,0.5s 的感知延迟换实现简单(不用维护 Condition 的通知方)。
    """
    while True:
        if task_id in _user_cancelled:
            return True
        await asyncio.sleep(poll)


@dataclass
class _MemStream:
    events: deque = field(default_factory=deque)  # [(id:int, event:str, data:dict)]
    next_id: int = 1
    done: bool = False
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    last_activity: float = field(default_factory=time.time)


_mem_streams: dict[str, _MemStream] = {}
_redis_client = None


def _redis_enabled() -> bool:
    return bool(get_settings().get("redis", "enabled", default=False))


def _stream_block_seconds() -> float:
    """XREAD 单次阻塞等待时长。

    **它和 socket 读超时必须成对设置**,见 _get_redis() 的说明。所以两处都从这一个
    函数取值,避免两个数字写在不同地方然后各改各的。
    """
    return float(get_settings().get("redis", "stream_block_seconds", default=15))


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        cfg = get_settings().get("redis", default={})
        block = _stream_block_seconds()
        kwargs: dict = dict(
            host=cfg["host"], port=cfg["port"], password=cfg.get("password") or None, db=cfg.get("db", 0),
            decode_responses=True,
            # **socket 读超时必须大于 XREAD 的阻塞时长**,否则客户端会先于服务端超时,
            # 抛 `Timeout reading from host:6379`。这个坑很隐蔽:XREAD BLOCK 是正常的
            # 长轮询,服务端在没有新事件时本来就会挂满整个 block 时长;客户端读超时
            # 设得比它小,等于每次长轮询都必然报错一次。
            # 这里统一按 block + 10s 计算,并且和下面的 xread 用同一个来源,不会漂移。
            socket_timeout=block + 10,
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
        protocol_val = cfg.get("protocol")
        if protocol_val:
            kwargs["protocol"] = int(protocol_val)
        _redis_client = aioredis.Redis(**kwargs)
    return _redis_client


def _get_mem_stream(task_id: str) -> _MemStream:
    stream = _mem_streams.get(task_id)
    if stream is None:
        stream = _MemStream()
        _mem_streams[task_id] = stream
    return stream


async def publish(task_id: str, event: str, data: dict) -> str:
    """发布一条事件,返回事件 id(字符串,SSE 端点原样作为 `id:` 字段下发)。"""
    if _redis_enabled():
        r = _get_redis()
        key = f"{_STREAM_PREFIX}{task_id}"
        event_id = await r.xadd(key, {"event": event, "data": json.dumps(data, ensure_ascii=False)}, maxlen=_MAX_BUFFER, approximate=True)
        await r.expire(key, _IDLE_TTL_SECONDS)
        return event_id

    stream = _get_mem_stream(task_id)
    async with stream.cond:
        eid = stream.next_id
        stream.next_id += 1
        stream.events.append((eid, event, data))
        while len(stream.events) > _MAX_BUFFER:
            stream.events.popleft()
        stream.last_activity = time.time()
        if event in TERMINAL_EVENTS:
            stream.done = True
        stream.cond.notify_all()
    return str(eid)


async def close_stream(task_id: str) -> None:
    """执行侧收尾时调用,确保即便没有显式发 done/error/waiting_human 也能唤醒订阅者退出。"""
    if _redis_enabled():
        return
    stream = _mem_streams.get(task_id)
    if stream is None:
        return
    async with stream.cond:
        stream.done = True
        stream.cond.notify_all()


async def subscribe(task_id: str, last_id: str | None = None, timeout_seconds: float = 900.0) -> AsyncIterator[tuple[str, str, dict]]:
    """从 last_id(不含)之后开始,持续 yield (event_id, event_name, data),
    直到遇到终止事件或超时。last_id=None 表示从头开始(新连接的正常情形)。"""
    if _redis_enabled():
        async for item in _subscribe_redis(task_id, last_id, timeout_seconds):
            yield item
        return
    async for item in _subscribe_mem(task_id, last_id, timeout_seconds):
        yield item


async def _subscribe_redis(task_id: str, last_id: str | None, timeout_seconds: float) -> AsyncIterator[tuple[str, str, dict]]:
    r = _get_redis()
    key = f"{_STREAM_PREFIX}{task_id}"
    cursor = last_id or "0"
    deadline = time.time() + timeout_seconds
    block_ms = int(_stream_block_seconds() * 1000)
    while time.time() < deadline:
        try:
            resp = await r.xread({key: cursor}, block=block_ms, count=200)
        except RedisTimeoutError:
            # 长轮询期间没有新事件是**完全正常**的情况,不是错误。即便超时参数被配歪了,
            # 也应该当作"这一轮没数据"继续下一轮,而不是把整条 SSE 流打断
            # ——之前正是这里把 TimeoutError 一路抛到 _pump,导致操练台直接报错。
            continue
        except RedisConnectionError:
            log.warning(f"Redis 连接中断,{_RECONNECT_DELAY}s 后重试订阅 task {task_id}")
            await asyncio.sleep(_RECONNECT_DELAY)
            continue
        if not resp:
            continue
        # redis-py 版本差异:xread 传单个 {stream: cursor} dict 时,
        #   - 4.x 返回 [(stream, [(entry_id, fields), ...])](list 解包)
        #   - 5.x+/8.x 返回 {stream: [[(entry_id, fields), ...]]}(dict,value 里多套一层 list)
        # 这里兼容两种格式,否则新版会 KeyError: 0,SSE 订阅一拿到数据就崩。
        if isinstance(resp, dict):
            batch = next(iter(resp.values()))
            entries = batch[0] if batch else []
        else:
            _, entries = resp[0]
        for entry_id, fields in entries:
            event = fields["event"]
            data = json.loads(fields["data"])
            cursor = entry_id
            yield entry_id, event, data
            if event in TERMINAL_EVENTS:
                return


async def _subscribe_mem(task_id: str, last_id: str | None, timeout_seconds: float) -> AsyncIterator[tuple[str, str, dict]]:
    stream = _get_mem_stream(task_id)
    after = int(last_id) if last_id else 0
    deadline = time.time() + timeout_seconds
    while True:
        async with stream.cond:
            pending = [e for e in stream.events if e[0] > after]
            if not pending and not stream.done:
                try:
                    await asyncio.wait_for(stream.cond.wait(), timeout=max(1.0, deadline - time.time()))
                except asyncio.TimeoutError:
                    return
                continue
        for eid, event, data in pending:
            after = eid
            yield str(eid), event, data
            if event in TERMINAL_EVENTS:
                return
        if stream.done and not pending:
            return
        if time.time() > deadline:
            return


def cleanup_idle_mem_streams() -> int:
    """可选:巡检协程周期调用,清理长期无人订阅的内存缓冲区(仅内存后端需要)。"""
    now = time.time()
    stale = [tid for tid, s in _mem_streams.items() if s.done and now - s.last_activity > _IDLE_TTL_SECONDS]
    for tid in stale:
        _mem_streams.pop(tid, None)
    return len(stale)
