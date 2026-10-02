"""
M4-08:LiteLLM 多模型路由封装。

本模块承担三件事,顺序上都发生在真正打到模型之前:
1. **模型解析**:运行时登记的模型(管理后台「系统设置·模型与预算」)优先于 config.yaml
   的静态清单;解析结果带 TTL 缓存,避免每次 LLM 调用都打一次数据库(一个 10 步的
   ReAct 循环原本会产生 10+ 次多余的 DB 往返)。
2. **超时与重试**:任何一次模型调用都必须有超时——否则模型端 hang 住会一直占着 worker
   槽位,只能等租约巡检回收。瞬时错误(超时/5xx/限流)做有限次指数退避重试,确定性
   错误(4xx 参数错、鉴权失败)立即抛出不重试。此外还有**按模型的熔断器**:模型连续
   失败达到阈值后进入冷却期,期间请求秒级快速失败(不打模型),冷却结束放行一个探测
   请求、成功即恢复——避免模型故障时每个任务各自挂满超时重试(一次故障拖垮一批任务)。
3. **用量回传**:所有调用(包括目标漂移自检这类"内部"调用)都返回真实 token 数,由
   调用方计入护栏与成本账,不允许有绕过计费的旁路调用。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import litellm

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("runtime_service.llm.client")

# ── 模型配置缓存(进程内,带 TTL) ──────────────────────────────────────────
# 运行时模型配置存在数据库里,允许管理后台改完不重启即时生效。但"即时"不需要精确到
# 每一次调用——加一个短 TTL 缓存,把 N 次 LLM 调用的 N 次查库压成每 TTL 一次。
# 配置变更最多延迟 TTL 秒生效,这个代价在运维上完全可接受。
_MODEL_CACHE_TTL_SECONDS = 30.0
_model_cache: dict[str, dict] = {}
_model_cache_at: float = 0.0
_model_cache_lock = asyncio.Lock()


def invalidate_model_cache() -> None:
    """管理后台改动模型配置后可主动调用,让下一次解析立刻回源。"""
    global _model_cache_at, _default_model_cache
    _model_cache_at = 0.0
    _default_model_cache = (0.0, None)


async def _db_model_configs() -> dict[str, dict]:
    global _model_cache, _model_cache_at
    now = time.monotonic()
    if _model_cache_at and (now - _model_cache_at) < _MODEL_CACHE_TTL_SECONDS:
        return _model_cache

    async with _model_cache_lock:
        # 双重检查:等锁期间可能已经有其它协程刷新过了
        now = time.monotonic()
        if _model_cache_at and (now - _model_cache_at) < _MODEL_CACHE_TTL_SECONDS:
            return _model_cache
        try:
            from cogni_agent.common.db import get_sessionmaker
            from cogni_agent.governance_event_bus_service.settings import get_active_model_configs

            async with get_sessionmaker()() as db:
                _model_cache = await get_active_model_configs(db)
            _model_cache_at = time.monotonic()
        except Exception:  # noqa: BLE001 - 数据库不可用时保留旧缓存,并优雅退回静态配置
            log.debug("查询 gov_llm_model_configs 失败,使用上次缓存/静态清单")
    return _model_cache


def _static_model_configs() -> dict[str, dict]:
    """config.yaml 里的静态模型清单,**自动跳过没配到 API Key 的条目**。

    config.yaml 里的 api_key 写的是 ${DEEPSEEK_API_KEY} 这类占位符,环境变量没配时会
    解析成空串。以前这类条目照样进清单,于是"看起来有三个模型可选",实际调用时才
    401/参数错。现在直接跳过——模型应该在「系统设置 · 模型与预算」页面里配,
    环境变量只是给不想用页面的部署留的兜底。

    例外:base_url 指向本地服务(ollama / vLLM / LM Studio 等)通常不需要 key,
    这类即使 api_key 为空也保留。
    """
    out: dict[str, dict] = {}
    for m in get_settings().get("runtime", "models", default=[]) or []:
        name = m.get("name")
        if not name:
            continue
        if not m.get("api_key") and not _is_keyless_endpoint(m.get("base_url")):
            log.debug(f"模型 {name} 未配置 API Key,跳过(可在「系统设置·模型与预算」中配置)")
            continue
        # 静态配置里的 name 就是供应商模型 ID,补齐 model_id 字段让调用组装统一走它
        out[name] = {**m, "model_id": m.get("model_id") or name}
    return out


def _is_keyless_endpoint(base_url: str | None) -> bool:
    """本地/自建推理服务通常不校验 API Key。"""
    if not base_url:
        return False
    return any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))


_default_model_cache: tuple[float, str | None] = (0.0, None)


async def _db_default_model() -> str | None:
    """页面上设的**对话**默认模型的 **id**(带 TTL 缓存,与模型配置缓存同一个周期)。

    返回不可变的 id 而非展示名:用户在页面改展示名不影响这个默认值,
    解析时按 id 主键直接命中 db 清单,不依赖任何可能变化的别名。
    """
    global _default_model_cache
    at, val = _default_model_cache
    if at and (time.monotonic() - at) < _MODEL_CACHE_TTL_SECONDS:
        return val
    try:
        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.governance_event_bus_service.settings import get_default_model_id

        async with get_sessionmaker()() as db:
            val = await get_default_model_id(db, "chat")
        _default_model_cache = (time.monotonic(), val)
    except Exception:  # noqa: BLE001 - 查不到就用文件配置,不因此让调用失败
        return val
    return val


def _match_db_config(db_models: dict[str, dict], name: str) -> str | None:
    """在 db 模型清单中按别名定位模型,返回清单 key(不可变的模型 id)。

    匹配顺序:id → 展示名 name → 供应商 model_id。后两者用户都可以在页面改
    (改展示名/换供应商 ID),因此只能当别名兜底,不能当身份标识。
    """
    if name in db_models:
        return name
    for _k, _v in db_models.items():
        if _v.get("name") == name or (_v.get("model_id") and _v["model_id"] == name):
            log.info(f"模型名 {name!r} 命中「{_v.get('name') or _k}」")
            return _k
    return None


async def _model_cfg(model_name: str | None) -> tuple[str, dict]:
    """解析实际使用的模型,返回 (清单key, 配置)。

    清单 key 是**不可变的模型 id**(db 清单)或静态配置名(config.yaml 的静态清单
    不会在运行时改,key 即 name)。调用方显式指定时三种形态都能解析:
    模型 id / 展示名 name / 供应商 model_id —— 后两者允许页面改名,只能当别名匹配。

    优先级:
      1. 调用方显式指定
      2. 页面上「设为默认」的**对话**模型(gov_llm_model_configs.model_type='chat' 且 is_default)
      3. **页面上配置的任意一个可用对话模型**(排除嵌入模型)
      4. config.yaml 的 runtime.default_model

    第 3 条是这次补上的,它解决一个很坑的情况:用户在页面配好了模型和 Key,却没点
    「设为默认」,于是解析回落到 config.yaml 的 default_model —— 而那个名字可能压根
    不在他配置的模型里,结果报「未知模型 'xxx'」。用户的意图明明白白:我配了模型就是
    要用它。所以只要页面上有可用对话模型,就绝不回落到配置文件里那个可能已经过时的名字。
    嵌入模型(model_type='embedding',向量入库/检索用)不参与对话兜底。

    这种情况会打一条 WARNING 提示去设默认,因为有多个模型时"取最早的一个"只是兜底,
    不是用户显式表达的选择。
    """
    if model_name:
        name = model_name
    else:
        name = await _db_default_model()
        if not name:
            db_models = await _db_model_configs()
            chat_models = {k: v for k, v in db_models.items() if v.get("model_type", "chat") != "embedding"}
            if chat_models:
                # 页面上有对话模型但没设默认:用第一个,并提醒去设置。绝不回落到 config.yaml——
                # 那个名字很可能和用户实际配置的模型对不上。
                name = next(iter(chat_models))
                log.warning(
                    f"「系统设置 · 模型与预算」里有 {len(chat_models)} 个对话模型但都没有设为默认,"
                    f"本次使用 {chat_models[name].get('name', name)!r}。建议明确点一下「设为默认」,"
                    f"否则新增/删除模型时实际使用哪个会跟着变。"
                )
            else:
                name = get_settings().get("runtime", "default_model")

    db_models = await _db_model_configs()
    key = _match_db_config(db_models, name)
    if key:
        return key, db_models[key]

    models = _static_model_configs()
    cfg = models.get(name)
    if not cfg:
        # 静态清单的 key 即 name,但 runtime.default_model 也可能填的是供应商 ID,兜底匹配
        for _k, _v in models.items():
            if _v.get("model_id") == name:
                cfg = _v
                break
    if not cfg:
        available_db = sorted(_v.get("name") or k for k, _v in db_models.items())
        available_file = sorted(models)
        if available_db:
            raise ValueError(
                f"未知模型 {name!r}。「系统设置 · 模型与预算」里已配置的是 {available_db};"
                f"请在页面上把要用的那个「设为默认」,或修改 config.yaml 的 runtime.default_model。"
            )
        if available_file:
            raise ValueError(
                f"未知模型 {name!r}。config.yaml 里可用的是 {available_file},"
                f"且「系统设置 · 模型与预算」中还没有配置任何模型。"
            )
        raise ValueError(
            "系统中还没有任何可用模型。请到「系统设置 · 模型与预算」添加一个模型(填好 API Key)"
            "并设为默认。"
        )
    return name, cfg


# ── 超时与重试 ────────────────────────────────────────────────────────────
def _runtime_cfg(key: str, default):
    return get_settings().get("runtime", "llm", key, default=default)


async def _aiter_with_idle_timeout(stream, idle_seconds: float):
    """流式迭代 + chunk 间隙空闲超时。

    没有这个兜底时,模型端建立连接后一直不吐数据(排队/半开 hang),`async for chunk`
    会无限挂起——`asyncio.timeout` 包住整个图也没用,因为 LLM 调用不响应取消,
    任务会一直 RUNNING 直到租约巡检才回收(期间前端光标一直闪、发送按钮锁死)。

    每次取下一个 chunk 限时 idle_seconds,超时抛 TimeoutError(上层 `_is_retryable`
    会重试整个调用)。注意:超时会取消 `__anext__`,该流进入损坏状态——但超时后我们
    直接抛错离开、不再迭代它,重试会发起全新请求,因此无影响。
    """
    it = stream.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=idle_seconds)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise TimeoutError(f"模型流式响应超过 {idle_seconds}s 无数据,判定为挂起") from None
        yield chunk


def _is_retryable(exc: BaseException) -> bool:
    """只重试"再试一次可能就好了"的错误;参数错误/鉴权失败重试多少次都是一样的结果。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    for cls_name in ("Timeout", "APIConnectionError", "ServiceUnavailableError",
                     "RateLimitError", "InternalServerError", "APIError"):
        cls = getattr(litellm.exceptions, cls_name, None)
        if cls and isinstance(exc, cls):
            # APIError 是基类,按状态码细分:4xx 不重试(除 429 限流)
            status = getattr(exc, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                return False
            return True
    return False


# ── LLM 熔断器(按模型分桶,进程内) ────────────────────────────────────────
# 模型服务故障(超时/5xx/限流)时,与其让每个任务各自重试挂满超时,不如在统一出口按
# 模型维度快速熔断:连续失败达到阈值后,冷却期内直接抛错(不打模型),冷却结束后放行
# 一个探测请求,成功即恢复、失败则重新熔断。这让意图分诊/ReAct 循环在模型故障时
# 秒级拿到失败并让任务快速落 FAILED,而不是每个任务都挂满 ~3 分钟。
# 注意:熔断状态是进程内的,多 worker 部署下各自独立(可接受——熔断本就是局部降级)。
_breaker_failure_threshold = 2       # 连续失败的独立调用次数阈值(同一请求的多重试只计一次)
_breaker_cooldown_seconds = 30.0     # 熔断冷却时间:期间请求快速失败,冷却后放行探测
_breakers: dict[str, "_Breaker"] = {}


class _Breaker:
    """单模型熔断状态:CLOSED(正常) → OPEN(熔断) → HALF_OPEN(冷却结束放行探测)。"""

    __slots__ = ("failures", "half_open", "opened_at")

    def __init__(self) -> None:
        self.failures = 0
        self.half_open = False
        self.opened_at = 0.0

    def open(self, now: float) -> None:
        """进入 OPEN:清掉半开标记,记录熔断时刻。"""
        self.half_open = False
        self.opened_at = now

    def reset(self) -> None:
        """调用成功:关闭熔断,清零计数。"""
        self.failures = 0
        self.half_open = False
        self.opened_at = 0.0


# 厂商拒绝模型名时,响应体里通常**直接列出了可用的名字**:
#   "The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed deepseek-r1"
# 这是排查这类问题最关键的一句,但它被埋在上百行 litellm 堆栈里。
_SUPPORTED_RE = re.compile(
    r"supported\s+(?:API\s+)?model\s+names?\s+(?:are|is)[:\s]+(.+?)(?:,?\s*but\s+you\s+passed|[.\n]|$)",
    re.I)


def _explain_if_model_rejected(exc: Exception, model: str) -> Exception:
    """把「模型名不被该端点接受」翻译成能照着改的说明。

    **不改变异常类型**,只换一句人话 —— 上层的重试/熔断判断都依赖异常类型,
    换了类型会把那些逻辑一起打乱。
    """
    text = str(exc)
    low = text.lower()
    if "supported" not in low or "model" not in low:
        return exc
    m = _SUPPORTED_RE.search(text)
    if not m:
        return exc

    names = [n.strip().strip("`'\"") for n in m.group(1).split(",") if n.strip()]
    if not names:
        return exc
    passed = model.split("/", 1)[-1] or model
    try:
        return type(exc)(
            f"这个接口地址不认识模型名 {passed!r}。\n"
            f"它支持的模型是:{'、'.join(names)}\n"
            f"请到「系统设置 · 模型与预算」把模型名改成其中之一,或换一个匹配的接口地址。"
        )
    except Exception:  # noqa: BLE001 - 某些异常类型的构造签名不接受单个字符串
        return exc


async def _acompletion_with_retry(**kwargs):
    """统一出口:所有模型调用都从这里走,保证超时、重试与熔断策略一致。

    调用方可通过 kwargs 覆盖默认策略(如 Triager 用更短超时、失败即降级不重试):
    `timeout` / `read_timeout` / `max_retries` 会被 pop 出来用于本次调用,不透传给
    litellm。

    超时分层(与行业主流一致):
    - `read_timeout`(read 语义:两次读数据之间的最大间隔)传给 litellm/httpx,默认
      300s——给长生成(推理模型)留足空间;
    - `timeout`(total 语义:整次调用的硬上限)由应用层 asyncio.wait 兜底,默认 60s。
      因为 litellm 对部分 provider(aiohttp transport)的 timeout 传递不可靠,配置 60s
      实际可能 hang 到 180s+ 才抛(conv_12739f 事故),所以 total 必须在应用层强制。
    """
    timeout = float(kwargs.pop("timeout", _runtime_cfg("request_timeout_seconds", 60)))
    read_timeout = float(kwargs.pop("read_timeout", _runtime_cfg("read_timeout_seconds", 300)))
    max_retries = int(kwargs.pop("max_retries", _runtime_cfg("max_retries", 2)))
    backoff = _runtime_cfg("retry_backoff_seconds", 1.0)
    cb = _runtime_cfg("circuit_breaker", {}) or {}
    cb_enabled = bool(cb.get("enabled", True))
    cb_threshold = int(cb.get("failure_threshold", _breaker_failure_threshold))
    cb_cooldown = float(cb.get("cooldown_seconds", _breaker_cooldown_seconds))

    model_key = str(kwargs.get("model") or "?")
    breaker = _breakers.setdefault(model_key, _Breaker())
    now = time.monotonic()

    # 熔断快速失败:OPEN 冷却期内 / HALF_OPEN 探测中 → 不打模型,立即抛错
    if cb_enabled and (breaker.half_open or breaker.failures >= cb_threshold):
        if breaker.half_open or now - breaker.opened_at < cb_cooldown:
            log.warning(
                f"[debug] 模型 {model_key} 处于熔断冷却期(已连续失败 "
                f"{breaker.failures} 次),本次请求快速失败"
            )
            raise RuntimeError(
                f"模型 {model_key} 连续失败已触发熔断(冷却 {cb_cooldown:.0f}s),"
                "请检查模型服务/配额/接入点状态后重试"
            )
        breaker.half_open = True  # 冷却结束,放行这一次探测请求

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            # 超时分层:litellm 传 read_timeout(read 语义,默认 300s,长生成留足空间),
            # 应用层 asyncio.wait 用 total(timeout)做硬兜底——litellm 对部分 provider
            # (aiohttp transport)的 timeout 传递不可靠,配置 60s 实际可能 hang 到 180s+
            # 才抛(conv_12739f 事故)。注意不能依赖 wait_for——它超时后要等被取消的
            # 协程真正结束,而 LLM 调用可能不响应取消,会跟着一起挂住;所以超时后
            # cancel,再给最多 2s 宽限期让底层 HTTP 连接真正关闭(避免僵尸请求占着
            # 服务端队列/限流配额——此前不等待直接抛,叠加重试可能多个大请求同时
            # 挂在 ark 上),随后立即抛错,不再等待后台协程回收。
            task = asyncio.ensure_future(litellm.acompletion(timeout=read_timeout, **kwargs))
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                task.cancel()
                try:
                    await asyncio.wait({task}, timeout=2.0)
                except Exception:  # noqa: BLE001 - 宽限期只是尽力而为,不因取消失败再抛
                    pass
                raise TimeoutError(
                    f"模型调用超过 {timeout}s 未完成,判定为挂起,已取消该次尝试"
                )
            resp = task.result()
            if cb_enabled:
                breaker.reset()
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= max_retries or not _is_retryable(exc):
                break
            delay = backoff * (2 ** attempt)
            log.warning(f"模型调用失败({type(exc).__name__}),{delay:.1f}s 后第 {attempt + 1} 次重试")
            await asyncio.sleep(delay)

    # 整次调用(含所有重试)最终失败时才计一次熔断计数。同一请求的重试是"自我抢救",
    # 不该把一次慢请求的多次尝试叠加成"连续失败"去熔断整个模型——否则 Triager 一次
    # 超时降级后,降级路径(L2 全量执行)会被同一个熔断器快速失败堵死,「失败降级、
    # 可用性优先」的设计就失效了(Triager 只是轻量分诊,超时多半是大请求生成慢,
    # 并不代表模型整体故障)。
    if last_exc is not None:
        if cb_enabled and _is_retryable(last_exc):
            breaker.failures += 1
            if breaker.failures >= cb_threshold:
                breaker.open(time.monotonic())
                log.warning(
                    f"模型 {model_key} 连续失败 {breaker.failures} 次,触发熔断 "
                    f"{cb_cooldown:.0f}s(期间请求将秒级快速失败)"
                )
        raise last_exc
    return None  # pragma: no cover - 循环必然在 success 或 raise 中结束


def _litellm_model(model_cfg: dict, name: str) -> str:
    """拼出 litellm 的模型标识 —— 用供应商模型 ID(model_id),不是展示名(name)。

    name 只是给人看的别名(如「豆包对话」),真正调模型要用供应商提供的 ID
    (如 deepseek-chat / ep-xxx / doubao-embedding-vision-251215)。

    国内厂商(火山方舟/DeepSeek/百炼/智谱/Kimi)基本都提供 OpenAI 兼容接口,统一走
    `openai/` 前缀 + 自定义 base_url,而不是依赖 litellm 内置的各家适配 ——
    内置适配的字段映射有时会滞后于厂商更新,而兼容接口的行为是可预期的。
    换厂商只需要改 base_url。
    """
    from cogni_agent.runtime_service.llm.providers import litellm_prefix

    return f"{litellm_prefix(model_cfg.get('provider', 'openai'))}/{model_cfg.get('model_id') or name}"


@dataclass
class ToolCallDelta:
    id: str | None = None
    name: str | None = None
    arguments: str = ""


def _usage_cost_usd(usage: Any) -> float | None:
    """提取模型/网关在响应里自带的成本(USD)。

    litellm 1.93 的 Usage 有 `cost` 字段(总成本,USD),部分模型/网关在 usage 里
    直接返回计费;老版本或个别实现走 `total_cost`。两者都没有返回 None,
    表示"模型侧没有返回计费",调用方据此决定是否展示与本地估算的差异。
    """
    for attr in ("cost", "total_cost"):
        try:
            v = getattr(usage, attr, None)
        except Exception:  # noqa: BLE001 - 个别响应对象属性访问可能抛错,兜底
            v = None
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


@dataclass
class LLMResult:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","name","arguments"(str)}]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None
    # 思考流(30 号,推理模型):reasoning_content 增量累积——仅当轮流式展示
    # (SSE reasoning 事件),不进对话历史(推理模型的思考是一次性的,回传下轮
    # 会污染上下文且多数服务报错,DeepSeek 官方明确要求)。
    reasoning_content: str = ""
    # 模型/网关响应里自带的成本(USD)。绝大多数国内模型不返回,None 表示没有。
    cost_usd: float | None = None
    # 本次调用实际使用的模型清单 key(不可变 model id),用于成本明细按模型归集。
    model: str | None = None
    # ── 耗时拆分(执行日志用,毫秒;0 = 没测到,如 mock / 非流式) ──
    # 一次「内容生成」86 秒(任务 5877db72 第 6 步),以前只能看到一个总数:
    # 分不清是排队 / 首包慢,还是模型在思考,还是在往外吐字。拆成三段就清楚了:
    #   ttft_ms          发出请求 → 收到第一个有内容的分片(排队 + 预填充)
    #   first_output_ms  发出请求 → 第一个正文 / 工具调用分片(之前那段是在思考)
    #   total_ms         发出请求 → 流结束
    ttft_ms: float = 0.0
    first_output_ms: float = 0.0
    total_ms: float = 0.0


# ── Mock 模型(本地离线测试用) ─────────────────────────────────────────────
# 在「系统设置 · 模型与预算」里配置一个名字在 MOCK_MODEL_NAMES 里的模型(或 provider
# 填 mock),所有用到它的调用不再经过真实 LLM,直接返回本地模拟数据——完全不依赖
# 真实 LLM API 也能跑通完整流程(意图判定/任务执行/漂移自检/推荐追问)。
MOCK_MODEL_NAMES = {"test"}
_MOCK_NO_SYSTEM_REPLY = (
    "刚才的回答解决了你的疑问吗?\n"
    "想了解更详细的方案吗?\n"
    "需要我针对你的场景进一步说明吗?"
)


def _is_mock_model(name: str, model_cfg: dict) -> bool:
    """是否命中测试模型:配置的展示名/供应商 ID 在 MOCK_MODEL_NAMES 里(大小写不敏感),或 provider 填了 mock。

    注意 name 参数是解析链返回的清单 key(可能是不可变的模型 id),因此判定优先取
    配置里的展示名 / model_id —— 页面配置 mock 模型时,名字填在 name 或 model_id 都行。
    """
    if model_cfg.get("provider") == "mock":
        return True
    return (model_cfg.get("name") or model_cfg.get("model_id") or name or "").strip().lower() in MOCK_MODEL_NAMES


def _mock_reply_content(messages: list[dict]) -> str:
    """mock 回复:回显最后一条 user 消息,让回答看起来贴合当前问题。"""
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = str(m.get("content") or "").strip()[:80]
            break
    if not user_text:
        user_text = "(空)"
    return (
        f"(模拟回复 · 测试模型)收到你的问题:「{user_text}」。"
        "当前处于离线测试模式,本条内容由本地 mock 生成,不代表真实模型能力。"
        "需要正式使用时,请在「系统设置 · 模型与预算」切换模型。"
    )


def _mock_llm_result(messages: list[dict]) -> LLMResult:
    """构造与真实响应等价结构的 mock 结果;token 按字符粗估,预算护栏与成本账照常生效。

    有 system 的调用是主执行链路(agent/意图/漂移自检等),返回贴合问题的文本;
    无 system 的调用是「推荐追问」这类辅助调用,按其协议返回多行问题列表。
    """
    if any(m.get("role") == "system" for m in messages):
        content = _mock_reply_content(messages)
    else:
        content = _MOCK_NO_SYSTEM_REPLY
    result = LLMResult(content=content, finish_reason="stop")
    result.prompt_tokens, result.completion_tokens = _estimate_tokens(messages, result)
    return result


async def _mock_stream(messages: list[dict], writer: Callable[[dict], None] | None) -> LLMResult:
    """mock 同样模拟流式:把回复内容按小段逐个 writer 推送,SSE 打字机效果与真实模型一致。

    30 号:先推一小段 reasoning 增量(模拟推理模型的思考流),供本地无真实
    thinking 模型时测试 SSE reasoning 事件链路。
    """
    result = _mock_llm_result(messages)
    if writer:
        for i in range(0, 24, 8):
            writer({"type": "reasoning", "delta": f"(mock 思考流 {i // 8 + 1})用户在问数据,需要先确定时间窗口…"[i:i + 8]})
        step = 6
        for i in range(0, len(result.content), step):
            writer({"type": "token", "delta": result.content[i:i + step]})
    return result


async def complete(model_name: str | None, messages: list[dict], **kwargs) -> Any:
    """统一模型调用入口(非流式)。返回 litellm 原始响应对象。"""
    name, model_cfg = await _model_cfg(model_name)
    return await _acompletion_with_retry(
        model=_litellm_model(model_cfg, name),
        messages=messages,
        api_key=model_cfg.get("api_key"),
        base_url=model_cfg.get("base_url"),
        **kwargs,
    )


async def complete_collected(model_name: str | None, messages: list[dict], **kwargs) -> LLMResult:
    """非流式调用 + 统一成 LLMResult(带 token 数),供内部辅助调用(漂移自检/查询改写/
    输出审核)使用——这样这些"内部"调用的 token 也能被调用方计入护栏与成本账,
    不会出现绕过计费的旁路调用。"""
    name, model_cfg = await _model_cfg(model_name)
    if _is_mock_model(name, model_cfg):
        log.info(f"模型 {model_cfg.get('name') or name} 命中测试模型,返回 mock 数据")
        return _mock_llm_result(messages)
    resp = await complete(name, messages, **kwargs)
    result = LLMResult(model=name)
    try:
        choice = resp.choices[0]
        result.content = getattr(choice.message, "content", None) or ""
        result.finish_reason = getattr(choice, "finish_reason", None)
    except (AttributeError, IndexError):
        pass
    usage = getattr(resp, "usage", None)
    if usage:
        result.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        result.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        result.cost_usd = _usage_cost_usd(usage)
    return result


async def stream_and_collect(
    model_name: str | None,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    response_format: dict | None = None,
    writer: Callable[[dict], None] | None = None,
    **kwargs: Any,
) -> LLMResult:
    """
    M4-08 + 文档 1.2「流式(stream)」:真正的逐 token 推送——调用方(graphs.py 的
    agent 节点)传入 langgraph 的 get_stream_writer() 作为 writer,每收到一个内容
    增量就 writer({"type":"token","delta":...}),SSE 端点据此实时打字机输出。

    同时按 OpenAI 流式协议的 index 累积工具调用分片(id/name 只在首个分片出现,
    arguments 分片拼接),流结束后返回与非流式响应等价的结构化结果,让上层(工具
    调用解析/校验)不需要关心底层是否走了流式通道。

    某些国产/自建模型的 OpenAI 兼容层不支持 stream_options.include_usage,拿不到
    真实 Token 数时退化为按字符粗估(见 _estimate_tokens),避免用量与预算护栏
    静默归零——宁可不准,也不能不记。
    """
    name, model_cfg = await _model_cfg(model_name)
    if _is_mock_model(name, model_cfg):
        log.info(f"模型 {model_cfg.get('name') or name} 命中测试模型,返回 mock 流式数据")
        return await _mock_stream(messages, writer)
    result = LLMResult(model=name)
    tool_deltas: dict[int, ToolCallDelta] = {}
    _t0 = time.monotonic()

    def _mark(first_output: bool) -> None:
        ms = (time.monotonic() - _t0) * 1000
        if not result.ttft_ms:
            result.ttft_ms = round(ms, 1)
        if first_output and not result.first_output_ms:
            result.first_output_ms = round(ms, 1)

    stream = await _acompletion_with_retry(
        model=_litellm_model(model_cfg, name),
        messages=messages,
        api_key=model_cfg.get("api_key"),
        base_url=model_cfg.get("base_url"),
        tools=tools or None,
        tool_choice=tool_choice if tools else None,
        response_format=response_format,
        stream=True,
        stream_options={"include_usage": True},
        **kwargs,
    )

    stream_idle = _runtime_cfg("stream_idle_timeout_seconds", 120)
    async for chunk in _aiter_with_idle_timeout(stream, stream_idle):
        usage = getattr(chunk, "usage", None)
        if usage:
            result.prompt_tokens = getattr(usage, "prompt_tokens", 0) or result.prompt_tokens
            result.completion_tokens = getattr(usage, "completion_tokens", 0) or result.completion_tokens
            cost_usd = _usage_cost_usd(usage)
            if cost_usd is not None:
                result.cost_usd = cost_usd
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if getattr(choice, "finish_reason", None):
            result.finish_reason = choice.finish_reason
        if delta is None:
            continue
        content = getattr(delta, "content", None)
        if content:
            _mark(True)
            result.content += content
            if writer:
                writer({"type": "token", "delta": content})
        # 思考流(30 号):推理模型(DeepSeek-R1/GLM thinking/Qwen3 等)的
        # delta.reasoning_content——有则流(前端灰色思考区),无则静默
        # (普通模型优雅降级,零配置面)。只流出展示,不进 messages 历史。
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            _mark(False)
            result.reasoning_content += reasoning
            if writer:
                writer({"type": "reasoning", "delta": reasoning})
        delta_tool_calls = getattr(delta, "tool_calls", None)
        if delta_tool_calls:
            _mark(True)
            for tc in delta_tool_calls:
                idx = getattr(tc, "index", 0) or 0
                acc = tool_deltas.setdefault(idx, ToolCallDelta())
                if getattr(tc, "id", None):
                    acc.id = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        acc.name = fn.name
                    if getattr(fn, "arguments", None):
                        acc.arguments += fn.arguments

    for idx in sorted(tool_deltas.keys()):
        acc = tool_deltas[idx]
        if acc.name:
            result.tool_calls.append({"id": acc.id or f"call_{idx}", "name": acc.name, "arguments": acc.arguments})

    if not result.prompt_tokens and not result.completion_tokens:
        result.prompt_tokens, result.completion_tokens = _estimate_tokens(messages, result)
        log.debug(f"模型 {name} 未返回 usage,按字符粗估 token(prompt≈{result.prompt_tokens})")
    result.total_ms = round((time.monotonic() - _t0) * 1000, 1)
    return result


def _estimate_tokens(messages: list[dict], result: LLMResult) -> tuple[int, int]:
    """兜底粗估:中文约 1.5 字符/token,英文约 4 字符/token,统一按 2.5 取中。
    只在模型不返回 usage 时使用,目的是让预算护栏不至于完全失效。"""
    def _chars(text: Any) -> int:
        return len(text) if isinstance(text, str) else len(str(text or ""))

    prompt_chars = sum(_chars(m.get("content")) for m in messages)
    completion_chars = _chars(result.content) + sum(_chars(tc.get("arguments")) for tc in result.tool_calls)
    return int(prompt_chars / 2.5), int(completion_chars / 2.5)
