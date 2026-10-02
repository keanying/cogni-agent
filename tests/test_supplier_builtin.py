# -*- coding: utf-8 -*-
"""景区 → 供应商 ID 内置(sys_supplier_ids)+ 前置取参失败不再算「模型格式错误」。

现场任务 ababbb8c:一轮并发 4 个票务工具,前置的供应商查询一出问题,
4 个调用各记一次「格式错误」,当轮超过 3 次上限,任务以「模型无法自我纠正」失败 ——
原因说的完全不对,模型也没有任何改正的机会。
"""
from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path

import pytest

from cogni_agent.runtime_service.engine import resolver as rv
from cogni_agent.runtime_service.engine import tools_adapter as ta
from cogni_agent.tool_center_service import builtin_tools as bi

ROOT = Path(__file__).resolve().parent.parent


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture(autouse=True)
def _clean_cache():
    bi._SUPPLIER_CACHE.clear()
    ta._TASK_PARAM_CACHE.clear()
    yield
    bi._SUPPLIER_CACHE.clear()
    ta._TASK_PARAM_CACHE.clear()


@pytest.fixture
def platform(monkeypatch):
    """数据平台替身:记下每次请求,返回可配置的 body。"""
    from cogni_agent.config import get_settings
    from cogni_agent.tool_center_service.sandbox import local

    calls = []
    state = {"body": {"status": True, "data": [{"scenicId": "PFTSCA01018088", "supplierId": [29821585]}]},
             "status_code": 200}

    async def execute(self, **kw):
        calls.append(kw)
        return {"status_code": state["status_code"], "body": state["body"]}

    monkeypatch.setattr(local.LocalSandbox, "execute", execute)
    real = get_settings()

    class _S:
        def get(self, *keys, default=None):
            if keys == ("tool_center", "secrets"):
                return {"data_platform_key": "pfk_FAKEtestKEYforFixtureONLY0000"}
            return real.get(*keys, default=default)

    import cogni_agent.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: _S())
    return calls, state


@run_async
async def test_builtin_returns_ids_and_caches(platform):
    calls, _ = platform
    r = await bi.builtin_supplier_ids(scenic_id="PFTSCA01018088")
    assert r == {"scenicId": "PFTSCA01018088", "supplierId": [29821585]}
    req = calls[0]
    assert req["type_config"]["url"].endswith(bi.SUPPLIER_URI)
    assert req["type_config"]["headers"]["xpftkey"] == "{{data_platform_key}}", "密钥用占位,由沙箱替换"
    assert req["params"] == {"scenicId": "PFTSCA01018088"}
    again = await bi.builtin_supplier_ids(scenic_id="PFTSCA01018088")
    assert again["supplierId"] == [29821585] and len(calls) == 1, "10 分钟内不重复请求"


@run_async
async def test_builtin_explains_failures(platform):
    calls, state = platform
    assert "没有景区" in (await bi.builtin_supplier_ids())["error"]
    state["body"], state["status_code"] = {"status": False, "msg": "认证无效"}, 401
    r = await bi.builtin_supplier_ids(scenic_id="S1")
    assert "HTTP 401" in r["error"] and "认证无效" in r["error"]
    state["body"], state["status_code"] = {"status": True, "data": []}, 200
    assert "没有查到供应商" in (await bi.builtin_supplier_ids(scenic_id="S2"))["error"]


def test_builtin_is_registered_and_old_names_map_to_it():
    assert "sys_supplier_ids" in bi.BUILTIN
    assert bi.superseded_by("全局-通过景区ID获取供应商ID列表") == "sys_supplier_ids"
    assert bi.superseded_by("通过景区ID获取供应商ID列表") == "sys_supplier_ids"
    props = bi.BUILTIN["sys_supplier_ids"]["schema"]["properties"]
    assert set(props) == {"scenic_id"}, "景区由执行层按会话覆盖,不给模型别的入口"


def _spec(name, *, sources=None, type_="http"):
    return rv.ToolSpec(id=name, name=name, description="", type=type_, risk_level="low",
                       json_schema={"type": "object", "properties": {}}, type_config={},
                       param_sources=sources or {}, func_name=name)


