# -*- coding: utf-8 -*-
"""Markdown 接口文档 → 可调用的工具定义。

解析的是**运维手上那份文档原样**,不是另发明一种格式。认的结构:

    # 1.17 BI报表配置信息              ← 标题(工具名)
    ## 1.17.1 API概览                  ← URI / 请求方式 / 认证方式
    ## 1.17.2 请求参数                 ← 表格:序号 | 字段 | 类型 | 必传 | 示例 | 注释
    ## 1.17.3 返回参数                 ← 表格:序号 | 字段 | 类型 | 示例 | 注释
    ## 1.17.4 示例                     ← curl(取 header)+ 返回示例 json(取数据体位置)

小节标题只按**关键词**认(「概览 / 请求参数 / 返回参数 / 示例」),编号随便写;
表头列名也按关键词认(「字段 / 类型 / 必传 / 示例 / 注释」),多一列少一列都不影响。
这样不同人写的文档都能进来 —— 要求所有人先统一格式,等于这个功能不会被用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# 类型词 → JSON Schema 类型。文档里怎么写的都有,这里尽量认全
_TYPE_MAP = {
    "string": "string", "str": "string", "text": "string", "字符串": "string",
    "int": "integer", "integer": "integer", "long": "integer", "整数": "integer",
    "number": "number", "float": "number", "double": "number", "decimal": "number", "数字": "number",
    "bool": "boolean", "boolean": "boolean", "布尔": "boolean",
    "array": "array", "list": "array", "数组": "array",
    "object": "object", "dict": "object", "map": "object", "对象": "object",
}
_TRUE_WORDS = {"是", "y", "yes", "true", "必传", "必填", "required", "1"}


class ApiDocParseError(ValueError):
    """解析失败。message 里必须说清**哪一节没看懂**,而不是一句「格式错误」。"""


@dataclass
class ApiField:
    """一个字段(请求参数或返回参数)。"""

    name: str                       # 原样字段名(可能带 data[].xxx 这种路径)
    type: str = "string"            # JSON Schema 类型
    required: bool = False
    example: str = ""
    desc: str = ""

    @property
    def path(self) -> list[str]:
        """返回参数里 `data[].profile[].uri` 这种路径,拆成段供结果层定位数据体。"""
        return [p for p in re.split(r"\.|\[\]", self.name) if p]

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "required": self.required,
                "example": self.example, "desc": self.desc}

    def to_response_field(self) -> dict:
        """响应字段说明的存储结构(与编辑页 ResponseSchemaEditor 一致)。
        单位文档里没有,留空让人补 —— 猜一个单位比空着更危险。"""
        return {"path": self.name, "type": self.type, "unit": "",
                "desc": self.desc, "example": self.example}


@dataclass
class ApiDoc:
    """一份接口文档解析出来的全部内容。"""

    title: str = ""
    uri: str = ""
    method: str = "POST"
    auth: str = ""
    summary: str = ""
    request_fields: list[ApiField] = field(default_factory=list)
    response_fields: list[ApiField] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    sample_response: Any = None
    base_url: str = ""
    markdown: str = ""
    warnings: list[str] = field(default_factory=list)

    # ── 派生:给模型看的请求 schema ──────────────────────────────────
    def request_schema(self) -> dict:
        """请求参数表 → JSON Schema。

        系统变量(scenic_id 之类)照样列进去:执行时沙箱会用真实值覆盖模型填的,
        但让模型知道这个接口按景区取数,它才不会去问用户「要查哪个景区」。
        """
        props: dict[str, dict] = {}
        required: list[str] = []
        for f in self.request_fields:
            if not f.name or "[]" in f.name or "." in f.name:
                continue                       # 嵌套请求参数交给 desc 说明,不进顶层 schema
            spec: dict[str, Any] = {"type": f.type}
            desc = f.desc or ""
            if f.example:
                desc = f"{desc}(示例:{f.example})" if desc else f"示例:{f.example}"
                spec["examples"] = [f.example]
            if desc:
                spec["description"] = desc
            props[f.name] = spec
            if f.required:
                required.append(f.name)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema

    def data_path(self) -> list[str]:
        """数据体在返回结构里的位置,如 ["data"] / ["data", "list"]。

        结果压缩层要先找到「那一堆行」在哪儿,才能去筛选和统计;
        找不到就整个返回体当结果,那样很快就会把模型的上下文撑爆。
        """
        for f in self.response_fields:
            segs = f.path
            if len(segs) >= 1 and f.type == "array" and segs[0] not in ("code", "msg", "trace_id", "status"):
                return segs[:1] if len(segs) == 1 else segs[:1]
        # 没有明确的数组字段:按常见约定猜一层
        names = {f.path[0] for f in self.response_fields if f.path}
        for guess in ("data", "result", "rows", "list", "items"):
            if guess in names:
                return [guess]
        return []

    def to_type_config(self) -> dict:
        """直接用作 http 工具的 type_config。"""
        return {
            "method": self.method,
            "url": (self.base_url.rstrip("/") + self.uri) if self.base_url and self.uri.startswith("/") else (self.uri or self.base_url),
            "headers": dict(self.headers),
            "params_schema": self.request_schema(),
            "allow_override": False,
            # 给结果压缩层:数据体在哪、每个字段是什么
            "response_schema": {
                "data_path": self.data_path(),
                # 存成工具编辑页「响应字段说明」的同一结构(path/type/unit/desc)。
                # 以前存的是 to_dict() 的 name —— 详情页、编辑页、给模型的响应说明
                # 都按 path 读,于是字段名整列是空的,模型也从没拿到过这份说明。
                "fields": [f.to_response_field() for f in self.response_fields],
                "sample": self.sample_response,
            },
            # 原文留档:改工具时人看的是这份,不是被翻译过的 schema
            "api_doc_markdown": self.markdown,
        }

    def to_dict(self) -> dict:
        return {"title": self.title, "uri": self.uri, "method": self.method, "auth": self.auth,
                "summary": self.summary, "base_url": self.base_url,
                "headers": self.headers,
                "request_fields": [f.to_dict() for f in self.request_fields],
                "response_fields": [f.to_dict() for f in self.response_fields],
                "data_path": self.data_path(), "sample_response": self.sample_response,
                "warnings": self.warnings}


# ── 解析细节 ────────────────────────────────────────────────────────

# Markdown 转义(`\-` `\.` `\(` …)。飞书/语雀导出的文档里到处都是,
# 不还原的话工具名会变成「\.1 票务\-今日客流统计」,字段说明里也全是反斜杠。
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
_HTML_TAG = re.compile(r"<[^>]{1,20}>")


def _unescape(text: str) -> str:
    return _MD_ESCAPE.sub(r"\1", text or "")


def _clean_name(text: str) -> str:
    """字段名:还原转义、去掉 HTML 残留(`startTime<br>` 这种)。

    表格单元格里换行常被导出成 `<br>`,不去掉的话参数名就叫 `startTime<br>` ——
    模型照着它传参,接口收到一个它不认识的字段,真正的 startTime 缺失,
    回一句「参数错误」,看起来像接口坏了。
    """
    return _HTML_TAG.sub("", _unescape(text)).strip()


# ── 凭证识别 ─────────────────────────────────────────────────────────
#
# **curl 示例里的认证头几乎总是真实密钥**(文档作者复制的是能跑的命令)。
# 原先这里原样存进工具的 headers,同时整份文档原文也存进 api_doc_markdown ——
# 一份文档导入一次,密钥就以明文进了库,而且在工具详情、调试导出、版本快照里
# 到处都能看到。这里改成:认出凭证类请求头 → 换成占位符 → 原文里的明文一并抹掉。
_CRED_HEADER = re.compile(r"(?:^|[-_])(?:[a-z]*key|token|secret|authorization|auth|password|passwd|signature|sign)$",
                          re.I)
# 平台级共用密钥:按请求头名映射到服务端配置里的一个命名密钥。
# 这类 key 是整个平台共用的,不是某个用户的,不能走 {{credential}}
# (那个按用户 × 工具存,拿来放平台 key 等于每个用户要对每个工具各录一遍)。
PLATFORM_SECRET_HEADERS: dict[str, str] = {"xpftkey": "data_platform_key"}
# 文档里明文出现、但 --header 正则没抓到的形态(引号缺失的 curl 就是这样)
_BARE_SECRET = re.compile(r"(?i)\b(xpftkey|[\w-]*api[_-]?key|x-api-key|authorization)\s*[:=]\s*"
                          r"['\"]?(?:Bearer\s+)?([A-Za-z0-9_\-\.]{12,})")


def is_credential_header(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in PLATFORM_SECRET_HEADERS or bool(_CRED_HEADER.search(n))


def placeholder_for(header_name: str) -> str:
    key = PLATFORM_SECRET_HEADERS.get((header_name or "").strip().lower())
    return "{{" + key + "}}" if key else "{{credential}}"


def _redact(doc: "ApiDoc") -> None:
    """把凭证从 headers 与原文里拿掉。只改这份解析结果,不碰调用方的输入。"""
    secrets: set[str] = set()
    for k in list(doc.headers):
        v = doc.headers[k]
        if is_credential_header(k) and v and "{{" not in v:
            secrets.add(v.split()[-1])          # "Bearer xxx" 只取令牌本身
            doc.headers[k] = placeholder_for(k)
            where = ("服务端配置 tool_center.secrets." + PLATFORM_SECRET_HEADERS[k.lower()]
                     if k.lower() in PLATFORM_SECRET_HEADERS else "工具详情页的「凭证」")
            doc.warnings.append(
                f"请求头 `{k}` 是凭证,已替换为占位 `{doc.headers[k]}`,"
                f"真实值请配置在{where}(不会存进工具配置)")
    for m in _BARE_SECRET.finditer(doc.markdown or ""):
        secrets.add(m.group(2))
    for sec in sorted(secrets, key=len, reverse=True):
        if len(sec) >= 8:
            doc.markdown = doc.markdown.replace(sec, "***")


def _norm_type(raw: str) -> str:
    t = (raw or "").strip().lower()
    t = re.sub(r"[\(（].*?[\)）]", "", t).strip()
    for key, val in _TYPE_MAP.items():
        if t == key or t.startswith(key):
            return val
    return "string"


def _split_row(line: str) -> list[str]:
    """拆一行表格。**首尾各只去一个竖线**,空单元格要保留。

    以前用的是 strip("|"),会把行首**所有**竖线都吃掉:子字段行的序号列是空的
    (`||data[].scenicId|string|…|景区ID|`),空单元格被吃掉后整行左移一列 ——
    字段名变成了「string」,类型变成了示例值,说明跑到了示例里。
    单元格里转义的竖线(`\|`)不当分隔符。
    """
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|") and not t.endswith("\\|"):
        t = t[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", t)]


def _is_sep_row(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())) and "-" in line


def _parse_table(lines: list[str]) -> list[dict]:
    """把 md 表格解析成 [{表头: 值}]。表头按关键词归一,所以列序无所谓。

    **多级字段列**:嵌套字段常见两种写法 ——
      `|序号|字段||类型|…` + `|||hour|string|…`      (字段后面跟一个空表头列放子字段)
      `|序号|字段|字段|字段|类型|…` + `||||boxOfficeAmount|…` (每一级一列)
    「字段」列和紧跟其后的空表头 / 重复「字段」列都算名称列,名字落在第几列就是第几层。
    以前只认第一个「字段」列:子字段那一格是空的,整行被当成无名行丢掉 ——
    票务类 15 个接口的返回字段因此一个都没进响应说明(文档 34 行,解析出 3 行)。
    """
    rows: list[dict] = []
    header: list[str] | None = None
    name_cols: list[int] = []
    for line in lines:
        if not line.strip().startswith("|"):
            if header is not None:
                header, name_cols = None, []      # 表格结束
            continue
        if _is_sep_row(line):
            continue
        cells = _split_row(line)
        if header is None:
            header = [_norm_header(c) for c in cells]
            name_cols = []
            if "field" in header:
                i = header.index("field")
                while i < len(header) and (header[i] == "field" or header[i] == "_"):
                    name_cols.append(i)
                    i += 1
            continue
        row = {header[i]: cells[i] for i in range(min(len(header), len(cells)))
               if i not in name_cols}
        if name_cols:
            depth, name = 0, ""
            for d, ci in enumerate(name_cols):
                if ci < len(cells) and cells[ci].strip():
                    depth, name = d, cells[ci]
                    break
            row["field"], row["_depth"] = name, depth
        rows.append(row)
    return rows


def _norm_header(cell: str) -> str:
    c = (cell or "").strip().lower()
    if "字段" in c or "参数" in c or "name" in c:
        return "field"
    if "类型" in c or "type" in c:
        return "type"
    if "必传" in c or "必填" in c or "required" in c:
        return "required"
    if "示例" in c or "example" in c or "样例" in c:
        return "example"
    if "注释" in c or "说明" in c or "描述" in c or "desc" in c or "备注" in c:
        return "desc"
    if "序号" in c or c in ("#", "no", "序"):
        return "idx"
    return c or "_"


def _fields_from_rows(rows: list[dict], *, with_required: bool) -> list[ApiField]:
    """行 → 字段。带层级的行(见 _parse_table)按「最近一个上一层的行」拼成完整路径:
    父字段是数组 → `data[].hour`;是对象 → `data.max.avgOrderValue`。"""
    out: list[ApiField] = []
    stack: list[tuple[int, str, str]] = []          # 已见过的行:(层级, 完整路径, 类型)
    for r in rows:
        name = _clean_name((r.get("field") or "").strip().strip("`"))
        if not name or name in ("字段", "参数"):
            continue
        depth = int(r.get("_depth") or 0)
        typ = _norm_type(r.get("type", ""))
        full = name
        # 父字段 = 往前最近的、层级更浅的**数组/对象**行。不能按栈弹:文档里常把 msg 这种
        # 同层标量行夹在 data 和它的子字段之间(票务-分时:data → msg → hour…),
        # 按栈会把 data 弹掉(子字段成了无父的顶层),按「最近一行」又会拼出 msg.hour。
        parent_row = next((x for x in reversed(stack)
                           if x[0] < depth and x[2] in ("array", "object")), None)
        if depth > 0 and parent_row and "." not in name and "[]" not in name:
            _, parent, ptype = parent_row
            full = f"{parent}[].{name}" if ptype == "array" else f"{parent}.{name}"
        stack.append((depth, full, typ))
        out.append(ApiField(
            name=full,
            type=typ,
            required=(r.get("required", "").strip().lower() in _TRUE_WORDS) if with_required else False,
            example=_HTML_TAG.sub(" ", _unescape(r.get("example") or "")).strip(),
            desc=_HTML_TAG.sub(" ", _unescape(r.get("desc") or "")).strip(),
        ))
    return out


def _sections(md: str) -> list[tuple[str, list[str]]]:
    """按 ## / ### 切小节,返回 [(标题, 该节的行)]。节前的内容归到标题 ""。"""
    out: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur: list[str] = []
    for line in md.splitlines():
        if re.match(r"^#{2,4}\s+", line):
            out.append((cur_title, cur))
            cur_title, cur = re.sub(r"^#{2,4}\s+", "", line).strip(), []
        else:
            cur.append(line)
    out.append((cur_title, cur))
    return out


