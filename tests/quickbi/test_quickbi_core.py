"""Quick BI 集成模块核心测试:签名 / 客户端 / 事件归一化 / 能力编排。

不依赖数据库、不依赖外网:用 httpx.ASGITransport 把请求直接送进 mock_quickbi_server,
mock 端用**独立实现**的官方签名算法验签,签名不对会返回 403。
测试函数全部是同步函数 + asyncio.run,与 pytest-asyncio / anyio 插件都不冲突。
"""
from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest

from cogni_agent.tool_center_service.quickbi import capabilities as cap
from cogni_agent.tool_center_service.quickbi import dashboard as dash
from cogni_agent.tool_center_service.quickbi import filesupport as fs
from cogni_agent.tool_center_service.quickbi.client import ClientConfig, QuickBIClient, parse_sse_block
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
from cogni_agent.tool_center_service.quickbi.events import ResultAccumulator, parse_report_events
from cogni_agent.tool_center_service.quickbi.signing import build_string_to_sign, sign
from tests.quickbi import mock_quickbi_server as mock


def _cfg(**kw) -> ClientConfig:
    base = dict(server_domain="https://bi.aliyun.com", api_key=mock.API_KEY, api_secret=mock.API_SECRET,
                user_token=mock.USER_TOKEN)
    base.update(kw)
    return ClientConfig(**base)


def _client(**kw) -> QuickBIClient:
    return QuickBIClient(_cfg(**kw), transport=httpx.ASGITransport(app=mock.app))


async def _nosleep(_):
    return None


async def _collect(agen) -> tuple[list[dict], ResultAccumulator]:
    events = [e async for e in agen]
    acc = ResultAccumulator()
    acc.extend(events)
    return events, acc


@pytest.fixture(autouse=True)
def _reset_state():
    mock.STATE.__init__()
    yield


# ── 签名 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("params", [None, {}, {"b": "2", "a": "1", "empty": "", "none": None},
                                    {"q": "中文 值&x=1", "z": 3}])
def test_signature_matches_official(params):
    ours = sign("get", "/openapi/v2/x", params, "ak", "sk", "nonce-1", "1700000000000")
    official = mock.official_signature("get", "/openapi/v2/x", params, "ak", "sk", "nonce-1", "1700000000000")
    assert ours == official


def test_string_to_sign_layout():
    s = build_string_to_sign("GET", "/u", {"b": 2, "a": 1}, "AK", "N", "T")
    assert s == "GET\n/u\na=1&b=2\nX-Gw-AccessId:AK\nX-Gw-Nonce:N\nX-Gw-Timestamp:T"


def test_wrong_secret_is_rejected():
    async def go():
        async with _client(api_secret="bad") as qbi:
            await cap.list_cubes(qbi)
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert ei.value.kind == "auth" and ei.value.status == 403


def test_missing_credentials_is_config_error():
    async def go():
        async with _client(api_key="", api_secret="") as qbi:
            await cap.list_cubes(qbi)
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert ei.value.kind == "config"
    assert "一键复制 skill 配置" in str(ei.value)


# ── SSE 分帧 ────────────────────────────────────────────────────────

def test_parse_sse_block_variants():
    assert parse_sse_block('event:message\ndata:{"type":"text","data":"hi"}') == {"type": "text", "data": "hi"}
    assert parse_sse_block('data: {"type":"x"}') == {"type": "x"}
    assert parse_sse_block("event:ping") is None
    assert parse_sse_block("data:not-json")["type"] == "raw"


# ── 小Q问数 · 数据集 ─────────────────────────────────────────────────

def test_dataset_qa_with_cube_id():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_dataset_qa(qbi, question="各平台销售额", cube_id=mock.CUBE_SALES))
    events, acc = asyncio.run(go())
    types = [e["type"] for e in events]
    assert "trace" in types and "chart" in types
    assert "finish 之后" not in acc.answer, "finish 之后的事件不应被消费"
    chart = acc.charts[0]
    assert chart["chart_type"] == "bar"
    assert [f["name"] for f in chart["fields"]] == ["销售平台", "销售额(SUM)"]   # 字段名来自 DSL
    assert chart["rows"][0] == {"销售平台": "天猫", "销售额(SUM)": 1200.5}      # 数值转型
    assert acc.text("conclusion") == "天猫销售额最高,为 1200.5。"
    assert acc.to_result()["reasoning"] == "先按平台汇总销售额。"
    assert acc.to_result()["trace_id"] == "trace-qa-001"
    assert mock.STATE.last_body["runningBySkill"] is True
    assert mock.STATE.last_body["userId"] == mock.USER_TOKEN