def _builtin_spec():
    return next(s for s in rv._builtin_specs() if s.name == "sys_supplier_ids")


@run_async
async def test_old_reference_is_served_by_the_builtin(platform):
    """人改过、没被启动自动修正的票务工具,声明里还写着老工具名:由内置那一份提供。"""
    target = _spec("票务-今日报表汇总", sources={"supplierId": {
        "tool": "全局-通过景区ID获取供应商ID列表", "key": "$ctx.scenicId", "path": "body.data[0].supplierId"}})
    tools = {"sys_supplier_ids": _builtin_spec(), target.name: target}
    args, err = await ta._resolve_param_sources(None, task_id="t1", spec=target, merged_args={},
                                                bound_tools=tools, scenic_id="PFTSCA01018088",
                                                role="super_admin", user_id="u", depth=0)
    assert err is None and args["supplierId"] == [29821585]


@run_async
async def test_upstream_failure_is_an_error_not_a_format_error_and_is_cached(platform):
    calls, state = platform
    state["body"], state["status_code"] = {"status": False, "msg": "认证无效"}, 401
    src = {"supplierId": {"tool": "sys_supplier_ids", "key": "$ctx.scenicId", "path": "supplierId"}}
    tools = {"sys_supplier_ids": _builtin_spec()}
    kinds, msgs = [], []
    for name in ("票务-今日报表汇总", "票务-产品报表汇总", "票务-门票报表汇总", "票务-销售渠道报表汇总"):
        t = _spec(name, sources=src)
        tools[name] = t
        _, err = await ta._resolve_param_sources(None, task_id="t2", spec=t, merged_args={},
                                                 bound_tools=tools, scenic_id="PFTSCA01018088",
                                                 role="super_admin", user_id="u", depth=0)
        kinds.append(err.kind)
        msgs.append(err.content)
    assert kinds == ["error"] * 4, "上游/配置问题,改参数解决不了,不能算模型格式错误"
    assert len(calls) == 1, "同一任务里挂了的前置接口只打一次"
    assert "票务-产品报表汇总" in msgs[1] and "认证无效" in msgs[1], "缓存的失败要换成当前工具名"


def test_format_retries_count_rounds_not_calls():
    s = (ROOT / "cogni_agent/runtime_service/engine/graphs.py").read_text(encoding="utf-8")
    seg = s[s.index("_kinds = [o.kind for _c, o, _ms, _p in pairs]"):][:400]
    assert 'if "ok" in _kinds:\n        guardrail.format_retries = 0' in seg, "有成功就清零(「连续」)"
    assert 'elif "format_error" in _kinds:\n        gr.check_and_increment_format_retry(guardrail)' in seg
    loop = s[s.index("for call, outcome, real_ms, parallel in pairs:"):][:300]
    assert "check_and_increment_format_retry" not in loop, "不能再按单个调用累加"


def test_resolver_does_not_look_for_builtins_in_the_library():
    src = (ROOT / "cogni_agent/runtime_service/engine/resolver.py").read_text(encoding="utf-8")
    assert "ref not in bi.BUILTIN and not bi.superseded_by(ref)" in src


def test_superseded_http_tool_is_dropped_quietly():
    class Ctx:
        def __init__(self):
            self.infos, self.warns = [], []

        def info(self, code, msg, obj=None):
            self.infos.append(code)

        def warn(self, code, msg, obj=None):
            self.warns.append(code)

    ctx = Ctx()
    keep = _spec("票务-今日报表汇总")
    old = _spec("全局-通过景区ID获取供应商ID列表")
    out = rv._drop_superseded_builtins([keep, old], skill_name="we", skill_id="s", ctx=ctx)
    assert [t.name for t in out] == ["票务-今日报表汇总"]
    assert ctx.infos == ["tool_superseded_by_builtin"] and ctx.warns == [], "能调通的老工具被取代不是故障"


def test_disabled_superseded_tools_are_info_not_warnings():
    src = (ROOT / "cogni_agent/runtime_service/engine/resolver.py").read_text(encoding="utf-8")
    seg = src[src.index("if not tool.active or getattr(tool, \"deleted_at\", None):"):][:1200]
    assert 'ctx.info("tool_superseded"' in seg and "superseded_by(tool.name)" in seg
