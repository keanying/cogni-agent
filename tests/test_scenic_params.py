# -*- coding: utf-8 -*-
"""景区级接口参数(0089 app_scenic_param)+ 执行链耗时留痕。

钉住的现场:任务 5877db72 —— OTA 直销接口要 account(商户账号),系统里没地方按景区配,
用户只能在对话里补一句「OTA商户ID我补充一下:vbk_569202」;分诊把它改写成
「……等待用户提出具体查询需求」,执行层拿着这句空目标从头规划,整轮 186 秒,
其中四次内容生成占 158 秒,而执行日志里分诊耗时恒为 0.0、prompt_tokens 被打成 ***。
"""
from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base

ROOT = Path(__file__).resolve().parent.parent
SCENIC = "PFTSCA01018088"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh_db():
    from cogni_agent.runtime_service.engine import scenic_params as sp

    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    sp.invalidate()
    yield
    sp.invalidate()


OTA_SCHEMA = {"type": "object", "properties": {
    "scenicId": {"type": "string"}, "startTime": {"type": "string"}, "endTime": {"type": "string"},
    "account": {"type": "string", "description": "OTA 商户账号"}}, "required": ["account"]}


def _p(values=("vbk_569202",), **kw):
    from cogni_agent.runtime_service.engine.scenic_params import ParamValue, ResolvedParam

    base = dict(key="ota_account", label="OTA 商户账号", arg_names=("account",),
                values=tuple(v if isinstance(v, ParamValue) else ParamValue(v) for v in values))
    base.update(kw)
    return ResolvedParam(**base)


def _graphs_fns(*names):
    """graphs.py 依赖 langchain,本沙箱没装 —— 只把这几个纯函数(和它们用到的常量)摘出来跑。"""
    import ast

    src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "graphs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names + ("_runtime_cfg_int",))
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "PRIOR_ANSWER_CHARS" for t in n.targets))]
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "graphs_subset", "exec"), ns)
    return [ns[n] for n in names]


# ── 一、注入规则 ───────────────────────────────────────────────────────

def test_fill_only_when_model_left_it_empty():
    from cogni_agent.runtime_service.engine.scenic_params import apply_to_args

    inj = apply_to_args(OTA_SCHEMA, {"scenicId": SCENIC}, [_p()])
    assert inj.args["account"] == "vbk_569202" and inj.fanout is None
    assert inj.notes == [{"arg": "account", "param": "ota_account", "how": "fill", "overrode_model_value": False}]
    # 模型(用户在对话里点名)给了别的账号:fill 模式尊重它
    inj = apply_to_args(OTA_SCHEMA, {"account": "vbk_other"}, [_p()])
    assert inj.args["account"] == "vbk_other" and inj.notes == []


def test_force_overrides_model_value_and_records_it():
    from cogni_agent.runtime_service.engine.scenic_params import apply_to_args

    inj = apply_to_args(OTA_SCHEMA, {"account": "vbk_made_up"}, [_p(fill_mode="force")])
    assert inj.args["account"] == "vbk_569202" and inj.notes[0]["overrode_model_value"] is True


def test_never_adds_args_the_tool_does_not_declare():
    """没声明的入参不往里塞:多出来的字段有的接口会报错,有的会被当成过滤条件。"""
    from cogni_agent.runtime_service.engine.scenic_params import apply_to_args

    schema = {"type": "object", "properties": {"scenicId": {"type": "string"}}}
    inj = apply_to_args(schema, {"scenicId": SCENIC}, [_p()])
    assert "account" not in inj.args and inj.notes == []


