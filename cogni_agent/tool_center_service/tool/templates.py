"""
工具配置模板与字段说明 —— **由后端下发,不在前端硬编码**。

为什么放后端:
- 真正消费 `type_config` 的是执行引擎和沙箱(见 sandbox/local.py、mcp/protocol.py)。
  字段含义、必填性、可用变量都由它们决定。写在前端就变成两份真相,后端加了字段
  前端不知道,前端写的说明和后端行为对不上——这类不一致排查起来非常费劲。
- 大模型也要看这些说明。Tool 的 description 会进 function-calling 的 schema,
  统一在这里维护,人看的和模型看的就是同一份。

前端通过 GET /api/admin/tools/templates 拿到全部模板,用来渲染表单、字段提示(?)、
示例与教程,不需要自己写死任何一段。
"""
from __future__ import annotations

# ── 模板变量:所有工具类型共用 ────────────────────────────────────────────
# 这些变量在**执行时由系统注入**,不需要模型提供,也不该让用户手填。
TEMPLATE_VARIABLES = [
    {"name": "{{scenic_id}}", "desc": "当前任务所属景区 ID。集团级任务为空字符串", "example": "wuyishan"},
    {"name": "{{org_id}}", "desc": "同 scenic_id,集团级任务时取值为 group", "example": "wuyishan"},
    {"name": "{{user_id}}", "desc": "发起本次调用的用户 ID", "example": "u_10086"},
    {"name": "{{now}}", "desc": "当前时间(UTC,ISO 8601)", "example": "2026-07-27T12:00:00+00:00"},
    {"name": "{{参数标识}}", "desc": "系统参数(「系统设置 · 参数配置」定义,景区可在「景区配置 · 接口参数」覆盖),"
                                "如 {{ota_account}}。可写在地址 / 请求头 / SQL 里;参数定义里把某个参数的 default 写成 "
                                "\"{{ota_account}}\" 则该参数由系统填、模型看不到。多值参数按定义里的用法逐个调用或一次传入",
     "example": "vbk_569202"},
]

_VAR_NOTE = (
    "系统变量(scenic_id / org_id / user_id / now)由执行引擎在调用前自动替换,"
    "**不会**要求模型提供;params_schema 里声明的参数才由模型填写。"
)

