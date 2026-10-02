# -*- coding: utf-8 -*-
"""用 Markdown 接口文档定义工具 + 结果压缩 + 系统内置工具。

三件事各自的底线:

- **文档能解析**:运维贴进来的是他手上那份文档原样,不是一种新格式。
  编号、列序、多一列少一列都不该影响解析;认证 header 要从 curl 里取到
  (那一节是唯一能跑的,表格反而常漏)。
- **大结果不被截断**:上千行原样塞进对话,要么超上下文被截断(模型基于半截数据作答
  且不自知),要么占满把后面的追问挤掉 —— 两种都不报错。所以先压成「摘要 + 样本」。
- **时间由代码算**:模型自己推日期推错了很难发现,它照样给一份结构完整、
  数字却对不上的回答。
"""
from __future__ import annotations

import json

import pytest

from cogni_agent.tool_center_service import builtin_tools as bi
from cogni_agent.tool_center_service.apidoc import ApiDocParseError, from_markdown
from cogni_agent.tool_center_service.apidoc.digest import compress_tool_result, render_digest

# 现场给的那份文档,原样(包括「1.17.2」这种编号和全角冒号)
REAL_DOC = """# 1.17 BI报表配置信息

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

**返回示例：**

```json
{"status": true, "data": [{"profile": [{"uri": "https://bi/x", "timeType": "2026上半年报告"}],
 "opinion": "https://bi/y"}], "msg": "", "code": 0, "trace_id": "e27195fc"}
```
"""


# ── 一、文档解析 ────────────────────────────────────────────────────

def test_parses_the_real_doc():
    doc = from_markdown(REAL_DOC)
    assert doc.title == "BI报表配置信息"          # 标题前的编号被去掉
    assert doc.uri == "/v1/data/digitalBrain/api/quickBiLink"
    assert doc.method == "POST"
    assert doc.base_url == "https://data.12301dev.com"
    # 认证 header 只在 curl 里出现 —— 不从那儿取就得再问人一遍
    # 认证头换成占位,真实值走服务端配置(tool_center.secrets)。
    # 这里原先断言的是「明文 key 原样存进 headers」—— 钉住的恰恰是那个安全问题:
    # 导入一次文档,密钥就以明文进了库。测试文档里的 key 也换成了假的,真 key 不进仓库。
    assert doc.headers == {"xpftkey": "{{data_platform_key}}"}
    assert "pfk_FAKEtestKEYforFixtureONLY0000" not in doc.markdown, "原文里的明文也要抹掉"
    assert doc.sample_response["status"] is True


def test_request_schema_marks_required_and_keeps_examples():
    schema = from_markdown(REAL_DOC).request_schema()
    assert schema["required"] == ["scenicId"]
    p = schema["properties"]["scenicId"]
    assert p["type"] == "string" and "景区ID" in p["description"]
    assert p["examples"] == ["PFTSCA01018088"]   # 给了示例,模型就不会瞎编格式


def test_response_table_locates_the_data_body():
    """返回参数表不是可有可无的:结果压缩层靠它知道「那一堆行」在哪。"""
    doc = from_markdown(REAL_DOC)
    assert doc.data_path() == ["data"]
    names = [f.name for f in doc.response_fields]
    assert "data[].profile[].uri" in names
    assert doc.response_fields[0].type == "boolean"   # bool → boolean
    assert [f for f in doc.response_fields if f.name == "code"][0].type == "integer"


def test_type_config_is_directly_usable_as_an_http_tool():
    cfg = from_markdown(REAL_DOC).to_type_config()
    assert cfg["method"] == "POST"
    assert cfg["url"] == "https://data.12301dev.com/v1/data/digitalBrain/api/quickBiLink"
    assert cfg["headers"]["xpftkey"] == "{{data_platform_key}}", "明文密钥不能进工具配置"
    assert "pfk_" not in cfg["api_doc_markdown"], "留档的原文里也不能有"
    assert cfg["params_schema"]["required"] == ["scenicId"]
    assert cfg["response_schema"]["data_path"] == ["data"]
    assert cfg["api_doc_markdown"].startswith("# 1.17")   # 原文留档,改工具时人看这份


