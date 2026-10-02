# -*- coding: utf-8 -*-
"""非 Quick BI 那条链(Tool / MCP / Skill / Agent)的回归。

这条链和 Quick BI 是两套东西:Quick BI 是把问题转给小Q,这边是我们自己的执行引擎 ——
解析技能 → 挂工具 → 模型决定调哪个 → 分发执行 → 结果回灌给模型。
它的故障长相也不一样:**不报错,只是答得不对**。所以这里钉的都是「静默失效」那一类:

- 工具返回上千行,原样塞进对话被截断,模型基于半截 JSON 作答且不自知;
- 系统变量({{scenic_id}})没渲染,HTTP 工具把字面量打出去、DB 工具绑成 NULL(查出来是空集,不报错);
- 模型编了一个不存在的工具名,如果不打回去,它会接着编;
- 内置工具(时间对齐/修正)缺席 —— 模型自己推日期推错了,照样给一份结构完整、数字对不上的回答。
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db():
    """只给需要落库的用例用(建表不便宜,这个文件里大多数用例是纯函数)。"""
    from cogni_agent.common import db as common_db
    from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型
    from cogni_agent.common.db import Base

    tables = [t for t in Base.metadata.sorted_tables
              if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]

    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=tables))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    asyncio.run(reset())
    yield


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


# ══════════════════════════════════════════════════════════════════════
# 一、工具返回先压缩再截断 —— 三条路径都要走到
# ══════════════════════════════════════════════════════════════════════

def test_every_tool_result_path_compresses_before_truncating():
    """扫描 graphs.py:每一处 execute_single_tool_call 的结果都必须过压缩层。

    为什么用扫描而不是逐个测:这条链有三处分发(ReAct 主循环 / 单工具直出 / 编排并行),
    前两处接了压缩,**编排那条漏了** —— 而它恰恰是一次跑好几个工具、返回量最大的那条路。
    漏了不会报错:几个上千行的结果拼起来截断,模型看到几段半截 JSON,照样答得像模像样。
    新增第四处分发时,这条会立刻红。
    """
    src = Path("cogni_agent/runtime_service/engine/graphs.py").read_text(encoding="utf-8")
    dispatches = src.count("await execute_single_tool_call(")
    compressed = src.count("ctx.compress_or_truncate(")
    assert dispatches >= 3, f"分发点只找到 {dispatches} 处,扫描口径可能失效了"
    assert compressed >= dispatches, (
        f"{dispatches} 处工具分发,只有 {compressed} 处过了压缩层 —— "
        "漏掉的那条会把上千行返回原样截断给模型")


@run_async
async def test_large_result_is_compressed_not_truncated():
    """上千行的返回压成「数据摘要 + 样本行」,而不是砍掉后半截。"""
    from cogni_agent.runtime_service.engine import toolresult as tr

    rows = [{"日期": f"2026-09-{i % 30 + 1:02d}", "渠道": ["OTA", "直销", "团队"][i % 3],
             "订单量": 100 + i, "金额": 1000.5 + i} for i in range(1200)]
    payload = {"code": 0, "data": rows}
    schema = {"type": "object", "properties": {"data": {"type": "array"}}}

    raw_len = len(json.dumps(payload, ensure_ascii=False))
    out = tr.compress_or_truncate(payload, response_schema=schema, max_chars=4000)

    assert raw_len > 50_000, "样本得足够大才测得出压缩"
    assert len(out) < 4000, f"压完还有 {len(out)} 字"
    # 保住的必须是**统计口径**,不是前 N 行
    assert "1200" in out, "总行数要保住 —— 模型得知道自己看到的是摘要不是全部"
    assert "订单量" in out and "渠道" in out, "列名要保住"
    assert "截断" not in out, "走到截断就说明压缩没生效"


@run_async
async def test_compression_falls_back_to_truncation_without_schema():
    """没有返回参数表时退回截断 —— 但要退得干净,不能抛异常。"""
    from cogni_agent.runtime_service.engine import toolresult as tr

    out = tr.compress_or_truncate({"whatever": ["x" * 100 for _ in range(200)]},
                                   response_schema=None, max_chars=500)
    assert isinstance(out, str) and len(out) <= 1200


# ══════════════════════════════════════════════════════════════════════
# 二、系统内置工具
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_builtin_tools_are_all_callable():
    """6 个内置工具都要能调通,并且都出现在给模型的函数清单里。

    这类能力(取当天、算区间、对齐同比)每个景区都要、配法完全一样,却因为「没人配」而缺席 ——
    而模型自己推日期推错了很难发现:它照样给一份结构完整、数字却对不上的回答。
    """
    from cogni_agent.tool_center_service import builtin_tools as bi

    names = {s["name"] if isinstance(s, dict) and "name" in s else s.get("function", {}).get("name")
             for s in bi.function_schemas()}
    assert names == set(bi.BUILTIN), f"函数清单与注册表对不上:{names} vs {set(bi.BUILTIN)}"

    for name in bi.BUILTIN:
        assert bi.get(name) is not None, f"{name} 取不到实现"


@run_async
async def test_date_helpers_are_deterministic():
    """时间类工具要给确定的结果 —— 它存在的理由就是不让模型自己推。"""
    from cogni_agent.tool_center_service import builtin_tools as bi

    rng = bi.get("sys_date_range")(period="last_7d")
    assert set(rng) >= {"start", "end"}, rng
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rng["start"]), rng
    assert rng["start"] <= rng["end"]

    shifted = bi.get("sys_date_shift")(date="2026-03-01", days=-1)
    assert str(shifted).startswith("2026-02-28") or "2026-02-28" in json.dumps(shifted, default=str)

    aligned = bi.get("sys_align_period")(start="2026-09-01", end="2026-09-30", mode="yoy")
    assert "2025-09-01" in json.dumps(aligned, ensure_ascii=False, default=str), aligned


# ══════════════════════════════════════════════════════════════════════
# 三、接口文档 → 工具 → 压缩层,一条线要串得起来
# ══════════════════════════════════════════════════════════════════════

_DOC = """# 1.17 BI报表配置信息