def test_multi_value_modes():
    """同一个景区多个商户账号:按定义里的多值用法 —— 逐个调用 / 列表一次 / 逗号拼接。"""
    from cogni_agent.runtime_service.engine.scenic_params import ParamValue, apply_to_args

    two = (ParamValue("vbk_569202", "携程主账号"), ParamValue("vbk_778899", "携程子账号"))
    inj = apply_to_args(OTA_SCHEMA, {}, [_p(values=two, multi_mode="fanout")])
    assert inj.fanout and inj.fanout["key"] == "ota_account" and inj.fanout["args"] == ["account"]
    assert inj.args["account"] == "vbk_569202", "先填第一个值过校验,执行时逐个替换"

    inj = apply_to_args(OTA_SCHEMA, {}, [_p(values=two, multi_mode="join")])
    assert inj.args["account"] == "vbk_569202,vbk_778899" and inj.fanout is None

    arr = {"type": "object", "properties": {"accounts": {"type": "array", "items": {"type": "string"}}}}
    inj = apply_to_args(arr, {}, [_p(values=two, multi_mode="fanout", arg_names=("accounts",))])
    assert inj.args["accounts"] == ["vbk_569202", "vbk_778899"] and inj.fanout is None, "入参本身是数组就一次传完"

    # 用户点名了其中一个 → 模型填了它,不再逐个调用
    inj = apply_to_args(OTA_SCHEMA, {"account": "vbk_778899"}, [_p(values=two, multi_mode="fanout")])
    assert inj.args["account"] == "vbk_778899" and inj.fanout is None


def test_schema_default_ref_is_system_filled_and_missing_is_reported():
    from cogni_agent.runtime_service.engine.scenic_params import apply_to_args, schema_param_refs

    schema = {"type": "object", "properties": {
        "account": {"type": "string", "default": "{{ota_account}}"},
        "shop": {"type": "string", "default": "{{shop_id}}"},
        "day": {"type": "string", "default": "{{today}}"}}}
    refs = schema_param_refs(schema)
    assert refs == {"account": "ota_account", "shop": "shop_id"}, "内置变量不算系统参数"
    inj = apply_to_args(schema, {}, [_p()], param_refs=refs)
    assert inj.args["account"] == "vbk_569202" and inj.missing == ["shop_id"]


def test_render_obj_whole_vs_inline():
    from cogni_agent.runtime_service.engine.scenic_params import ParamValue, by_key, render_obj

    pm = by_key([_p(values=(ParamValue("a1"), ParamValue("a2")), multi_mode="list")])
    out = render_obj({"accounts": "{{ota_account}}", "q": "账号 {{ota_account}} / {{today}}"}, pm)
    assert out == {"accounts": ["a1", "a2"], "q": "账号 a1,a2 / {{today}}"}
    assert render_obj("{{ota_account}}", pm, pick={"ota_account": "a2"}) == "a2"


def test_prompt_block_hides_secret_values_and_describes_multi():
    from cogni_agent.runtime_service.engine.scenic_params import ParamValue, prompt_block, prompt_vars

    two = (ParamValue("vbk_569202", "携程主账号"), ParamValue("vbk_778899"))
    txt = prompt_block([_p(values=two, multi_mode="fanout"),
                        _p(key="api_pwd", label="接口口令", values=("s3cr3t-9999",), arg_names=("pwd",),
                           is_secret=True, fill_mode="force")])
    assert "vbk_569202(携程主账号)" in txt and "共 2 个" in txt and "各调用一次" in txt
    assert "s3cr3t" not in txt and "接口口令" in txt and "固定值" in txt
    assert prompt_vars([_p(key="api_pwd", values=("s3cr3t-9999",), is_secret=True)]) == {"api_pwd": "(已配置)"}


def test_template_vars_cannot_override_builtins():
    from cogni_agent.runtime_service.engine.scenic_params import template_vars
    from cogni_agent.tool_center_service.sandbox import local

    tv = template_vars([_p()])
    assert tv == {"ota_account": "vbk_569202", "scenic_ota_account": "vbk_569202"}, "兼容 v3.58.0 的 {{scenic_…}} 写法"
    sysv = local.system_vars({"scenic_id": SCENIC, "extra_vars": {**tv, "scenic_id": "EVIL", "today": "1999-01-01"}})
    assert sysv["ota_account"] == "vbk_569202"
    assert sysv["scenic_id"] == SCENIC and sysv["today"] != "1999-01-01"


# ── 二、两层取值 + 落到工具执行 ─────────────────────────────────────────

async def _seed(multi_mode="single", defaults=(), scenic_values=None, **kw):
    from cogni_agent.gateway_service.models import SysParamDef, SysParamValue

    async with common_db.get_sessionmaker()() as db:
        db.add(SysParamDef(param_key="ota_account", label="OTA 商户账号", multi_mode=multi_mode,
                           default_values=[{"value": v, "label": ""} for v in defaults],
                           arg_names=["account"], **kw))
        if scenic_values is not None:
            db.add(SysParamValue(param_key="ota_account", scenic_id=SCENIC, values=scenic_values))
        await db.commit()