def _code_blocks(lines: list[str]) -> list[tuple[str, str]]:
    """取出 ```lang ... ``` 代码块,返回 [(lang, 内容)]。"""
    out: list[tuple[str, str]] = []
    lang = ""
    buf: list[str] = []
    inside = False
    for line in lines:
        m = re.match(r"^\s*```(\w*)", line)
        if m and not inside:
            inside, lang, buf = True, (m.group(1) or "").lower(), []
            continue
        if inside and re.match(r"^\s*```", line):
            out.append((lang, "\n".join(buf)))
            inside = False
            continue
        if inside:
            buf.append(line)
    return out


# 引号可有可无。文档里同一份导出,有的 curl 带引号、有的不带(复制时被编辑器吃掉了),
# 只认带引号的那种,不带的就解析成「没有地址、没有认证头」—— 工具照样建得出来、
# 列表里看着好好的,一调用才失败。现场这份 30 个接口里有 5 个是这样,
# 其中一个恰好是「按景区取供应商 ID」,它一坏,另外 10 个要供应商 ID 的接口跟着全坏。
_HEADER_RE = re.compile(r"""(?:--header|-H)\s+['"]?([A-Za-z0-9_\-]+)\s*:\s*([^'"\\\n]*?)\s*['"]?\s*(?:\\\s*$|$|(?=\s--))""", re.M)
_URL_RE = re.compile(r"""(?:--location\s+)?(?:--request\s+\w+\s+|-X\s+\w+\s+)?['"]?(https?://[^\s'"\\]+)""")
_METHOD_RE = re.compile(r"(?:--request|-X)\s+(\w+)")