## 概述
获取景区绑定的 BI 报表地址。

| 接口地址 | /v1/data/digitalBrain/api/quickBiLink |
| --- | --- |
| 请求方式 | POST |

## 请求参数
| 参数名 | 类型 | 必填 | 示例值 | 说明 |
| --- | --- | --- | --- | --- |
| scenicId | String | 是 | PFTSCA01018088 | 景区ID |

## 返回参数
| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| code | Integer | 状态码 |
| data | Object | 报表配置 |

## 请求示例
```bash
curl --location --request POST 'https://example.com/v1/data/digitalBrain/api/quickBiLink' \\
--header 'xpftkey: pfk_xxx' \\
--data '{"scenicId":"PFTSCA01018088"}'
```
"""


def test_api_doc_carries_response_schema_into_the_tool_config():
    """文档里那张**返回参数表**必须一路带进 type_config。

    压缩层就是靠它知道「数据体在哪一层、每列是什么」的 —— 断在这里的话,
    前面那条压缩用例在真实工具上根本不会生效,而且不报错。
    """
    from cogni_agent.tool_center_service.apidoc import from_markdown

    doc = from_markdown(_DOC)
    cfg = doc.to_type_config()
    assert doc.uri == "/v1/data/digitalBrain/api/quickBiLink"
    assert doc.method == "POST"
    assert doc.headers.get("xpftkey"), "认证 header 要从 curl 里取到"
    assert doc.request_schema()["required"] == ["scenicId"]
    assert cfg.get("response_schema"), "返回参数表没进 type_config,压缩层就瞎了"
    assert doc.data_path() == ["data"], doc.data_path()


# ══════════════════════════════════════════════════════════════════════
# 四、系统变量由执行上下文给,模型说了不算
# ══════════════════════════════════════════════════════════════════════

def test_system_vars_override_model_supplied_values():
    """{{scenic_id}} 等系统变量以执行上下文为准。

    允许模型覆盖 scenic_id 就是跨租户 —— 而且是静默的:它会老老实实返回**别的景区**的数据。
    """
    from cogni_agent.tool_center_service.sandbox import local

    ctx = {"scenic_id": "PFTSCA01018088", "user_id": "u-1", "role": "scenic_admin", "org_id": None}
    sysv = local.system_vars(ctx)
    assert sysv["scenic_id"] == "PFTSCA01018088"
    for key in ("now", "today", "yesterday", "month_start", "year", "month", "day"):
        assert key in sysv, f"缺少系统变量 {key}"


# ══════════════════════════════════════════════════════════════════════
# 五、模型编了个不存在的工具名
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_hallucinated_tool_name_is_corrected_not_executed():
    """编出来的工具名要被打回,并把可用清单告诉模型 —— 不打回它会接着编。"""
    from cogni_agent.runtime_service.engine.tools_adapter import execute_single_tool_call
    from cogni_agent.runtime_service.engine.resolver import ToolSpec

    real = ToolSpec(id="t-1", name="sys_now", description="取当前时间", type="builtin",
                    risk_level="low", json_schema={"type": "object", "properties": {}},
                    type_config={}, func_name="sys_now")
    outcome = await execute_single_tool_call(
        None, task_id="task-1", tool_name="查一下客流吧",
        raw_arguments={}, bound_tools={"sys_now": real},
        scenic_id="PFTSCA01018088", role="scenic_admin", user_id="u-1")

    assert outcome.kind == "hallucination"
    assert "sys_now" in outcome.content, "要把可用工具清单给出来"
    assert not outcome.ok


# ══════════════════════════════════════════════════════════════════════
# 六、审批的第三种动作:改参数后批准
# ══════════════════════════════════════════════════════════════════════
#
# 审批人看到「要取消订单 A」时,常见需求是「改成取消订单 B 再执行」。
# 原先只有批准/驳回两个动作,这种情况只能驳回、让用户从头再问一遍。
#
# 关键约束:改过的参数**必须重新过校验**,而且按**任务的**景区/角色 ——
# 任务恢复后仍以原用户身份执行,拿审批人的权限放行等于开了条提权路径。

def _risky_tool():
    from cogni_agent.runtime_service.engine.resolver import ToolSpec

    return ToolSpec(
        id="t-cancel", name="取消订单", description="取消一笔订单", type="http",
        risk_level="high",
        json_schema={"type": "object",
                     "properties": {"order_id": {"type": "string"},
                                    "scenic_id": {"type": "string"}},
                     "required": ["order_id"]},
        type_config={}, func_name="cancel_order")


def test_plain_boolean_approval_still_works():
    """既有审批不受影响:resume 是个 bool 时照常走批准/驳回。"""
    from cogni_agent.runtime_service.engine import tools_adapter as ta

    spec = _risky_tool()
    args = {"order_id": "A-1"}
    for resume in (True, False):
        ok, out, err = ta._apply_approval_patch(spec, args, resume, "PFTSCA01018088", "scenic_admin")
        assert ok is resume and out == args and err is None


def test_approve_with_edited_params_replaces_the_argument():
    """改成取消订单 B —— 参数换掉,照常执行。"""
    from cogni_agent.runtime_service.engine import tools_adapter as ta

    spec = _risky_tool()
    ok, out, err = ta._apply_approval_patch(
        spec, {"order_id": "A-1"}, {"approved": True, "params": {"order_id": "B-2"}},
        "PFTSCA01018088", "scenic_admin")
    assert ok is True and err is None
    assert out["order_id"] == "B-2"


def test_edited_params_are_revalidated_against_the_schema():
    """审批人改出一个不合法的值 —— 要拦下来,而不是照着执行。"""
    from cogni_agent.runtime_service.engine import tools_adapter as ta

    spec = _risky_tool()
    ok, out, err = ta._apply_approval_patch(
        spec, {"order_id": "A-1"}, {"approved": True, "params": {"order_id": {"不是": "字符串"}}},
        "PFTSCA01018088", "scenic_admin")
    assert err is not None and err.kind == "format_error"
    assert "参数规范" in err.content
    assert out["order_id"] == "A-1", "拦下来之后不能把坏值留在参数里"


def test_edited_params_cannot_cross_tenants():
    """**这条是重点**:改参数不能成为跨景区的口子。

    任务恢复后仍以原用户身份执行 —— 审批人把景区改成别家的,就等于借审批提权。
    校验按任务的景区/角色,不是审批人的。
    """
    from cogni_agent.runtime_service.engine import tools_adapter as ta

    spec = _risky_tool()
    ok, out, err = ta._apply_approval_patch(
        spec, {"order_id": "A-1", "scenic_id": "PFTSCA01018088"},
        {"approved": True, "params": {"scenic_id": "PFTSCB02000052"}},
        "PFTSCA01018088", "scenic_admin")
    assert err is not None and "景区" in err.content
    assert out["scenic_id"] == "PFTSCA01018088"

    # 集团角色不受景区限制(与既有口径一致)
    ok2, out2, err2 = ta._apply_approval_patch(
        spec, {"order_id": "A-1", "scenic_id": "PFTSCA01018088"},
        {"approved": True, "params": {"scenic_id": "PFTSCB02000052"}},
        None, "super_admin")
    assert err2 is None and out2["scenic_id"] == "PFTSCB02000052"


def test_edited_params_cannot_override_system_injected_values():
    """系统注入的参数(forced_params)不给改 —— 让审批人改它等于把注入机制架空。"""
    from cogni_agent.runtime_service.engine import tools_adapter as ta
    from cogni_agent.runtime_service.engine.resolver import ToolSpec

    spec = ToolSpec(
        id="t-1", name="查询", description="", type="http", risk_level="high",
        json_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        type_config={}, forced_params={"scenic_id": "PFTSCA01018088"}, func_name="q")
    ok, out, err = ta._apply_approval_patch(
        spec, {"q": "x", "scenic_id": "PFTSCA01018088"},
        {"approved": True, "params": {"q": "y", "scenic_id": "PFTSCB02000052"}},
        "PFTSCA01018088", "scenic_admin")
    assert err is None and ok is True
    assert out["q"] == "y", "模型可控参数可以改"
    assert out["scenic_id"] == "PFTSCA01018088", "系统注入的不能被审批人改掉"


def test_rejecting_with_params_is_refused_at_the_route_level():
    """驳回时不该同时改参数 —— 语义上是矛盾的,路由层直接拦。"""
    import inspect

    from cogni_agent.governance_event_bus_service import routes

    src = inspect.getsource(routes.resolve_approval_route)
    assert "驳回时不能同时修改参数" in src


def test_objects_are_not_coerced_into_python_repr_strings():
    """string 字段遇到 dict/list 时原样留着,交给 Schema 报错 —— 不要 str() 成 repr。

    `str({"a": 1})` 得到 `"{'a': 1}"`,这是个**合法字符串**,于是 Schema 校验放行,
    然后原样发给接口。接口收到一串花括号回一个业务错误,看起来像「接口有问题」。
    报错比静默发错好。这条是写「改参数后批准」的校验用例时撞出来的:
    审批人填了个结构错的值,本该被拦下,却被悄悄转成了字符串。
    """
    from cogni_agent.runtime_service.engine.tools_adapter import coerce_args_to_schema

    schema = {"type": "object", "properties": {"order_id": {"type": "string"},
                                               "tags": {"type": "array", "items": {"type": "string"}}}}
    out = coerce_args_to_schema(schema, {"order_id": {"不是": "字符串"}, "tags": [1, 2]})
    assert isinstance(out["order_id"], dict), "对象不该被转成 repr 字符串"
    assert out["tags"] == ["1", "2"], "数组里的标量该转还是要转"

    # 正常的标量转换不受影响
    assert coerce_args_to_schema(schema, {"order_id": 123})["order_id"] == "123"


# ══════════════════════════════════════════════════════════════════════
# 七、按用户那份完整接口文档解析(含嵌套返回字段)
# ══════════════════════════════════════════════════════════════════════

_FULL_DOC = """# 1.17 BI报表配置信息