@run_async
async def test_scenic_value_overrides_system_default():
    from cogni_agent.runtime_service.engine import scenic_params as sp

    await _seed(defaults=("vbk_default",), scenic_values=[{"value": "vbk_569202", "label": ""}])
    async with common_db.get_sessionmaker()() as db:
        mine = await sp.load(db, SCENIC)
        other = await sp.load(db, "PFTSCX00012325")
    assert mine[0].strings == ["vbk_569202"] and mine[0].source == "scenic"
    assert other[0].strings == ["vbk_default"] and other[0].source == "default"


@run_async
async def test_disabled_or_empty_param_is_not_loaded():
    from cogni_agent.runtime_service.engine import scenic_params as sp

    await _seed(defaults=("x",), enabled=False)
    async with common_db.get_sessionmaker()() as db:
        assert await sp.load(db, SCENIC) == []


def _ota_spec(**kw):
    from cogni_agent.runtime_service.engine.resolver import ToolSpec

    return ToolSpec(id="t-ota", name="ota_revenue", description="OTA直销-OTA营收指标", type="http",
                    type_config=kw.pop("type_config", {}), risk_level="low", json_schema=OTA_SCHEMA, **kw)


async def _run(spec, args, monkeypatch, fail_on=None):
    from cogni_agent.runtime_service.engine import tools_adapter as ta

    calls = []

    async def fake_invoke(db, tool_id, scenic_id, role, params, **kw):
        calls.append({"params": params, "extra_vars": kw.get("extra_vars")})
        if fail_on and params.get("account") == fail_on:
            raise RuntimeError("upstream 500")
        return {"status_code": 200, "body": {"account": params.get("account")}}
    monkeypatch.setattr(ta, "invoke_tool", fake_invoke)
    async with common_db.get_sessionmaker()() as db:
        out = await ta.execute_single_tool_call(
            db, task_id="task-1", tool_name=spec.name, raw_arguments=args,
            bound_tools={spec.name: spec}, scenic_id=SCENIC, role="scenic_admin", user_id="u-1")
    return out, calls


@run_async
async def test_required_account_is_filled_before_validation(monkeypatch):
    """以前:account 必填 → 先撞「缺少必填参数」→ 模型去问用户。配了之后直接填上、正常调用。"""
    await _seed(scenic_values=[{"value": "vbk_569202", "label": ""}])
    out, calls = await _run(_ota_spec(), {"scenicId": SCENIC, "startTime": "2026-09-21", "endTime": "2026-09-21"}, monkeypatch)
    assert out.kind == "ok", out.content
    assert calls[0]["params"]["account"] == "vbk_569202"
    assert calls[0]["extra_vars"]["ota_account"] == "vbk_569202"
    assert out.injected[0]["arg"] == "account" and out.injected[0]["how"] == "fill"


@run_async
async def test_fanout_calls_once_per_value_and_merges(monkeypatch):
    """一个景区两个商户账号、接口一次只收一个:各调一次,结果合并;一个失败不影响另一个。"""
    await _seed(multi_mode="fanout", scenic_values=[
        {"value": "vbk_569202", "label": "携程主账号"}, {"value": "vbk_778899", "label": "携程子账号"}])
    out, calls = await _run(_ota_spec(), {"scenicId": SCENIC}, monkeypatch, fail_on="vbk_778899")
    assert out.kind == "ok"
    assert [c["params"]["account"] for c in calls] == ["vbk_569202", "vbk_778899"]
    assert [c["extra_vars"]["ota_account"] for c in calls] == ["vbk_569202", "vbk_778899"]
    res = out.raw_result
    assert res["fanout"]["count"] == 2 and res["results"][0]["ok"] and res["results"][0]["label"] == "携程主账号"
    assert res["results"][1]["ok"] is False and "upstream 500" in res["results"][1]["error"]


@run_async
async def test_fanout_triggered_by_url_reference(monkeypatch):
    """工具地址里写了 {{ota_account}}(入参里没有):同样逐个调用,每次地址里换成那一个值。"""
    await _seed(multi_mode="fanout", scenic_values=[{"value": "a1", "label": ""}, {"value": "a2", "label": ""}])
    spec = _ota_spec(type_config={"url": "https://x/ota/{{ota_account}}/summary"})
    spec.json_schema = {"type": "object", "properties": {"scenicId": {"type": "string"}}}
    out, calls = await _run(spec, {"scenicId": SCENIC}, monkeypatch)
    assert [c["extra_vars"]["ota_account"] for c in calls] == ["a1", "a2"] and out.kind == "ok"