HTTP_TEMPLATE = {
    "type": "http",
    "label": "HTTP 接口",
    "summary": "调用一个 HTTP 接口。适合「执行动作」和「精确取数」,如下单、查订单、查实时客流。",
    "fields": [
        {
            "key": "method", "label": "请求方法", "widget": "select", "required": True, "section": "basic",
            "options": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET",
            "description": "HTTP 方法。GET 时参数拼到查询字符串,其余方法参数作为 JSON 请求体发送。",
        },
        {
            "key": "url", "label": "请求地址", "widget": "text", "required": True, "section": "basic",
            "default": "https://api.example.com/scenic/{{scenic_id}}/orders",
            "description": f"完整 URL,支持 {{{{变量}}}} 模板语法。{_VAR_NOTE}",
            "placeholder": "https://api.example.com/scenic/{{scenic_id}}/orders",
        },
        {
            "key": "headers", "label": "请求头", "widget": "kv", "required": False, "default": {}, "section": "basic",
            "description": "键值对形式的请求头。值同样支持 {{变量}};需要密钥时不要直接写明文,"
                           "填 {{credential}} 占位,再到「凭证」里维护真实值(加密存储,不回显)。",
            "example": {"Content-Type": "application/json", "Authorization": "Bearer {{credential}}"},
        },
        {
            "key": "params_schema", "label": "参数定义", "widget": "schema", "required": False, "section": "params",
            "default": {"type": "object", "properties": {}, "required": []},
            "description": "标准 JSON Schema,声明**由大模型填写**的参数。每个参数务必写清 description ——"
                           "模型就是靠它判断该不该调用这个工具、以及每个字段填什么。写得含糊会直接导致调用错误。",
            "example": {
                "type": "object",
                "properties": {
                    "city_name": {"type": "string", "description": "城市名称,如「厦门市」。必须是中文全称,不要用拼音或简称"},
                    "days": {"type": "integer", "description": "查询未来几天,取值 1-7", "minimum": 1, "maximum": 7},
                },
                "required": ["city_name"],
            },
        },
        {
            "key": "allow_override", "label": "允许运行时覆盖参数", "widget": "switch", "section": "params",
            "required": False, "default": True,
            "description": "关闭后,操练台的「参数覆盖」对该工具不生效。涉及资金/写操作的接口建议关掉。",
        },
        {
            "key": "response_schema", "label": "响应字段说明", "widget": "response", "required": False, "section": "response",
            "default": {"fields": [], "sample": None},
            "description": "响应字段表(path/类型/单位/说明)——执行链在该工具**首次成功返回**后把说明"
                           "附给大模型,让它正确解读响应语义。重点写清**空值/零值含义**(如「null=无销售,"
                           "≠错误」「0=当日无销售(有效值)」)与单位,模型就不会把零值误读成「数据不可用」。",
            "example": {"fields": [
                {"path": "code", "type": "int", "desc": "0=成功,非0=失败"},
                {"path": "data.orderCount", "type": "int", "desc": "订单数;0=当日无销售(有效值)"},
                {"path": "data.totalAmount", "type": "number", "unit": "元", "desc": "售票总收入;无销售时为 null(≠错误,≠0)"},
            ], "sample": {"code": 0, "data": {"orderCount": 0, "totalAmount": None}}},
        },
    ],
    # 基础信息(方法/地址/请求头)与参数定义。请求头属于"这个接口怎么连"的一部分,
    # 和方法、地址是同一件事,不单独拆区。
    "sections": [
        {"id": "basic", "label": "基础信息", "desc": "方法、地址、请求头"},
        {"id": "params", "label": "参数定义", "desc": "由大模型填写的参数(JSON Schema)"},
        {"id": "response", "label": "响应字段说明", "desc": "响应语义表——首调后附给大模型,防误读空值/零值"},
    ],
    "example": {
        "method": "GET",
        "url": "https://api.example.com/scenic/{{scenic_id}}/weather",
        "headers": {"Authorization": "Bearer {{credential}}"},
        "params_schema": {
            "type": "object",
            "properties": {"city_name": {"type": "string", "description": "城市名称,如「厦门市」"}},
            "required": ["city_name"],
        },
    },
    "tutorial": [
        {"title": "1. 它解决什么问题",
         "body": "把一个已有的 HTTP 接口包装成大模型能调用的工具。模型只看得到 description 和 "
                 "params_schema,看不到 url 和 headers —— 所以密钥放在 headers 里是安全的。"},
        {"title": "2. 变量从哪来",
         "body": "系统变量(scenic_id/org_id/user_id/now)由执行引擎注入,和当前任务的租户与用户绑定,"
                 "模型无法伪造。这一点很重要:租户隔离靠的就是它,不要把 scenic_id 写进 params_schema "
                 "让模型填,那等于把越权的口子交给模型。"},
        {"title": "3. description 怎么写",
         "body": "写给模型看,不是写给同事看。要包含:这个参数是什么、取值范围/格式、有什么约束。"
                 "反例「城市」;正例「城市名称,如「厦门市」。必须是中文全称,不要用拼音或简称」。"},
        {"title": "4. 密钥怎么处理",
         "body": "headers 里写 {{credential}} 占位,真实值到工具详情页的「凭证」中维护。"
                 "凭证以 Fernet 加密存库,调用时才解密注入,前端与模型都拿不到明文。"},
        {"title": "5. 先预览再保存",
         "body": "用下方「预览请求」看渲染后的真实报文(URL/Header/Body 都替换过变量),"
                 "确认无误再保存;保存后还能在工具详情页做一次真实调用验证。"},
    ],
}