## 1.17.1 API概览

- **URI**: `/v1/data/digitalBrain/api/quickBiLink`
- **请求方式**: POST
- **认证方式**: api key 【需联系数据平台开发获取】

## 1.17.2 请求参数

| 序号 | 字段 | 类型 | 必传 | 示例 | 注释 |
| --- | --- | --- | --- | --- | --- |
| 1 | scenicId | string | 是 | PFTSCA01018088 | 景区ID |

## 1.17.3 返回参数

| 序号 | 字段 | 类型 | 示例 | 注释 |
| --- | --- | --- | --- | --- |
| 1 | status | bool | true | 请求状态 |
| 2 | data | array | [...] | 数据体 |
|  | data[].profile | array | [...] | 画像客群信息 |
|  | data[].profile[].uri | string | https://bi.aliyuncs.com/... | BI地址 |
|  | data[].profile[].timeType | string | 2026上半年报告 | 报告时间周期 |
|  | data[].opinion | string | https://bi.aliyuncs.com/... | 舆情链接 |
| 3 | msg | string |  | 错误信息 |
| 4 | code | int | 0 | 状态码 |
| 5 | trace_id | string | e27195fc68f171f7d9ce3b6cbe42be66 | 追踪ID |

## 1.17.4 示例

```bash
curl --location --request POST 'https://data.12301dev.com/v1/data/digitalBrain/api/quickBiLink' \\
--header 'xpftkey: pfk_FAKEtestKEYforFixtureONLY0000' \\
--header 'Content-Type: application/json' \\
--data-raw '{"scenicId":"PFTSCA01018088"}'
```
"""


def test_parses_the_real_doc_including_nested_response_fields():
    """按现场那份文档**原样**解析。

    比早先那份多两处会绊人的地方:返回参数表里有嵌套路径(data[].profile[].uri),
    而且这些行的「序号」列是空的 —— 按序号断行的解析器会在这里丢掉一半字段。
    """
    from cogni_agent.tool_center_service.apidoc import from_markdown

    doc = from_markdown(_FULL_DOC)
    assert doc.title == "BI报表配置信息"
    assert doc.uri == "/v1/data/digitalBrain/api/quickBiLink"
    assert doc.method == "POST"
    # 域名和认证 header 从 curl 里取(那一节是唯一能跑的,表格反而常漏)。
    # 认证头的**名字**照取,**值**换成占位 —— 明文不进工具配置(真实值走服务端配置)。
    assert doc.base_url == "https://data.12301dev.com"
    assert doc.headers.get("xpftkey") == "{{data_platform_key}}"
    assert doc.request_schema()["required"] == ["scenicId"]

    names = [f.name for f in doc.response_fields]
    assert "data[].profile[].uri" in names, f"嵌套字段丢了:{names}"
    assert "data[].opinion" in names
    assert doc.data_path() == ["data"]
    # 凭证脱敏的提示是**有意**给出的(告诉运维真实 key 该配在哪),不是「没看懂」;
    # 这条断言要守的是后者,所以把它排除在外。
    assert not [w for w in doc.warnings if "凭证" not in w], doc.warnings


@run_async
async def test_small_nested_response_is_not_mangled():
    """小的嵌套响应要原样透传 —— BI 链接被截断的话,给出去就是个打不开的地址。"""
    from cogni_agent.runtime_service.engine import toolresult as tr
    from cogni_agent.tool_center_service.apidoc import from_markdown

    schema = from_markdown(_FULL_DOC).to_type_config()["response_schema"]
    sample = {"status": True, "code": 0, "data": [{
        "profile": [{"uri": "https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=78c6e2e5",
                     "timeType": "2026上半年报告"}],
        "opinion": "https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=55acdac6"}]}
    out = tr.compress_or_truncate(sample, response_schema=schema, max_chars=4000)
    assert "pageId=78c6e2e5" in out and "pageId=55acdac6" in out


@run_async
async def test_column_notes_from_the_doc_show_up_in_the_digest():
    """压缩后的列名要带上文档里的**注释** —— 模型看 `uri` 猜不出是什么,看「BI地址」就知道。"""
    from cogni_agent.runtime_service.engine import toolresult as tr
    from cogni_agent.tool_center_service.apidoc import from_markdown

    schema = from_markdown(_FULL_DOC).to_type_config()["response_schema"]
    big = {"status": True, "code": 0,
           "data": [{"uri": f"https://bi/{i}", "timeType": "上半年"} for i in range(1500)]}
    out = tr.compress_or_truncate(big, response_schema=schema, max_chars=4000)
    assert "1500" in out, "总行数要保住"
    assert "BI地址" in out, f"列注释没用上:{out[:200]}"


# ══════════════════════════════════════════════════════════════════════
# 八、时间参数算出来,别让模型推
# ══════════════════════════════════════════════════════════════════════
#
# 现场:「近一日*(即昨天 2026-09-19)景区 PFTSCA01018088 的数据」
# 分诊判对了(数据查询 → we),也列出了缺失参数 period/start/end/rows ——
# 然后**一个工具都没调**,直接生成文字,回了句「如需要近一日的数据请告诉我」。
# 日期用户已经写得不能再清楚,那几个参数本来就是能算的。

def test_resolves_the_exact_phrasing_from_the_incident():
    from cogni_agent.runtime_service.engine import timeslots as ts

    got = ts.resolve("近一日*(即昨天 2026-09-19)景区 PFTSCA01018088 的数据",
                     {"start", "end", "period", "rows"})
    assert got["start"] == "2026-09-19" and got["end"] == "2026-09-19"
    assert got["period"] == "昨天"
    assert "rows" not in got, "只填时间参数,别往调用里塞工具没声明的东西"


def test_explicit_date_beats_the_relative_phrase():
    """句子里写了日期就以它为准。

    相对说法算出来的是「运行当天的昨天」—— 跨过零点、或者任务在队列里排了一会儿,
    两者就不是同一天,而这种错不会报错,只是数字对不上。
    """
    from cogni_agent.runtime_service.engine import timeslots as ts

    got = ts.resolve("昨天(2026-05-01)的客流", {"start", "end"})
    assert got == {"start": "2026-05-01", "end": "2026-05-01"}


def test_common_phrasings():
    from cogni_agent.runtime_service.engine import timeslots as ts

    assert ts.detect_period("今天的数据") == "今天"
    assert ts.detect_period("最近一日") == "昨天"
    assert ts.detect_period("近7天订单") == "近7天"
    assert ts.detect_period("最近 30 日") == "近30天"
    assert ts.detect_period("上个月的营收") == "上月"
    # 认不出就返回 None,不自己发明口径
    assert ts.detect_period("近3天") is None, "sys_date_range 没定义 3 天的口径,不该硬编一个"
    assert ts.detect_period("帮我看看情况") is None


def test_only_fills_params_the_tools_actually_declare():
    """只看工具自己声明的参数名。

    不维护一张「业务上可能叫什么」的清单 —— 那种清单是工具配置的影子复制,
    加一个工具忘了同步就静默失效。
    """
    from cogni_agent.runtime_service.engine import timeslots as ts

    schemas = [{"properties": {"start": {}, "end": {}, "scenic_id": {}}}]
    assert ts.missing_time_slots(schemas, {}) == {"start", "end"}
    # 已经有值的不再填(APP 端日期选择器选过的优先级更高)
    assert ts.missing_time_slots(schemas, {"start": "2026-01-01"}) == {"end"}
    # 工具没声明时间参数就什么都不填
    assert ts.missing_time_slots([{"properties": {"q": {}}}], {}) == set()


def test_nothing_is_filled_when_the_sentence_has_no_time():
    """算不出来就不填 —— 留给模型问、或者留给澄清,总好过填一个猜的日期。"""
    from cogni_agent.runtime_service.engine import timeslots as ts

    assert ts.resolve("景区 PFTSCA01018088 的数据", {"start", "end"}) == {}


# ══════════════════════════════════════════════════════════════════════
# 九、「跟随最新」的工具绑定必须真的能用
# ══════════════════════════════════════════════════════════════════════
#
# 现场任务 2876affe:技能 we 是「已发布」,界面上三个工具好好列着、还标着「跟随最新」,
# 而运行时它们一个都不在 —— 能力清单里只剩 6 个系统内置工具。模型没有取数工具,
# 只能凭上下文说话,答得像模像样却不是查出来的,而且全程不报错。
#
# 根因:resolver 把「无 pin」一律判成不可用跳过,理由是「不回落主表」。
# 方向没错但话说过头了 —— **跟随最新 ≠ 回落主表**:解析到该工具最新的
# 已发布版本快照,一样是版本化、可追溯的。界面提供的选项,运行时就该认。

def test_follow_latest_binding_resolves_to_the_latest_published_snapshot(fresh_db):
    asyncio.run(_follow_latest_ok())


async def _follow_latest_ok():
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.runtime_service.engine import resolver as rv
    from cogni_agent.tool_center_service.tool.models import Tool
    from cogni_agent.tool_center_service.versioning import (
        STATUS_PUBLISHED, ResourceVersion)

    async with get_sessionmaker()() as db:
        db.add(Tool(id="t-radar", name="雷达图指标", owner_user_id="u-1",
                    visibility="group", active=True))
        for v, desc in ((1, "旧版"), (2, "最新已发布")):
            db.add(ResourceVersion(
                id=f"rv-{v}", resource_type="tool", resource_id="t-radar", version=v,
                status=STATUS_PUBLISHED, created_by="u-1",
                snapshot={"name": "雷达图指标", "description": desc, "type": "http",
                          "risk_level": "low",
                          "type_config": {"method": "GET", "url": "https://x/radar"}}))
        await db.commit()

        ctx = rv._Ctx(user_id="u-1", role="super_admin", scenic_id=None, overrides={})
        # tool_pins 为空 = 界面上的「跟随最新」
        specs = await rv._load_authorized_tools(db, ["t-radar"], ctx=ctx, skill_name="we",
                                                tool_pins={}, skill_id="sk-we")

    assert len(specs) == 1, "「跟随最新」的绑定不该被跳过 —— 跳了模型就没有取数工具"
    assert specs[0].description == "最新已发布", "要解析到最新的那一版"
    # 仍然是版本化的:溯源里记着实际用的版本,不是回落主表
    src = ctx.provenance["skills"]["sk-we"]["tools"]["t-radar"]
    assert src["source"] == "latest_published" and src["version"] == 2
    assert not [d for d in ctx.diagnostics if d.level == "warn"], "正常解析不该报警告"


def test_follow_latest_says_so_when_the_tool_was_never_published(fresh_db):
    """一版都没发布时仍然跳过 —— 但要说清**下一步做什么**。"""
    asyncio.run(_follow_latest_unpublished())


async def _follow_latest_unpublished():
    """一版都没发布时仍然跳过 —— 但要说清**下一步做什么**。"""
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.runtime_service.engine import resolver as rv
    from cogni_agent.tool_center_service.tool.models import Tool

    async with get_sessionmaker()() as db:
        db.add(Tool(id="t-draft", name="草稿工具", owner_user_id="u-1",
                    visibility="group", active=True))
        await db.commit()

        ctx = rv._Ctx(user_id="u-1", role="super_admin", scenic_id=None, overrides={})
        specs = await rv._load_authorized_tools(db, ["t-draft"], ctx=ctx, skill_name="we",
                                                tool_pins={}, skill_id="sk-we")

    assert specs == []
    warns = [d for d in ctx.diagnostics if d.level == "warn"]
    assert warns and warns[0].code == "tool_unpublished"
    assert "发布一版" in warns[0].message, "要告诉人怎么修,不是只说不可用"


def test_resolve_diagnostics_are_seeded_into_the_graph_timeline():
    """解析期告警必须进 graph_input 的 timeline 种子,不能只 _publish。

    落库的时间线只取 graph state(`final_state["timeline"]`),光 _publish 的事件
    活不过这一次 SSE 连接:页面一关就没了,导出执行详情里一个字都看不到。
    而它们恰恰是「为什么一个工具都没调」的答案 —— 现场任务 6dbbf1c8 正是如此:
    三个工具全被跳过,告警发了,没人看得见。

    用扫描而不是跑整条链:这段在 executor 里,跑通它要整套 langgraph。
    扫描能钉住的正是最容易退化的那一点 —— 有人加了新事件却只记得 _publish。
    """
    from pathlib import Path

    src = Path("cogni_agent/runtime_service/engine/executor.py").read_text(encoding="utf-8")
    assert '"step": "resolve_diagnostic"' in src
    assert "diagnostic_events.append" in src, "告警要收集起来"
    # graph_input 的 timeline 种子里必须带上它(种子那行紧跟 diagnostic_events)
    assert '"timeline": diagnostic_events' in src, "timeline 种子里没有解析期告警"


# ══════════════════════════════════════════════════════════════════════
# 十、字段表的 Markdown 录入(工具编辑页的「参数定义」「响应字段说明」)
# ══════════════════════════════════════════════════════════════════════
#
# 表格式录入在简单接口上还行,遇到嵌套结构(data[].profile[].uri)点到第十行
# 就没人愿意继续了 —— 于是这一栏干脆不填。而不填的代价很隐蔽:
# 参数定义缺了,模型靠猜传参;响应字段说明缺了,压缩层不知道数据体在哪、每列是什么,
# 上千行的返回只能原样截断。两种都不报错。

def test_parse_a_response_table_with_nested_paths():
    from cogni_agent.tool_center_service.apidoc import parse_fields

    r = parse_fields("""| 序号 | 字段 | 类型 | 示例 | 注释 |