def test_tolerates_other_people_formatting():
    """不同人写的文档都得能进来 —— 要求先统一格式,等于这个功能不会被用。"""
    doc = from_markdown("""## 概览
- URI: /v1/x/y
- 请求方式: GET

### 请求参数
| 参数 | 类型 | 必填 | 说明 |
| - | - | - | - |
| page | int | 否 | 页码 |
| kw | 字符串 | 是 | 关键字 |
""")
    assert doc.uri == "/v1/x/y" and doc.method == "GET"
    s = doc.request_schema()
    assert s["required"] == ["kw"]
    assert s["properties"]["page"]["type"] == "integer"


def test_unparseable_doc_says_which_part_is_missing():
    """看不懂就说哪一节看不懂,而不是给一个空壳 —— 空壳会一路跑到线上才炸。"""
    with pytest.raises(ApiDocParseError) as e:
        from_markdown("# 一个标题\n\n随便写点什么,既没有 URI 也没有 curl")
    assert "URI" in str(e.value) and "curl" in str(e.value)
    with pytest.raises(ApiDocParseError):
        from_markdown("   ")


def test_missing_response_table_is_warned_not_fatal():
    doc = from_markdown("## 概览\n- URI: /v1/x\n- 请求方式: POST\n")
    assert any("返回参数" in w for w in doc.warnings)
    assert doc.uri == "/v1/x"                     # 仍然可用,只是压缩能力受限


# ── 二、结果压缩 ────────────────────────────────────────────────────

def _rows(n: int) -> list[dict]:
    return [{"date": f"2026-09-{i % 28 + 1:02d}",
             "channel": ["携程", "美团", "抖音", "直销"][i % 4],
             "orders": i % 500, "amount": round((i % 500) * 12.5, 2)} for i in range(n)]


def test_large_result_is_compressed_not_truncated():
    payload = {"status": True, "data": _rows(1200), "code": 0}
    schema = {"data_path": ["data"],
              "fields": [{"name": "data[].orders", "desc": "订单量"},
                         {"name": "data[].channel", "desc": "渠道"}]}
    digest = compress_tool_result(payload, response_schema=schema)
    assert digest is not None and digest["row_count"] == 1200
    text = render_digest(digest)

    raw_len = len(json.dumps(payload, ensure_ascii=False))
    assert len(text) < raw_len / 10, "压缩后应当小一个数量级"
    # 关键在于**保住了统计口径**,而不只是变短
    assert "共 1200 行" in text
    assert "合计" in text and "订单量" in text          # 数值列给了合计/均值/区间
    assert "渠道" in text and "%" in text                # 类别列给了占比
    assert "首尾各几行" in text                          # 样本取首尾,不是只取前 N 行
    assert "携程" in text


def test_small_result_is_left_alone():
    """行数少就原样给 —— 压缩反而丢信息。"""
    assert compress_tool_result({"data": [{"a": 1}, {"a": 2}]}) is None


def test_finds_the_data_body_even_without_a_schema():
    """没给 data_path 也要能找到那一堆行:挑最长的字典列表,猜错的代价只是摘要不够贴切。"""
    payload = {"code": 0, "result": {"list": _rows(60)}, "extra": [{"x": 1}]}
    digest = compress_tool_result(payload, response_schema=None)
    assert digest is not None and digest["row_count"] == 60
    assert "result" in digest["data_path"]


def test_compression_failure_falls_back_instead_of_losing_the_result():
    """压缩失败不能把结果弄丢 —— 返回 None 让调用方原样给模型。"""
    weird = {"data": [{"a": {"nested": object()}} for _ in range(50)]}
    out = compress_tool_result(weird)
    assert out is None or out["row_count"] == 50


# ── 三、内置工具 ────────────────────────────────────────────────────

