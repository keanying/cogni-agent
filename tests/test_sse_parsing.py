"""
SSE 分隔符回归测试。

这条 bug 杀伤力大但极其隐蔽:sse-starlette 用 CRLF 空行(\r\n\r\n)分隔事件,而前端
早期按 LF 空行(\n\n)切 —— 字节序列 \r \n \r \n 里**根本不存在 \n\n 子串**,于是一个
事件都切不出来,buffer 无限增长。表现为"流式输出完全没有,内容最后一次性蹦出来"
(最后那下是别的兜底路径补的)。

这里把契约固定住:服务端换 SSE 库或改分隔符时测试会失败,提醒同步检查前端解析器
(web/src/lib/api.ts::streamChat)。
"""
from __future__ import annotations

import re
from pathlib import Path

API_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "api.ts"

CRLF_BLANK = "\r\n\r\n"
LF_BLANK = "\n\n"


def _sep_line() -> str:
    src = API_TS.read_text(encoding="utf-8")
    return next((ln for ln in src.split("\n") if "const SEP" in ln), "")


def test_parser_covers_both_separators():
    """前端的分隔符正则必须同时覆盖 CRLF 与 LF 空行。"""
    line = _sep_line()
    assert line, "streamChat 里应当有一个 SEP 分隔符正则"
    # 源码形如: const SEP = /\r\n\r\n|\n\n|\r\r/;
    assert "|" in line, f"SEP 应覆盖多种分隔符,实际:{line.strip()}"
    assert line.count("\\r") >= 2, f"SEP 必须覆盖 CRLF 分隔符,实际:{line.strip()}"


def _code_only(src: str) -> str:
    """去掉注释再检查。文件里那段说明 bug 成因的注释**故意**写了错误写法,
    直接全文匹配会误报。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)          # 块注释
    return "\n".join(l for l in src.split("\n") if not l.strip().startswith(("*", "//")))


def test_parser_does_not_split_on_lf_only():
    code = _code_only(API_TS.read_text(encoding="utf-8"))
    assert 'buffer.split("\\n\\n")' not in code, "只按 LF 空行切分,在 CRLF 下一个事件都切不出来"


def test_parser_preserves_leading_whitespace():
    """data 行只能去掉紧跟冒号的一个空格,不能 trim():
    token 增量里的空格和缩进都是内容,trim 掉输出会粘成一团。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "data += line.slice(5).trim()" not in src, "不能对 data 行做 trim()"


def _parse(raw: str) -> list[tuple[str, str]]:
    """与前端等价的解析逻辑,用来验证真实报文能被正确切分。"""
    out: list[tuple[str, str]] = []
    buf = raw
    sep = re.compile(r"\r\n\r\n|\n\n|\r\r")
    while (m := sep.search(buf)) is not None:
        chunk, buf = buf[: m.start()], buf[m.end():]
        if not chunk.strip():
            continue
        event, data = "message", []
        for line in re.split(r"\r\n|\n|\r", chunk):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                v = line[5:]
                data.append(v[1:] if v.startswith(" ") else v)
        if data:
            out.append((event, "\n".join(data)))
    return out


def test_parses_real_crlf_stream():
    raw = (
        f'event: status{CRLF_BLANK[:2]}data: {{"status": "QUEUED"}}{CRLF_BLANK}'
        f'id: 1-0{CRLF_BLANK[:2]}event: token{CRLF_BLANK[:2]}data: {{"delta": "   缩进要保留"}}{CRLF_BLANK}'
        f'event: done{CRLF_BLANK[:2]}data: {{"answer": "ok"}}{CRLF_BLANK}'
    )
    events = _parse(raw)
    assert [e for e, _ in events] == ["status", "token", "done"]
    assert '"delta": "   缩进要保留"' in events[1][1], "data 行的前导空格必须保留"


def test_parses_lf_stream_too():
    """兼容只用 LF 空行的服务端实现。"""
    raw = (f'event: token{LF_BLANK[:1]}data: {{"delta": "a"}}{LF_BLANK}'
           f'event: done{LF_BLANK[:1]}data: {{}}{LF_BLANK}')
    assert [e for e, _ in _parse(raw)] == ["token", "done"]