DB_TEMPLATE = {
    "type": "db",
    "label": "数据库查询",
    "summary": "在已登记的数据源上执行只读 SQL。适合取业务数据做分析。",
    "fields": [
        {
            "key": "datasource_id", "label": "数据源", "widget": "datasource-select", "required": True, "section": "basic",
            "description": "选择「知识中心 · 数据库知识」里已登记的数据源。连接信息(host/账号/密码)"
                           "在那里统一维护,这里只引用 ID,避免同一套库信息在多个工具里重复抄写。",
        },
        {
            "key": "sql_template", "label": "SQL 模板", "widget": "sql", "required": True, "section": "params",
            "default": "SELECT * FROM orders WHERE scenic_id = {{scenic_id}} AND order_date >= {{start_date}} LIMIT 100",
            "description": "只允许 SELECT。{{变量}} 会被转换成**驱动层参数占位符**(asyncpg 用 $1、"
                           "MySQL 用 %s)后再执行,不是字符串拼接,因此天然免疫 SQL 注入。"
                           "未写 LIMIT 时系统会自动补一个上限。",
        },
        {
            "key": "response_schema", "label": "响应字段说明", "widget": "response", "required": False, "section": "response",
            "default": {"fields": [], "sample": None},
            "description": "响应字段表(path/类型/单位/说明)——执行链在该工具**首次成功返回**后把说明"
                           "附给大模型,让它正确解读响应语义。重点写清**空值/零值含义**(如「null=无销售,"
                           "≠错误」「0=当日无销售(有效值)」)与单位,模型就不会把零值误读成「数据不可用」。",
            "example": {"fields": [
                {"path": "code", "type": "int", "desc": "0=成功,非0=失败"},
                {"path": "data.orderCount", "type": "int", "desc": "订单数;0=当日无销售(有效值)"},
                {"path": "data.totalAmount", "type": "number", "unit": "元", "desc": "售票总收入;无销售时为 null(≠错误,≠0)"},
            ], "sample": {"code": 0, "data": {"orderCount": 0, "totalAmount": None}}},
        },
    ],
    "sections": [
        {"id": "basic", "label": "基础信息", "desc": "选择已登记的数据源"},
        {"id": "params", "label": "SQL 与参数", "desc": "只读 SQL 模板,{{变量}} 自动参数化"},
        {"id": "response", "label": "响应字段说明", "desc": "结果集字段语义表——首查后附给大模型"},
    ],
    "example": {
        "datasource_id": "(从下拉选择)",
        "sql_template": "SELECT order_date, SUM(amount) AS total FROM orders "
                        "WHERE scenic_id = {{scenic_id}} AND order_date >= {{start_date}} "
                        "GROUP BY order_date ORDER BY order_date",
    },
    "tutorial": [
        {"title": "1. datasource_id 是什么",
         "body": "就是「知识中心 · 数据库知识」里维护的数据源。先去那里新增数据源并测试连接通过,"
                 "这里的下拉框就会出现它。"},
        {"title": "2. 变量与参数的区别",
         "body": "系统变量(scenic_id 等)由引擎注入;其余变量(如 start_date)会自动成为模型需要"
                 "填写的参数。**不要把 scenic_id 交给模型填** —— 引擎注入的才是真实租户上下文,"
                 "模型填的可以是任意值,那就越权了。"},
        {"title": "3. 为什么只允许 SELECT",
         "body": "工具由大模型自主调用,写操作一旦被误调后果不可逆。需要写库请做成 HTTP 工具,"
                 "由你自己的服务端接口把关,并把风险等级设为 high 走人工审批。"},
    ],
}

# ── Python 插件骨架 ──────────────────────────────────────────────────────
#
# 之前只给了一个 echo 样例,等于让人从零开始猜怎么写。实际写出来的插件五花八门:
# 有的把结果 print 成裸文本、有的把调试信息也打到 stdout 污染返回值、
# 有的忘了处理参数缺失直接 KeyError。
#
# 现在按用途给几套骨架,并把**输入输出约定固化成一个 main(params) 函数** ——
# 使用者只需要填 main 的函数体,读 stdin / 写 stdout / 异常处理这些样板由骨架负责。
# 这样既降低了写错的概率,产出的插件形态也统一,便于后续维护和排查。

_PLUGIN_PREAMBLE = '''"""
插件约定:
  - 入参:由大模型按下面「入参定义」填写,通过 stdin 以 JSON 传入
  - 出参:main() 的返回值会被序列化成 JSON 打到 stdout
  - 调试:print 到 stderr(sys.stderr),**不要打到 stdout** —— 那会污染返回值
  - 异常:直接抛出即可,框架会捕获并把错误信息回传给模型,由它决定重试还是换方案
"""
import json
import sys
'''