@run_async
async def test_missing_reference_is_a_config_error(monkeypatch):
    spec = _ota_spec(param_refs={"account": "ota_account"})
    out, calls = await _run(spec, {"scenicId": SCENIC}, monkeypatch)
    assert out.kind == "error" and "{{ota_account}}" in out.content and "接口参数" in out.content and calls == []


# ── 三、管理接口 ───────────────────────────────────────────────────────

@run_async
async def test_system_def_then_scenic_override_round_trip():
    from cogni_agent.common.errors import Forbidden, ValidationError
    from cogni_agent.gateway_service import routes_app_ops as r
    from cogni_agent.gateway_service import routes_params as rp
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.user_center_service.models import SysScenic

    admin = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
    scenic_admin = CurrentUser(user_id="u-s", scenic_id=SCENIC, role="scenic_admin")
    async with common_db.get_sessionmaker()() as db:
        db.add(SysScenic(scenic_id=SCENIC, scenic_name="九龙潭", is_enabled=True))
        await db.commit()
        d = await rp.save_param("ota_account", rp.ParamDefSave(
            label="OTA 商户账号", multi_mode="fanout", arg_names=["account"],
            default_values=[rp.ParamValueItem(value="vbk_default")]), db=db, user=admin)
        assert d["ref"] == "{{ota_account}}" and d["default_values"][0]["value"] == "vbk_default"
        # 景区角色不能改定义,但能改本景区取值
        with pytest.raises(Forbidden):
            await rp.save_param("x_key", rp.ParamDefSave(), db=db, user=scenic_admin)
        view = await r.save_scenic_params(SCENIC, r.ScenicParamsSaveRequest(items=[r.ScenicParamOverride(
            param_key="ota_account", values=[{"value": "vbk_569202", "label": "携程主账号"},
                                             {"value": "vbk_778899", "label": ""}])]), db=db, user=scenic_admin)
        it = view["items"][0]
        assert it["overridden"] and [v["value"] for v in it["values"]] == ["vbk_569202", "vbk_778899"]
        assert (await rp.list_params(db=db, user=admin))["items"][0]["scenic_overrides"] == 1
        # values=None → 回到系统默认
        view = await r.save_scenic_params(SCENIC, r.ScenicParamsSaveRequest(items=[r.ScenicParamOverride(
            param_key="ota_account", values=None)]), db=db, user=scenic_admin)
        assert view["items"][0]["overridden"] is False

        # 校验:保留名、非法标识、入参名被别的参数占了、单值填多个
        for bad in ("scenic_id", "data_platform_key", "Bad-Key"):
            with pytest.raises(ValidationError):
                await rp.save_param(bad, rp.ParamDefSave(), db=db, user=admin)
        with pytest.raises(ValidationError):
            await rp.save_param("other", rp.ParamDefSave(arg_names=["account"]), db=db, user=admin)
        with pytest.raises(ValidationError):
            await rp.save_param("single_one", rp.ParamDefSave(default_values=[
                rp.ParamValueItem(value="a"), rp.ParamValueItem(value="b")]), db=db, user=admin)
        # 景区里填未定义的参数 → 拒
        with pytest.raises(ValidationError):
            await r.save_scenic_params(SCENIC, r.ScenicParamsSaveRequest(items=[r.ScenicParamOverride(
                param_key="nope", values=[{"value": "1"}])]), db=db, user=admin)
        refs = await rp.list_refs(db=db, user=scenic_admin)
        assert refs["items"][0]["ref"] == "{{ota_account}}" and "default_values" not in refs["items"][0], "引用清单不带值"