def test_dataset_qa_auto_table_search():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_dataset_qa(qbi, question="上周客流是多少"))
    events, acc = asyncio.run(go())
    assert acc.meta["cube_id"] == mock.CUBE_TRAFFIC and acc.meta["cube_how"] == "table_search"
    assert ("POST", "/openapi/v2/smartq/tableSearch") in mock.STATE.calls


def test_dataset_qa_table_search_failure_falls_back_to_relevance():
    mock.STATE.fail_table_search = True

    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_dataset_qa(qbi, question="景区客流统计 今天多少人"))
    _, acc = asyncio.run(go())
    assert acc.meta["cube_id"] == mock.CUBE_TRAFFIC and acc.meta["cube_how"] == "relevance"


def test_dataset_qa_cube_name_exact():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_dataset_qa(qbi, question="总额", cube_name="订单销售明细"))
    _, acc = asyncio.run(go())
    assert acc.meta["cube_id"] == mock.CUBE_SALES and acc.meta["cube_how"] == "name_exact"


def test_business_error_is_translated():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_dataset_qa(qbi, question="x", cube_id="no-perm"))
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert ei.value.code == "AE0510200000"
    assert "权限" in str(ei.value)


def test_wrong_user_token_translated():
    async def go():
        async with _client(user_token="someone-else") as qbi:
            return await cap.check_connection(qbi)
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert ei.value.code == "AE0150100004" and "不在 Quick BI 组织内" in str(ei.value)


# ── 小Q问数 · 上传文件 ───────────────────────────────────────────────

def test_file_upload_and_qa():
    async def go():
        async with _client() as qbi:
            up = await cap.upload_chat_file(qbi, "人员.csv", "部门,人数\n研发,30\n销售,12\n".encode())
            events, acc = await _collect(cap.run_file_qa(qbi, question="各部门人数", file_id=up["file_id"]))
            return up, events, acc
    up, events, acc = asyncio.run(go())
    assert up["file_id"].startswith("file-")
    assert acc.charts[0]["rows"] == [{"部门": "研发", "人数": 30}, {"部门": "销售", "人数": 12}]
    assert acc.text("report") == "研发部人数最多。"
    assert "groupby" in acc.to_result()["code"]
    assert any(s["title"] == "分析计划" for s in acc.steps)


def test_file_upload_rejects_large_and_bad_type():
    async def go(name, size):
        async with _client() as qbi:
            await cap.upload_chat_file(qbi, name, b"x" * size)
    with pytest.raises(QuickBIError, match="5MB"):
        asyncio.run(go("a.xlsx", fs.MAX_FILE_BYTES + 1))
    with pytest.raises(QuickBIError):
        asyncio.run(go("a.pdf", 10))


# ── 小Q报表 · 仪表板 ─────────────────────────────────────────────────

TOKEN3RD_URL = ("https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=88c9e5de-2d4d-4ba9-a045-6f9b71cf0c66"
                "&accessTicket=807cc156-9109-4dfc-b4d7-ce5c670ba566&dd_orientation=auto")


def test_extract_page_id_from_token3rd_url():
    assert dash.extract_page_id(TOKEN3RD_URL) == mock.PAGE_ID
    assert dash.url_access_ticket(TOKEN3RD_URL) == "807cc156-9109-4dfc-b4d7-ce5c670ba566"
    assert dash.extract_page_id(mock.PAGE_ID) == mock.PAGE_ID


