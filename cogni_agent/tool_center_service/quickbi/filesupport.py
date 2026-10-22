"""上传文件支持:校验、Excel/CSV → Markdown(小Q解读)、Markdown 表格 → CSV(报告引用)。"""
from __future__ import annotations

import csv
import io
import re
from pathlib import PurePath

MAX_FILE_BYTES = 5 * 1024 * 1024            # Quick BI OpenAPI 单文件上限
MAX_INSIGHT_CHARS = 100_000                  # dataInterpretationStream 数据上限(与官方 skill 一致)
QA_SUFFIXES = {".xls", ".xlsx", ".csv"}      # 小Q问数(copilot/parse)
REPORT_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".csv"}   # 小Q报告 uploadReferenceFile
INSIGHT_SUFFIXES = {".xls", ".xlsx", ".csv"}
# 小Q文档解析(document/upload 远程 OCR 支持的类型,与官方 skill 一致)
DOC_PARSE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
                      ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"}
ALL_SUFFIXES = QA_SUFFIXES | REPORT_SUFFIXES | DOC_PARSE_SUFFIXES

MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jp2": "image/jp2",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def suffix(name: str) -> str:
    return PurePath(name or "").suffix.lower()


def validate_upload(name: str, size: int) -> None:
    s = suffix(name)
    if s not in ALL_SUFFIXES:
        raise ValueError(f"不支持的文件类型 {s or '(无后缀)'},仅支持 {', '.join(sorted(ALL_SUFFIXES))}")
    if size <= 0:
        raise ValueError("文件为空")
    if size > MAX_FILE_BYTES:
        raise ValueError("文件超过 5MB 上限(Quick BI OpenAPI 限制)。请勿在本地裁剪后上传,如需更大文件请升级 Quick BI 版本")


def capabilities_for(name: str) -> list[str]:
    s = suffix(name)
    caps = []
    if s in QA_SUFFIXES:
        caps.append("file_qa")
    if s in INSIGHT_SUFFIXES:
        caps.append("insight")
    if s in REPORT_SUFFIXES:
        caps.append("report")
    if s in DOC_PARSE_SUFFIXES:
        caps.append("doc_parse")
    return caps


def _md_escape(v) -> str:
    return ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ").strip()


def rows_to_markdown(title: str, rows: list[list]) -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    header = [_md_escape(c) for c in rows[0]] + [""] * (width - len(rows[0]))
    lines = [f"## {title}\n", "| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    for r in rows[1:]:
        padded = [_md_escape(c) for c in r] + [""] * (width - len(r))
        lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines)


def _decode_text(content: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "replace")


def to_markdown(name: str, content: bytes) -> str:
    """xls/xlsx/csv → Markdown 表格(多 Sheet 合并)。"""
    s = suffix(name)
    parts: list[str] = []
    if s == ".csv":
        rows = list(csv.reader(io.StringIO(_decode_text(content))))
        md = rows_to_markdown(PurePath(name).stem, rows)
        if md:
            parts.append(md)
    elif s == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
            raise ValueError("解析 xlsx 需要安装 openpyxl(uv add openpyxl)") from exc
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                rows = [["" if c is None else c for c in row] for row in ws.iter_rows(values_only=True)]
                md = rows_to_markdown(ws.title, rows)
                if md:
                    parts.append(md)
        finally:
            wb.close()
    elif s == ".xls":
        try:
            import xlrd  # type: ignore
        except ImportError as exc:
            raise ValueError("解析 .xls(Excel 97-2003)需要安装 xlrd;建议另存为 .xlsx 后上传") from exc
        wb = xlrd.open_workbook(file_contents=content)
        for sheet in wb.sheets():
            rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
            md = rows_to_markdown(sheet.name, rows)
            if md:
                parts.append(md)
    else:
        raise ValueError(f"解读仅支持 xls/xlsx/csv,当前为 {s}")
    if not parts:
        raise ValueError("文件中没有可解读的数据")
    return "\n\n".join(parts)


_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


def markdown_tables(md: str) -> list[tuple[str, list[list[str]]]]:
    """从 Markdown 文本里抽出所有表格 → [(标题, 行)]。标题取表格前最近的标题行。"""
    tables: list[tuple[str, list[list[str]]]] = []
    title, cur = "", []
    for line in (md or "").splitlines():
        m = _TABLE_ROW.match(line)
        if m:
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", m.group(1))]
            if all(re.fullmatch(r":?-{2,}:?", c) or c == "" for c in cells) and any(cells):
                continue  # 分隔行
            cur.append([c.replace("\\|", "|") for c in cells])
            continue
        if cur:
            tables.append((title or f"表{len(tables) + 1}", cur))
            cur = []
        if line.lstrip().startswith("#"):
            title = line.lstrip("# ").strip()
    if cur:
        tables.append((title or f"表{len(tables) + 1}", cur))
    return tables


# 报告引用文件最多带几份(仪表板快照里的前 N 张表)
REPORT_REF_LIMIT = 5


def markdown_to_csv_files(md: str, *, base_name: str, limit: int = REPORT_REF_LIMIT) -> list[tuple[str, bytes]]:
    """仪表板快照 Markdown → 若干 CSV(每张表一个),用作小Q报告的引用文件。"""
    # 只有表头的表不要:快照偶尔整份都是空表头(运行 c431ec19),原样上传就是几份空 CSV,
    # 报告只能靠联网搜索或者编
    tables = [t for t in markdown_tables(md) if len(t[1] or []) > 1]
    return tables_to_csv_files(tables[:limit], base_name=base_name)


def tables_to_csv_files(tables, *, base_name: str) -> list[tuple[str, bytes]]:
    """[(标题, 行)] → [(文件名, CSV 字节)],报告引用文件用。"""
    out = []
    for i, (title, rows) in enumerate(tables, 1):
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        data = buf.getvalue().encode("utf-8-sig")
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", title)[:40] or f"table{i}"
        out.append((f"{base_name}_{i}_{safe}.csv", data))
    return out