@run_async
async def test_secret_values_are_masked_and_kept():
    from cogni_agent.gateway_service import routes_app_ops as r
    from cogni_agent.gateway_service import routes_params as rp
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.gateway_service.models import SysParamValue
    from cogni_agent.user_center_service.models import SysScenic
    from sqlalchemy import select

    admin = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
    async with common_db.get_sessionmaker()() as db:
        db.add(SysScenic(scenic_id=SCENIC, scenic_name="九龙潭", is_enabled=True))
        await db.commit()
        await rp.save_param("api_pwd", rp.ParamDefSave(label="口令", is_secret=True), db=db, user=admin)
        view = await r.save_scenic_params(SCENIC, r.ScenicParamsSaveRequest(items=[r.ScenicParamOverride(
            param_key="api_pwd", values=[{"value": "s3cr3t-9999"}])]), db=db, user=admin)
        assert view["items"][0]["values"][0]["value"] == "••••9999"
        await r.save_scenic_params(SCENIC, r.ScenicParamsSaveRequest(items=[r.ScenicParamOverride(
            param_key="api_pwd", values=None, keep_secret=True)]), db=db, user=admin)
        row = (await db.execute(select(SysParamValue))).scalars().one()
        assert row.values[0]["value"] == "s3cr3t-9999", "keep_secret 不动原值"


def test_migration_and_comments_exist():
    from cogni_agent.common import schema as sc

    mig = next(m for m in sc.MIGRATIONS if m.id == "0090_system_params")
    assert "DROP TABLE" in mig.sql and "INSERT INTO" in mig.sql, "0089 的数据要迁过来"
    for t, cols in (("sys_param_def", ("param_key", "label", "description", "multi_mode", "default_values",
                                        "arg_names", "fill_mode", "is_secret", "enabled")),
                    ("sys_param_value", ("param_key", "scenic_id", "values"))):
        assert sc.TABLE_COMMENTS.get(t), t
        for c in cols:
            assert sc.COLUMN_COMMENTS[t].get(c), f"{t}.{c} 没有注释"


def test_ui_wiring():
    web = ROOT / "web" / "src"
    assert "SystemParamsPanel" in (web / "pages" / "System" / "SystemSettings.tsx").read_text(encoding="utf-8")
    assert "ScenicParamsCard" in (web / "pages" / "AppOps" / "ScenicConfigDetail.tsx").read_text(encoding="utf-8")
    for f in ("pages/Tools/SkillEdit.tsx", "pages/Tools/ToolNew.tsx", "pages/Tools/MCPNew.tsx"):
        assert "ParamRefPicker" in (web / f).read_text(encoding="utf-8"), f
    api = (web / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "/admin/params/refs" in api and "saveScenicParams" in api


# ── 四、耗时相关的改动 ─────────────────────────────────────────────────

def test_empty_rewritten_goal_is_ignored():
    from cogni_agent.runtime_service.engine import intent

    assert intent.is_empty_goal("补充OTA商户ID为vbk_569202，等待用户提出具体查询需求")
    assert not intent.is_empty_goal("查询 9/21 OTA 直销成交额(已补充:account=vbk_569202)")
    src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "intent.py").read_text(encoding="utf-8")
    assert "补充参数不是新问题" in src


def test_prior_answers_are_clipped_in_history():
    (_clip_prior_answer,) = _graphs_fns("_clip_prior_answer")

    long = "表" * 5000
    out = _clip_prior_answer(long, 1500)
    assert len(out) < 1600 and "原长 5000 字" in out
    assert _clip_prior_answer("短回答", 1500) == "短回答"


def test_tool_policy_asks_for_parallel_calls():
    (_tool_usage_policy,) = _graphs_fns("_tool_usage_policy")

    assert "并行" in _tool_usage_policy()


def test_llm_perf_splits_queue_think_output():
    (_llm_perf,) = _graphs_fns("_llm_perf")
    from types import SimpleNamespace as LLMResult   # llm.client 依赖 litellm,本沙箱没装;字段同 LLMResult

    r = LLMResult(content="答", reasoning_content="想" * 300, prompt_tokens=12000, completion_tokens=800,
                  model="deepseek", ttft_ms=2000, first_output_ms=60000, total_ms=86000)
    perf = _llm_perf(r, [{"role": "system", "content": "x" * 100}, {"role": "user", "content": "q"}])
    assert (perf["queue_ms"], perf["think_ms"], perf["output_ms"], perf["total_ms"]) == (2000, 58000, 26000, 86000)
    assert perf["reasoning_chars"] == 300 and perf["messages"] == 2 and perf["system_chars"] == 100


