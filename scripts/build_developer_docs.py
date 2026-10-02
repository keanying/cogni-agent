# -*- coding: utf-8 -*-
"""生成开发者 API 文档(cogni_agent/static/developer/index.html)。

**为什么用生成而不是手写 HTML**:对外文档最怕的是「每个接口写法不一样」——
有的给了 curl 没给返回、有的漏了必传标记,集成方每看一个接口都要重新适应一遍。
把接口描述写成数据(Api 对象),渲染成统一的四段式:

    N.M   接口标题
    N.M.1 API 概览(URI / 请求方式 / 认证方式 / 参数类型)
    N.M.2 请求参数(序号 / 字段 / 类型 / 必传 / 示例 / 注释)
    N.M.3 返回参数(序号 / 字段 / 类型 / 示例 / 注释)
    N.M.4 示例(可直接粘进终端的 curl + 真实形状的返回 JSON)

章节号由列表顺序自动算,插一个接口不用手工重排编号 —— 手工编号迟早会重号,
而重号的文档没人敢照着写代码。

用法:  uv run python scripts/build_developer_docs.py
       生成物随包发布,浏览器打开 /developer 即可(免登录)。

**安全**:这里只能出现占位密钥(sk-zhilv-xxxxxxxx / your-host)。
真实的 api_key、user_token 一律不进文档 —— 它是公开页面。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cogni_agent" / "static" / "developer" / "index.html"

_APIDOC_CURL = """curl --location --request POST 'https://<host>/api/admin/tools/from-api-doc' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{ "markdown": "# 1.17 BI报表配置信息\\n\\n## API概览\\n- **URI**: /v1/...", "scenic_id": "PFTSCA01002434" }'"""

HOST = "https://your-host"
KEY = "sk-zhilv-xxxxxxxx"


# ══════════════════════════ 数据模型 ══════════════════════════

@dataclass
class P:
    """请求参数的一行。required 用中文「是/否」渲染,与样例保持一致。"""
    name: str
    type: str
    required: bool
    example: str = ""
    note: str = ""


@dataclass
class R:
    """返回参数的一行。name 用 `data[].xxx` 这种点号路径表示层级。"""
    name: str
    type: str
    example: str = ""
    note: str = ""


@dataclass
class Api:
    title: str                      # 「分类-名称」,与左侧导航一致
    uri: str
    method: str                     # GET / POST
    summary: str                    # 一句话说清「什么时候用它」
    req: list[P] = field(default_factory=list)
    resp: list[R] = field(default_factory=list)
    curl: str = ""
    resp_json: str = ""
    auth: str = "API Key【管理后台 → 工具中心 → API Key 创建】"
    content_type: str = "application/json"
    notes: list[str] = field(default_factory=list)   # 额外说明(HTML 片段)
    anchor: str = ""

    def __post_init__(self):
        if not self.anchor:
            self.anchor = re.sub(r"[^a-z0-9]+", "-", self.uri.lower()).strip("-") + "-" + self.method.lower()


@dataclass
class Prose:
    """非接口章节(概览、鉴权、附录),内容是现成的 HTML。"""
    title: str
    body: str
    anchor: str


@dataclass
class Chapter:
    title: str
    items: list                     # Api | Prose
    anchor: str


# ══════════════════════════ 渲染 ══════════════════════════

def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def code_block(text: str, lang: str = "") -> str:
    """带行号与复制按钮的代码块 —— 样例里那种。"""
    lines = text.rstrip("\n").split("\n")
    body = "\n".join(
        f'<tr><td class="ln">{i}</td><td class="lc">{esc(l) or "&nbsp;"}</td></tr>'
        for i, l in enumerate(lines, 1))
    return (f'<div class="codewrap"><div class="codebar"><span>{esc(lang or "代码块")}</span>'
            f'<button class="copy" type="button">复制</button></div>'
            f'<table class="code"><tbody>{body}</tbody></table></div>')


def req_table(rows: list[P]) -> str:
    if not rows:
        return '<p class="none">无需请求参数。</p>'
    trs = "".join(
        f'<tr><td>{i}</td><td class="f">{esc(r.name)}</td><td>{esc(r.type)}</td>'
        f'<td class="{"yes" if r.required else "no"}">{"是" if r.required else "否"}</td>'
        f'<td class="ex">{esc(r.example)}</td><td>{esc(r.note)}</td></tr>'
        for i, r in enumerate(rows, 1))
    return ('<table class="spec"><thead><tr><th style="width:56px">序号</th><th>字段</th><th style="width:86px">类型</th>'
            f'<th style="width:64px">必传</th><th>示例</th><th>注释</th></tr></thead><tbody>{trs}</tbody></table>')


def resp_table(rows: list[R]) -> str:
    """返回参数表。

    每张表都自动带上信封三件套(code / message / trace_id)——接口的字段只描述
    `data` 里面的部分。不带的话,集成方看每个接口都要回头翻一遍「响应信封」那一节,
    第一次接入时最容易漏的就是「为什么我的 data 在外面套了一层」。
    层级用缩进表示:顶层字段给编号,子字段留空(与 data[].xxx 的写法一致)。
    """
    if not rows:
        return ('<p class="none">这个接口返回的是事件流,不是 JSON —— '
                '事件结构见附录「SSE 事件结构」。</p>')

    env_head = [R("code", "int", "0", "状态码,0 = 成功,非 0 见「响应信封与错误码」"),
                R("message", "string", "success", "提示信息;失败时是可直接展示的中文原因"),
                R("data", "object", "{…}", "数据体,下面是它的内部字段")]
    env_tail = [R("trace_id", "string", "b6289ec8…", "追踪 ID,与响应头 X-Trace-Id 一致,排查时给运维")]

    trs, n = [], 0
    for r in env_head + rows + env_tail:
        # 深度 = 点号/数组标记的层数;data.result.charts[].title 比 data.id 再缩进一级
        depth = r.name.count(".")
        nested = depth > 0
        no = "" if nested else str(n := n + 1)
        pad = f' style="padding-left:{12 + depth * 16}px"' if nested else ""
        trs.append(f'<tr><td>{no}</td><td class="f{" sub" if nested else ""}"{pad}>{esc(r.name)}</td>'
                   f'<td>{esc(r.type)}</td><td class="ex">{esc(r.example)}</td><td>{esc(r.note)}</td></tr>')
    return ('<table class="spec"><thead><tr><th style="width:56px">序号</th><th>字段</th><th style="width:86px">类型</th>'
            f'<th>示例</th><th>注释</th></tr></thead><tbody>{"".join(trs)}</tbody></table>')


def render_api(no: str, a: Api) -> str:
    out = [f'<h3 id="{a.anchor}">{no} {esc(a.title)}</h3>',
           f'<p class="lead">{a.summary}</p>']
    out.append(f'<h4>{no}.1 API概览</h4>')
    out.append('<table class="spec kv">'
               f'<tr><th>URI</th><td class="ep"><span class="m {a.method.lower()}">{a.method}</span>{esc(a.uri)}</td></tr>'
               f'<tr><th>请求方式</th><td>{esc(a.method)}</td></tr>'
               f'<tr><th>认证方式</th><td>{esc(a.auth)}</td></tr>'
               f'<tr><th>参数类型</th><td>{esc(a.content_type)}</td></tr></table>')
    for nt in a.notes:
        out.append(f'<div class="note">{nt}</div>')
    out.append(f'<h4>{no}.2 请求参数</h4>{req_table(a.req)}')
    out.append(f'<h4>{no}.3 返回参数</h4>{resp_table(a.resp)}')
    out.append(f'<h4>{no}.4 示例</h4>')
    if a.curl:
        out.append(code_block(a.curl, "请求"))
    if a.resp_json:
        out.append('<p class="sub-cap">返回示例:</p>' + code_block(a.resp_json, "返回"))
    return "\n".join(out)


def render(chapters: list[Chapter]) -> str:
    nav, body, quick = [], [], []
    for ci, ch in enumerate(chapters, 1):
        nav.append(f'<div class="grp">{ci} {esc(ch.title)}</div>')
        body.append(f'<h2 id="{ch.anchor}">{ci} {esc(ch.title)}</h2>')
        for ii, it in enumerate(ch.items, 1):
            no = f"{ci}.{ii}"
            nav.append(f'<a href="#{it.anchor}">{no} {esc(it.title)}</a>')
            if isinstance(it, Api):
                body.append(render_api(no, it))
                quick.append((no, it))
            else:
                body.append(f'<h3 id="{it.anchor}">{no} {esc(it.title)}</h3>{it.body}')

    rows = "".join(
        f'<tr><td>{no}</td><td><a href="#{a.anchor}">{esc(a.title)}</a></td>'
        f'<td><span class="m {a.method.lower()}">{a.method}</span></td>'
        f'<td class="ep">{esc(a.uri)}</td><td>{a.summary}</td></tr>' for no, a in quick)
    quick_html = ('<table class="spec"><thead><tr><th style="width:56px">编号</th><th style="width:190px">接口</th>'
                  '<th style="width:64px">方式</th><th style="width:270px">URI</th><th>什么时候用</th></tr></thead>'
                  f'<tbody>{rows}</tbody></table>')

    return TEMPLATE.replace("{{NAV}}", "\n".join(nav)) \
                   .replace("{{QUICK}}", quick_html) \
                   .replace("{{BODY}}", "\n".join(body))