_PLUGIN_ENTRY = '''

# ────────────── 以下为框架样板,一般不需要改动 ──────────────
if __name__ == "__main__":
    _params = json.loads(sys.stdin.read() or "{}")
    _result = main(_params)
    print(json.dumps(_result, ensure_ascii=False, default=str))
'''

PYTHON_SKELETONS = [
    {
        "id": "transform",
        "label": "数据加工",
        "desc": "把上游拿到的数据清洗、换算、重组成想要的结构。最常用的一类。",
        "code": _PLUGIN_PREAMBLE + '''

def main(params: dict) -> dict:
    """把原始记录整理成便于阅读的结构。

    params 里应包含 records(上游工具返回的原始数据)。
    """
    records = params.get("records") or []

    cleaned = []
    for r in records:
        # 逐条清洗:补默认值、统一单位、丢掉不需要的字段
        cleaned.append({
            "name": (r.get("name") or "").strip(),
            "amount_yuan": round(float(r.get("amount_fen", 0)) / 100, 2),  # 分 -> 元
            "date": (r.get("date") or "")[:10],
        })

    # 只返回模型需要的信息。把几百条原始记录整个塞回去会撑爆上下文,
    # 而且模型也用不上——它要的是结论,不是原始数据。
    return {
        "total_count": len(cleaned),
        "total_amount_yuan": round(sum(x["amount_yuan"] for x in cleaned), 2),
        "items": cleaned[:20],
        "truncated": len(cleaned) > 20,
    }
''' + _PLUGIN_ENTRY,
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {"type": "array", "description": "待加工的原始记录数组",
                            "items": {"type": "object"}},
            },
            "required": ["records"],
        },
    },
    {
        "id": "aggregate",
        "label": "统计聚合",
        "desc": "按维度分组、求和/计数/排名。适合「按天统计」「TOP N」这类问题。",
        "code": _PLUGIN_PREAMBLE + '''from collections import defaultdict


def main(params: dict) -> dict:
    """按指定维度聚合并排序。"""
    records = params.get("records") or []
    group_by = params.get("group_by") or "date"
    metric = params.get("metric") or "amount"
    top_n = int(params.get("top_n") or 10)

    buckets = defaultdict(lambda: {"count": 0, "sum": 0.0})
    for r in records:
        key = str(r.get(group_by, "未知"))
        buckets[key]["count"] += 1
        try:
            buckets[key]["sum"] += float(r.get(metric) or 0)
        except (TypeError, ValueError):
            # 单条脏数据不该让整个统计失败,跳过并在结果里说明
            buckets[key].setdefault("invalid", 0)
            buckets[key]["invalid"] = buckets[key].get("invalid", 0) + 1

    rows = [{"key": k, **v} for k, v in buckets.items()]
    rows.sort(key=lambda x: x["sum"], reverse=True)

    return {
        "group_by": group_by,
        "metric": metric,
        "total_groups": len(rows),
        "rows": rows[:top_n],
        # 明确告知被截断,否则模型会以为这就是全部,基于不完整数据下结论
        "truncated": len(rows) > top_n,
    }
''' + _PLUGIN_ENTRY,
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {"type": "array", "description": "待聚合的记录数组", "items": {"type": "object"}},
                "group_by": {"type": "string", "description": "分组维度字段名,如 date / city"},
                "metric": {"type": "string", "description": "求和字段名,如 amount"},
                "top_n": {"type": "integer", "description": "返回前 N 组,默认 10"},
            },
            "required": ["records"],
        },
    },
    {
        "id": "format",
        "label": "格式转换",
        "desc": "在 JSON / CSV / Markdown 表格之间转换。适合把数据整理成便于展示的形态。",
        "code": _PLUGIN_PREAMBLE + '''

def main(params: dict) -> dict:
    """把记录数组转成 Markdown 表格,便于直接展示给用户。"""
    records = params.get("records") or []
    columns = params.get("columns") or (list(records[0].keys()) if records else [])
    max_rows = int(params.get("max_rows") or 50)

    if not records:
        return {"markdown": "(没有数据)", "row_count": 0}

    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(str(r.get(c, "")).replace("|", "\\\\|") for c in columns) + " |"
        for r in records[:max_rows]
    ]
    md = "\\n".join([head, sep, *body])
    if len(records) > max_rows:
        md += f"\\n\\n(共 {len(records)} 行,以上为前 {max_rows} 行)"

    return {"markdown": md, "row_count": len(records)}
''' + _PLUGIN_ENTRY,
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {"type": "array", "description": "记录数组", "items": {"type": "object"}},
                "columns": {"type": "array", "description": "要展示的列,留空则取第一条记录的全部字段",
                            "items": {"type": "string"}},
                "max_rows": {"type": "integer", "description": "最多展示多少行,默认 50"},
            },
            "required": ["records"],
        },
    },
    {
        "id": "compute",
        "label": "业务计算",
        "desc": "按规则做判断或计算,如价格测算、承载量预警、日期推算。",
        "code": _PLUGIN_PREAMBLE + '''

def main(params: dict) -> dict:
    """示例:按当前在园人数与最大承载量判断预警等级。"""
    current = int(params.get("current_visitors") or 0)
    capacity = int(params.get("max_capacity") or 0)

    if capacity <= 0:
        # 参数不合理时**明确报错**,而不是返回一个看起来正常的结果。
        # 静默返回 0% 会让模型据此告诉用户"一切正常",那是更糟的结果。
        raise ValueError("max_capacity 必须大于 0,请确认景区承载量已正确配置")

    ratio = current / capacity
    if ratio >= 0.9:
        level, advice = "红色预警", "已接近满载,建议立即限流并启动分流预案"
    elif ratio >= 0.7:
        level, advice = "黄色预警", "客流偏高,建议加强引导、准备限流"
    else:
        level, advice = "正常", "客流平稳"

    return {
        "current_visitors": current,
        "max_capacity": capacity,
        "load_ratio": round(ratio, 4),
        "level": level,
        "advice": advice,
    }
''' + _PLUGIN_ENTRY,
        "input_schema": {
            "type": "object",
            "properties": {
                "current_visitors": {"type": "integer", "description": "当前在园人数"},
                "max_capacity": {"type": "integer", "description": "最大承载量"},
            },
            "required": ["current_visitors", "max_capacity"],
        },
    },
]