def test_token_counts_are_not_masked_as_secrets():
    """prompt_tokens 被「token」一刀切成 ***,执行日志里恰恰看不到上下文有多大。"""
    from cogni_agent.tool_center_service.quickbi.trace import mask

    out = mask({"prompt_tokens": 35906, "completion_tokens": 6759, "user_token": "abc123xyz",
                "llm": {"prompt_tokens": 100}})
    assert out["prompt_tokens"] == 35906 and out["completion_tokens"] == 6759
    assert out["user_token"] == "***" and out["llm"]["prompt_tokens"] == 100


def test_export_has_timing_breakdown():
    from cogni_agent.control_plane_service.task_debug import _timing_breakdown

    tl = [
        {"step": "intent_check", "llm": {"total_ms": 12000, "prompt_tokens": 5000, "completion_tokens": 200}},
        {"step": "generation", "will_call_tools": ["OTA"], "llm": {"total_ms": 30000, "queue_ms": 1000,
                                                                    "think_ms": 25000, "output_ms": 4000}},
        {"step": "tool_call", "real_duration_ms": 4000, "parallel": False},
        {"step": "generation", "will_call_tools": ["OTA", "OTA"], "llm": {"total_ms": 4000}},
        {"step": "tool_call", "real_duration_ms": 5300, "parallel": True},
        {"step": "tool_call", "real_duration_ms": 5400, "parallel": True},
        {"step": "generation", "preview": "# 报告", "llm": {"total_ms": 9000}},
    ]
    md = "\n".join(_timing_breakdown(tl))
    assert "## 耗时拆解" in md and "意图分诊 | 12.0" in md
    assert "内容生成 #1 → 调工具 OTA | 30.0" in md and "思考 25000" in md
    assert "工具调用(并行按批取最长) | 9.4" in md, "串行 4.0 + 并行一批取最长 5.4"


@run_async
async def test_0090_moves_v358_rows_and_drops_old_table():
    """v3.58.0 已经按 0089 建了 app_scenic_param 并填了值的库:0090 要把值原样迁过来再删表。"""
    from sqlalchemy import text

    from cogni_agent.common import schema as sc

    m89 = next(m for m in sc.MIGRATIONS if m.id == "0089_app_scenic_param")
    m90 = next(m for m in sc.MIGRATIONS if m.id == "0090_system_params")
    engine = common_db.get_engine()
    async with engine.begin() as conn:
        for t in ("sys_param_value", "sys_param_def", "app_scenic_param"):
            await conn.execute(text(f"DROP TABLE IF EXISTS {sc.P}{t}"))
        for st in sc._split_statements(m89.sql):
            await conn.execute(text(st))
        await conn.execute(text(
            f"INSERT INTO {sc.P}app_scenic_param (id, scenic_id, param_key, label, value, arg_names, mode, is_secret, enabled, description) "
            f"VALUES ('1', '{SCENIC}', 'ota_account', 'OTA 商户账号', 'vbk_569202', '[\"account\"]', 'fill', false, true, '说明')"))
        for st in sc._split_statements(m90.sql):
            await conn.execute(text(st))
        d = (await conn.execute(text(f"SELECT param_key, label, arg_names::text, description FROM {sc.P}sys_param_def"))).all()
        v = (await conn.execute(text(f"SELECT scenic_id, \"values\"::text FROM {sc.P}sys_param_value"))).all()
        gone = (await conn.execute(text(f"SELECT to_regclass('{sc.P}app_scenic_param')"))).scalar()
    assert d == [("ota_account", "OTA 商户账号", '["account"]', "说明")]
    assert v[0][0] == SCENIC and "vbk_569202" in v[0][1]
    assert gone is None


def test_resolver_strips_ref_params_from_model_schema():
    """参数定义里 default="{{ota_account}}" 的入参:模型看不到(也就不会去问用户),执行时系统填。"""
    from types import SimpleNamespace

    from cogni_agent.runtime_service.engine import resolver

    tool = SimpleNamespace(id="t1", name="OTA营收", description="", type="http", risk_level="low",
                           type_config={"method": "POST", "url": "https://x", "params_schema": {
                               "type": "object", "required": ["account", "startTime"], "properties": {
                                   "account": {"type": "string", "default": "{{ota_account}}"},
                                   "startTime": {"type": "string"}}}})
    ctx = resolver._Ctx(user_id="u", role="super_admin", scenic_id=SCENIC, overrides={})
    spec = resolver._to_tool_spec(tool, ctx=ctx)
    assert spec.param_refs == {"account": "ota_account"}
    assert "account" not in spec.json_schema["properties"] and spec.json_schema.get("required") == ["startTime"]
    assert "account" in spec.validate_schema["properties"]