# ══════════════════════════ 页面骨架 ══════════════════════════

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>数字大脑 · 开发者 API 文档</title>
<style>
  :root{--bg:#f6f8fb;--panel:#fff;--line:#e5e9f0;--text:#1e293b;--muted:#64748b;--brand:#1677ff;
        --code-bg:#f8fafc;--code-fg:#0f172a;--ok:#10b981;--warn:#f59e0b;--err:#ef4444}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
  a{color:var(--brand);text-decoration:none}
  a:hover{text-decoration:underline}
  .wrap{display:flex;min-height:100vh}
  nav{width:280px;flex:0 0 280px;background:#0f1729;color:#cbd5e1;padding:18px 0;position:sticky;top:0;
      height:100vh;overflow:auto}
  nav h1{color:#fff;font-size:15px;margin:0 20px 4px}
  nav .sub{color:#64748b;font-size:11px;margin:0 20px 14px}
  nav a{display:block;color:#94a3b8;padding:5px 20px 5px 28px;font-size:12.5px}
  nav a:hover{background:rgba(255,255,255,.06);color:#fff;text-decoration:none}
  nav .grp{color:#e2e8f0;font-size:12px;font-weight:700;margin:16px 20px 4px}
  nav .find{margin:0 16px 10px;width:calc(100% - 32px);padding:6px 10px;border-radius:8px;border:1px solid #1e293b;
            background:#0b1220;color:#e2e8f0;font-size:12px;outline:none}
  main{flex:1;min-width:0;padding:30px 44px 90px;max-width:1080px}
  h2{font-size:21px;margin:44px 0 10px;padding-top:14px;border-top:2px solid var(--line)}
  h2:first-of-type{border-top:0;margin-top:0;padding-top:0}
  h3{font-size:17px;margin:30px 0 6px;scroll-margin-top:14px}
  h4{font-size:13px;margin:18px 0 6px;color:#0f172a;font-weight:700}
  p,li{color:#334155}
  .lead{color:var(--muted);margin:4px 0 6px}
  .none{color:#94a3b8;font-size:13px;margin:6px 0}
  .sub-cap{margin:12px 0 4px;color:var(--muted);font-size:13px}
  code{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:12.5px;
       font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  table.spec{width:100%;border-collapse:collapse;margin:8px 0 4px;background:var(--panel);
             box-shadow:0 0 0 1px var(--line);border-radius:8px;overflow:hidden}
  table.spec th,table.spec td{padding:7px 11px;text-align:left;font-size:12.5px;
                              border-bottom:1px solid var(--line);vertical-align:top}
  table.spec th{background:#f1f5f9;color:#475569;font-weight:700;font-size:12px;white-space:nowrap}
  table.spec tr:last-child td{border-bottom:0}
  table.spec td.f{font-family:ui-monospace,Menlo,Consolas,monospace;color:#0f172a;font-weight:600}
  table.spec td.f.sub{padding-left:26px;font-weight:400;color:#475569}
  table.spec td.ex{font-family:ui-monospace,Menlo,Consolas,monospace;color:#475569;word-break:break-all}
  table.spec td.yes{color:var(--err);font-weight:700}
  table.spec td.no{color:#94a3b8}
  table.spec.kv th{width:110px}
  .ep{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
  .m{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;font-weight:700;color:#fff;margin-right:6px}
  .get{background:#10b981}.post{background:#1677ff}.put{background:#f59e0b}.del{background:#ef4444}
  .codewrap{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:8px 0 6px;background:var(--code-bg)}
  .codebar{display:flex;justify-content:space-between;align-items:center;padding:5px 10px;background:#eef2f7;
           border-bottom:1px solid var(--line);font-size:11.5px;color:var(--muted)}
  .copy{background:#fff;border:1px solid var(--line);border-radius:6px;padding:2px 9px;font-size:11px;cursor:pointer;color:#475569}
  .copy:hover{background:#f1f5f9}
  table.code{width:100%;border-collapse:collapse;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
             font-size:12.5px;line-height:1.7}
  table.code td.ln{width:38px;text-align:right;padding:0 10px 0 6px;color:#94a3b8;user-select:none;
                   background:#f1f5f9;border-right:1px solid var(--line)}
  table.code td.lc{padding:0 12px;color:var(--code-fg);white-space:pre}
  .note{border-left:3px solid var(--brand);background:#eff6ff;padding:9px 13px;border-radius:0 8px 8px 0;margin:10px 0;font-size:13px}
  .warn{border-left:3px solid var(--warn);background:#fffbeb;padding:9px 13px;border-radius:0 8px 8px 0;margin:10px 0;font-size:13px}
  .steps{counter-reset:s;padding:0;margin:10px 0;list-style:none}
  .steps li{counter-increment:s;position:relative;padding:0 0 10px 30px}
  .steps li::before{content:counter(s);position:absolute;left:0;top:2px;width:20px;height:20px;border-radius:50%;
                    background:var(--brand);color:#fff;font-size:12px;font-weight:700;text-align:center;line-height:20px}
  @media(max-width:900px){nav{display:none}main{padding:18px}}
</style>
</head>
<body>
<div class="wrap">
<nav>
  <h1>数字大脑 开发者文档</h1>
  <p class="sub">对外 API · 无需登录即可查阅</p>
  <input class="find" id="find" placeholder="过滤接口(输入关键字)" />
{{NAV}}
</nav>
<main>
<h2 id="quick">接口速查</h2>
<p class="lead">全部对外接口一览。每个接口下面都是同一套结构:API 概览 → 请求参数 → 返回参数 → 可直接粘进终端的示例。</p>
{{QUICK}}
{{BODY}}
</main>
</div>
<script>
// 复制:取同一个代码块里所有行的文本(行号在另一列,不会被带上)
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copy');
  if (!btn) return;
  var wrap = btn.closest('.codewrap');
  var text = Array.prototype.map.call(wrap.querySelectorAll('td.lc'), function (td) { return td.textContent; }).join('\\n');
  navigator.clipboard.writeText(text).then(function () {
    btn.textContent = '已复制';
    setTimeout(function () { btn.textContent = '复制'; }, 1200);
  });
});
// 左侧过滤:接口多了之后,肉眼从几十条里找一条比搜索慢得多
document.getElementById('find').addEventListener('input', function (e) {
  var kw = e.target.value.trim().toLowerCase();
  document.querySelectorAll('nav a').forEach(function (a) {
    a.style.display = !kw || a.textContent.toLowerCase().indexOf(kw) >= 0 ? '' : 'none';
  });
});
</script>
</body>
</html>
"""


# ══════════════════════════ 内容:1 开始 ══════════════════════════
# 文档按**系统的四块**分章,和后台菜单、代码目录是同一套说法:
#   1 开始          —— 三种鉴权、信封、事件结构,所有章节都要先看这一章
#   2 前端 APP      —— /api/app/*,移动端/小程序用,账号登录
#   3 管理后端      —— /api/admin/*,管理后台页面用,账号登录
#   4 Quick BI 与开放平台 —— /v1/*,对外集成用 API Key(Quick BI 是主体)
# 分章按「谁来调」而不是按「什么功能」:同一个人接同一块,不必在章节间跳。

_FIRST_CURL = (
    f"curl --location --request POST '{HOST}/v1/quickbi/query' \\\n"
    f"--header 'Authorization: Bearer {KEY}' \\\n"
    "--header 'Content-Type: application/json' \\\n"
    "--data-raw '{\n"
    '  "question": "本月各平台销售额",\n'
    '  "target": { "cube_id": "abc123" }\n'
    "}'"
)

_SSE_SAMPLE = """event: run
data: {"type":"run","run_id":"run_7c1f2b9d","capability":"dataset_qa"}

event: stage
data: {"type":"status","message":"正在选择数据集…","channel":"thinking"}

event: text
data: {"type":"text","delta":"本月各平台销售额合计 386.2 万元","channel":"data"}

event: chart
data: {"type":"chart","channel":"data","chart":{"title":"各平台销售额","chart_type":"bar",
       "columns":["平台","销售额"],"rows":[["抖音",1203560]]}}

event: done
data: {"type":"done","run_id":"run_7c1f2b9d","status":"success","answer":"…","duration_ms":4210}"""

_E2E = (
    "# 1. 看这个景区能问什么(拿 fixed_question_id 或 cube_id)\n"
    f"curl -s -H 'Authorization: Bearer {KEY}' '{HOST}/v1/quickbi/catalog?ask_type=qa'\n"
    "\n"
    "# 2. 原样问其中一条固定问题\n"
    f"curl -s -X POST '{HOST}/v1/quickbi/query' \\\n"
    f"  -H 'Authorization: Bearer {KEY}' -H 'Content-Type: application/json' \\\n"
    '  -d \'{"fixed_question_id":"issue_3f2a9c11b2d34e56"}\'\n'
    "\n"
    "# 3. 或者自己组问题,打在同一份数据上(流式)\n"
    f"curl -N -X POST '{HOST}/v1/quickbi/query' \\\n"
    f"  -H 'Authorization: Bearer {KEY}' -H 'Content-Type: application/json' \\\n"
    '  -d \'{"question":"上月各渠道退票率","target":{"cube_id":"abc123"},"stream":true}\''
)

START = Chapter("开始", anchor="start", items=[
    Prose("三步跑通第一个请求", anchor="start-3", body=f"""
<ol class="steps">
  <li><b>先看清自己要接哪一块</b>:移动端/小程序 → 第 2 章(账号登录);管理后台页面 → 第 3 章(账号登录);
      外部系统集成 → 第 4 章(API Key)。三块的鉴权方式不同,别混用。</li>
  <li><b>拿凭证</b>:外部集成在管理后台「工具中心 · API Key」创建,形如 <code>{KEY}</code>(只显示一次);
      前端/管理端用账号登录换 <code>access_token</code>。</li>
  <li><b>发请求</b>:把下面这段里的 <code>{HOST}</code> 和 Key 换成你的,直接粘进终端。</li>
</ol>
{code_block(_FIRST_CURL, "最小可用请求")}
<div class="note">没有 <code>cube_id</code> 也能问:不传 <code>target</code> 时平台按该 Key 所属景区自动挑已登记的数据。
先调一次「目录」接口看看这个景区有什么,是最省事的起手式。</div>
"""),

    Prose("服务地址与通用约定", anchor="start-base", body="""
<table class="spec">
  <thead><tr><th style="width:150px">面向谁</th><th style="width:170px">前缀</th><th style="width:150px">鉴权</th><th>本文档章节</th></tr></thead>
  <tbody>
    <tr><td>移动端 / 小程序</td><td class="ep">/api/app/*</td><td>账号登录(JWT)</td><td>第 2 章 前端 APP</td></tr>
    <tr><td>管理后台页面</td><td class="ep">/api/admin/*</td><td>账号登录(JWT)</td><td>第 3 章 管理后端</td></tr>
    <tr><td>外部系统集成</td><td class="ep">/v1/*</td><td>API Key</td><td>第 4 章 Quick BI 与开放平台</td></tr>
    <tr><td>健康检查</td><td class="ep">/health</td><td>无</td><td>—</td></tr>
    <tr><td>交互式接口清单</td><td class="ep">/docs</td><td>无</td><td>—</td></tr>
  </tbody>
</table>
<ul>
  <li><b>请求体一律 JSON</b>(<code>Content-Type: application/json</code>);只有文件上传用
      <code>multipart/form-data</code>。</li>
  <li><b>每个响应都带 <code>X-Trace-Id</code> 响应头</b>。出问题时把这个值发给平台运维,
      能直接定位到那一次调用的完整链路;也可以自己在请求头传 <code>X-Trace-Id</code>,平台会沿用。</li>
  <li><b>流式用 SSE</b>(<code>text/event-stream</code>),不是 WebSocket;请求体里加
      <code>"stream": true</code> 开启(APP 端是单独的订阅端点)。</li>
</ul>
"""),

    Prose("鉴权一:API Key(外部集成)", anchor="start-auth-key", body=f"""
<p>所有 <code>/v1/*</code> 接口统一用这一个请求头:</p>
{code_block(f"Authorization: Bearer {KEY}", "请求头")}
<h4>Key 的两个属性,直接决定你能看到什么</h4>
<table class="spec">
  <thead><tr><th style="width:130px">属性</th><th>含义</th></tr></thead>
  <tbody>
    <tr><td>所属景区</td><td>景区级 Key 只能看到本景区 + 集团级的连接、资产与固定问题;集团级 Key(未绑景区)能看到全部。</td></tr>
    <tr><td>限定 Agent</td><td>限定了可调用 Agent 的 Key <b>不能</b>调用 Quick BI 开放接口(返回 403)。
        要用 Quick BI,请用未限定 Agent 的 Key。</td></tr>
  </tbody>
</table>
<div class="note">多租户场景可以额外传 <code>X-End-User: &lt;你的终端用户标识&gt;</code>,
平台会把这次调用归到该终端用户名下,便于分摊成本与排查。</div>
"""),

    Prose("鉴权二:账号登录(前端 / 管理后台)", anchor="start-auth-jwt", body=f"""
<p><code>/api/app/*</code> 与 <code>/api/admin/*</code> 用账号登录换来的令牌:</p>
{code_block("Authorization: Bearer <access_token>", "请求头")}
<ol class="steps">
  <li>取 RSA 公钥(免登录),用它加密密码 —— 明文密码不要过网。</li>
  <li>调登录接口拿 <code>access_token</code> + <code>refresh_token</code>。</li>
  <li><code>access_token</code> 过期前用 <code>refresh_token</code> 换新的;两个令牌都会滚动更新。</li>
</ol>
<div class="warn"><code>must_change_password=true</code> 时必须先改密码,否则后续接口会一直被拦。
这是首次登录与管理员重置密码后的状态。</div>
"""),

    Prose("响应信封与错误码", anchor="start-envelope", body=f"""
<p>所有非流式 JSON 响应都包一层信封,业务数据在 <code>data</code> 里:</p>
{code_block('''{
  "code": 0,
  "message": "success",
  "data": { ... },
  "trace_id": "b6289ec8..."
}''', "响应信封")}
<table class="spec">
  <thead><tr><th style="width:56px">序号</th><th>字段</th><th style="width:86px">类型</th><th>示例</th><th>注释</th></tr></thead>
  <tbody>
    <tr><td>1</td><td class="f">code</td><td>int</td><td class="ex">0</td><td>0 = 成功,非 0 = 失败</td></tr>
    <tr><td>2</td><td class="f">message</td><td>string</td><td class="ex">success</td><td>失败时是可直接展示给人看的中文原因</td></tr>
    <tr><td>3</td><td class="f">data</td><td>object / array</td><td class="ex">{{…}}</td><td>业务数据</td></tr>
    <tr><td>4</td><td class="f">trace_id</td><td>string</td><td class="ex">b6289ec8…</td><td>追踪 ID,与响应头 X-Trace-Id 一致</td></tr>
  </tbody>
</table>
<div class="warn">写客户端时请<b>只判断 <code>code</code>,不要只看 HTTP 状态码</b>;并且永远记录
<code>trace_id</code> —— 排查时它比任何日志都管用。</div>
<h4>错误码</h4>
<table class="spec">
  <thead><tr><th style="width:80px">HTTP</th><th style="width:230px">典型 message</th><th>怎么处理</th></tr></thead>
  <tbody>
    <tr><td>401</td><td>API Key 无效 / 已吊销 / 已过期;或令牌过期</td><td>换有效 Key;账号登录场景用 refresh_token 换新令牌</td></tr>
    <tr><td>403</td><td>该 API Key 限定了可调用的 Agent</td><td>Quick BI 请用未限定 Agent 的 Key</td></tr>
    <tr><td>403</td><td>agent_not_distributed / skill_not_distributed</td><td>该资源还没分发给这个调用方,联系管理员在「分发中心」分配</td></tr>
    <tr><td>403</td><td>需要 group_admin 或 super_admin 权限</td><td>管理端接口按角色收口,换有权限的账号</td></tr>
    <tr><td>404</td><td>连接 / 资产 / 问题不存在或无权访问</td><td>先用目录类接口确认 id;景区级身份只看得到本景区</td></tr>
    <tr><td>400</td><td>问题不能为空 / 缺少参数 xxx</td><td>按本页的请求参数表补齐</td></tr>
    <tr><td>500</td><td>上游 Quick BI 报错</td><td>带上 trace_id 找平台运维</td></tr>
  </tbody>
</table>
"""),

    Prose("SSE 事件结构", anchor="start-sse", body=f"""
<p>所有流式接口都推这套事件。<b>事件名就是 JSON 里的 <code>type</code></b>,一行 <code>data:</code> 一个 JSON 对象:</p>
{code_block(_SSE_SAMPLE, "事件流")}
<table class="spec">
  <thead><tr><th style="width:110px">事件</th><th style="width:90px">channel</th><th>含义与怎么渲染</th></tr></thead>
  <tbody>
    <tr><td class="f">run</td><td>—</td><td>本次运行开始,带 run_id。<b>先存下来</b>:后面查结果、对账都靠它</td></tr>
    <tr><td class="f">stage</td><td>thinking</td><td>过程信息(选表、生成 SQL、执行中)。渲染成「正在思考」那类提示,不要当回答</td></tr>
    <tr><td class="f">token / text</td><td>data</td><td>回答正文,按 <code>delta</code> 追加拼接(APP 端叫 token,开放平台叫 text)</td></tr>
    <tr><td class="f">chart / structured</td><td>data</td><td>图表:开放平台给原始 chart,APP 端给渲染好的 ECharts option</td></tr>
    <tr><td class="f">report</td><td>data</td><td>报告链接(报告类才有)</td></tr>
    <tr><td class="f">usage</td><td>—</td><td>本次 token 用量</td></tr>
    <tr><td class="f">done</td><td>—</td><td>结束。<code>status</code> = success / failed,失败时 <code>error</code> 是原因</td></tr>
    <tr><td class="f">error</td><td>—</td><td>致命错误,流随即结束</td></tr>
  </tbody>
</table>
<div class="warn"><b>thinking 与 data 必须分开渲染。</b>把 thinking 当回答直接显示,用户会看到一堆
「正在选择数据集…」混在结论里 —— 这是接入时最常见的一个坑。</div>
"""),

    Prose("图表结果结构", anchor="start-charts", body=f"""
<p><code>result.charts[]</code> 里每一张图都是这个形状,直接喂给前端图表库即可:</p>
{code_block('''{
  "title": "各平台销售额",
  "chart_type": "bar",
  "columns": ["平台", "销售额"],
  "rows": [["抖音", 1203560], ["美团", 986400]],
  "truncated": false
}''', "chart")}
<table class="spec">
  <thead><tr><th style="width:56px">序号</th><th>字段</th><th style="width:86px">类型</th><th>示例</th><th>注释</th></tr></thead>
  <tbody>
    <tr><td>1</td><td class="f">title</td><td>string</td><td class="ex">各平台销售额</td><td>图表标题</td></tr>
    <tr><td>2</td><td class="f">chart_type</td><td>string</td><td class="ex">bar</td><td>bar / line / pie / table / scatter…</td></tr>
    <tr><td>3</td><td class="f">columns</td><td>array</td><td class="ex">["平台","销售额"]</td><td>列名,与 rows 的每一项一一对应</td></tr>
    <tr><td>4</td><td class="f">rows</td><td>array</td><td class="ex">[["抖音",1203560]]</td><td>数据行</td></tr>
    <tr><td>5</td><td class="f">truncated</td><td>bool</td><td class="ex">false</td><td>true = 行数超过 1000 已截断,完整数据请缩小问题范围再问</td></tr>
  </tbody>
</table>
"""),

    Prose("端到端示例:从目录到答案", anchor="start-e2e", body=f"""
<p>外部集成最常见的流程,三步:</p>
{code_block(_E2E, "bash")}
<div class="note">接入顺序建议:先用第 1 步的目录把「这个景区有什么」摸清楚并缓存下来
(它变化很慢),再按需发起提问 —— 比每次都让模型从零选数据既快又稳。</div>
"""),
])


# ══════════════════════════ 内容:2 前端 APP ══════════════════════════

JWT = "账号登录(JWT):Authorization: Bearer &lt;access_token&gt;"
JWT_FREE = "无需鉴权(登录前调用)"

APP = Chapter("前端 APP 接口(/api/app)", anchor="app", items=[
    Api(
        title="登录-获取 RSA 公钥", uri="/api/app/auth/public-key", method="GET",
        auth=JWT_FREE, content_type="—(GET 无请求体)",
        summary="登录前先拿公钥,用它加密密码 —— 明文密码不要过网。公钥可以缓存,重启服务才会变。",
        req=[],
        resp=[
            R("data.public_key", "string", "-----BEGIN PUBLIC KEY-----…", "RSA 公钥(PEM)"),
            R("data.algorithm", "string", "RSA-OAEP", "加密算法"),
            R("data.key_size", "int", "2048", "密钥长度"),
        ],
        curl=f"curl --location --request GET '{HOST}/api/app/auth/public-key'",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": { "public_key": "-----BEGIN PUBLIC KEY-----\\nMIIBIjANBgkq…", "algorithm": "RSA-OAEP", "key_size": 2048 },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="登录-账号登录", uri="/api/app/auth/login", method="POST",
        auth=JWT_FREE,
        summary="账号或手机号 + 密码登录,拿令牌与用户信息。新设备可能要求短信验证码。",
        req=[
            P("account", "string", True, "13800000001", "账号或手机号"),
            P("password", "string", True, "(RSA 密文)", "rsa_encrypt=true 时为公钥加密后的 Base64 密文"),
            P("rsa_encrypt", "bool", False, "true", "密码是否已用 RSA 加密,推荐 true"),
            P("sms_code", "string", False, "123456", "新设备登录时需要"),
            P("device_id", "string", False, "8F2C-…", "设备 UUID,用于识别新设备"),
            P("device_name", "string", False, "iPhone 15 / iOS 17.4", "设备名,登录记录里显示"),
            P("device_os", "string", False, "iOS", "iOS / Android"),
            P("push_token", "string", False, "fcm:xxx", "APNs / FCM 推送 Token"),
            P("app_version", "string", False, "1.1.1", "APP 版本号"),
        ],
        resp=[
            R("data.access_token", "string", "eyJhbGciOi…", "业务接口用它;有效期较短"),
            R("data.refresh_token", "string", "eyJhbGciOi…", "换新 access_token 用"),
            R("data.must_change_password", "bool", "false", "true = 必须先改密码,否则后续接口会被拦"),
            R("data.user.id", "string", "u_10086", "用户 ID"),
            R("data.user.name", "string", "张三", "姓名"),
            R("data.user.role", "string", "scenic_admin", "角色"),
            R("data.user.scenic_id", "string", "PFTSCA01002434", "绑定景区;空 = 集团级账号"),
            R("data.user.scenic_name", "string", "泰宁九龙潭生态旅游区", "景区名(取自景区主数据)"),
            R("data.user.phone", "string", "138****0001", "手机号已打码"),
        ],
        curl=f"""curl --location --request POST '{HOST}/api/app/auth/login' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "account": "13800000001",
  "password": "<RSA 加密后的 Base64>",
  "rsa_encrypt": true,
  "device_id": "8F2C-4A11-…",
  "device_os": "iOS"
}}'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs…",
    "refresh_token": "eyJhbGciOiJIUzI1NiIs…",
    "must_change_password": false,
    "user": {
      "id": "u_10086", "name": "张三", "account": "zhangsan", "phone": "138****0001",
      "role": "scenic_admin", "scenic_id": "PFTSCA01002434",
      "scenic_name": "泰宁九龙潭生态旅游区", "scenic_grade_level": "4A"
    }
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="登录-刷新令牌", uri="/api/app/auth/refresh", method="POST",
        auth=JWT_FREE,
        summary="access_token 快过期时换一对新的。两个令牌都会滚动更新,旧的立即失效。",
        req=[P("refresh_token", "string", True, "eyJhbGciOi…", "登录时拿到的 refresh_token")],
        resp=[
            R("data.access_token", "string", "eyJhbGciOi…", "新的访问令牌"),
            R("data.refresh_token", "string", "eyJhbGciOi…", "新的刷新令牌,替换本地存的那个"),
        ],
        notes=['建议在<b>请求发出前</b>判断剩余有效期(不足 2 分钟就先换),不要只靠 401 兜底 —— '
               'SSE 连上之后中途过期撞不到 401,流会静静地断掉。'],
        curl=f"""curl --location --request POST '{HOST}/api/app/auth/refresh' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "refresh_token": "eyJhbGciOiJIUzI1NiIs…" }}'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": { "access_token": "eyJhbGciOiJIUzI1NiIs…", "refresh_token": "eyJhbGciOiJIUzI1NiIs…" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="我的-当前用户信息", uri="/api/app/me", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="当前登录用户是谁、绑在哪个景区。APP 启动后第一件事。",
        req=[],
        resp=[
            R("data.id", "string", "u_10086", "用户 ID"),
            R("data.name", "string", "张三", "姓名"),
            R("data.role", "string", "scenic_admin", "角色"),
            R("data.scenic_id", "string", "PFTSCA01002434", "绑定景区;空 = 集团级账号"),
            R("data.scenic_name", "string", "泰宁九龙潭生态旅游区", "景区名(取自景区主数据)"),
            R("data.position", "string", "运营专员", "职位"),
            R("data.must_change_password", "bool", "false", "true = 需要先改密码"),
        ],
        notes=['<b>集团级账号(scenic_id 为空)要特殊处理</b>:很多数据接口按景区取数,'
               '集团账号不选景区就会拿到空列表 —— 前端应引导他先选一个景区,而不是显示一片空白。'],
        curl=f"""curl --location --request GET '{HOST}/api/app/me' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "id": "u_10086", "name": "张三", "account": "zhangsan", "phone": "138****0001",
    "role": "scenic_admin", "scenic_id": "PFTSCA01002434",
    "scenic_name": "泰宁九龙潭生态旅游区", "position": "运营专员",
    "must_change_password": false
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-问题列表(三级树)", uri="/api/app/businessIssueList", method="POST",
        auth=JWT,
        summary="APP 首页那棵问题树:主题 → 维度 → 指标 → 问题。选完指标就拿到该指标下的问题,点一条直接发起。",
        req=[P("scenicId", "string", False, "PFTSCA01002434", "看哪个景区的问题;不传按当前账号绑定的景区")],
        resp=[
            R("data.scenicId", "string", "PFTSCA01002434", "本次树归属的景区"),
            R("data.topics", "array", "[…]", "主题列表"),
            R("data.topics[].topic", "string", "票务", "主题中文名"),
            R("data.topics[].topicId", "string", "ticketing", "主题编码;<b>同名键</b>下是该主题的维度数组"),
            R("…[topicId][].dimension", "string", "售票概况", "维度中文名"),
            R("…[topicId][].dimensionId", "string", "ticketingOverview", "维度编码;同名键下是指标数组"),
            R("…[dimensionId][].metric", "string", "总量", "指标中文名"),
            R("…[dimensionId][].metricId", "string", "totalVolume", "指标编码;同名键下是问题数组"),
            R("…[metricId][].issues", "array", "[…]", "该指标下的问题"),
            R("…issues[].issueId", "string", "issue_3f2a…", "问题 ID:发起提问时原样回传"),
            R("…issues[].businessClass", "string", "quick_bi", "quick_bi 直连 Quick BI / local_system 走对话引擎"),
            R("…issues[].quickbiLinkId", "string", "c1f2e8a0", "Quick BI 连接 ID(= connection_id,同一个值两个名字)"),
            R("…issues[].title", "string", "本月售票总量", "按钮上显示的短标题"),
            R("…issues[].issue", "string", "本月各平台销售额", "问题原文"),
            R("…issues[].capabilityLabel", "string", "问数 · 数据集", "能力中文名,可作角标"),
        ],
        notes=['<b>前端不用判断该调哪个接口</b>:统一发「提问-发起提问」,后端按 '
               '<code>businessClass</code> 自己分流(Quick BI 的事件会翻译成 APP 已有的事件推到同一个通道)。',
               '问题挂在三级标签上,<b>后台没挂标签的问题不会出现在这棵树里</b> —— '
               '运营反馈「配了看不到」时,先去后台看那条问题的主题/维度/指标是不是空的。'],
        curl=f"""curl --location --request POST '{HOST}/api/app/businessIssueList' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{}}'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "scenicId": "PFTSCA01002434",
    "topics": [
      {
        "topic": "票务", "topicId": "ticketing",
        "ticketing": [
          {
            "dimension": "售票概况", "dimensionId": "ticketingOverview",
            "ticketingOverview": [
              {
                "metric": "总量", "metricId": "totalVolume",
                "totalVolume": [
                  { "issues": [
                      { "issueId": "issue_3f2a9c11b2d34e56",
                        "businessClass": "quick_bi",
                        "quickbiLinkId": "c1f2e8a0",
                        "title": "本月售票总量",
                        "issue": "本月各平台销售额",
                        "capabilityLabel": "问数 · 数据集" }
                  ] }
                ]
              }
            ]
          }
        ]
      }
    ]
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-可问问题清单(平铺)", uri="/api/app/questions", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="不按三级树、只要一份平铺清单时用它:APP 问题库 + Quick BI 固定问题合在一起,每条都标了该怎么发起。",
        req=[
            P("theme_code", "string", False, "ticket", "按 APP 问题库的主题筛"),
            P("domain", "string", False, "票务", "按 Quick BI 数据域筛"),
            P("ask_type", "string", False, "qa", "按 Quick BI 请求类别筛"),
            P("include_quickbi", "bool", False, "true", "false = 只要问题库那一半"),
        ],
        resp=[
            R("data.items", "array", "[…]", "问题清单"),
            R("data.items[].source", "string", "quickbi", "bank 问题库 / quickbi Quick BI 固定问题"),
            R("data.items[].ask", "string", "quickbi", "建议的发起方式:chat / quickbi"),
            R("data.items[].id", "string", "issue_3f2a…", "问题 ID"),
            R("data.items[].question", "string", "本月各平台销售额", "问题原文"),
            R("data.items[].title", "string", "本月售票总量", "短标题"),
            R("data.items[].topic", "string", "票务", "三级标签:主题"),
            R("data.items[].metric", "string", "总量", "三级标签:指标"),
            R("data.items[].target_data_name", "string", "票务明细", "问的是哪份数据"),
            R("data.bank_total", "int", "36", "问题库条数"),
            R("data.quickbi_total", "int", "12", "Quick BI 固定问题条数"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/app/questions?ask_type=qa' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      { "source": "quickbi", "ask": "quickbi", "id": "issue_3f2a9c11b2d34e56",
        "question": "本月各平台销售额", "title": "本月售票总量",
        "topic": "票务", "metric": "总量", "target_data_name": "票务明细" }
    ],
    "total": 48, "bank_total": 36, "quickbi_total": 12
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="提问-发起提问", uri="/api/app/chat", method="POST",
        auth=JWT,
        summary="唯一的发起入口。点问题树里的问题就传 issue_id;自由提问就传 message + 目标。返回 task_id,拿它订阅事件流。",
        req=[
            P("issue_id", "string", False, "issue_3f2a…", "问题树里那条问题的 issueId;传了它就不用再传目标与问题文案"),
            P("message", "string", False, "本月各平台销售额", "提问内容;带 issue_id 时可留空(用问题原文)"),
            P("agent_id", "string", False, "agt_3f2a9c", "自由提问时的执行目标(与 skill_id 二选一)"),
            P("skill_id", "string", False, "skl_88c1", "技能直调(与 agent_id 二选一)"),
            P("conversation_id", "string", False, "conv_7f2a…", "续聊传上一轮的会话 ID;不传则新开会话"),
            P("question_id", "string", False, "qb_123", "老入口的问题 ID(「试试问我」等),后端也会尝试按它分流"),
        ],
        resp=[
            R("data.task_id", "string", "task_9c2f…", "订阅事件流用它;命中预热缓存时为空字符串"),
            R("data.conversation_id", "string", "conv_7f2a…", "本轮所属会话,续聊回传它"),
            R("data.target_type", "string", "quickbi", "本轮实际打给谁:agent / skill / quickbi"),
            R("data.target_id", "string", "issue_3f2a…", "对应的目标 ID"),
            R("data.hit_prewarm", "bool", "false", "true = 命中预热缓存直出,不必开 SSE"),
            R("data.prewarm_result", "object", "{…}", "命中缓存时的完整答案(含图表)"),
        ],
        notes=['<code>target_type=quickbi</code> 时事件也从同一个流出来,前端无需分叉渲染。',
               '同一会话里<b>上一轮没结束不能发下一轮</b>(返回 403 并带上那个 task_id)——'
               '要么等它结束,要么先调「提问-中断本轮」。',
               '<b>两种写法都受支持</b>:新前端点问题树传 <code>issue_id</code>;老入口(「试试问我」、'
               '关注问题)传 <code>question_id</code> + <code>skill_id</code>/<code>agent_id</code> + '
               '<code>message</code>。后者后端也会尝试按 question_id 反查问题配置 —— '
               '否则一条配在 Quick BI 上的问题会被本地 Agent 接走,答出来一个看着像样、其实没查过数的结果。'],
        curl=f"""# ① 点问题树里的问题:只传 issue_id,目标与问题文案都从那条配置带出
curl --location --request POST '{HOST}/api/app/chat' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "issue_id": "issue_3f2a9c11b2d34e56" }}'

# ② 续聊 / 老入口(问题命中技能时前端把 skill_id 与 question_id 原样回传)
curl --location --request POST '{HOST}/api/app/chat' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "skill_id": "0a1b2c3d-1111-4222-8333-444455556666",
  "message": "总评趋势:按评论发布时间看总评论数的分布",
  "conversation_id": "conv_c583b10c569a42d1",
  "question_id": "0a1b2c3d-7777-4888-8999-aaaabbbbcccc"
}}'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "task_id": "task_9c2f1b7d",
    "conversation_id": "conv_7f2a83c1",
    "target_type": "quickbi",
    "target_id": "issue_3f2a9c11b2d34e56",
    "hit_prewarm": false
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="提问-订阅回答事件", uri="/api/app/chat/{task_id}/stream", method="GET",
        auth="账号登录(JWT);EventSource 连接",
        content_type="text/event-stream(SSE)",
        summary="订阅上一步 task_id 的回答事件。支持断线重连(Last-Event-ID)、15 秒心跳、单连接最长 300 秒。",
        req=[
            P("task_id", "string", True, "task_9c2f…", "路径参数:发起提问返回的 task_id"),
            P("Last-Event-ID", "string", False, "1694-0", "请求头:断线重连时带上,从该事件之后续推"),
        ],
        resp=[],
        notes=['单次连接 300 秒上限:长任务(报告)断开后用同一个 task_id 带 Last-Event-ID 重连即可,不会丢事件。',
               '任务已经跑完才连上也没关系:流里没有事件时,服务端会查库补一个 done/error,不会让你干等。'],
        curl=f"""curl --location --request GET '{HOST}/api/app/chat/task_9c2f1b7d/stream' \\
--header 'Authorization: Bearer <access_token>' \\
--no-buffer""",
        resp_json="""event: stage
data: {"type":"status","message":"正在查询票务数据…","channel":"thinking"}

event: token
data: {"type":"token","delta":"本月各平台销售额合计 386.2 万元","channel":"data"}

event: structured
data: {"type":"structured","payload":{"kind":"echarts","option":{ ... }}}

event: done
data: {"type":"done","status":"success","answer":"…"}"""),

    Api(
        title="提问-中断本轮", uri="/api/app/chat/{task_id}/cancel", method="POST",
        auth=JWT,
        summary="用户点「停止」时调它,服务端停止生成并把任务收尾,会话立刻可以发下一轮。",
        req=[P("task_id", "string", True, "task_9c2f…", "路径参数:要中断的 task_id")],
        resp=[
            R("data.task_id", "string", "task_9c2f…", "被中断的任务"),
            R("data.status", "string", "CANCELLED", "收尾后的状态"),
        ],
        curl=f"""curl --location --request POST '{HOST}/api/app/chat/task_9c2f1b7d/cancel' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": { "task_id": "task_9c2f1b7d", "status": "CANCELLED" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="提问-Quick BI 直连", uri="/api/app/quickbi/ask", method="POST",
        auth=JWT, content_type="application/json(响应为 SSE)",
        summary="想跳过任务队列、直接拿 Quick BI 原生事件流时用它。常规场景用「提问-发起提问」即可,后端会自己分流。",
        req=[
            P("issue_id", "string", False, "issue_3f2a…", "按问题树里的问题发起(businessClass 必须是 quick_bi)"),
            P("question", "string", False, "本月各平台销售额", "自由提问内容"),
            P("connection_id", "string", False, "c1f2e8a0", "指定 Quick BI 连接"),
            P("capability", "string", False, "dataset_qa", "能力;留空自动路由"),
            P("target", "object", False, '{"cube_id":"abc123"}', "目标数据"),
            P("scenic_id", "string", False, "PFTSCA01002434", "本次提问归属景区"),
            P("session_id", "string", False, "sess_5b8e…", "在已有会话里追问"),
        ],
        resp=[],
        curl=f"""curl --location --request POST '{HOST}/api/app/quickbi/ask' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "issue_id": "issue_3f2a9c11b2d34e56" }}' \\
--no-buffer""",
        resp_json="""event: run
data: {"type":"run","run_id":"run_7c1f2b9d"}

event: text
data: {"type":"text","delta":"本月各平台销售额合计 386.2 万元","channel":"data"}

event: done
data: {"type":"done","run_id":"run_7c1f2b9d","status":"success"}"""),

    Api(
        title="会话-会话列表", uri="/api/app/conversations", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="「我的对话」列表,按最近活跃排序。",
        req=[
            P("page", "int", False, "1", "页码,默认 1"),
            P("page_size", "int", False, "20", "每页条数"),
        ],
        resp=[
            R("data.items", "array", "[…]", "会话列表"),
            R("data.items[].id", "string", "conv_7f2a…", "会话 ID"),
            R("data.items[].title", "string", "本月各平台销售额", "标题(取首条提问)"),
            R("data.items[].executor_type", "string", "quickbi", "这条会话的执行目标类型"),
            R("data.items[].updated_at", "string", "2026-09-19T10:48:18", "最近活跃时间"),
            R("data.total", "int", "36", "总条数"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/app/conversations?page=1&page_size=20' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      { "id": "conv_7f2a83c1", "title": "本月各平台销售额",
        "executor_type": "quickbi", "updated_at": "2026-09-19T10:48:18" }
    ],
    "total": 36, "page": 1, "page_size": 20
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="会话-历史消息", uri="/api/app/conversations/{conversation_id}/messages", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="打开一条历史会话时拉它的消息,含当时生成的图表 —— 回放不需要重新问一次。",
        req=[P("conversation_id", "string", True, "conv_7f2a…", "路径参数:会话 ID")],
        resp=[
            R("data.items", "array", "[…]", "消息列表,按时间正序"),
            R("data.items[].id", "string", "msg_1a2b…", "消息 ID(点赞/点踩用它)"),
            R("data.items[].role", "string", "assistant", "user / assistant"),
            R("data.items[].content", "string", "本月各平台销售额合计…", "正文"),
            R("data.items[].structured", "object", "{…}", "图表等结构化内容(有则回放)"),
            R("data.items[].created_at", "string", "2026-09-19T10:48:18", "时间"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/app/conversations/conv_7f2a83c1/messages' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0,
  "message": "success",
  "data": {
    "items": [
      { "id": "msg_1a2b3c", "role": "user", "content": "本月各平台销售额",
        "created_at": "2026-09-19T10:47:50" },
      { "id": "msg_1a2b3d", "role": "assistant", "content": "本月各平台销售额合计 386.2 万元…",
        "structured": { "kind": "echarts", "option": { } },
        "created_at": "2026-09-19T10:48:18" }
    ]
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),
])


# ══════════════════════════ 内容:3 管理后端 ══════════════════════════
# 管理端接口给后台页面用,按角色收口(多数要 group_admin / super_admin)。
# 这里只收录**对接会用到**的那些:登录、景区主数据、问题体系、API Key。
# 模型配置、权限矩阵这类纯后台页面的接口不进公开文档 —— 写了等于把后台形状也公开了。

ADMIN = Chapter("管理后端接口(/api/admin)", anchor="admin", items=[
    Api(
        title="登录-获取 RSA 公钥", uri="/api/admin/auth/public-key", method="GET",
        auth=JWT_FREE, content_type="—(GET 无请求体)",
        summary="管理后台登录前取公钥加密密码。与 APP 端是同一把密钥。",
        req=[],
        resp=[
            R("data.public_key", "string", "-----BEGIN PUBLIC KEY-----…", "RSA 公钥(PEM)"),
            R("data.algorithm", "string", "RSA-OAEP", "加密算法"),
            R("data.key_size", "int", "2048", "密钥长度"),
        ],
        curl=f"curl --location --request GET '{HOST}/api/admin/auth/public-key'",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "public_key": "-----BEGIN PUBLIC KEY-----\\nMIIBIjANBgkq…", "algorithm": "RSA-OAEP", "key_size": 2048 },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="登录-管理端登录", uri="/api/admin/auth/login", method="POST",
        auth=JWT_FREE,
        summary="管理后台登录。account 字段填账号或手机号(两者都认)。",
        req=[
            P("phone", "string", True, "13800000001", "账号或手机号(字段名沿用 phone,填账号也可以)"),
            P("password", "string", True, "(RSA 密文)", "rsa_encrypt=true 时为公钥加密后的 Base64 密文"),
            P("rsa_encrypt", "bool", False, "true", "密码是否已加密,推荐 true"),
            P("sms_code", "string", False, "123456", "新设备登录时需要"),
        ],
        resp=[
            R("data.access_token", "string", "eyJhbGciOi…", "管理端接口用它"),
            R("data.refresh_token", "string", "eyJhbGciOi…", "换新令牌用"),
            R("data.must_change_password", "bool", "false", "true = 必须先改密码"),
        ],
        curl=f"""curl --location --request POST '{HOST}/api/admin/auth/login' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "phone": "13800000001", "password": "<RSA 密文>", "rsa_encrypt": true }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "access_token": "eyJhbGciOiJIUzI1NiIs…", "refresh_token": "eyJhbGciOiJIUzI1NiIs…",
            "must_change_password": false },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="登录-当前账号能做什么", uri="/api/admin/auth/capabilities", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="按「角色 × 景区」返回当前账号的增删改权限,管理端按它决定按钮显不显示、能不能点。"
                "自己按角色名硬编码的话,加一个角色要改好几处页面,改漏了就是「按钮能点、点了报错」。",
        req=[],
        resp=[
            R("data.role", "string", "scenic_admin", "角色代码"),
            R("data.role_label", "string", "景区管理员", "角色中文名,直接拿去展示"),
            R("data.scenic_id", "string|null", "PFTSCA01002434", "账号绑定的景区;集团账号为 null"),
            R("data.is_group", "bool", "false", "是否集团级账号(集团账号看全部景区)"),
            R("data.manage_all", "bool", "false", "跨景区增删改 —— 集团三角色为 true"),
            R("data.can_create", "bool", "true", "能不能新增/修改(集团只读为 false)"),
            R("data.can_delete", "bool", "true", "能不能删除(景区操作员为 false)"),
            R("data.scope_scenic_id", "string|null", "PFTSCA01002434",
              "景区角色的作用范围:只能动这个景区下的内容;集团角色为 null(不受限)"),
        ],
        notes=[
            '判定口径(六个角色):集团开发 / 集团管理员 / 超级管理员 <b>不受限</b>,增改删跨景区;'
            '集团只读<b>只能看</b>;景区管理员<b>本景区全部内容</b>可增改删;'
            '景区操作员本景区<b>能增改、不能删</b>。',
            '两条最容易踩的细则:① <code>scenic_id</code> 为空的内容是<b>集团级</b>的(所有景区共用),'
            '景区角色<b>能用不能改</b>;② 判定按<b>景区</b>不按创建者 —— 同景区换个管理员接手,'
            '照样管得了前任建的东西。',
            '这只是给界面用的「按钮开关」,<b>后端每个写接口都会再判一次</b>;绕过前端直接调接口,'
            '越权同样会被拒(403)。',
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/auth/capabilities' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "role": "scenic_admin", "role_label": "景区管理员",
    "scenic_id": "PFTSCA01002434", "is_group": false, "manage_all": false,
    "can_create": true, "can_delete": true, "scope_scenic_id": "PFTSCA01002434"
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="工具-按接口文档创建", uri="/api/admin/tools/from-api-doc", method="POST",
        auth=JWT,
        summary="把一份 Markdown 接口文档直接变成可被智能体调用的工具(自动发布 v1)。"
                "先调 /parse 预览解析结果,确认无误再调这个保存。",
        req=[
            P("markdown", "string", True, "# 1.17 BI报表配置信息…",
              "接口文档原文,整段贴进来即可。认的结构:API概览(URI/请求方式/认证方式)、"
              "请求参数表、返回参数表、示例(curl + 返回 json)。编号与列序不限"),
            P("name", "string", False, "BI报表配置信息", "工具名;留空取文档标题"),
            P("description", "string", False, "取景区的 BI 看板地址", "给模型看的说明;留空取文档概述"),
            P("scenic_id", "string", False, "PFTSCA01002434", "归属景区;留空 = 集团级"),
            P("default_base_url", "string", False, "https://&lt;数据平台域名&gt;",
              "文档里只写了 URI 没写域名时用它"),
        ],
        resp=[
            R("data.id", "string", "3f2a9c…", "新建的工具 ID"),
            R("data.name", "string", "BI报表配置信息", "工具名"),
            R("data.doc.request_fields", "array", "[…]", "解析出来的请求参数"),
            R("data.doc.data_path", "array", '["data"]', "数据体在返回结构里的位置"),
            R("data.warnings", "array", "[…]", "解析时的提醒(如缺返回参数表)"),
        ],
        notes=[
            '为什么不用 JSON Schema:它是给机器看的,人写起来啰嗦、读起来更啰嗦,'
            '而手上现成的恰恰是一份接口文档。手工翻译一遍既慢又容易译错,'
            '而且译错的表现很隐蔽 —— 模型照着错的 schema 传参,接口回一个业务错误,'
            '看起来像「接口有问题」,其实是参数名少了个字母。',
            '**返回参数表不是可有可无的**:结果压缩层靠它知道数据体在哪一层、每列是什么,'
            '才谈得上按列筛选与统计。没有它,上千行的返回只能原样截断。',
            '认证 header 从 curl 示例里取(那一节是唯一能直接跑的,表格反而常漏)。',
        ],
        curl=_APIDOC_CURL,
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "3f2a9c12…", "name": "BI报表配置信息",
            "doc": { "uri": "/v1/data/digitalBrain/api/quickBiLink", "method": "POST",
                     "data_path": ["data"] },
            "warnings": [],
            "message": "已创建工具「BI报表配置信息」并发布 v1;接下来把它绑到 Skill 上就能被智能体调用" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="主题解读-预热报告", uri="/api/admin/app-ops/theme-issues/prewarm", method="POST",
        auth=JWT,
        summary="现在就把主题解读报告跑一遍。报告一条要 3~10 分钟,所以**不等它跑完** ——"
                "接口只把任务丢到后台,立刻返回本次安排了哪几条。",
        req=[
            P("scenic_id", "string", False, "PFTSCA01002434", "给哪个景区预热;留空 = 当前账号所属景区"),
            P("theme_code", "string", False, "ticket",
              "只跑某一个主题(overview/passenger_flow/ticket/public_opinion/ota_direct_sales);留空 = 全部"),
            P("force", "bool", False, "false", "忽略 12 小时新鲜度强制重跑"),
        ],
        resp=[
            R("data.scenic_id", "string", "PFTSCA01002434", "本次预热的景区"),
            R("data.started", "array", '["ticket","public_opinion"]', "已经开始生成的主题"),
            R("data.skipped", "array", '["overview"]', "跳过的(12 小时内跑过,还新鲜)"),
            R("data.message", "string", "已开始生成 2 份报告…", "可直接展示给运营的说明"),
        ],
        notes=['平台还有一个**定时预热**(每小时一轮,按 景区 × 主题 扫),'
               '正常情况下不需要手动调这个接口 —— 它是给「刚换了看板想立刻看效果」用的。',
               '跑完之后 APP 滑到该主题卡片直接出结果(12 小时内回放同一份),'
               '不必再等报告生成。'],
        curl=f"""curl --location --request POST '{HOST}/api/admin/app-ops/theme-issues/prewarm' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "scenic_id": "PFTSCA01002434" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "scenic_id": "PFTSCA01002434", "started": ["ticket", "public_opinion"],
            "skipped": ["overview", "passenger_flow", "ota_direct_sales"],
            "message": "已开始生成 2 份报告,一份通常要 3~10 分钟;跑完后前台点主题卡片直接出结果" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="数据资产-修改归属景区", uri="/api/admin/quickbi/assets/{asset_id}", method="PUT",
        auth=JWT,
        summary="改已登记数据资产的名称与归属景区。改的是同一行(asset_id 不变),"
                "所以**指着这个资产的问题一条都不用动**。",
        req=[
            P("name", "string", False, "九龙潭·经营看板", "显示名称;不传 = 不改"),
            P("scenic_id", "string", False, "PFTSCA01002434",
              "归属景区;空串 = 解绑回集团级(所有景区共用);不传 = 不改"),
        ],
        resp=[
            R("data.id", "string", "fa9f3b14…", "资产 ID(不变)"),
            R("data.name", "string", "九龙潭·经营看板", "新名称"),
            R("data.scenic_id", "string|null", "PFTSCA01002434", "新归属;null = 集团级"),
        ],
        notes=['没有这个接口的话,改归属只能「删了重登记」—— 而删掉的那一刻,'
               '所有指着这个 asset_id 的问题全部失效,发起时报「资产不存在或无权访问」,'
               '而运营不会把这件事和刚才那次删除联系起来。',
               '归属为空 = 集团级:所有景区都能用这块看板(能用不能改);'
               '绑到某个景区后,只有该景区与集团账号能用。'],
        curl=f"""curl --location --request PUT '{HOST}/api/admin/quickbi/assets/fa9f3b14-4587-4e5c-a990-79d44f39a5eb' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "scenic_id": "PFTSCA01002434" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "fa9f3b14-4587-4e5c-a990-79d44f39a5eb", "kind": "dashboard",
            "name": "九龙潭·经营看板", "scenic_id": "PFTSCA01002434" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-已登记景区列表", uri="/api/admin/scenics", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="平台已登记的景区(成员管理绑景区、各处景区下拉都用它)。名称/等级/属地以景区主数据为准。",
        req=[
            P("keyword", "string", False, "九龙潭", "按名称或景区 ID 模糊搜"),
            P("is_enabled", "bool", False, "true", "只看已开通 / 已停用"),
            P("page", "int", False, "1", "页码"),
            P("page_size", "int", False, "20", "每页条数"),
        ],
        resp=[
            R("data.items", "array", "[…]", "景区列表"),
            R("data.items[].scenic_id", "string", "PFTSCA01002434", "景区 ID(与主数据一致)"),
            R("data.items[].scenic_name", "string", "泰宁九龙潭生态旅游区", "名称,取自主数据"),
            R("data.items[].scenic_grade_level", "string", "4A", "等级"),
            R("data.items[].province_name", "string", "福建省", "省份(主数据)"),
            R("data.items[].city_name", "string", "三明市", "城市(主数据)"),
            R("data.items[].in_master_data", "bool", "true", "false = 平台有、主数据里查不到,需要核对"),
            R("data.items[].is_enabled", "bool", "true", "是否开通平台服务"),
            R("data.items[].user_count", "int", "3", "绑在该景区的账号数"),
            R("data.total", "int", "12", "总条数"),
        ],
        notes=['景区信息的唯一真源是主数据维表 <code>dim_prd_scenic_base_yr_f</code>;'
               '平台侧只存「是否开通、套餐」这类状态。所以这里的名称改不了 —— 要改去主数据改。'],
        curl=f"""curl --location --request GET '{HOST}/api/admin/scenics?page=1&page_size=20' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "items": [
      { "scenic_id": "PFTSCA01002434", "scenic_name": "泰宁九龙潭生态旅游区",
        "scenic_grade_level": "4A", "province_name": "福建省", "city_name": "三明市",
        "in_master_data": true, "is_enabled": true, "user_count": 3 }
    ],
    "total": 12, "page": 1, "page_size": 20, "total_pages": 1
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-主数据检索", uri="/api/admin/scenics/catalog", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="景区主数据里搜景区(模糊匹配名称/别名/ID)。「登记景区」的候选池、各处景区选择器都从这里取。",
        req=[
            P("keyword", "string", False, "九龙潭", "模糊搜:名称 / 别名 / 景区 ID"),
            P("province", "string", False, "福建省", "按省份筛"),
            P("grade", "string", False, "4A", "按等级筛"),
            P("registered", "bool", False, "false", "true 只看已登记,false 只看未登记,不传 = 全部"),
            P("enabled", "bool", False, "true",
              "true 只看已登记「且已开通」的。各处的「绑景区」下拉都该传它 —— "
              "停用的景区绑上去是一条配了也不生效的记录,列出来只会让人以为它能用"),
            P("page", "int", False, "1", "页码"),
            P("page_size", "int", False, "20", "每页条数,上限 200"),
        ],
        resp=[
            R("data.items", "array", "[…]", "候选景区,已登记的排在最前"),
            R("data.items[].scenic_id", "string", "PFTSCA01002434", "景区 ID"),
            R("data.items[].scenic_name", "string", "泰宁九龙潭生态旅游区", "名称"),
            R("data.items[].scenic_alias", "string", "九龙潭", "别名"),
            R("data.items[].scenic_grade_level", "string", "4A", "等级"),
            R("data.items[].province_name", "string", "福建省", "省份"),
            R("data.items[].city_name", "string", "三明市", "城市"),
            R("data.items[].scenic_main_type", "string", "自然风光", "主类型"),
            R("data.items[].registered", "bool", "false", "是否已登记到平台"),
            R("data.items[].is_enabled", "bool", "false", "是否已开通服务"),
            R("data.items[].user_count", "int", "0", "绑在该景区的账号数"),
            R("data.total", "int", "17167", "命中总数(主数据可能上万条,务必分页)"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/scenics/catalog?keyword=九龙潭&registered=false' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "items": [
      { "scenic_id": "PFTSCA01002434", "scenic_name": "泰宁九龙潭生态旅游区", "scenic_alias": "九龙潭",
        "scenic_grade_level": "4A", "province_name": "福建省", "city_name": "三明市",
        "scenic_main_type": "自然风光", "registered": false, "is_enabled": false, "user_count": 0 }
    ],
    "total": 1, "page": 1, "page_size": 20, "total_pages": 1
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-筛选项", uri="/api/admin/scenics/catalog/filters", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="主数据里实际出现过的省份与等级,给筛选下拉用(写死枚举早晚和主数据对不上)。",
        req=[],
        resp=[
            R("data.provinces", "array", '["上海市","福建省"]', "省份候选"),
            R("data.grades", "array", '["3A","4A","5A"]', "等级候选"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/scenics/catalog/filters' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "provinces": ["上海市", "福建省"], "grades": ["3A", "4A", "5A"] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-登记景区", uri="/api/admin/scenics/register", method="POST",
        auth=JWT,
        summary="把主数据里的景区登记到平台(可批量)。名称/等级一律取主数据,不接受手填。",
        req=[
            P("scenic_ids", "array", True, '["PFTSCA01002434"]', "要登记的景区 ID 列表"),
            P("enable", "bool", False, "true", "登记后是否直接开通服务,默认 true"),
        ],
        resp=[
            R("data.registered", "int", "1", "本次新登记的条数"),
            R("data.skipped", "int", "1", "已登记而跳过的条数(不报错)"),
            R("data.items", "array", '["PFTSCA01002434"]', "新登记的景区 ID"),
        ],
        notes=['主数据里查无此景区会整批拒绝(404)—— 平台侧凭空多出一个主数据没有的景区,'
               '后面所有按 scenic_id 的关联都会对不上。'],
        curl=f"""curl --location --request POST '{HOST}/api/admin/scenics/register' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "scenic_ids": ["PFTSCA01002434"], "enable": true }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "registered": 1, "skipped": 0, "items": ["PFTSCA01002434"] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-开通/停用", uri="/api/admin/scenics/{scenic_id}", method="PATCH",
        auth=JWT, content_type="query 参数",
        summary="开通或停用某个景区的平台服务。还没登记但主数据里有的景区,会先按主数据自动登记再置状态。",
        req=[
            P("scenic_id", "string", True, "PFTSCA01002434", "路径参数:景区 ID"),
            P("is_enabled", "bool", True, "true", "query 参数:true 开通 / false 停用"),
        ],
        resp=[
            R("data.scenic_id", "string", "PFTSCA01002434", "景区 ID"),
            R("data.scenic_name", "string", "泰宁九龙潭生态旅游区", "名称"),
            R("data.is_enabled", "bool", "true", "当前状态"),
        ],
        curl=f"""curl --location --request PATCH '{HOST}/api/admin/scenics/PFTSCA01002434?is_enabled=true' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "scenic_id": "PFTSCA01002434", "scenic_name": "泰宁九龙潭生态旅游区",
            "scenic_grade_level": "4A", "is_enabled": true, "user_count": 0 },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="景区-按主数据刷新", uri="/api/admin/scenics/sync", method="POST",
        auth=JWT, content_type="query 参数",
        summary="用主数据刷新已登记景区的名称/等级(不动开通状态与套餐)。主数据改了名之后调一次。",
        req=[P("scenic_id", "string", False, "PFTSCA01002434", "query 参数:只刷这一个;不传则刷全部已登记景区")],
        resp=[
            R("data.updated", "int", "2", "名称/等级有变化并已更新的条数"),
            R("data.missing", "array", '["GHOST"]', "主数据里已查不到的景区 —— 平台记录保留(用户还绑着),但要人工核对"),
        ],
        curl=f"""curl --location --request POST '{HOST}/api/admin/scenics/sync' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "updated": 2, "missing": [] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="成员-分页查询", uri="/api/admin/users/paged", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="系统成员列表。景区名取自景区主数据,不是平台侧另存的一份。",
        req=[
            P("keyword", "string", False, "张三", "按姓名/账号/手机号搜"),
            P("role", "string", False, "scenic_admin", "按角色筛"),
            P("scenic_id", "string", False, "PFTSCA01002434", "按景区筛"),
            P("is_active", "bool", False, "true", "按启用状态筛"),
            P("page", "int", False, "1", "页码"),
            P("page_size", "int", False, "20", "每页条数"),
        ],
        resp=[
            R("data.items", "array", "[…]", "成员列表"),
            R("data.items[].id", "string", "u_10086", "用户 ID"),
            R("data.items[].name", "string", "张三", "姓名"),
            R("data.items[].account", "string", "zhangsan", "账号"),
            R("data.items[].role", "string", "scenic_admin", "角色"),
            R("data.items[].scenic_id", "string", "PFTSCA01002434", "绑定景区;空 = 集团级"),
            R("data.items[].scenic_name", "string", "泰宁九龙潭生态旅游区", "景区名"),
            R("data.items[].is_active", "bool", "true", "是否启用"),
            R("data.total", "int", "36", "总条数"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/users/paged?page=1&page_size=20' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "items": [
      { "id": "u_10086", "name": "张三", "account": "zhangsan", "role": "scenic_admin",
        "scenic_id": "PFTSCA01002434", "scenic_name": "泰宁九龙潭生态旅游区", "is_active": true }
    ],
    "total": 36, "page": 1, "page_size": 20, "total_pages": 2
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),
])

ADMIN.items += [
    Api(
        title="标签-三级标签列表", uri="/api/admin/app-ops/dimension-labels", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="主题 → 维度 → 指标 的全部路径。`tree=true` 直接给折好的三级树,前端选择器拿来即用。",
        req=[
            P("tree", "bool", False, "true", "true = 返回折好的三级树;false/不传 = 平铺列表"),
            P("active", "bool", False, "true", "只看启用的标签"),
            P("topic_id", "string", False, "ticketing", "只看某个主题下的"),
        ],
        resp=[
            R("data.topics", "array", "[…]", "tree=true 时:主题数组"),
            R("data.topics[].topic", "string", "票务", "主题中文名"),
            R("data.topics[].topic_id", "string", "ticketing", "主题编码"),
            R("data.topics[].dimensions", "array", "[…]", "该主题下的维度"),
            R("…dimensions[].metrics", "array", "[…]", "该维度下的指标(metric / metric_id / label_id)"),
            R("data.items", "array", "[…]", "tree 未开启时:平铺的三级路径列表"),
        ],
        notes=['一行标签 = 一条完整的「主题/维度/指标」路径(不是三张表)。'
               '名称三元组与编码三元组各有一道唯一约束:名称重复会让下拉里出现两个一样的选项,'
               '编码重复会让前台按编码分组时把两条路径混成一条。'],
        curl=f"""curl --location --request GET '{HOST}/api/admin/app-ops/dimension-labels?tree=true&active=true' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "topics": [
      { "topic": "票务", "topic_id": "ticketing",
        "dimensions": [
          { "dimension": "售票概况", "dimension_id": "ticketingOverview",
            "metrics": [ { "metric": "总量", "metric_id": "totalVolume", "label_id": 13 } ] }
        ] }
    ]
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="标签-新增三级标签", uri="/api/admin/app-ops/dimension-labels", method="POST",
        auth=JWT,
        summary="新增一条三级路径。六个字段都必填 —— 中文名给人看,编码给程序分组,缺一个都用不了。",
        req=[
            P("topic", "string", True, "票务", "主题中文名"),
            P("topic_id", "string", True, "ticketing", "主题编码"),
            P("dimension", "string", True, "售票概况", "维度中文名"),
            P("dimension_id", "string", True, "ticketingOverview", "维度编码"),
            P("metric", "string", True, "总量", "指标中文名"),
            P("metric_id", "string", True, "totalVolume", "指标编码"),
            P("sort_order", "int", False, "120", "同维度下展示顺序,小的在前"),
            P("active", "bool", False, "true", "是否启用"),
        ],
        resp=[
            R("data.id", "int", "41", "标签 ID"),
            R("data.topic", "string", "票务", "主题中文名"),
            R("data.metric_id", "string", "totalVolume", "指标编码"),
            R("data.active", "bool", "true", "是否启用"),
        ],
        notes=['重复时会明确告诉你<b>撞的是名称还是编码</b> —— 只说「已存在」的话,'
               '运营会以为自己点重了,实际可能是编码撞了而名称不同。'],
        curl=f"""curl --location --request POST '{HOST}/api/admin/app-ops/dimension-labels' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "topic": "票务", "topic_id": "ticketing",
  "dimension": "售票概况", "dimension_id": "ticketingOverview",
  "metric": "总量", "metric_id": "totalVolume", "sort_order": 120
}}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": 41, "topic": "票务", "topic_id": "ticketing",
            "dimension": "售票概况", "dimension_id": "ticketingOverview",
            "metric": "总量", "metric_id": "totalVolume", "sort_order": 120, "active": true },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="标签-修改三级标签", uri="/api/admin/app-ops/dimension-labels/{label_id}", method="PATCH",
        auth=JWT,
        summary="只传要改的字段。改了名称会同步到已挂在它下面的问题上,前台不会出现一个空的旧分组。",
        req=[
            P("label_id", "int", True, "41", "路径参数:标签 ID"),
            P("topic", "string", False, "票务", "主题中文名"),
            P("dimension", "string", False, "售票概况", "维度中文名"),
            P("metric", "string", False, "总销量", "指标中文名"),
            P("sort_order", "int", False, "130", "展示顺序"),
            P("active", "bool", False, "false", "停用后不进前台选择器,已挂它的问题不会丢"),
        ],
        resp=[
            R("data.id", "int", "41", "标签 ID"),
            R("data.metric", "string", "总销量", "改后的指标名"),
            R("data.active", "bool", "true", "是否启用"),
        ],
        curl=f"""curl --location --request PATCH '{HOST}/api/admin/app-ops/dimension-labels/41' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "metric": "总销量" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": 41, "topic": "票务", "dimension": "售票概况", "metric": "总销量", "active": true },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="标签-删除三级标签", uri="/api/admin/app-ops/dimension-labels/{label_id}", method="DELETE",
        auth=JWT, content_type="—(DELETE 无请求体)",
        summary="删除一条三级路径。下面还挂着问题时会拒绝 —— 删了那些问题会在前台凭空消失。",
        req=[P("label_id", "int", True, "41", "路径参数:标签 ID")],
        resp=[
            R("data.ok", "bool", "true", "是否成功"),
            R("data.message", "string", "已删除标签「票务/售票概况/总量」", "可直接展示的结果说明"),
        ],
        curl=f"""curl --location --request DELETE '{HOST}/api/admin/app-ops/dimension-labels/41' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "ok": true, "message": "已删除标签「票务/售票概况/总量」" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-问题列表", uri="/api/admin/app-ops/issues", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="统一问题表:APP 问题与 Quick BI 固定问题都在这张表里,靠 business_class 区分怎么发起。",
        req=[
            P("scenic_id", "string", False, "PFTSCA01002434", "按景区筛(本景区 + 集团级通用问题)"),
            P("business_class", "string", False, "quick_bi", "quick_bi / local_system"),
            P("topic_id", "string", False, "ticketing", "按主题筛"),
            P("dimension_id", "string", False, "ticketingOverview", "按维度筛"),
            P("metric_id", "string", False, "totalVolume", "按指标筛"),
            P("keyword", "string", False, "退票", "按标题/问题原文搜"),
            P("active", "bool", False, "true", "按上下架筛"),
        ],
        resp=[
            R("data.items", "array", "[…]", "问题列表"),
            R("data.items[].issue_id", "string", "issue_3f2a…", "问题 ID(对外一律用它)"),
            R("data.items[].business_class", "string", "quick_bi", "quick_bi 直连 Quick BI / local_system 走对话引擎"),
            R("data.items[].connection_id", "string", "c1f2e8a0", "Quick BI 连接;quickbi_link_id 是同一个值"),
            R("data.items[].capability", "string", "dataset_qa", "Quick BI 能力"),
            R("data.items[].target", "object", '{"cube_id":"abc123"}', "目标数据(Quick BI)"),
            R("data.items[].target_data_name", "string", "票务明细", "目标数据名称,列表直接显示"),
            R("data.items[].topic", "string", "票务", "三级标签:主题"),
            R("data.items[].metric_id", "string", "totalVolume", "三级标签:指标编码;为空 = 不会出现在 APP 问题树"),
            R("data.items[].title", "string", "本月售票总量", "标题"),
            R("data.items[].issue", "string", "本月各平台销售额", "问题原文"),
            R("data.items[].active", "bool", "true", "是否上架"),
            R("data.items[].source_question_id", "string", "0a1b2c3d-…", "迁移溯源:老表 ID,老配置按它也能查到这一行"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/app-ops/issues?business_class=quick_bi' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "items": [
      { "issue_id": "issue_3f2a9c11b2d34e56", "business_class": "quick_bi",
        "connection_id": "c1f2e8a0", "quickbi_link_id": "c1f2e8a0",
        "capability": "dataset_qa", "target": { "cube_id": "abc123" },
        "target_data_id": "abc123", "target_data_name": "票务明细",
        "topic": "票务", "topic_id": "ticketing",
        "dimension": "售票概况", "dimension_id": "ticketingOverview",
        "metric": "总量", "metric_id": "totalVolume",
        "title": "本月售票总量", "issue": "本月各平台销售额",
        "active": true, "source_question_id": null }
    ]
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-新增问题", uri="/api/admin/app-ops/issues", method="POST",
        auth=JWT,
        summary="新增一条问题。quick_bi 类必须给连接;挂上三级标签才会出现在 APP 的问题树里。",
        req=[
            P("business_class", "string", False, "quick_bi", "quick_bi / local_system,默认 local_system"),
            P("title", "string", True, "本月售票总量", "列表与按钮上的短标题"),
            P("issue", "string", True, "本月各平台销售额", "真正发出去的问题原文"),
            P("connection_id", "string", False, "c1f2e8a0", "Quick BI 连接;business_class=quick_bi 时必填"),
            P("capability", "string", False, "dataset_qa", "Quick BI 能力"),
            P("target", "object", False, '{"cube_id":"abc123"}', "Quick BI 目标数据"),
            P("agent_id", "string", False, "agt_3f2a9c", "local_system:指定 Agent"),
            P("skill_id", "string", False, "skl_88c1", "local_system:指定技能(优先于 Agent)"),
            P("topic_id", "string", False, "ticketing", "三级标签:主题编码(三级要么全给、要么全不给)"),
            P("dimension_id", "string", False, "ticketingOverview", "三级标签:维度编码"),
            P("metric_id", "string", False, "totalVolume", "三级标签:指标编码"),
            P("scenic_id", "string", False, "PFTSCA01002434", "归属景区;留空 = 全景区通用"),
            P("model_instruction", "string", False, "只取本月,异常阈值 ±10%", "给模型的口径说明"),
            P("param_template", "object", False, '{"month":"{{month}}"}', "参数模板,发起前由代码展开"),
            P("sort_order", "int", False, "0", "展示顺序"),
            P("active", "bool", False, "true", "是否上架"),
        ],
        resp=[
            R("data.issue_id", "string", "issue_3f2a…", "新问题的 ID"),
            R("data.business_class", "string", "quick_bi", "业务渠道类别"),
            R("data.topic", "string", "票务", "中文名由后端按标签表回填,前端只传编码"),
            R("data.metric", "string", "总量", "同上"),
        ],
        notes=['只传编码、不传中文名:名字是会被改的,前端存一份就会和库里对不上。'
               '组合不存在时直接报错,而不是存进去让前台分出一个空壳节点。'],
        curl=f"""curl --location --request POST '{HOST}/api/admin/app-ops/issues' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "business_class": "quick_bi",
  "title": "本月售票总量",
  "issue": "本月各平台销售额",
  "connection_id": "c1f2e8a0",
  "capability": "dataset_qa",
  "target": {{ "cube_id": "abc123" }},
  "topic_id": "ticketing", "dimension_id": "ticketingOverview", "metric_id": "totalVolume"
}}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "issue_id": "issue_3f2a9c11b2d34e56", "business_class": "quick_bi",
    "connection_id": "c1f2e8a0", "quickbi_link_id": "c1f2e8a0",
    "topic": "票务", "dimension": "售票概况", "metric": "总量",
    "title": "本月售票总量", "issue": "本月各平台销售额", "active": true
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-修改问题", uri="/api/admin/app-ops/issues/{issue_id}", method="PATCH",
        auth=JWT,
        summary="只传要改的字段。三级标签传空串三件套 = 把这条问题从 APP 问题树上摘下来(管理端仍可执行)。",
        req=[
            P("issue_id", "string", True, "issue_3f2a…", "路径参数:问题 ID"),
            P("title", "string", False, "本月售票总量", "标题"),
            P("issue", "string", False, "本月各渠道销售额", "问题原文"),
            P("target", "object", False, '{"cube_id":"def456"}', "换一份目标数据"),
            P("metric_id", "string", False, "totalVolume", "换挂载的指标"),
            P("active", "bool", False, "false", "下架"),
        ],
        resp=[
            R("data.issue_id", "string", "issue_3f2a…", "问题 ID"),
            R("data.issue", "string", "本月各渠道销售额", "改后的问题原文"),
            R("data.active", "bool", "false", "是否上架"),
        ],
        curl=f"""curl --location --request PATCH '{HOST}/api/admin/app-ops/issues/issue_3f2a9c11b2d34e56' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "issue": "本月各渠道销售额" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "issue_id": "issue_3f2a9c11b2d34e56", "issue": "本月各渠道销售额", "active": true },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="问题-删除问题", uri="/api/admin/app-ops/issues/{issue_id}", method="DELETE",
        auth=JWT, content_type="—(DELETE 无请求体)",
        summary="删除一条问题。前台随即看不到它。",
        req=[P("issue_id", "string", True, "issue_3f2a…", "路径参数:问题 ID")],
        resp=[
            R("data.ok", "bool", "true", "是否成功"),
            R("data.message", "string", "已删除问题「本月售票总量」", "可直接展示的结果说明"),
        ],
        curl=f"""curl --location --request DELETE '{HOST}/api/admin/app-ops/issues/issue_3f2a9c11b2d34e56' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "ok": true, "message": "已删除问题「本月售票总量」" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="主题解读-查看配置", uri="/api/admin/app-ops/theme-issues", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="APP 首页那 5 张主题卡片(概览/客群/票务/舆情/OTA直销)绑 Quick BI 了没 —— "
                "没绑的话点下去只会走通用对话,回一句「请告诉我您需要查询什么」。",
        req=[P("scenic_id", "string", False, "PFTSCA01002434", "看哪个景区;不传按当前账号所属景区")],
        resp=[
            R("data.scenic_id", "string", "PFTSCA01002434", "本次查看的景区"),
            R("data.connection_id", "string", "c1f2e8a0", "该景区当前可用的 Quick BI 连接;为空 = 还没绑连接"),
            R("data.items", "array", "[…]", "五个主题的状态"),
            R("data.items[].theme_code", "string", "ticket", "overview / passenger_flow / ticket / public_opinion / ota_direct_sales"),
            R("data.items[].theme", "string", "票务", "主题中文名"),
            R("data.items[].ready", "bool", "true", "false = 未绑 Quick BI,前台点了不出报告"),
            R("data.items[].issue_id", "string", "issue_theme_ticket_…", "对应的问题 ID"),
            R("data.items[].scope", "string", "景区专属", "景区专属 / 集团级 / 未配置"),
            R("data.items[].target_data_name", "string", "票务大盘", "打在哪份数据上;空 = 交给 Quick BI 智能选表"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/app-ops/theme-issues' \
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "scenic_id": "PFTSCA01002434", "connection_id": "c1f2e8a0",
    "items": [
      { "theme_code": "ticket", "theme": "票务", "ready": true,
        "issue_id": "issue_theme_ticket_9c2f1b7d", "scope": "景区专属",
        "connection_id": "c1f2e8a0", "target_data_name": "票务大盘" }
    ]
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="主题解读-一键生成", uri="/api/admin/app-ops/theme-issues", method="POST",
        auth=JWT,
        summary="按景区把 5 条主题解读问题建出来:绑该景区的连接、挑该主题的仪表板、挂上三级标签。"
                "建完前台点卡片就出报告。",
        req=[
            P("scenic_id", "string", False, "PFTSCA01002434", "给哪个景区生成;留空 = 生成集团级通用的"),
            P("connection_id", "string", False, "c1f2e8a0", "绑哪条连接;留空 = 按景区自动解析"),
            P("overwrite", "bool", False, "false", "true = 已存在的也按当前连接/数据重新绑一遍"),
        ],
        resp=[
            R("data.connection_id", "string", "c1f2e8a0", "实际绑定的连接"),
            R("data.created", "array", '["ticket"]', "新建的主题"),
            R("data.updated", "array", "[]", "重绑的主题(overwrite=true 时)"),
            R("data.skipped", "array", '["overview"]', "已存在而跳过的主题"),
        ],
        notes=['该景区没有可用连接时会直接拒绝并说清楚去哪儿配 —— '
               '生成一批绑不上连接的问题,等于把问题留到用户点下去的那一刻才暴露。',
               '解读是报告类的重活:12 小时内重复点同一主题会直接回放上一次成功的结果(预热直出),'
               '不会每点一次重跑一遍。'],
        curl=f"""curl --location --request POST '{HOST}/api/admin/app-ops/theme-issues' \
--header 'Authorization: Bearer <access_token>' \
--header 'Content-Type: application/json' \
--data-raw '{{ "scenic_id": "PFTSCA01002434" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "connection_id": "c1f2e8a0",
            "created": ["overview", "passenger_flow", "ticket", "public_opinion", "ota_direct_sales"],
            "updated": [], "skipped": [] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="API Key-创建", uri="/api/admin/api-keys", method="POST",
        auth=JWT,
        summary="给外部集成方发一把 Key。**明文只在这一次返回**,关掉页面就再也看不到。",
        req=[
            P("expires_in_days", "int", False, "90", "多少天后过期;不传 = 不过期"),
            P("agent_allowed_ids", "array", False, '["agt_3f2a9c"]', "限定只能调这些 Agent;"
                                                                     "<b>限定后不能调 Quick BI 开放接口</b>"),
        ],
        resp=[
            R("data.id", "string", "key_7f2a…", "Key ID(吊销时用)"),
            R("data.key", "string", "sk-zhilv-xxxxxxxx", "完整明文,仅此一次"),
            R("data.prefix_display", "string", "sk-zhilv-xxxx****", "列表里展示用的前缀"),
            R("data.scenic_id", "string", "PFTSCA01002434", "所属景区(跟随创建者);空 = 集团级 Key"),
            R("data.expires_at", "string", "2026-12-18T23:59:59", "过期时间"),
        ],
        curl=f"""curl --location --request POST '{HOST}/api/admin/api-keys' \\
--header 'Authorization: Bearer <access_token>' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "expires_in_days": 90 }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "id": "key_7f2a83c1", "key": "sk-zhilv-xxxxxxxx", "prefix_display": "sk-zhilv-xxxx****",
    "scenic_id": "PFTSCA01002434", "expires_at": "2026-12-18T23:59:59"
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="API Key-列表", uri="/api/admin/api-keys", method="GET",
        auth=JWT, content_type="—(GET 无请求体)",
        summary="当前景区下已发出的 Key(不含明文)。",
        req=[],
        resp=[
            R("data", "array", "[…]", "Key 列表(本接口 data 直接是数组)"),
            R("data[].id", "string", "key_7f2a…", "Key ID"),
            R("data[].prefix_display", "string", "sk-zhilv-xxxx****", "前缀展示"),
            R("data[].revoked", "bool", "false", "是否已吊销"),
            R("data[].expires_at", "string", "2026-12-18T23:59:59", "过期时间"),
            R("data[].created_at", "string", "2026-09-19T10:48:18", "创建时间"),
        ],
        curl=f"""curl --location --request GET '{HOST}/api/admin/api-keys' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": [
    { "id": "key_7f2a83c1", "prefix_display": "sk-zhilv-xxxx****", "revoked": false,
      "expires_at": "2026-12-18T23:59:59", "created_at": "2026-09-19T10:48:18" }
  ],
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="API Key-吊销", uri="/api/admin/api-keys/{key_id}/revoke", method="POST",
        auth=JWT, content_type="—(无请求体)",
        summary="立即吊销一把 Key,之后所有用它的调用都会 401。泄露时第一时间调它。",
        req=[P("key_id", "string", True, "key_7f2a…", "路径参数:Key ID")],
        resp=[R("data.ok", "bool", "true", "是否成功")],
        curl=f"""curl --location --request POST '{HOST}/api/admin/api-keys/key_7f2a83c1/revoke' \\
--header 'Authorization: Bearer <access_token>'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "ok": true },
  "trace_id": "b6289ec8a1f24c37"
}"""),
]


# ══════════════════════════ 内容:4 Quick BI 与开放平台 ══════════════════════════
# /v1/* 这一整层都是给**外部系统集成**用的(API Key 鉴权),Quick BI 是其中的主体;
# 另外两个 OpenAI 兼容接口也放在这一章 —— 它们与 Quick BI 共用同一把 Key、同一套信封,
# 分到别处只会让集成方在两章之间来回翻。
# 章末收录管理端的 Quick BI 接口:配问题、配连接是在后台做的,接入方偶尔也要看一眼。

RUN_FIELDS = [
    R("data.id", "string", "run_7c1f…", "运行记录 ID(run_id),后续查结果/报告进度都用它"),
    R("data.status", "string", "success", "running 生成中 / success 成功 / failed 失败"),
    R("data.capability", "string", "dataset_qa", "实际使用的能力(自动路由时看这个)"),
    R("data.question", "string", "本月各平台销售额", "实际发给 Quick BI 的问题文本"),
    R("data.answer", "string", "本月各平台销售额合计…", "文字回答;图表类问题这里是结论摘要"),
    R("data.result.charts", "array", "[…]", "图表数据,结构见「图表结果结构」"),
    R("data.result.charts[].title", "string", "各平台销售额", "图表标题"),
    R("data.result.charts[].chart_type", "string", "bar", "bar / line / pie / table…"),
    R("data.result.charts[].columns", "array", '["平台","销售额"]', "列名"),
    R("data.result.charts[].rows", "array", '[["抖音",1203560]]', "数据行,单图最多 1000 行(超出 truncated=true)"),
    R("data.result.sql", "string", "SELECT …", "Quick BI 生成的 SQL(有则返回)"),
    R("data.report_url", "string", "https://bi.aliyun.com/…", "报告类才有:报告在线查看地址"),
    R("data.duration_ms", "int", "4210", "本次耗时(毫秒);命中加速缓存时为 0"),
    R("data.error", "string", "", "失败原因,成功时为空"),
    R("data.from_cache", "bool", "true",
      "这一份是不是「加速」预热好的结果。平台会按景区 + 问题原文预热,命中时直接回放,"
      "毫秒级返回;不需要缓存请改用会话接口提问"),
    R("data.cached_at", "string", "2026-09-20T10:30:00+08:00",
      "命中加速时:这份结果是什么时候跑出来的。展示给终端用户时建议带上它,"
      "否则用户会当成刚取的实时数"),
]

TARGET_NOTE = ('<code>target</code> 指「问哪份数据」,四选一:<code>{"cube_id":"…"}</code> 数据集、'
               '<code>{"page_id":"…"}</code> 仪表板、<code>{"asset_id":"…"}</code> 已登记资产、'
               '<code>{"file_id":"…"}</code> 上传的文件。<b>都不给</b>时平台按该 Key 所属景区自动挑一份已登记的数据。')

_RUN_JSON = """{
  "code": 0, "message": "success",
  "data": {
    "id": "run_7c1f2b9d", "status": "success", "capability": "dataset_qa",
    "question": "本月各平台销售额",
    "answer": "本月各平台销售额合计 386.2 万元,抖音渠道占比最高…",
    "result": {
      "charts": [ { "title": "各平台销售额", "chart_type": "bar",
                    "columns": ["平台", "销售额"],
                    "rows": [["抖音", 1203560], ["美团", 986400]], "truncated": false } ],
      "sql": "SELECT platform, SUM(amount) FROM ticket_order WHERE ..."
    },
    "duration_ms": 4210, "error": "",
    "from_cache": false, "cached_at": null
  },
  "trace_id": "b6289ec8a1f24c37"
}"""

QBI = Chapter("Quick BI 与开放平台(/v1)", anchor="qbi", items=[
    Api(
        title="目录-能问什么/能问哪份数据", uri="/v1/quickbi/catalog", method="GET",
        content_type="—(GET 无请求体)",
        summary="接入第一个该调的接口:一次拿到这个景区的全部固定问题,每条都带着可以直接发起提问的数据标识。",
        req=[
            P("connection_id", "string", False, "c1f2e8a0", "只看某个 Quick BI 连接下的内容"),
            P("scenic_id", "string", False, "PFTSCA01002434", "只看某个景区;不传按 API Key 所属景区"),
            P("ask_type", "string", False, "qa", "qa 问数 / insight 解读 / dashboard 报表 / report 报告"),
            P("domain", "string", False, "票务", "数据域:票务/财务/舆情/客群/核销/营销/流量/画像…"),
            P("include_assets", "bool", False, "true", "false = 只返回问题清单,不返回该景区全部数据集/仪表板"),
        ],
        resp=[
            R("data.scenic_id", "string", "PFTSCA01002434", "本次目录归属的景区"),
            R("data.ask_types", "array", "[…]", "请求类别字典(key + label)"),
            R("data.domains", "array", '["票务","舆情"]', "该景区用到的数据域"),
            R("data.questions", "array", "[…]", "固定问题清单"),
            R("data.questions[].id", "string", "issue_3f2a…", "问题 ID,拿去发起提问"),
            R("data.questions[].title", "string", "本月售票总量", "标题"),
            R("data.questions[].question", "string", "本月各平台销售额", "问题原文"),
            R("data.questions[].ask_type", "string", "qa", "请求类别"),
            R("data.questions[].capability", "string", "dataset_qa", "能力"),
            R("data.questions[].topic", "string", "票务", "三级标签:主题"),
            R("data.questions[].dimension", "string", "售票概况", "三级标签:维度"),
            R("data.questions[].metric", "string", "总量", "三级标签:指标"),
            R("data.questions[].target", "object", '{"kind":"dataset","cube_id":"…"}', "可直接用于提问的数据标识"),
            R("data.questions[].target_data_name", "string", "票务明细", "目标数据名称"),
            R("data.questions[].model_instruction", "string", "先给结论再给数据…",
              "口径说明:发起时会「追加」在问题后面发给模型(不替换问题原文)。"
              "按钮上的问题要短,而模型需要的恰恰是这些约束"),
            R("data.datasets", "array", "[…]", "该景区已登记的数据集(include_assets=true 时)"),
            R("data.dashboards", "array", "[…]", "该景区已登记的仪表板"),
        ],
        notes=['拿到之后两种发起方式:①「原样问这条」→ 在「提问-一次性提问」里传 <code>fixed_question_id</code>;'
               '②「自己组问题打在同一份数据上」→ 传 <code>question</code> + <code>target</code>。'],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/catalog?ask_type=qa' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "scenic_id": "PFTSCA01002434",
    "ask_types": [ { "key": "qa", "label": "问数" }, { "key": "insight", "label": "解读" } ],
    "domains": ["票务", "舆情"],
    "questions": [
      { "id": "issue_3f2a9c11b2d34e56", "title": "本月售票总量", "question": "本月各平台销售额",
        "ask_type": "qa", "capability": "dataset_qa",
        "topic": "票务", "dimension": "售票概况", "metric": "总量",
        "target": { "kind": "dataset", "asset_id": "a7c2e1f0", "cube_id": "abc123", "name": "票务明细" },
        "target_data_name": "票务明细" }
    ],
    "datasets": [ { "asset_id": "a7c2e1f0", "name": "票务明细", "cube_id": "abc123" } ],
    "dashboards": []
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="提问-一次性提问", uri="/v1/quickbi/query", method="POST",
        summary="主力接口:问数 / 解读 / 报表都走它。给一句话 + 问哪份数据,拿回答与图表。",
        req=[
            P("question", "string", True, "本月各平台销售额", "问题原文;传了 fixed_question_id 时可留空"),
            P("fixed_question_id", "string", False, "issue_3f2a…", "按固定问题发起:只传 id,问题/能力/目标数据从那条配置带出"),
            P("connection_id", "string", False, "c1f2e8a0", "指定 Quick BI 连接;不传用默认连接"),
            P("capability", "string", False, "dataset_qa", "dataset_qa 数据集问数 / dashboard_qa 仪表板问数 / "
                                                          "file_qa 文件问数 / insight 解读 / report 报告;留空 = 按问题意图自动路由"),
            P("target", "object", False, '{"cube_id":"abc123"}', "问哪份数据,见本节下方说明"),
            P("locale", "string", False, "zh_CN", "语种,默认跟随连接配置"),
            P("scenic_id", "string", False, "PFTSCA01002434", "本次提问归属景区;不传按 API Key 所属景区"),
            P("stream", "bool", False, "false", "true = SSE 流式(边算边推);false = 等跑完一次性返回"),
        ],
        resp=RUN_FIELDS,
        notes=[TARGET_NOTE,
               '<b>报告类很慢</b>(可达几十分钟):<code>capability=report</code> 且非流式时,'
               '本接口<b>立即返回</b> <code>status=running</code> 和 run_id,之后用「报告-查询进度」轮询,'
               '不会把你的请求挂住。'],
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/query' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "question": "本月各平台销售额",
  "target": {{ "cube_id": "abc123" }},
  "stream": false
}}'""",
        resp_json=_RUN_JSON),

    Api(
        title="提问-执行固定问题", uri="/v1/quickbi/fixed-questions/{id}/run", method="POST",
        summary="执行一条固定问题。问题文本里的 {{变量}} 用 params 填。与传 fixed_question_id 等价,区别是这里能换景区、能填参数。",
        req=[
            P("id", "string", True, "issue_3f2a…", "路径参数:问题 ID(来自目录接口)"),
            P("params", "object", False, '{"month":"2026-09"}', "问题里 {{变量}} 的取值;缺参数会返回 400"),
            P("scenic_id", "string", False, "PFTSCA01002434", "换个景区问同一条问题;不传沿用问题绑定的景区"),
            P("stream", "bool", False, "false", "true = SSE 流式返回"),
        ],
        resp=RUN_FIELDS,
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/fixed-questions/issue_3f2a9c11b2d34e56/run' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "params": {{ "month": "2026-09" }} }}'""",
        resp_json=_RUN_JSON),

    Api(
        title="清单-固定问题", uri="/v1/quickbi/fixed-questions", method="GET",
        content_type="—(GET 无请求体)",
        summary="只要问题清单、不要数据标识时用它(比目录接口轻)。",
        req=[
            P("connection_id", "string", False, "c1f2e8a0", "按连接筛"),
            P("category", "string", False, "日常巡检", "按自定义分类筛"),
            P("domain", "string", False, "票务", "按数据域筛"),
            P("ask_type", "string", False, "qa", "按请求类别筛"),
            P("scenic_id", "string", False, "PFTSCA01002434", "按景区筛(本景区 + 集团级)"),
        ],
        resp=[
            R("data.items", "array", "[…]", "问题清单"),
            R("data.items[].id", "string", "issue_3f2a…", "问题 ID"),
            R("data.items[].title", "string", "本月售票总量", "标题"),
            R("data.items[].question", "string", "本月各平台销售额", "问题原文"),
            R("data.items[].capability", "string", "dataset_qa", "能力"),
            R("data.items[].capability_label", "string", "问数 · 数据集", "能力中文名"),
            R("data.items[].ask_type", "string", "qa", "请求类别"),
            R("data.items[].domain", "string", "票务", "数据域"),
            R("data.items[].scenic_id", "string", "PFTSCA01002434", "归属景区;空 = 集团级(全景区通用)"),
            R("data.items[].connection_id", "string", "c1f2e8a0", "所属 Quick BI 连接"),
        ],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/fixed-questions?domain=票务' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "items": [
    { "id": "issue_3f2a9c11b2d34e56", "title": "本月售票总量", "question": "本月各平台销售额",
      "capability": "dataset_qa", "capability_label": "问数 · 数据集",
      "ask_type": "qa", "ask_type_label": "问数", "domain": "票务", "category": "日常巡检",
      "scenic_id": "PFTSCA01002434", "connection_id": "c1f2e8a0" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="会话-创建会话", uri="/v1/quickbi/sessions", method="POST",
        summary="要连着追问(「那上个月呢」)就先开一个会话:目标数据粘在会话上,后面每轮不用再传。",
        req=[
            P("connection_id", "string", False, "c1f2e8a0", "指定连接;不传用默认连接"),
            P("capability", "string", False, "auto", "auto = 每轮按意图路由(默认);也可锁死成 dataset_qa 等"),
            P("target", "object", False, '{"cube_id":"abc123"}', "会话粘住的数据,写法同「提问-一次性提问」"),
            P("title", "string", False, "票务日常巡检", "会话标题,便于在管理端回看"),
        ],
        resp=[
            R("data.id", "string", "sess_5b8e…", "会话 ID,后续追问用它"),
            R("data.title", "string", "票务日常巡检", "标题"),
            R("data.capability", "string", "auto", "本会话的能力策略"),
            R("data.target", "object", '{"cube_id":"abc123"}', "粘住的目标数据"),
            R("data.turn_count", "int", "0", "已进行轮次"),
        ],
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/sessions' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "capability": "auto", "target": {{ "cube_id": "abc123" }}, "title": "票务日常巡检" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "sess_5b8e1a2c", "title": "票务日常巡检", "capability": "auto",
            "target": { "cube_id": "abc123" }, "turn_count": 0 },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="会话-追问", uri="/v1/quickbi/sessions/{id}/messages", method="POST",
        summary="在会话里问下一句。短追问(「那上个月呢」)会自动带上上一轮的问题,不必自己拼上下文。",
        req=[
            P("id", "string", True, "sess_5b8e…", "路径参数:会话 ID"),
            P("question", "string", True, "那上个月呢", "本轮问题"),
            P("capability", "string", False, "dataset_qa", "只改这一轮的能力;不传沿用会话设置"),
            P("target", "object", False, '{"cube_id":"def456"}', "只改这一轮的目标数据"),
            P("stream", "bool", False, "false", "true = SSE 流式返回"),
        ],
        resp=RUN_FIELDS,
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/sessions/sess_5b8e1a2c/messages' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "question": "那上个月呢" }}'""",
        resp_json=_RUN_JSON),

    Api(
        title="会话-会话详情", uri="/v1/quickbi/sessions/{id}", method="GET",
        content_type="—(GET 无请求体)",
        summary="看这个会话粘的是哪份数据、已经问了几轮。",
        req=[P("id", "string", True, "sess_5b8e…", "路径参数:会话 ID")],
        resp=[
            R("data.id", "string", "sess_5b8e…", "会话 ID"),
            R("data.title", "string", "票务日常巡检", "标题"),
            R("data.target", "object", '{"cube_id":"abc123"}', "粘住的目标数据"),
            R("data.turn_count", "int", "3", "已进行轮次"),
            R("data.runs", "array", "[…]", "历史运行记录摘要(问题、状态、时间)"),
        ],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/sessions/sess_5b8e1a2c' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "sess_5b8e1a2c", "title": "票务日常巡检", "capability": "auto",
            "target": { "cube_id": "abc123" }, "turn_count": 3,
            "runs": [ { "id": "run_a3f81c72", "question": "那上个月呢", "status": "success" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="报告-创建报告", uri="/v1/quickbi/reports", method="POST",
        summary="生成一份完整分析报告。<b>立即返回</b>,报告在后台慢慢生成(可达几十分钟),之后轮询进度。",
        req=[
            P("question", "string", True, "生成本月票务经营分析报告", "报告主题"),
            P("connection_id", "string", False, "c1f2e8a0", "指定连接;不传用默认连接"),
            P("target", "object", False, '{"file_id":"f_88a1c390"}', "报告引用的数据/文件"),
            P("locale", "string", False, "zh_CN", "语种"),
        ],
        resp=[
            R("data.id", "string", "run_ec5d…", "运行记录 ID,轮询进度用它"),
            R("data.status", "string", "running", "创建后固定为 running"),
            R("data.chat_id", "string", "qrep_7f21…", "Quick BI 侧的报告会话 ID"),
            R("data.report_url", "string", "", "生成完成后才有值"),
        ],
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/reports' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{ "question": "生成本月票务经营分析报告" }}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "run_ec5d40b9", "status": "running", "chat_id": "qrep_7f21a9c3", "report_url": "" },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="报告-查询进度", uri="/v1/quickbi/reports/{run_id}", method="GET",
        content_type="—(GET 无请求体)",
        summary="轮询报告进度。建议 10 秒一次;status 变成 success 后 report_url 就是在线查看地址。",
        req=[P("run_id", "string", True, "run_ec5d…", "路径参数:创建报告时返回的 id")],
        resp=[
            R("data.id", "string", "run_ec5d…", "运行记录 ID"),
            R("data.status", "string", "running", "running 生成中 / success 已完成 / failed 失败"),
            R("data.report_url", "string", "https://bi.aliyun.com/…", "完成后的在线查看地址"),
            R("data.answer", "string", "本月票务经营分析…", "报告正文摘要"),
            R("data.result.progress.finished", "bool", "false", "上游是否已结束"),
            R("data.result.progress.event_count", "int", "128", "已累计的事件数,可当进度条用"),
        ],
        notes=['轮询期间偶发的网络超时<b>不代表报告失败</b> —— 平台只在 Quick BI 明确报错(额度/权限/配置)时'
               '才收尾为 failed,其余情况保持 running 继续拉。所以客户端也不要一超时就判死。'],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/reports/run_ec5d40b9' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "run_ec5d40b9", "status": "success",
            "report_url": "https://bi.aliyun.com/copilot/qreportReplay?caseId=qrep_7f21a9c3",
            "answer": "本月票务经营分析:总销售额 386.2 万元…",
            "result": { "progress": { "finished": true, "event_count": 213 } } },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="报告-订阅生成过程", uri="/v1/quickbi/reports/{run_id}/stream", method="GET",
        content_type="text/event-stream(SSE)",
        summary="不想轮询就订阅它:报告生成的每一步以 SSE 推过来,生成完成自动结束。",
        req=[P("run_id", "string", True, "run_ec5d…", "路径参数:创建报告时返回的 id")],
        resp=[],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/reports/run_ec5d40b9/stream' \\
--header 'Authorization: Bearer {KEY}' \\
--no-buffer""",
        resp_json="""event: stage
data: {"type":"status","message":"正在汇总票务数据…","channel":"thinking"}

event: text
data: {"type":"text","delta":"本月票务经营分析","channel":"data"}

event: done
data: {"type":"done","run_id":"run_ec5d40b9","status":"success",
       "report_url":"https://bi.aliyun.com/copilot/qreportReplay?caseId=qrep_7f21a9c3"}"""),

    Api(
        title="结果-查询运行记录", uri="/v1/quickbi/runs/{run_id}", method="GET",
        content_type="—(GET 无请求体)",
        summary="按 run_id 回看某一次提问的完整结果(回答、图表、SQL、耗时)。流式调用结束后想拿结构化结果,用它。",
        req=[P("run_id", "string", True, "run_7c1f…", "路径参数:运行记录 ID")],
        resp=RUN_FIELDS,
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/runs/run_7c1f2b9d' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json=_RUN_JSON),

    Api(
        title="数据-上传文件问数", uri="/v1/quickbi/files", method="POST",
        content_type="multipart/form-data",
        summary="把 Excel/CSV 传上来直接问。返回的 file_id 填进提问接口的 target 即可。",
        req=[
            P("file", "file", True, "sales.xlsx", "xls / xlsx / csv,单个文件 ≤ 5MB"),
            P("connection_id", "string", False, "c1f2e8a0", "指定连接;不传用默认连接"),
        ],
        resp=[
            R("data.id", "string", "f_88a1c390", "文件 ID:target 里填 {\"file_id\": \"…\"}"),
            R("data.file_name", "string", "sales.xlsx", "原文件名"),
            R("data.size", "int", "20480", "字节数"),
            R("data.detail.columns", "array", '["日期","平台","销售额"]', "解析出的列名"),
            R("data.detail.row_count", "int", "1200", "解析出的行数"),
        ],
        curl=f"""curl --location --request POST '{HOST}/v1/quickbi/files' \\
--header 'Authorization: Bearer {KEY}' \\
--form 'file=@"/path/to/sales.xlsx"'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "id": "f_88a1c390", "file_name": "sales.xlsx", "size": 20480,
            "detail": { "columns": ["日期", "平台", "销售额"], "row_count": 1200 } },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="数据-数据集列表", uri="/v1/quickbi/datasets", method="GET",
        content_type="—(GET 无请求体)",
        summary="Quick BI 侧的数据集(cube)清单,用来拿 cube_id。",
        req=[
            P("connection_id", "string", False, "c1f2e8a0", "指定连接;不传用默认连接"),
            P("keyword", "string", False, "票务", "按名称模糊搜"),
        ],
        resp=[
            R("data.items", "array", "[…]", "数据集清单"),
            R("data.items[].cube_id", "string", "abc123", "数据集 ID:target 里填 {\"cube_id\": \"…\"}"),
            R("data.items[].cube_name", "string", "票务明细", "数据集名称"),
        ],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/datasets?keyword=票务' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "items": [ { "cube_id": "abc123", "cube_name": "票务明细" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="数据-已登记资产", uri="/v1/quickbi/assets", method="GET",
        content_type="—(GET 无请求体)",
        summary="平台侧已登记的仪表板 / 数据集。与数据集列表的区别:这里是运营挑选过、带景区归属的那一批。",
        req=[
            P("connection_id", "string", False, "c1f2e8a0", "按连接筛"),
            P("kind", "string", False, "dashboard", "dashboard 仪表板 / dataset 数据集"),
            P("scenic_id", "string", False, "PFTSCA01002434", "只看某景区(集团级资产始终一并返回)"),
        ],
        resp=[
            R("data.items", "array", "[…]", "资产清单"),
            R("data.items[].id", "string", "a7c2e1f0", "资产 ID:target 里填 {\"asset_id\": \"…\"}"),
            R("data.items[].kind", "string", "dashboard", "dashboard / dataset"),
            R("data.items[].name", "string", "票务大盘", "名称"),
            R("data.items[].ref_id", "string", "PAGE-ID-…", "Quick BI 侧标识:仪表板是 pageId,数据集是 cubeId"),
            R("data.items[].scenic_id", "string", "PFTSCA01002434", "归属景区;空 = 集团级"),
        ],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/assets?kind=dashboard' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "items": [ { "id": "a7c2e1f0", "kind": "dashboard", "name": "票务大盘",
                         "ref_id": "PAGE-ID-FROM-QUICKBI", "scenic_id": "PFTSCA01002434" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="基础-连接列表", uri="/v1/quickbi/connections", method="GET",
        content_type="—(GET 无请求体)",
        summary="这把 Key 能用哪些 Quick BI 连接(一套连接 = 一套访问凭证 = 一个数据范围)。",
        req=[],
        resp=[
            R("data.items", "array", "[…]", "连接清单"),
            R("data.items[].id", "string", "c1f2e8a0", "连接 ID:各接口的 connection_id"),
            R("data.items[].name", "string", "集团 Quick BI", "连接名称"),
            R("data.items[].server_domain", "string", "https://bi.aliyun.com", "Quick BI 服务地址"),
            R("data.items[].is_default", "bool", "true", "是否默认连接(不传 connection_id 时用它)"),
            R("data.items[].default_locale", "string", "zh_CN", "默认语种"),
        ],
        notes=['返回里<b>不含任何密钥</b>:api_secret 与 user_token 加密存储,任何接口都不回显。'],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/connections' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "items": [ { "id": "c1f2e8a0", "name": "集团 Quick BI",
                         "server_domain": "https://bi.aliyun.com",
                         "is_default": true, "default_locale": "zh_CN" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="基础-能力总览", uri="/v1/quickbi/capabilities", method="GET",
        content_type="—(GET 无请求体)",
        summary="平台支持哪些能力、各自的限制(文件大小、图表行数、报告超时)。接入前对一遍口径用。",
        req=[],
        resp=[
            R("data.capabilities", "array", "[…]", "能力清单(key / label / 说明)"),
            R("data.scenarios", "array", "[…]", "典型场景(固定问题 / 对话 / 报告)"),
            R("data.limits", "object", "{…}", "限制:文件大小、单图最大行数、报告最长等待等"),
        ],
        curl=f"""curl --location --request GET '{HOST}/v1/quickbi/capabilities' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "capabilities": [
      { "key": "dataset_qa", "label": "问数 · 数据集", "desc": "对数据集提问,不选数据集时自动智能选表" },
      { "key": "insight", "label": "解读", "desc": "仪表板快照 / 上传文件的趋势、异常、归因解读" }
    ],
    "scenarios": [ { "key": "fixed", "label": "固定问题", "desc": "预置问题 × 能力 × 目标数据,一键/批量执行" } ],
    "limits": { "max_file_mb": 5, "max_rows_per_chart": 1000, "report_max_wait_seconds": 1800 }
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="LLM-发起对话(OpenAI 兼容)", uri="/v1/chat/completions", method="POST",
        summary="把一句话发给平台里配置好的 Agent,拿回答。请求/响应形状与 OpenAI 兼容,现成的 SDK 改个 base_url 就能用。",
        req=[
            P("agent_id", "string", True, "agt_3f2a9c", "调用哪个 Agent(平台扩展字段,放在 body 顶层)"),
            P("messages", "array", True, '[{"role":"user","content":"本月客流"}]', "对话消息,取最后一条 user 消息作为提问"),
            P("messages[].role", "string", True, "user", "user / assistant / system"),
            P("messages[].content", "string", True, "本月客流如何", "消息正文"),
            P("stream", "bool", False, "false", "true = SSE 流式;默认 false(等跑完一次性返回)"),
            P("user", "string", False, "u_10086", "末端用户标识,用于成本归属"),
        ],
        resp=[
            R("data.id", "string", "chatcmpl-9f2c…", "本次补全 ID"),
            R("data.object", "string", "chat.completion", "固定值"),
            R("data.created", "int", "1769812345", "Unix 时间戳(秒)"),
            R("data.model", "string", "agt_3f2a9c", "实际执行的 agent_id"),
            R("data.choices", "array", "[…]", "回答列表(当前固定 1 条)"),
            R("data.choices[].message.content", "string", "本月累计客流 12.3 万人次…", "回答正文"),
            R("data.choices[].finish_reason", "string", "stop", "stop 正常结束 / error 失败 / content_filter 被取消"),
            R("data.usage.total_tokens", "int", "484", "合计 token,按它计费"),
        ],
        notes=['非流式是<b>阻塞</b>的:平台建任务后等它跑完再返回,最长等 300 秒。'
               '长问题(尤其是要查数据的)建议用 <code>"stream": true</code>,否则客户端超时会比服务端先到。'],
        curl=f"""curl --location --request POST '{HOST}/v1/chat/completions' \\
--header 'Authorization: Bearer {KEY}' \\
--header 'Content-Type: application/json' \\
--data-raw '{{
  "agent_id": "agt_3f2a9c",
  "messages": [{{ "role": "user", "content": "本月客流如何" }}],
  "stream": false
}}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": {
    "id": "chatcmpl-9f2c7b1d4e5a6c8d9e0f", "object": "chat.completion",
    "created": 1769812345, "model": "agt_3f2a9c",
    "choices": [ { "index": 0,
                   "message": { "role": "assistant", "content": "本月累计客流 12.3 万人次,同比 +8.6%…" },
                   "finish_reason": "stop" } ],
    "usage": { "prompt_tokens": 356, "completion_tokens": 128, "total_tokens": 484 }
  },
  "trace_id": "b6289ec8a1f24c37"
}"""),

    Api(
        title="LLM-可用 Agent 列表", uri="/v1/models", method="GET",
        content_type="—(GET 无请求体)",
        summary="这把 Key 能调哪些 Agent。返回形状对齐 OpenAI 的 models 接口,id 就是 agent_id。",
        req=[],
        resp=[
            R("data.object", "string", "list", "固定值"),
            R("data.data", "array", "[…]", "可调用的 Agent 列表"),
            R("data.data[].id", "string", "agt_3f2a9c", "agent_id,直接填进对话接口"),
            R("data.data[].object", "string", "model", "固定值"),
        ],
        notes=['返回的是这把 Key <b>被限定</b>的 Agent 列表。如果 Key 没做限定,这里是空数组 —— '
               '不代表没有 Agent 可用,而是不限,具体 agent_id 找管理员要。'],
        curl=f"""curl --location --request GET '{HOST}/v1/models' \\
--header 'Authorization: Bearer {KEY}'""",
        resp_json="""{
  "code": 0, "message": "success",
  "data": { "object": "list", "data": [ { "id": "agt_3f2a9c", "object": "model" } ] },
  "trace_id": "b6289ec8a1f24c37"
}"""),
])


CHAPTERS = [START, APP, ADMIN, QBI]


def main() -> None:
    html_text = render(CHAPTERS)
    # 兜底体检:公开页面绝不能带真实密钥
    leaked = re.findall(r"\b[0-9a-f]{32}\b", html_text)
    assert not leaked, f"文档里出现疑似密钥:{leaked[:3]}"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_text, encoding="utf-8")
    apis = sum(1 for ch in CHAPTERS for it in ch.items if isinstance(it, Api))
    print(f"已生成 {OUT}({len(CHAPTERS)} 章 / {apis} 个接口 / {len(html_text)//1024} KB)")


if __name__ == "__main__":
    main()