| --- | --- | --- | --- | --- |
| 1 | status | bool | true | 请求状态 |
| 2 | data | array | [...] | 数据体 |
|  | data[].profile[].uri | string | https://x | BI地址 |
| 3 | code | int | 0 | 状态码 |
""", kind="response")
    names = [f["name"] for f in r["fields"]]
    assert names == ["status", "data", "data[].profile[].uri", "code"]
    assert not r["warnings"]
    # 序号列空着的那一行不能丢 —— 嵌套字段的序号本来就是空的
    assert r["fields"][2]["desc"] == "BI地址"


def test_parse_a_request_table_reads_the_required_column():
    from cogni_agent.tool_center_service.apidoc import parse_fields

    r = parse_fields("""| 参数名 | 类型 | 必传 | 示例值 | 说明 |
| --- | --- | --- | --- | --- |
| scenicId | string | 是 | PFTSCA01018088 | 景区ID |
| rows | int | 否 | 30 | 返回行数 |
""", kind="request")
    by = {f["name"]: f for f in r["fields"]}
    assert by["scenicId"]["required"] is True and by["rows"]["required"] is False
    assert by["rows"]["type"] == "integer", "类型要归一化"
    # 返回参数表没有「必传」列 —— 按 response 解析时不能把它当成必填
    r2 = parse_fields("| 字段 | 类型 |\n| --- | --- |\n| a | string |", kind="response")
    assert r2["fields"][0]["required"] is False


def test_pasting_a_whole_doc_picks_the_right_section():
    """贴一整份接口文档也认 —— 人不该为了填一栏去手工裁剪文档。"""
    from cogni_agent.tool_center_service.apidoc import parse_fields

    doc = _FULL_DOC  # 用户那份真实文档(含请求参数与返回参数两节)
    req = [f["name"] for f in parse_fields(doc, kind="request")["fields"]]
    resp = [f["name"] for f in parse_fields(doc, kind="response")["fields"]]
    assert req == ["scenicId"], req
    assert "data[].profile[].uri" in resp and "trace_id" in resp
    assert "scenicId" not in resp, "别把请求参数混进返回参数里"


def test_unparseable_input_says_what_the_table_needs():
    """解析不出来时要说清**表格该长什么样**,而不是只说一句失败。"""
    from cogni_agent.tool_center_service.apidoc import parse_fields

    r = parse_fields("这里随便写了点东西,不是表格", kind="request")
    assert r["fields"] == []
    assert r["warnings"] and "字段" in r["warnings"][0] and "类型" in r["warnings"][0]