def test_export_step_details_and_gap_row():
    """导出里每次内容生成的上下文构成、思考开头结尾、调用参数都摊开;没被任何一步认领的时间单列一行。"""
    from cogni_agent.control_plane_service.task_debug import _step_details, _timing_breakdown, task_debug_markdown

    tl = [
        {"step": "intent_check", "ts": 0.0, "wall_ms": "09:35:27.100",
         "llm": {"total_ms": 12000, "prompt_tokens": 5000, "completion_tokens": 200},
         "detail": {"goal_chars": 20, "history_rounds": 2, "history_chars": 6000, "skills_offered": 1,
                    "rewritten_goal_used": False, "rewritten_goal_rejected": True}},
        {"step": "generation", "ts": 12.0, "wall_ms": "09:36:10.000", "will_call_tools": ["OTA"],
         "llm": {"model": "deepseek", "total_ms": 86000, "queue_ms": 2000, "think_ms": 80000, "output_ms": 4000,
                 "prompt_tokens": 12000, "completion_tokens": 900, "reasoning_chars": 5000, "messages": 5,
                 "system_chars": 4000, "prompt_chars": 20000, "output_chars": 0},
         "detail": {"round": 2, "started_at": "09:36:10.000", "wall_ms": 86000, "finish_reason": "tool_calls",
                    "tools_offered": 30, "context": {"messages_by_role": {"system": 1, "user": 2}, "history_messages": 4,
                                                     "history_answers_clipped": 2, "tool_result_chars": 800},
                    "reasoning_head": "用户补充了商户ID……", "reasoning_tail": "",
                    "output_preview": "", "tool_calls": [{"name": "OTA", "args": "{\"account\": \"vbk_569202\"}"}]}},
        {"step": "tool_call", "ts": 98.0, "tool_name": "OTA", "kind": "ok", "real_duration_ms": 3900, "parallel": False,
         "io": {"started_at": "09:37:40.001", "result_chars": 9000, "sent_to_model_chars": 1200, "compressed": True,
                "http_status": 200, "fanout": None}},
        {"step": "drift_check", "ts": 110.0, "on_track": False, "note": "REDIRECT: 停止主动查询",
         "llm": {"total_ms": 2400}, "acted": False},
        {"step": "generation", "ts": 186.0, "preview": "# 报告", "llm": {"total_ms": 9000}},
    ]
    md = "\n".join(_timing_breakdown(tl))
    assert "目标漂移自检 | 2.4" in md and "未拦截" in md
    assert "其它(步骤之间的空隙" in md, "186 秒跨度减去已统计的 ≈ 73 秒空隙要单列"
    det = "\n".join(_step_details(tl))
    assert "改写成了空目标,已丢弃" in det and "截短 2 条" in det and "思考 80000" in det
    assert "`OTA` {\"account\": \"vbk_569202\"}" in det and "用户补充了商户ID" in det
    full = task_debug_markdown({"task": {"id": "t", "status": "COMPLETED"}, "timeline": tl, "captured": {}})
    assert "HTTP 200" in full and "返回 9000 字 → 给模型 1200 字(已压缩)" in full
    assert "09:37:40.001" in full and "排队 2000 · 思考 80000 · 输出 4000 ms" in full


