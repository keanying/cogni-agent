"""
M1-03:限流中间件。

内部用户按 user_id 走令牌桶(Redis 计数器);开放平台按 api_key 限流,
额度与规则见 G-API-08 / tc_agents.rate_limit。此处给出基于 Redis 的
简化令牌桶实现,生产环境建议替换为更严格的滑动窗口算法。
"""
from __future__ import annotations

import time

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware

from cogni_agent.common.logging import get_logger

log = get_logger("gateway.middleware")
from starlette.requests import Request
from starlette.responses import JSONResponse

from cogni_agent.config import get_settings

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis | None:
    global _redis
    cfg = get_settings().get("redis", default={})
    if not cfg.get("enabled"):
        return None
    if _redis is None:
        kwargs: dict = dict(
            host=cfg["host"], port=cfg["port"], password=cfg.get("password") or None, db=cfg.get("db", 0),
            # **必须设短超时。** 限流在请求主路径上,Redis 挂掉时不设超时会让
            # 每个请求都等默认的 4 秒 —— 一个本来只是「限不了流」的问题,
            # 变成了「全站每个请求慢 4 秒」,吞吐直接塌掉。
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            # **必须显式关掉重试。** 只设超时是不够的:redis-py 默认会对连接错误
            # 重试 3 次,每次之间还有退避 —— 0.5 秒的超时被放大成 5 秒。
            # 限流在请求主路径上,这里不需要重试:失败了就放行,下次再试。
            retry_on_error=[],
            retry=None,
        )
        protocol_val = cfg.get("protocol")
        if protocol_val:
            kwargs["protocol"] = int(protocol_val)
        _redis = aioredis.Redis(**kwargs)
    return _redis


# 降级告警的节流:Redis 挂掉时每个请求都会走到这里,不节流的话日志会被刷爆,
# 真正有用的信息反而被淹没。
_last_degrade_warn = 0.0
_DEGRADE_WARN_INTERVAL = 60.0

# 熔断状态:失败后在这个时间窗内直接放行,不再尝试连 Redis
_circuit_until = 0.0
_CIRCUIT_SECONDS = 5.0


def _rate_limit_circuit_open() -> bool:
    return time.time() < _circuit_until


def _rate_limit_tripped() -> None:
    global _circuit_until
    _circuit_until = time.time() + _CIRCUIT_SECONDS


def _rate_limit_recovered() -> None:
    """成功一次就立刻恢复正常路径 —— Redis 回来了不该继续裸奔。"""
    global _circuit_until
    if _circuit_until:
        _circuit_until = 0.0


def _warn_rate_limit_degraded(exc: Exception) -> None:
    global _last_degrade_warn

    now = time.time()
    if now - _last_degrade_warn < _DEGRADE_WARN_INTERVAL:
        return
    _last_degrade_warn = now
    log.warning(
        f"Redis 不可用,限流已降级为「放行」:{exc}\n"
        f"  影响:当前不做频率限制,存在被刷的风险。\n"
        f"  处理:尽快恢复 Redis。服务本身不受影响 —— 限流是保护措施,"
        f"不该让它成为全站单点。"
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit_per_minute: int = 120):
        super().__init__(app)
        self.limit = limit_per_minute

    async def dispatch(self, request: Request, call_next):
        r = _get_redis()
        if r is None:
            return await call_next(request)  # 未启用 Redis 时不限流(本地开发)

        identity = request.headers.get("Authorization", request.client.host if request.client else "anon")
        bucket_key = f"ratelimit:{identity}:{int(time.time() // 60)}"
        # 熔断:刚失败过就直接放行,不再每个请求都去试。
        # 0.5 秒超时 × 高并发依然是可观的额外延迟,而 Redis 刚挂掉那一刻
        # 大概率还没恢复 —— 隔几秒探一次就够了。
        if _rate_limit_circuit_open():
            return await call_next(request)

        try:
            count = await r.incr(bucket_key)
            await r.expire(bucket_key, 65)
            _rate_limit_recovered()
        except Exception as exc:  # noqa: BLE001
            _rate_limit_tripped()
            # **Redis 挂了就放行,不要把请求打成 500。**
            # 限流是保护措施,不是业务功能 —— 让它成为全站单点是本末倒置:
            # Redis 一挂,一个本来只是「暂时限不了流」的问题就变成了「整站不可用」。
            # 降级期间可能被刷,但那远好过所有人都用不了。
            _warn_rate_limit_degraded(exc)
            return await call_next(request)

        if count > self.limit:
            return JSONResponse(status_code=429, content={"code": "rate_limited", "message": "请求过于频繁"})
        return await call_next(request)
