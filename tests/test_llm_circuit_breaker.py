"""LLM 熔断器单测:模型连续失败后秒级快速失败,冷却后探测恢复。

覆盖场景:
- 连续失败的独立调用达到阈值(同一请求的多重试只计一次) → 熔断打开,后续请求不再发起真实调用
- 熔断打开期间请求立即抛 RuntimeError(不重试、不打模型)
- 冷却结束放行一个探测请求,成功即恢复(CLOSED)
- 半开探测期间其余请求被拒绝
- 熔断按模型分桶,一个模型熔断不影响其他模型
- 确定性错误(4xx/鉴权失败)不重试、不触发熔断
"""
import pytest

from cogni_agent.runtime_service.llm import client


@pytest.fixture(autouse=True)
def _reset_breakers():
    client._breakers.clear()
    yield
    client._breakers.clear()


class _Fake4xx(Exception):
    """非 litellm 异常(带 status_code),`_is_retryable` 应判为不重试。"""

    status_code = 400


class _Resp:
    pass


@pytest.mark.asyncio
async def test_open_after_consecutive_failures(monkeypatch):
    """连续 2 次独立调用失败后熔断;同一请求的多重试只计一次失败,后续调用不再发起真实请求。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        raise TimeoutError("模型端无响应")

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    # 第 1 次独立调用:1 次 + 2 次重试 = 3 次真实调用,整次只计 1 次失败(未达阈值 2,不熔断)
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 3  # 1 次调用 + 2 次重试
    assert client._breakers["openai/m1"].failures == 1

    # 第 2 次独立调用失败 → 累计 2 次,熔断打开
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 6
    assert client._breakers["openai/m1"].failures == 2

    # 熔断打开:不发起真实调用,立即抛 RuntimeError
    with pytest.raises(RuntimeError, match="熔断"):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 6  # 无新增真实调用


@pytest.mark.asyncio
async def test_half_open_recovers_after_success(monkeypatch):
    """冷却结束后放行探测,探测成功 → 熔断关闭,后续调用恢复正常。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 6:  # 前 2 次独立调用(各 1+2 重试)失败
            raise TimeoutError("挂起")
        return _Resp()

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    with pytest.raises(RuntimeError, match="熔断"):
        await client._acompletion_with_retry(model="openai/m1", messages=[])

    # 模拟冷却结束 → 半开放行探测
    client._breakers["openai/m1"].opened_at = 0.0
    resp = await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert isinstance(resp, _Resp)  # 探测请求真实发出并成功
    brk = client._breakers["openai/m1"]
    assert brk.failures == 0 and not brk.half_open  # 已恢复 CLOSED


@pytest.mark.asyncio
async def test_half_open_blocks_other_requests(monkeypatch):
    """半开探测期间,其余并发请求被拒绝(不打模型)。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        raise TimeoutError("挂起")

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    # 模拟已有请求进入半开探测(探测尚未返回)
    client._breakers["openai/m1"].opened_at = 0.0
    client._breakers["openai/m1"].half_open = True
    with pytest.raises(RuntimeError, match="熔断"):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 3  # 未发起新的真实调用


@pytest.mark.asyncio
async def test_breaker_is_per_model(monkeypatch):
    """熔断按模型分桶:一个模型熔断不影响其他模型。"""
    calls = {"openai/m1": 0, "openai/m2": 0}

    async def flaky(**kwargs):
        calls[kwargs["model"]] += 1
        raise TimeoutError("挂起")

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    # m1 连续 2 次独立调用失败(各 1+2 重试) → 熔断
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    with pytest.raises(RuntimeError, match="熔断"):
        await client._acompletion_with_retry(model="openai/m1", messages=[])

    # m2 不受影响,正常调用(1 次 + 2 次重试)
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m2", messages=[])
    assert calls["openai/m1"] == 6
    assert calls["openai/m2"] == 3


@pytest.mark.asyncio
async def test_non_retryable_error_does_not_trip(monkeypatch):
    """确定性错误(4xx)不重试、不计入失败数、不触发熔断。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        raise _Fake4xx("bad request")

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    with pytest.raises(_Fake4xx):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 1  # 不重试
    assert client._breakers["openai/m1"].failures == 0  # 不计数


@pytest.mark.asyncio
async def test_success_resets_failures(monkeypatch):
    """失败未达阈值前若成功,计数清零(不误熔断)。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("一次抖动")
        return _Resp()

    monkeypatch.setattr(client.litellm, "acompletion", flaky)

    resp = await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert isinstance(resp, _Resp)  # 第一次失败后重试成功
    assert client._breakers["openai/m1"].failures == 0  # 计数已清零


@pytest.mark.asyncio
async def test_disabled_breaker_passes_through(monkeypatch):
    """enabled=false 时熔断器不拦截、不计数,行为退化为纯重试。"""
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        raise TimeoutError("挂起")

    monkeypatch.setattr(client.litellm, "acompletion", flaky)
    monkeypatch.setattr(
        client, "_runtime_cfg",
        lambda key, default=None: {"enabled": False} if key == "circuit_breaker" else default,
    )

    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    with pytest.raises(TimeoutError):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 6  # 两次调用各 3 次(1+2 重试),熔断被禁用未拦截


@pytest.mark.asyncio
async def test_hard_timeout_cuts_hung_non_streaming(monkeypatch):
    """非流式调用挂起(不吐数据)时,硬超时兜底按时失败,不会无限等待。

    复现 conv_12739f 事故:litellm 对 aiohttp transport 的 timeout 传递不可靠,
    配置 60s 实际 182s 才抛。asyncio.wait 兜底保证单次尝试不超 timeout。
    """
    import asyncio

    calls = {"n": 0}

    async def hang(**kwargs):
        calls["n"] += 1
        await asyncio.sleep(3600)  # 模拟模型端挂起,不返回

    monkeypatch.setattr(client.litellm, "acompletion", hang)
    monkeypatch.setattr(
        client, "_runtime_cfg",
        lambda key, default=None: {
            "request_timeout_seconds": 0.05,
            "max_retries": 0,
        }.get(key, default),
    )

    with pytest.raises(TimeoutError, match="判定为挂起"):
        await client._acompletion_with_retry(model="openai/m1", messages=[])
    assert calls["n"] == 1  # 只发起一次真实调用,没有无限重试
    await asyncio.sleep(0)  # 让被取消的后台协程完成清理
