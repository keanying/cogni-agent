# -*- coding: utf-8 -*-
"""LLM 成本计算(真实计费)。

单价解析优先级(「系统配置 > 用户手填 > 兜底估算」,多级兜底不会让计费失败):
  1. 模型单价配置表(`gov_llm_model_prices`,由「系统设置 · 模型单价」页面统一管理,
     按供应商 model_id 精确匹配,元/百万 token);
  2. 模型配置里手填的单价(`input_price_cny_per_m` / `output_price_cny_per_m`,元/百万 token);
  3. 兜底统一单价(`runtime.fallback_price_cny_per_m`,默认 15 元/百万 token)。

不再依赖 litellm 内置价格表:litellm 默认每次进程启动都会实时拉取 GitHub 计费表
(raw.githubusercontent.com),网络抖动会造成秒级挂起甚至请求超时。单价已完全由
系统内配置决定,并已在 cogni_agent/__init__.py 强制 litellm 走随包本地备份表。

单价表按进程内 TTL 缓存(30s)加载,页面改动后主动失效,与 client.py 模型解析缓存同策略。
"""
import time

from cogni_agent.common.logging import get_logger

log = get_logger("runtime_service.llm.cost")

# 单价配置表进程内缓存:model_id -> (input_cny_per_m, output_cny_per_m),可空。
_model_prices_cache: dict[str, tuple[float | None, float | None]] = {}
_model_prices_cache_at: float = 0.0
_MODEL_PRICES_TTL: float = 30.0


def invalidate_model_prices_cache() -> None:
    """页面改动单价后主动失效缓存,让下一次计费立即生效(见 routes.py)。"""
    global _model_prices_cache_at
    _model_prices_cache_at = 0.0


async def _refresh_model_prices_cache_if_stale() -> None:
    """从数据库加载全部模型单价到进程内缓存(TTL 30s)。失败仅告警,不影响计费兜底。"""
    global _model_prices_cache, _model_prices_cache_at
    if time.monotonic() - _model_prices_cache_at < _MODEL_PRICES_TTL:
        return
    try:
        from sqlalchemy import select

        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.governance_event_bus_service.settings import ModelPrice

        async with get_sessionmaker()() as db:
            rows = (await db.execute(select(ModelPrice))).scalars().all()
        _model_prices_cache = {
            r.model_id: (
                float(r.input_price_cny_per_m) if r.input_price_cny_per_m is not None else None,
                float(r.output_price_cny_per_m) if r.output_price_cny_per_m is not None else None,
            )
            for r in rows
        }
        _model_prices_cache_at = time.monotonic()
    except Exception as exc:  # noqa: BLE001 - 计费兜底不因 DB 异常失败
        _model_prices_cache_at = time.monotonic()  # 冷却,避免每次计费重复失败
        log.warning("[cost] 模型单价配置表加载失败,按兜底价估算: {}", exc)


def _cny_per_usd() -> float:
    from cogni_agent.config import get_settings

    try:
        return float(get_settings().get("runtime", "llm_cost", "cny_per_usd", default=7.2))
    except Exception:  # noqa: BLE001 - 计费兜底不因配置缺失失败
        return 7.2


def _fallback_price_cny_per_m() -> float:
    from cogni_agent.config import get_settings

    try:
        return float(get_settings().get("runtime", "llm_cost", "fallback_price_cny_per_m", default=15.0))
    except Exception:  # noqa: BLE001
        return 15.0


def resolve_unit_prices_cny_per_m(model_id: str, cfg: dict | None = None) -> tuple[float, float]:
    """返回 (input_cny_per_m, output_cny_per_m)。

    优先级:模型单价配置表(按 model_id 精确匹配) > 模型配置手填单价 > 兜底统一单价。
    单价表里只填了输入/输出其中一个时,另一个取同值,保证不会出现单边 0 价。
    """
    if model_id:
        p = _model_prices_cache.get(model_id)
        if p and (p[0] is not None or p[1] is not None):
            in_p, out_p = p
            if in_p is None:
                in_p = out_p
            if out_p is None:
                out_p = in_p
            return float(in_p or 0), float(out_p or 0)
    if cfg:
        in_p = cfg.get("input_price_cny_per_m")
        out_p = cfg.get("output_price_cny_per_m")
        if in_p is not None and out_p is not None:
            return float(in_p), float(out_p)
    fb = _fallback_price_cny_per_m()
    return fb, fb


def estimate_cost_cny(model_id: str, prompt_tokens: int, completion_tokens: int,
                      cfg: dict | None = None) -> float:
    """按单价估算成本(元)。prompt 与 completion 分开计价。"""
    in_p, out_p = resolve_unit_prices_cny_per_m(model_id, cfg)
    return round((prompt_tokens * in_p + completion_tokens * out_p) / 1_000_000, 4)


def usd_to_cny(usd: float) -> float:
    """把模型/网关在响应里返回的 USD 成本换算成人民币(按 `runtime.llm_cost.cny_per_usd`)。"""
    return usd * _cny_per_usd()


async def compute_cost_cny(model_key: str, prompt_tokens: int, completion_tokens: int) -> float:
    """异步版(供执行引擎在任务收尾时调用):按模型 key 解析配置,再走三级单价。

    `model_key` 是解析链里的模型标识(模型 id / 展示名 / 供应商 model_id 均可,
    由 `_model_cfg` 归一);先刷新单价配置表缓存,再按 单价表 > 模型手填 > 兜底 取价。
    任何异常都回退估算,不阻塞任务收尾。
    """
    await _refresh_model_prices_cache_if_stale()
    cfg: dict | None = None
    model_id = model_key or ""
    try:
        from cogni_agent.runtime_service.llm.client import _model_cfg

        if model_key:
            _key, cfg = await _model_cfg(model_key)
            model_id = (cfg or {}).get("model_id") or model_key
    except Exception as exc:  # noqa: BLE001
        log.warning("[cost] 解析模型配置失败({}),按 {!r} 兜底估算: {}", model_key, model_id, exc)
    return estimate_cost_cny(model_id, prompt_tokens, completion_tokens, cfg)