def test_date_range_pins_down_the_ambiguous_phrases():
    """「近7天」含不含今天、「本周」从周几起 —— 各人理解不同,模型也不稳定。
    这里定死一套,并把口径写进返回里让它照着用。"""
    r = bi.builtin_date_range("近7天", end="2026-09-19")
    assert (r["start"], r["end"], r["days"]) == ("2026-09-13", "2026-09-19", 7)
    assert "含今天" in r["note"]

    wk = bi.builtin_date_range("本周", end="2026-09-19")      # 周六
    assert wk["start"] == "2026-09-14" and "周一" in wk["note"]

    lm = bi.builtin_date_range("上月", end="2026-09-19")
    assert (lm["start"], lm["end"]) == ("2026-08-01", "2026-08-31")

    unknown = bi.builtin_date_range("随便什么", end="2026-09-19")
    assert "认不出" in unknown["note"] and "可用" in unknown["note"]


def test_align_period_yoy_and_mom():
    """同比/环比算错极难发现:报告照样成篇,只是对比基准是错的。"""
    yoy = bi.builtin_align_period("2026-09-01", "2026-09-19", "同比")["compare"]
    assert (yoy["start"], yoy["end"]) == ("2025-09-01", "2025-09-19")
    mom = bi.builtin_align_period("2026-09-01", "2026-09-19", "环比")["compare"]
    assert (mom["start"], mom["end"]) == ("2026-08-13", "2026-08-31")
    assert mom["days"] == 19                       # 等长


def test_date_shift_handles_month_ends():
    """3 月 31 日往前一个月不是「2 月 31 日」。"""
    assert bi.builtin_date_shift("2026-03-31", months=-1)["date"] == "2026-02-28"
    assert bi.builtin_date_shift("2026-01-31", months=1)["date"] == "2026-02-28"
    assert bi.builtin_date_shift("2026-09-19", days=-7)["date"] == "2026-09-12"


def test_table_query_does_the_arithmetic():
    """让模型在对话里肉眼找最大值、算占比,既慢又容易错。"""
    rows = [{"ch": "携程", "amt": 100}, {"ch": "携程", "amt": 250}, {"ch": "美团", "amt": 80}]
    out = bi.builtin_table_query(rows=rows, group_by="ch", agg="amt:sum", sort_by="-amt")
    assert out["rows"] == [{"ch": "携程", "amt": 350}, {"ch": "美团", "amt": 80}]

    filtered = bi.builtin_table_query(rows=rows, filter='ch == "携程"')
    assert filtered["row_count"] == 2

    bad = bi.builtin_table_query(rows=rows, filter="这不是合法语法 @@")
    assert "error" in bad and "hint" in bad and bad["columns"] == ["ch", "amt"]

    assert "error" in bi.builtin_table_query(rows=[])


def test_now_tells_the_model_what_day_it_is():
    """模型只知道训练时的世界。不给它这个,它要么问用户,要么按训练时的日期算。"""
    n = bi.builtin_now()
    assert n["timezone"].startswith("Asia/Shanghai")
    assert 1 <= n["weekday"] <= 7 and n["weekday_cn"] in "一二三四五六日"
    assert "UTC" in n["note"]


def test_all_builtins_are_exposed_as_function_schemas():
    schemas = bi.function_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert {"sys_now", "sys_date_range", "sys_align_period",
            "sys_table_query", "sys_quickbi_link"} <= names
    for s in schemas:
        f = s["function"]
        assert f["description"] and f["parameters"]["type"] == "object"


def test_builtins_are_available_to_every_skill():
    """这几样每个景区都要、配法完全一样,靠运营逐个绑的结果就是没人绑。"""
    import sys
    sys.modules.setdefault("langchain_core", type(sys)("langchain_core"))
    from cogni_agent.runtime_service.engine.resolver import _builtin_specs

    specs = _builtin_specs()
    assert {s.name for s in specs} == set(bi.BUILTIN)
    assert all(s.type == "builtin" and not s.allow_override for s in specs)
    assert specs[0].to_function_schema()["function"]["name"] == "sys_now"