def test_fetch_dashboard_structure():
    async def go():
        async with _client() as qbi:
            return await cap.fetch_dashboard(qbi, TOKEN3RD_URL)
    s = asyncio.run(go())
    assert s["name"] == "经营驾驶舱" and s["page_id"] == mock.PAGE_ID
    chart = s["charts"][0]
    assert chart["cube_id"] == mock.CUBE_SALES and chart["cube_name"] == "订单销售明细"
    assert chart["measures"][0]["name"] == "销售额"          # schema caption 覆盖原始字段名
    assert chart["filters"] == ["日期"]
    assert s["query_controls"][0]["fields"][0]["required"] is True
    assert s["cubes"][0]["cube_id"] == mock.CUBE_SALES
    assert any(q["capability"] == "insight" for q in s["suggested_questions"])


def test_dataportal_url_resolves_home_menu():
    async def go():
        async with _client() as qbi:
            return await cap.resolve_page(qbi, "https://bi.aliyun.com/product/view.htm?module=dashboard&productId=p-1")
    page = asyncio.run(go())
    assert page["page_id"] == mock.PAGE_ID and page["portal"]["menu_id"] == "m1"


def test_dataportal_url_non_dashboard_menu():
    async def go():
        async with _client() as qbi:
            return await cap.resolve_page(qbi, "https://bi.aliyun.com/product/view.htm?productId=p-1&menuId=m2")
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert "外部链接" in str(ei.value)


def test_dashboard_qa_restricts_to_dashboard_cubes():
    async def go():
        async with _client() as qbi:
            # 问题里提到「客流」,但仪表板只引用销售数据集 → 必须落在销售数据集
            return await _collect(cap.run_dashboard_qa(qbi, question="客流和销售额", dashboard=TOKEN3RD_URL))
    _, acc = asyncio.run(go())
    assert acc.meta["cube_id"] == mock.CUBE_SALES
    # v3.57:候选只剩一个数据集时直接单表问(不再走智能选表),也就没有 tableSearch
    assert ("POST", "/openapi/v2/smartq/tableSearch") not in mock.STATE.calls
    assert acc.charts


# ── 小Q解读 ────────────────────────────────────────────────────────

def test_insight_from_dashboard_snapshot():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_insight(qbi, question="趋势如何", dashboard=TOKEN3RD_URL, sleep=_nosleep))
    events, acc = asyncio.run(go())
    assert mock.STATE.snapshot_polls == 2
    # 回答里只有回答。上游把思维链和回答塞在同一个 type(summary/text)里,
    # 靠 subType=model_reasoning 区分 —— 读错这个字段,整条思维链就会灌进回答。
    assert acc.answer == "【用户问题解答】\n2 月销售额环比增长 50%。"
    assert "判定为类型C" not in acc.answer and "先判断意图类型" not in acc.answer
    reasoning = "".join(e["delta"] for e in events if e["type"] == "reasoning")
    assert "用户要求解读" in reasoning and "判定为类型C" in reasoning
    assert mock.STATE.last_body["userQuestion"].startswith("【关键要求】")
    assert mock.STATE.last_body["oapiUserId"] == mock.USER_TOKEN


def test_insight_from_uploaded_csv():
    md = fs.to_markdown("sales.csv", "月份,销售额\n1月,100\n".encode("utf-8"))
    assert "| 月份 | 销售额 |" in md

    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_insight(qbi, question="解读", markdown=md))
    _, acc = asyncio.run(go())
    assert "环比" in acc.answer
    assert mock.STATE.snapshot_polls == 0


def test_xlsx_to_markdown():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["月份", "销售额"])
    ws.append(["1月", 100])
    buf = io.BytesIO()
    wb.save(buf)
    md = fs.to_markdown("a.xlsx", buf.getvalue())
    assert "## 销售" in md and "| 1月 | 100 |" in md


def test_insight_rejects_oversized_data():
    async def go():
        async with _client() as qbi:
            return await _collect(cap.run_insight(qbi, question="x", markdown="a" * (fs.MAX_INSIGHT_CHARS + 1)))
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(go())
    assert ei.value.kind == "input"


# ── 小Q报告 ────────────────────────────────────────────────────────