PYTHON_TEMPLATE = {
    "type": "python",
    "label": "Python 插件",
    "summary": "在沙箱里执行一段 Python。适合做格式转换、复杂计算这类没有现成接口的逻辑。",
    "fields": [
        {
            "key": "script", "label": "Python 代码", "widget": "code", "language": "python", "required": True, "section": "basic",
            "skeletons": PYTHON_SKELETONS,   # 前端据此提供「按用途插入骨架」
            "default": PYTHON_SKELETONS[0]["code"],
            "description": "约定:入参从 stdin 读(JSON),结果打印到 stdout(JSON)。"
                           "沙箱只给最小环境变量,读不到数据库口令等敏感信息;"
                           "工作目录是一次性临时目录;有 CPU/内存/输出大小/超时限制。",
        },
        {
            "key": "input_schema", "label": "入参定义", "widget": "schema", "required": False, "section": "params",
            "default": {"type": "object", "properties": {}, "required": []},
            "description": "标准 JSON Schema,声明由大模型填写的入参。同样要写清每个字段的 description。",
            "example": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "待处理的原始文本"}},
                "required": ["text"],
            },
        },
        {
            "key": "response_schema", "label": "响应字段说明", "widget": "response", "required": False, "section": "response",
            "default": {"fields": [], "sample": None},
            "description": "响应字段表(path/类型/单位/说明)——执行链在该工具**首次成功返回**后把说明"
                           "附给大模型,让它正确解读响应语义。重点写清**空值/零值含义**(如「null=无销售,"
                           "≠错误」「0=当日无销售(有效值)」)与单位,模型就不会把零值误读成「数据不可用」。",
            "example": {"fields": [
                {"path": "code", "type": "int", "desc": "0=成功,非0=失败"},
                {"path": "data.orderCount", "type": "int", "desc": "订单数;0=当日无销售(有效值)"},
                {"path": "data.totalAmount", "type": "number", "unit": "元", "desc": "售票总收入;无销售时为 null(≠错误,≠0)"},
            ], "sample": {"code": 0, "data": {"orderCount": 0, "totalAmount": None}}},
        },
    ],
    "sections": [
        {"id": "basic", "label": "代码", "desc": "stdin 读 JSON,stdout 打印 JSON"},
        {"id": "params", "label": "入参定义", "desc": "由大模型填写的入参(JSON Schema)"},
        {"id": "response", "label": "响应字段说明", "desc": "返回值字段语义表——首调后附给大模型"},
    ],
    "example": {"script": "(见代码编辑器默认内容)", "input_schema": {"type": "object", "properties": {}}},
    "tutorial": [
        {"title": "1. 输入输出约定",
         "body": "stdin 读 JSON、stdout 打印 JSON。stdout 里不要打印调试信息,否则会污染返回值 —— "
                 "需要调试请打到 stderr。"},
        {"title": "2. 沙箱能做什么、不能做什么",
         "body": "能:标准库、CPU 计算、临时目录读写。不能:读取服务的环境变量(密钥已被清洗掉)、"
                 "访问项目源码目录。**注意**:本机沙箱模式下无法阻止网络访问,也防不住 fork 炸弹 —— "
                 "只适合运行你自己写的、受信任的代码。需要强隔离请把 tool_center.sandbox.mode 切到 docker。"},
        {"title": "3. 资源限制",
         "body": "默认 60 秒超时、512MB 内存、输出上限 1MB。超时会终止整个进程组;"
                 "输出超限会被截断并判为失败,所以不要把大批原始数据整个打印出来。"},
    ],
}

