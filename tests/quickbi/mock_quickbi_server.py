"""按官方协议实现的 Quick BI OpenAPI 模拟服务(Starlette),供离线测试使用。

- 每个请求都校验 X-Gw-* 签名(签名算法独立实现,逐行对照官方 skill 的 build_signature,
  不复用被测模块,避免「自己验自己」);
- SSE 事件格式对照官方脚本的解析逻辑(event:message / data:{"type","data"});
- 小Q报告:每次拉取 qreportChatData 返回「截至当前」的累积事件,第 3 次拉取结束;
- 仪表板快照:第 1 次 PROCESSING,第 2 次 SUCCESS。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib import parse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

API_KEY = "mock-ak"
API_SECRET = "mock-sk"
# 假的、但与真凭证同形状(32 位十六进制)的 token。
# **不要换成真的**:这些用例断言的是「密钥会被打码」,任何同形状的值都能证明,
# 而把真值写进仓库等于让它随代码、随打包、随日志到处走。
USER_TOKEN = "deadbeefcafe4f0e8a1b2c3d4e5f6071"
CUBE_SALES = "cube-sales-0001"
CUBE_TRAFFIC = "cube-traffic-0002"
PAGE_ID = "88c9e5de-2d4d-4ba9-a045-6f9b71cf0c66"


def official_signature(method, uri, params, access_id, access_key, nonce, timestamp):
    # ↓↓↓ 与 alibabacloud-quickbi-smartq/scripts/common/utils.py::build_signature 逐行一致 ↓↓↓
    if not params:
        request_query_string = ""
    else:
        parts = []
        for key in sorted(params):
            value = params[key]
            if value is None or value == "":
                continue
            parts.append(f"{key}={value}")
        request_query_string = "\n" + "&".join(parts) if parts else ""
    request_headers = (
        "\nX-Gw-AccessId:" + access_id
        + "\nX-Gw-Nonce:" + nonce
        + "\nX-Gw-Timestamp:" + timestamp
    )
    string_to_sign = method.upper() + "\n" + uri + request_query_string + request_headers
    encoded_string = parse.quote(string_to_sign, "")
    digest = hmac.new(access_key.encode("utf-8"), encoded_string.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class State:
    def __init__(self):
        self.report_polls = 0
        self.snapshot_polls = 0
        self.calls: list[tuple[str, str]] = []
        self.last_body: dict = {}
        self.fail_table_search = False
        self.report_running_chat: str | None = None
        self.two_cube_dashboard = False   # 仪表板带第二张图(客流,另一个数据集),测多表问数用


STATE = State()


async def _check_sign(request: Request) -> Response | None:
    h = request.headers
    if h.get("X-Gw-AccessId") != API_KEY:
        return JSONResponse({"success": False, "code": "AE0000000401", "message": "invalid access id"}, 401)
    params = None
    ctype = h.get("content-type", "")
    if request.method == "GET":
        params = dict(request.query_params)
    elif ctype.startswith("application/x-www-form-urlencoded"):
        params = dict(await request.form())
    expected = official_signature(request.method, request.url.path, params, API_KEY, API_SECRET,
                                  h.get("X-Gw-Nonce", ""), h.get("X-Gw-Timestamp", ""))
    if expected != h.get("X-Gw-Signature"):
        return JSONResponse({"success": False, "code": "AE0000000403", "message": "signature mismatch"}, 403)
    return None


def ok(data):
    return JSONResponse({"success": True, "code": "0", "message": "", "data": data})


def fail(code, message):
    return JSONResponse({"success": False, "code": code, "message": message, "data": None})


def sse(events, crlf=False):
    sep = "\r\n" if crlf else "\n"

    async def gen():
        for e in events:
            yield f"event:message{sep}data:{json.dumps(e, ensure_ascii=False)}{sep}{sep}"
    return StreamingResponse(gen(), media_type="text/event-stream")


async def endpoint(request: Request):
    bad = await _check_sign(request)
    if bad is not None:
        return bad
    path = request.url.path
    STATE.calls.append((request.method, path))
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = json.loads((await request.body()) or b"{}")
        STATE.last_body = body

    if path == "/openapi/v2/smartq/query/llmCubeWithThemeList":
        if request.query_params.get("userId") != USER_TOKEN:
            return fail("AE0150100004", "user not in organization")
        return ok({"cubeIds": {CUBE_SALES: "订单销售明细", CUBE_TRAFFIC: "景区客流统计"}})

    if path == "/openapi/v2/smartq/tableSearch":
        if STATE.fail_table_search:
            return fail("AE0581030099", "cubeIds can not be empty or over limit")
        q = body.get("userQuestion", "")
        return ok([CUBE_TRAFFIC] if "客流" in q else [CUBE_SALES])

    if path == "/openapi/v2/smartq/queryByQuestion":
        # SmartqQueryAbility(同步):multipleCubeIds(JSON 数组)= 多表问数,模型在其中选一张或多张表。
        # 与线上一致:传成逗号字符串直接 400(AE0510000038,Jackson 反序列化 ArrayList 失败)
        multi = body.get("multipleCubeIds")
        if isinstance(multi, str):
            return fail("AE0510000038", "invalid parameter: JSON parse error: Cannot construct instance of "
                        "`java.util.ArrayList` from String value (through reference chain: "
                        "SmartqQueryInput[\"multipleCubeIds\"])")
        ids = [str(x) for x in multi] if multi else ([body["cubeId"]] if body.get("cubeId") else [])
        if not ids:
            return fail("AE0581030099", "cubeId or multipleCubeIds required")
        steps = []
        if CUBE_SALES in ids:
            steps.append({"chartType": "BAR", "logicSql": "SELECT platform, SUM(amount)",
                          "metaType": [{"v": "string", "k": "销售平台", "t": "dimension"},
                                       {"v": "number", "k": "销售额", "t": "measure"}],
                          "values": [{"row": ["天猫", "1200.5"]}, {"row": ["京东", "800"]}],
                          "conclusionText": "天猫销售额最高"})
        if CUBE_TRAFFIC in ids:
            steps.append({"chartType": "LINE", "logicSql": "SELECT day, SUM(visitors)",
                          "metaType": [{"v": "string", "k": "日期", "t": "dimension"},
                                       {"v": "number", "k": "客流", "t": "measure"}],
                          "values": [{"row": ["09-20", "69"]}, {"row": ["09-21", "67"]}],
                          "conclusionText": "客流平稳"})
        if len(steps) == 1:
            return ok(steps[0])
        return ok({"chartType": "", "values": [], "metaType": [], "conclusionText": "天猫销售额最高,客流平稳",
                   "dataList": steps})

    if path == "/openapi/v2/smartq/queryByQuestionStream":
        if body.get("cubeId") == "no-perm":
            return fail("AE0510200000", "no permission")
        dsl = {"select": [{"name": "销售平台"}, {"name": "销售额", "aggregate": "SUM"}]}
        return sse([
            {"type": "trace", "data": "trace-qa-001"},
            {"type": "heartbeat", "data": ""},
            {"type": "reasoning", "data": "先按平台汇总"},
            {"type": "reasoning", "data": "销售额。"},
            {"type": "param", "data": json.dumps({"result": {"info": {
                "question": body.get("userQuestion"), "cubeId": body.get("cubeId"),
                "abstractDsl": json.dumps(dsl, ensure_ascii=False)}}}, ensure_ascii=False)},
            {"type": "sql", "data": "SELECT platform, SUM(amount) FROM t GROUP BY platform"},
            {"type": "olapResult", "data": json.dumps({
                "values": [{"row": ["天猫", "1200.5"]}, {"row": ["京东", "800"]}],
                "chartType": "BAR",
                "metaType": [{"v": "string", "k": "platform", "type": "row", "t": "dimension"},
                             {"v": "number", "k": "amount", "type": "column", "t": "measure"}],
                "logicSql": "SELECT platform, SUM(amount)", "conclusionText": "天猫销售额最高"},
                ensure_ascii=False)},
            {"type": "conclusion", "data": "天猫销售额最高,"},
            {"type": "conclusion", "data": "为 1200.5。"},
            {"type": "finish", "data": "done"},
            {"type": "text", "data": "finish 之后的事件不应被消费"},
        ], crlf=True)

    if path == "/openapi/v2/copilot/parse":
        form = await request.form()
        f = form.get("file")
        if form.get("oApiUserId") != USER_TOKEN or f is None:
            return fail("E1", "bad upload")
        content = await f.read()
        return ok({"fileId": f"file-{len(content)}", "sheets": [{"name": "Sheet1"}]})

    if path == "/openapi/v2/smartq/queryByQuestionStreamByFile":
        return sse([
            {"type": "trace", "data": "trace-file-001"},
            {"type": "plan", "data": "1. 读取数据 2. 统计"},
            {"type": "python", "data": {"code": "df.groupby('部门').size()", "result": "ok"}},
            {"type": "result", "data": json.dumps({"dataList": [{
                "title": "各部门人数", "chartType": "bar",
                "fieldInfo": [{"fieldName": "部门", "type": "string", "role": "dimension"},
                              {"fieldName": "人数", "type": "numerical", "role": "metric"}],
                "data": [["研发", 30], ["销售", 12]]}]}, ensure_ascii=False)},
            {"type": "reporter", "data": "研发部人数最多。"},
            {"type": "finish", "data": ""},
        ])

    if path == "/openapi/v2/snapshot/calling/shot":
        STATE.snapshot_polls += 1
        if STATE.snapshot_polls == 1:
            return ok({"status": "PROCESSING"})
        md = "## 销售概览\n\n| 月份 | 销售额 |\n| --- | --- |\n| 1月 | 100 |\n| 2月 | 150 |\n"
        return ok({"status": "SUCCESS", "resultMarkdownText": md})

    if path == "/openapi/v2/smartq/dataInterpretationStream":
        data = base64.b64decode(body.get("stringData", "")).decode("utf-8")
        assert "销售额" in data
        # 真实形态(取自现场导出):推理与回答用同一个 type,靠 subType 区分,
        # 且逐字增量下发。locale 这类噪音事件也照发,归一化要能忽略。
        return sse([
            {"type": "trace", "data": "trace-interpret-1"},
            {"type": "locale", "data": "zh_CN"},
            {"type": "summary", "subType": "model_reasoning", "data": "用户要求解读"},
            {"type": "summary", "subType": "model_reasoning", "data": "提供的数据。\n先判断意图类型。"},
            {"type": "summary", "subType": "model_reasoning", "data": "判定为类型C,按类型C的结构输出。"},
            {"type": "text", "data": "【用户问题解答】\n"},
            {"type": "text", "data": "2 月销售额环比增长 50%。"},
            {"type": "finish", "data": ""},
        ])

    if path == "/openapi/v2/skills/dashboard/handle":
        if body.get("id") != PAGE_ID:
            return fail("AE0510200000", "no permission")
        return ok(f"https://bi.aliyun.com/dashboard/view/pc.htm?pageId={PAGE_ID}")

    if path == "/openapi/v2/dashboard/query":
        comp = {
            "componentId": "c1", "componentName": "平台销售", "dataFromId": CUBE_SALES,
            "componentContent": json.dumps({"id": "i1", "type": "bar",
                                            "attribute": {"caption": "平台销售额", "fieldSettingMap": {}},
                                            "tileLayout": {"x": 0, "y": 0, "w": 6, "h": 4}}),
            "queryInput": json.dumps({"sourceId": CUBE_SALES, "area": [
                {"id": "area_row", "columnList": [{"pathId": "p1", "caption": "销售平台", "itemType": "dimension"}]},
                {"id": "area_column", "columnList": [{"pathId": "p2", "caption": "amount", "itemType": "measure",
                                                      "aggregator": "SUM"}]},
                {"id": "filters", "columnList": [{"caption": "日期", "levels": [{}]}]},
            ]}),
        }
        ctrl = {"componentId": "q1", "componentContent": json.dumps({"id": "i2", "type": "query2", "modelConfig": {
            "fieldConfigs": [{"labelName": "日期", "componentType": "datetime", "isRequired": True}]}}),
            "queryInput": "{}"}
        comps = [comp, ctrl]
        if STATE.two_cube_dashboard:
            comps.append({
                "componentId": "c2", "componentName": "客流趋势", "dataFromId": CUBE_TRAFFIC,
                "componentContent": json.dumps({"id": "i3", "type": "line",
                                                "attribute": {"caption": "客流趋势", "fieldSettingMap": {}},
                                                "tileLayout": {"x": 0, "y": 4, "w": 6, "h": 4}}),
                "queryInput": json.dumps({"sourceId": CUBE_TRAFFIC, "area": [
                    {"id": "area_row", "columnList": [{"pathId": "d1", "caption": "日期", "itemType": "dimension"}]},
                    {"id": "area_column", "columnList": [{"pathId": "m1", "caption": "客流", "itemType": "measure",
                                                          "aggregator": "SUM"}]}]}),
            })
        return ok({"name": "经营驾驶舱", "treeId": request.query_params.get("dashboardId"),
                   "components": comps})

    if path == "/openapi/v2/dataset/batchGetSchema":
        return ok([{"cubeId": CUBE_SALES, "data": {"cubeSchema": {"caption": "订单销售明细", "fields": [
            {"uniqueId": "p2", "caption": "销售额"}]}}}])

    if path == "/openapi/v2/dataportal/query":
        return ok({"name": "经营门户", "menu": [
            {"id": "m0", "title": "首页", "isHome": True, "isEmpty": True, "children": [
                {"id": "m1", "title": "驾驶舱", "content": [{"type": "report", "id": PAGE_ID, "name": "经营驾驶舱"}]}]},
            {"id": "m2", "title": "外链", "content": [{"type": "url", "id": "u1", "name": "官网"}]},
        ]})

    if path == "/openapi/v2/qreport/uploadReferenceFile":
        form = await request.form()
        f = form.get("file")
        if form.get("chatType") != "manus" or form.get("userId") != USER_TOKEN:
            return fail("E2", "bad form")
        name = f.filename
        stem, _, ext = name.rpartition(".")
        return ok({"fileId": f"ref-{stem}", "fileName": stem, "fileType": ext})

    if path == "/openapi/v2/smartq/createQreportChat":
        if STATE.report_running_chat:
            return PlainTextResponse(f"当前用户已有运行中的任务,问题:上周报告,chatId:{STATE.report_running_chat}")
        if not body.get("async") or body.get("oapiUserId") != USER_TOKEN:
            return fail("E3", "bad create")
        STATE.report_polls = 0
        return JSONResponse(body["chatId"])

    if path == "/openapi/v2/smartq/qreportChatData":
        STATE.report_polls += 1
        events = [
            {"type": "trace", "data": json.dumps({"content": "trace-report-1"})},
            {"type": "plan", "data": json.dumps({"function": "thinking", "content": "规划报告结构"})},
            {"type": "step", "data": json.dumps({"title": "数据分析", "desc": "分析上传数据"})},
        ]
        if STATE.report_polls >= 2:
            events += [
                {"type": "subStep", "data": json.dumps({"function": "onlineSearchResult",
                                                        "webItems": [{"title": "行业报告", "link": "https://x", "hostName": "x"}]})},
                {"type": "qreport", "data": json.dumps({"function": "structuredChart", "chart": {"dataList": [
                    {"title": "月度趋势", "fieldInfo": [{"fieldName": "月份", "role": "dimension"},
                                                    {"fieldName": "销售额", "type": "numerical", "role": "metric"}],
                     "data": [["1月", 100], ["2月", 150]]}]}})},
            ]
        if STATE.report_polls >= 3:
            events += [
                {"type": "qreport", "data": json.dumps({"function": "mainText", "content": "<p>报告正文</p>"})},
                {"type": "qreport", "data": json.dumps({"function": "qreportUsedToken", "promptTokens": 10,
                                                        "completionTokens": 20, "totalTokens": 30})},
            ]
        return ok(json.dumps(events, ensure_ascii=False))

    return JSONResponse({"success": False, "code": "404", "message": f"unknown {path}"}, 404)


app = Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])