def test_report_with_upload_full_flow():
    async def go():
        async with _client() as qbi:
            up = await cap.upload_report_file(qbi, "销售数据.csv", "月份,销售额\n1月,100\n".encode())
            events, acc = await _collect(cap.run_report(qbi, question="生成本月销售分析报告", uploads=[up],
                                                        poll_interval=0, sleep=_nosleep))
            return up, events, acc
    up, events, acc = asyncio.run(go())
    assert up == {"fileId": "ref-销售数据", "fileName": "销售数据", "fileType": "csv"}
    body = mock.STATE.last_body
    assert body["resources"] == [{"id": "ref-销售数据", "title": "销售数据", "type": "csv"}]
    assert json.loads(body["attachment"])["resource"]["files"][0]["iconType"] == "excel"
    assert mock.STATE.report_polls == 3
    final = [e for e in events if e["type"] == "report"][-1]
    assert final["finished"] and final["success"]
    assert final["report_url"] == f"https://bi.aliyun.com/copilot/qreportReplay?caseId={body['chatId']}"
    assert acc.usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    assert acc.text("report") == "报告正文"
    assert acc.charts and acc.charts[0]["title"] == "月度趋势"
    # 增量:每个事件只输出一次(3 次轮询累积返回,不能重复)
    assert sum(1 for e in events if e["type"] == "step" and e.get("title") == "数据分析") == 1


def test_report_reuses_running_task():
    mock.STATE.report_running_chat = "chat-running-1"

    async def go():
        async with _client() as qbi:
            return await cap.create_report(qbi, question="新报告")
    r = asyncio.run(go())
    assert r["reused"] and r["chat_id"] == "chat-running-1" and r["running_question"] == "上周报告"


def test_report_from_dashboard_snapshot_markdown():
    md = ("## 销售概览\n\n| 月份 | 销售额 |\n| --- | --- |\n| 1月 | 100 |\n\n"
          "## 客流\n\n| 日期 | 人数 |\n|:---|---:|\n| 1日 | 5 |\n")
    files = fs.markdown_to_csv_files(md, base_name="驾驶舱")
    assert [n for n, _ in files] == ["驾驶舱_1_销售概览.csv", "驾驶舱_2_客流.csv"]
    assert files[1][1].decode("utf-8-sig").splitlines() == ["日期,人数", "1日,5"]

    async def go():
        async with _client() as qbi:
            return await cap.report_uploads_from_markdown(qbi, md, base_name="驾驶舱")
    ups = asyncio.run(go())
    assert len(ups) == 2 and ups[0]["fileType"] == "csv"


def test_parse_report_events_sse_text():
    raw = 'data:{"type":"step","data":"{\\"title\\":\\"A\\"}"}\n\ndata:{"type":"finish","data":""}\n\n'
    evs = parse_report_events(raw)
    assert evs[0]["parsedData"] == {"title": "A"} and evs[1]["type"] == "finish"


def test_report_error_envelope():
    evs = parse_report_events(json.dumps({"success": False, "code": "AE0533330017", "message": "exceed"}))
    s = cap.summarize_report(json.dumps({"success": False, "code": "AE0533330017", "message": "exceed"}),
                             base="https://bi.aliyun.com", chat_id="c")
    assert evs[0]["type"] == "error"
    assert s["finished"] and not s["success"] and "免费额度" in s["error"]


# ── 路由 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,kw,expect", [
    ("帮我生成本月销售分析报告", {}, "report"),
    ("解读一下这个仪表板的趋势", {"has_dashboard": True}, "insight"),
    ("解读一下趋势", {}, "dataset_qa"),          # 没有文件/仪表板时「解读」退化为问数
    ("TOP10 部门", {"has_file": True}, "file_qa"),
    ("销售额是多少", {"has_dashboard": True}, "dashboard_qa"),
    ("销售额是多少", {}, "dataset_qa"),
    # 对齐 SKILL.md:数据集也能解读;有文档先走文档解析;报告关键词压过一切
    ("解读一下趋势", {"has_dataset": True}, "insight"),
    ("这份合同写了什么", {"has_file": True, "documents": 1}, "doc_parse"),
    ("解读这些文件的趋势", {"has_file": True, "documents": 3}, "doc_parse"),
    ("结合这些文件写一份复盘报告", {"has_file": True, "documents": 3}, "report"),
])
def test_route_capability(q, kw, expect):
    assert cap.route_capability(q, **kw) == expect