MCP_TEMPLATE = {
    "type": "mcp",
    "label": "MCP 工具",
    "summary": "来自外部 MCP Server 的工具,由「工具中心 · MCP」发现导入,一般不需要手工创建。",
    "fields": [
        {"key": "server_id", "label": "MCP Server", "widget": "text", "required": True,
         "description": "所属 MCP Server 的 ID。由发现流程自动写入。"},
        {"key": "remote_tool_name", "label": "远端工具名", "widget": "text", "required": True,
         "description": "远端 MCP Server 上的原始工具名。调用时按 JSON-RPC tools/call 传这个名字 —— "
                        "本地工具名可能带 Server 前缀(避免重名),两者不一定相同。"},
        {"key": "input_schema", "label": "入参定义", "widget": "schema", "required": False,
         "description": "远端 MCP Server 通过 tools/list 返回的 inputSchema,发现时自动同步,通常无需手改。"},
    ],
    "sections": [{"id": "basic", "label": "基础信息", "desc": "由发现流程自动写入"}],
    "example": {"server_id": "srv_xxx", "remote_tool_name": "get_weather",
                "input_schema": {"type": "object", "properties": {}}},
    "tutorial": [
        {"title": "正确的接入方式",
         "body": "到「工具中心 · MCP」新增 Server 并点「发现工具」,系统会按 MCP 协议自动导入并保持同步。"
                 "手工创建 mcp 类型工具容易和远端定义脱节,不推荐。"},
    ],
}

from cogni_agent.tool_center_service.quickbi.tool_type import QUICKBI_TEMPLATE  # noqa: E402  Quick BI 智能小Q

TOOL_TEMPLATES = {t["type"]: t for t in (HTTP_TEMPLATE, DB_TEMPLATE, PYTHON_TEMPLATE, MCP_TEMPLATE, QUICKBI_TEMPLATE)}


def all_templates() -> dict:
    """下发给前端的模板。

    **不再下发 TEMPLATE_VARIABLES 供表单展示**:系统变量(scenic_id/user_id/now)是
    执行引擎自动注入的,配置者不需要、也不应该关心它们的存在——把一列"可用变量"摆在
    表单里,反而会诱导人把 scenic_id 写进参数定义交给模型填,那是越权口子。
    需要了解变量语义的人看字段说明和使用教程即可,那里写得更清楚。
    变量本身仍然在请求预览里生效(能直观看到替换结果)。
    """
    return {"templates": list(TOOL_TEMPLATES.values())}
