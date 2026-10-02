# -*- coding: utf-8 -*-
"""分诊模型不可用时的回落。

现场:执行链里一句「意图分诊:Triager 不可用,已降级直接执行」,而且是 **0ms** 失败 ——
不是超时,是立刻抛异常。根因是 config.yaml 里配的 `runtime.intent_check.triage_model`
(一个火山方舟的 Doubao 模型)在这套环境的「模型配置」里没登记,而主链路用的是别的模型。

关键点:`llm/client._model_cfg` 的四级兜底**只在没显式指定模型时生效**。
分诊模型是显式指定的,解析不到直接抛「未知模型」—— 于是每一轮都降级,
意图分诊(路由裁剪 / 参数抽取 / 多轮改写)整个废掉,而主模型明明好好的。
intent.py 的注释当时还写着「名字拼错也只回落不会崩」,与实际相反。

分诊模型只是个**提速选项**,不该因为它没登记就把意图分诊废掉。
"""
from __future__ import annotations

import asyncio
import functools
import sys
import types

import pytest

# 沙箱里没装 litellm(llm/client 顶部 import 它)。这组用例测的是 intent.py 的**回落判定**,
# 与具体的 LLM SDK 无关 —— 塞一个空壳进 sys.modules,让 client 能被导入即可。
if "litellm" not in sys.modules:
    _stub = types.ModuleType("litellm")
    _stub.exceptions = types.SimpleNamespace()
    for _n in ("acompletion", "completion", "token_counter", "cost_per_token", "aembedding"):
        setattr(_stub, _n, lambda *a, **k: None)
    _stub.utils = types.SimpleNamespace(ModelResponse=object)
    sys.modules["litellm"] = _stub


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


class _Resp:
    def __init__(self, content):
        self.content, self.prompt_tokens, self.completion_tokens = content, 10, 5


_OK_JSON = '{"action":"execute","intent":"report","target_skills":["we"],"reason":"输出报告"}'


def _patch_llm(monkeypatch, *, fails_for: set[str], record: list):
    """只让 fails_for 里的模型抛「未知模型」,其余正常返回分诊 JSON。"""
    from cogni_agent.runtime_service.llm import client as llm_client

    async def fake(model, messages, **kw):
        record.append(model)
        if model in fails_for:
            raise ValueError(f"未知模型 {model!r}。「系统设置 · 模型与预算」里已配置的是 ['deepseek-v4']")
        return _Resp(_OK_JSON)

    monkeypatch.setattr(llm_client, "complete_collected", fake)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    # 缓存会让第二次调用不再进 LLM,掩盖回落行为
    from cogni_agent.runtime_service.engine import intent

    monkeypatch.setattr(intent, "_cfg", lambda k, d: 0 if "cache" in k else d)


def test_unregistered_triage_model_falls_back_to_the_main_model(monkeypatch):
    from cogni_agent.runtime_service.engine import intent

    calls: list = []
    _patch_llm(monkeypatch, fails_for={"doubao-not-registered"}, record=calls)
    monkeypatch.setattr(intent, "_triage_model", lambda fb: "doubao-not-registered")

    r = asyncio.run(intent.triage(
        goal="景区 PFTSCA01018088 输出一下报告",
        capabilities=[{"name": "we", "description": "报告技能"}],
        slots={}, model="deepseek-v4"))

    assert r.degraded is False, "分诊模型没登记不该把整个意图分诊废掉"
    assert calls == ["doubao-not-registered", "deepseek-v4"], calls
    assert r.target_skills == ["we"]


def test_still_degrades_when_the_main_model_is_broken_too(monkeypatch):
    """回落之后主模型也不行,才是真降级 —— 并且要说清为什么。"""
    from cogni_agent.runtime_service.engine import intent

    calls: list = []
    _patch_llm(monkeypatch, fails_for={"doubao-not-registered", "deepseek-v4"}, record=calls)
    monkeypatch.setattr(intent, "_triage_model", lambda fb: "doubao-not-registered")

    r = asyncio.run(intent.triage(goal="随便问问", capabilities=[], slots={}, model="deepseek-v4"))
    assert r.degraded is True
    assert "回落主模型后仍失败" in r.degraded_reason, r.degraded_reason


def test_non_model_errors_do_not_trigger_a_second_call(monkeypatch):
    """超时之类的错误不该再拿主模型重跑一遍 —— 那只是让用户多等一次。"""
    from cogni_agent.runtime_service.engine import intent
    from cogni_agent.runtime_service.llm import client as llm_client

    calls: list = []

    async def fake(model, messages, **kw):
        calls.append(model)
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_client, "complete_collected", fake)
    monkeypatch.setattr(intent, "_triage_model", lambda fb: "doubao-not-registered")

    r = asyncio.run(intent.triage(goal="随便问问", capabilities=[], slots={}, model="deepseek-v4"))
    assert r.degraded is True and len(calls) == 1, calls
    assert "TimeoutError" in r.degraded_reason


def test_degraded_reason_explains_bad_json(monkeypatch):
    """返回非法 JSON 时也要说清是「这个模型不擅长严格 JSON」,而不是一句「不可用」。"""
    from cogni_agent.runtime_service.engine import intent
    from cogni_agent.runtime_service.llm import client as llm_client

    async def fake(model, messages, **kw):
        return _Resp("这是一段大白话,不是 JSON")

    monkeypatch.setattr(llm_client, "complete_collected", fake)
    monkeypatch.setattr(intent, "_triage_model", lambda fb: None)

    r = asyncio.run(intent.triage(goal="随便问问", capabilities=[], slots={}, model="deepseek-v4"))
    assert r.degraded is True and "JSON" in r.degraded_reason