@pytest.mark.parametrize("q,n,expect", [
    ("解析这10个发票PDF,提取字段生成Excel", 10, None),      # 明说要表:Excel 就是交付物
    ("这些文件中的数据有什么规律和洞察", 4, "insight"),
    ("帮我查一下销售额最高的产品", 3, "file_qa"),            # 多份文档问具体数据
    ("这份合同写了什么", 1, None),                          # 单份文档:提正文
])
def test_doc_followup(q, n, expect):
    assert cap.doc_followup(q, n) == expect


def test_detect_locale():
    assert cap.detect_locale("请分析 Sales Dataset 的数据") == "zh_CN"
    assert cap.detect_locale("Analyze the '销售数据' dataset") == "en_US"


def test_llm_text_summary():
    acc = ResultAccumulator()
    acc.add({"type": "text", "delta": "结论", "section": "answer"})
    acc.add({"type": "chart", "chart": {"title": "T", "fields": [{"name": "a"}], "rows": [{"a": i} for i in range(40)],
                                        "row_count": 40}})
    t = acc.as_llm_text(max_rows=5)
    assert "结论" in t and "| 4 |" in t and "| 5 |" not in t and "共 40 行" in t


def test_multi_cube_empty_pick_falls_back_to_fanout(monkeypatch):
    """运行 31427b3e vs 027eee3d:小Q在 12 个数据集里只选了「供应商每日报表」,昨日一行「-」→「不存在」;
    逐个数据集取数能在「销售报表」取到 181 单。选中的表没数时要兜底逐个取。
    另:线上 dataList 的元素是 JSON 字符串,要能解析。"""
    empty = {"chartType": "INDICATOR_CARD", "metaType": [{"k": "销售票数", "v": "number", "t": "Measure"}],
             "values": [{"row": ["-"]}], "logicSql": "SELECT 1",
             "dataList": [json.dumps({"chartType": "CUSTOM_CHART", "logicSql": "SELECT 1"})]}

    class Q:
        user_id = "u"

        async def post_json(self, uri, payload, timeout=None):
            assert isinstance(payload["multipleCubeIds"], list)
            return empty

    async def fake_single(qbi, *, question, cube_id, locale="zh_CN", **kw):
        v = "364" if cube_id == "sales" else "-"
        yield {"type": "chart", "chart": {"fields": [{"name": "销售票数", "role": "metric"}],
                                          "rows": [{"销售票数": v}], "title": question}}
    monkeypatch.setattr(cap, "run_dataset_qa", fake_single)

    async def go():
        return [e async for e in cap.run_multi_cube_qa(Q(), question="昨日票务销售情况", cube_ids=["daily", "sales", "api"])]
    evs = asyncio.run(go())
    charts = [e for e in evs if e["type"] == "chart"]
    assert any(e.get("cube_id") == "sales" and e["chart"]["rows"][0]["销售票数"] == "364" for e in charts)
    assert not any(e.get("cube_id") in ("daily", "api") for e in charts)      # 空的不往下发
    assert any("逐个数据集取数" in (e.get("message") or "") for e in evs)


def test_multi_cube_with_values_does_not_fan_out(monkeypatch):
    ok = {"chartType": "INDICATOR_CARD", "metaType": [{"k": "销售票数", "v": "number", "t": "Measure"}],
          "values": [{"row": ["364"]}], "logicSql": "SELECT 1"}

    class Q:
        user_id = "u"

        async def post_json(self, uri, payload, timeout=None):
            return ok

    async def boom(*a, **k):
        raise AssertionError("有值时不该逐个取数")
        yield
    monkeypatch.setattr(cap, "run_dataset_qa", boom)
    evs = asyncio.run(_drain(cap.run_multi_cube_qa(Q(), question="q", cube_ids=["a", "b"])))
    assert [e for e in evs if e["type"] == "chart"]


async def _drain(gen):
    return [e async for e in gen]