@run_async
async def test_group_assigns_values_per_scenic_and_scenics_are_isolated():
    """集团在参数上统一给各景区分配值:同一个 {{ota_account}},景区 A、B 各取各的,互不可见。"""
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service import routes_params as rp
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.runtime_service.engine import scenic_params as sp
    from cogni_agent.user_center_service.models import SysScenic

    A, B, C = SCENIC, "PFTSCX00012325", "PFTSCZ99999999"
    admin = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
    async with common_db.get_sessionmaker()() as db:
        for sid, name in ((A, "九龙潭"), (B, "大金湖"), (C, "未分配景区")):
            db.add(SysScenic(scenic_id=sid, scenic_name=name, is_enabled=True))
        await db.commit()
        await rp.save_param("ota_account", rp.ParamDefSave(label="OTA 商户账号", multi_mode="fanout",
                                                             arg_names=["account"]), db=db, user=admin)
        got = await rp.save_assignments("ota_account", rp.AssignmentsSave(items=[
            rp.ScenicAssignment(scenic_id=A, values=[rp.ParamValueItem(value="vbk_A1"), rp.ParamValueItem(value="vbk_A2")]),
            rp.ScenicAssignment(scenic_id=B, values=[rp.ParamValueItem(value="vbk_B1")])]), db=db, user=admin)
        assert [(x["scenic_id"], x["scenic_name"], [v["value"] for v in x["values"]]) for x in got["items"]] == [
            (A, "九龙潭", ["vbk_A1", "vbk_A2"]), (B, "大金湖", ["vbk_B1"])]
        sp.invalidate()
        a, b, c = await sp.load(db, A), await sp.load(db, B), await sp.load(db, C)
        assert a[0].strings == ["vbk_A1", "vbk_A2"] and b[0].strings == ["vbk_B1"]
        assert c == [], "没分配、也没系统默认值的景区拿不到任何值(不会串到别的景区)"
        # 列表里去掉 B = 取消分配
        got = await rp.save_assignments("ota_account", rp.AssignmentsSave(items=[
            rp.ScenicAssignment(scenic_id=A, values=[rp.ParamValueItem(value="vbk_A1")])]), db=db, user=admin)
        assert [x["scenic_id"] for x in got["items"]] == [A]
        with pytest.raises(ValidationError):
            await rp.save_assignments("ota_account", rp.AssignmentsSave(items=[
                rp.ScenicAssignment(scenic_id="NOPE", values=[rp.ParamValueItem(value="x")])]), db=db, user=admin)


def test_param_modal_has_no_redundant_hints_and_tool_example_exists():
    web = ROOT / "web" / "src"
    panel = (web / "components" / "system" / "SystemParamsPanel.tsx").read_text(encoding="utf-8")
    assert "MULTI_HINT" not in panel and "用户点名了某个值" not in panel, "截图里标绿的说明已去掉"
    # 每个值分配到景区(值 + 备注 + 分配景区),而不是整个参数按景区分配
    assert "ValueRowsEditor" in panel and "取值与分配景区" in panel and "saveParamAssignments" in panel
    assert "ScenicAssignments" not in panel
    picker = (web / "components" / "common" / "ParamRefPicker.tsx").read_text(encoding="utf-8")
    assert '"default": "{{ota_account}}"' in picker, "Tool 编辑页给出请求体写法示例"


def test_value_rows_round_trip_ts():
    """按值组织(值 → 分配景区)↔ 按景区存储 的往返、单值重复分配、敏感值保持 —— 跑真模块。"""
    import shutil
    import subprocess

    tsx = shutil.which("tsx") or "/home/claude/.npm-global/bin/tsx"
    if not Path(tsx).exists():
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "param_rows.check.ts")], capture_output=True, text=True,
                       timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


@run_async
async def test_list_groups_values_by_value_with_scenic_names():
    from cogni_agent.gateway_service import routes_params as rp
    from cogni_agent.gateway_service.deps import CurrentUser
    from cogni_agent.user_center_service.models import SysScenic

    admin = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
    async with common_db.get_sessionmaker()() as db:
        db.add_all([SysScenic(scenic_id="A1", scenic_name="九龙潭", is_enabled=True),
                    SysScenic(scenic_id="B1", scenic_name="大金湖", is_enabled=True)])
        await db.commit()
        await rp.save_param("jiulongtan_ctrip", rp.ParamDefSave(label="OTA 九龙潭商户ID", multi_mode="list"), db=db, user=admin)
        await rp.save_assignments("jiulongtan_ctrip", rp.AssignmentsSave(items=[
            rp.ScenicAssignment(scenic_id="A1", values=[rp.ParamValueItem(value="vbk_569202", label="携程主账号")]),
            rp.ScenicAssignment(scenic_id="B1", values=[rp.ParamValueItem(value="vbk_569202", label="携程主账号")])]),
            db=db, user=admin)
        got = (await rp.list_params(db=db, user=admin))["items"][0]["value_groups"]
    assert got == [{"value": "vbk_569202", "label": "携程主账号", "scenics": ["九龙潭", "大金湖"], "is_default": False}]