def _parse_curl(curl: str) -> tuple[str, str, dict[str, str], Any]:
    """从 curl 示例里取 url / method / headers / body。

    curl 是文档里**最可信**的一节:它是能直接跑的,而上面的表格常常漏字段。
    认证 header(xpftkey / Authorization 这类)只在这里出现,不从这儿取就得再问人一遍。
    """
    url = (_URL_RE.search(curl).group(1) if _URL_RE.search(curl) else "")
    method = (_METHOD_RE.search(curl).group(1).upper() if _METHOD_RE.search(curl) else "")
    headers = {k.strip(): v.strip() for k, v in _HEADER_RE.findall(curl)}
    body: Any = None
    m = re.search(r"--data(?:-raw|-binary)?\s+'(.*?)'\s*(?:\\\s*\n|$)", curl, re.S)
    if m:
        try:
            body = json.loads(m.group(1))
        except (ValueError, TypeError):
            body = m.group(1)
    return url, method, headers, body


def from_markdown(md: str, *, default_base_url: str = "") -> ApiDoc:
    """解析一份 Markdown 接口文档。看不懂哪一节就说哪一节。"""
    if not (md or "").strip():
        raise ApiDocParseError("接口文档是空的:请把那份 Markdown 原样贴进来")

    doc = ApiDoc(markdown=md, base_url=default_base_url)
    title_m = re.search(r"^#\s+(.+)$", md, re.M)
    if title_m:
        # 「# 1.17 BI报表配置信息」→ 去掉前面的编号。
        # **先还原转义再去编号**:导出的标题是「1\.17 BI\-报表」,按原样去数字只去得掉
        # 开头那个「1」,剩下「\.17 BI\-报表」当工具名。
        doc.title = re.sub(r"^[\d.]+\s*", "", _unescape(title_m.group(1).strip())).strip()

    req_rows: list[dict] = []
    resp_rows: list[dict] = []
    for title, lines in _sections(md):
        t = title.lower()
        body = "\n".join(lines)
        if "概览" in title or "overview" in t:
            for line in lines:
                # 加不加粗、带不带反引号、中英文冒号,都认 —— 这一节各人写法差异最大,
                # 卡在格式上等于这个功能不会被用
                if m := re.search(r"\**\s*(?:URI|接口地址|请求地址|路径|path|url)\s*\**\s*[::]\s*`?([^`\s]+)`?",
                                  line, re.I):
                    doc.uri = m.group(1).strip()
                elif m := re.search(r"\**\s*(?:请求方式|请求方法|method)\s*\**\s*[::]\s*`?(\w+)`?", line, re.I):
                    doc.method = m.group(1).upper()
                elif m := re.search(r"\**\s*(?:认证方式|鉴权方式|auth)\s*\**\s*[::]\s*(.+)$", line, re.I):
                    doc.auth = m.group(1).strip().strip("*").strip()
        elif "请求参数" in title or "request" in t:
            req_rows = _parse_table(lines)
        elif "返回参数" in title or "响应参数" in title or "response" in t:
            resp_rows = _parse_table(lines)
        elif "示例" in title or "example" in t:
            for lang, code in _code_blocks(lines):
                if lang in ("bash", "shell", "sh", "curl") or code.strip().startswith("curl"):
                    url, method, headers, _body = _parse_curl(code)
                    if url:
                        doc.base_url = re.sub(r"(https?://[^/]+).*", r"\1", url)
                        if not doc.uri:
                            doc.uri = url[len(doc.base_url):] or url
                    if method:
                        doc.method = method
                    # Content-Type 由请求方式决定,不必存;认证类 header 必须留
                    doc.headers.update({k: v for k, v in headers.items()
                                        if k.lower() != "content-type"})
                elif lang == "json" or code.strip().startswith("{"):
                    try:
                        doc.sample_response = json.loads(code)
                    except (ValueError, TypeError):
                        doc.warnings.append("返回示例不是合法 JSON,已跳过(不影响调用,只是少了样例)")
        elif not title and not doc.summary:
            plain = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", "|", "`"))]
            if plain:
                doc.summary = plain[0][:200]

    doc.request_fields = _fields_from_rows(req_rows, with_required=True)
    doc.response_fields = _fields_from_rows(resp_rows, with_required=False)

    if not doc.uri and not doc.base_url:
        raise ApiDocParseError(
            "没找到接口地址:请在「API概览」里写 `- **URI**: /v1/...`,"
            "或者在「示例」里贴一段能跑的 curl(两者有其一即可)")
    if not doc.request_fields and doc.method in ("POST", "PUT", "PATCH"):
        doc.warnings.append(
            "没解析到请求参数表 —— 如果这个接口确实不需要参数就忽略;"
            "否则检查「请求参数」那一节的表头是不是「字段 / 类型 / 必传 / 示例 / 注释」")
    if not doc.response_fields:
        doc.warnings.append(
            "没解析到返回参数表:结果里哪一列是什么就无从判断,"
            "大结果集只能原样截断而不能按列筛选统计。建议补上「返回参数」表")
    if not doc.title:
        doc.title = doc.uri.rsplit("/", 1)[-1] or "未命名接口"
    doc.summary = _unescape(doc.summary)
    _redact(doc)
    return doc


def parse_fields(md: str, *, kind: str = "request") -> dict:
    """解析**一段** Markdown 字段表(不是整份文档)。

    工具编辑页的「参数定义」「响应字段说明」用它:运维手上是一份接口文档,
    最自然的操作是把那一节整段贴进来,而不是在表格里一行一行点「加字段」——
    嵌套结构(data[].profile[].uri)那种,点到第十行就没人愿意继续了。

    宽容一点:贴一整份文档也认(自动挑出对应那一节),只贴一张表也认。
    kind=request 时读「必传」列,response 不读(返回参数表没有这一列)。
    """
    text = md or ""
    if not text.strip():
        return {"fields": [], "warnings": ["内容为空"]}

    lines = text.splitlines()
    # 贴了整份文档:挑出对应的那一节;没有分节就当作整段都是表
    want = ("请求参数", "请求体", "入参", "body", "request") if kind == "request" \
        else ("返回参数", "响应参数", "返回体", "出参", "response", "返回结果")
    picked: list[str] | None = None
    for title, body in _sections(text):
        t = (title or "").lower()
        if any(w in title or w in t for w in want):
            picked = body
            break
    rows = _parse_table(picked if picked is not None else lines)
    fields = _fields_from_rows(rows, with_required=(kind == "request"))

    warnings: list[str] = []
    if not fields:
        warnings.append(
            "没解析出任何字段。表格至少要有「字段」和「类型」两列"
            "(列名认中英文:字段/参数/name、类型/type、必传/必填/required、"
            "示例/example、注释/说明/desc),列的顺序随意、多一列少一列都行。")
    return {"fields": [f.to_dict() for f in fields], "warnings": warnings}


def to_markdown(doc: ApiDoc) -> str:
    """反向生成一份规范的 md(给「从已有工具导出文档」用)。"""
    out = [f"# {doc.title}", ""]
    out += ["## API概览", "",
            f"- **URI**: `{doc.uri}`",
            f"- **请求方式**: {doc.method}",
            f"- **认证方式**: {doc.auth or '无'}", ""]
    if doc.request_fields:
        out += ["## 请求参数", "",
                "| 序号 | 字段 | 类型 | 必传 | 示例 | 注释 |",
                "| --- | --- | --- | --- | --- | --- |"]
        for i, f in enumerate(doc.request_fields, 1):
            out.append(f"| {i} | {f.name} | {f.type} | {'是' if f.required else '否'} | {f.example} | {f.desc} |")
        out.append("")
    if doc.response_fields:
        out += ["## 返回参数", "",
                "| 序号 | 字段 | 类型 | 示例 | 注释 |",
                "| --- | --- | --- | --- | --- |"]
        for i, f in enumerate(doc.response_fields, 1):
            out.append(f"| {i} | {f.name} | {f.type} | {f.example} | {f.desc} |")
        out.append("")
    return "\n".join(out)


def annotate_params(type_config: dict, annotations: dict[str, dict]) -> dict:
    """把 x-param-source / x-ctx-default 这类扩展声明并进参数 schema。

    **只接受 x- 开头的键**:这个入口是给「系统怎么补参」用的,不能顺手把参数类型、
    必填改掉 —— 那些该以接口文档为准,在这里被改写的话,文档和工具就对不上了。
    声明了不存在的参数直接报错,不静默忽略:写错参数名的声明等于没写,
    而那个参数会照旧交给模型去编。

    服务端导入接口与同步脚本共用这一份 —— 两份实现迟早对不上,
    表现是「脚本预览的和实际建出来的不一样」。
    """
    if not annotations:
        return type_config
    schema = dict(type_config.get("params_schema") or {})
    props = dict(schema.get("properties") or {})
    for name, ext in annotations.items():
        if name not in props:
            raise ApiDocParseError(
                f"参数扩展声明里的「{name}」在接口参数里不存在(现有:{'、'.join(props) or '无'})")
        bad = [k for k in (ext or {}) if not str(k).startswith("x-")]
        if bad:
            raise ApiDocParseError(f"参数「{name}」的扩展声明只能用 x- 开头的键,这些不行:{bad}")
        props[name] = {**props[name], **(ext or {})}
    schema["properties"] = props
    return {**type_config, "params_schema": schema}
