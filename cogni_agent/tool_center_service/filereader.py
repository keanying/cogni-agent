# -*- coding: utf-8 -*-
"""上传文件 → 智能体能用的东西。

**核心判断:表格类文件不该变成文本。**

一份 3000 行的 Excel 转成 Markdown 表格是四十多万字,塞不进上下文;截断之后模型看到的是
半张表,然后一本正经地基于半张表下结论。而把它变成一个**数据集句柄**,全部 sys_table_*
工具立刻就能用在它身上 —— 筛选、分组、统计、画图,和接口取回来的数据走完全同一条路。
所以这里的分工是:

    .xlsx/.xls/.csv   → 行(进数据留存,给句柄)   —— 之后用 sys_table_query / sys_chart 处理
    .docx/.pdf/.txt   → 文本(长了走摘要)         —— 文档本来就是给人读的
    .png/.jpg/…       → OCR 文本                 —— 图里的字,先认出来再说

每种解析都**单独降级**:缺哪个库就只报哪种格式不可用,并说清装什么 ——
而不是让整个文件工具挂掉,也不是抛一句 Python 的 ImportError 给模型看
(它会把那当成「这个工具坏了」,然后开始猜文件里有什么)。
"""
from __future__ import annotations

import csv
import io
from pathlib import PurePath
from typing import Any

from cogni_agent.common.logging import get_logger

log = get_logger("tool_center_service.filereader")

# 表格类:变成数据集句柄,交给 sys_table_* 处理
TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
# 文档类:变成文本
DOC_SUFFIXES = {".docx", ".pdf", ".txt", ".md", ".json", ".yaml", ".yml"}
# 图片类:OCR
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED = TABLE_SUFFIXES | DOC_SUFFIXES | IMAGE_SUFFIXES

# 单个文件上限。再大就不是「读一份文件」而是「导一批数据」了,那该走数据源接入,
# 不该让一次对话把几百兆嚼进内存。
MAX_FILE_BYTES = 20 * 1024 * 1024
# 表格最多读多少行:超过这个数,分析价值已经不在明细里了(该聚合),而内存是大家共用的
MAX_TABLE_ROWS = 50_000
# 文档最多返回多少字(再长由调用方走摘要)
MAX_DOC_CHARS = 200_000


class FileReadError(Exception):
    """解析失败。message 是**给模型看的**,所以必须说清是哪一种失败、下一步做什么。"""


def suffix(name: str) -> str:
    return PurePath(name or "").suffix.lower()


def kind_of(name: str) -> str:
    """表格 / 文档 / 图片 / 不支持。"""
    s = suffix(name)
    if s in TABLE_SUFFIXES:
        return "table"
    if s in DOC_SUFFIXES:
        return "doc"
    if s in IMAGE_SUFFIXES:
        return "image"
    return "unsupported"


def _need(module: str, pkg: str, what: str):
    """缺库时给一句人话,而不是把 ImportError 甩给模型。"""
    raise FileReadError(
        f"暂时读不了{what}:当前环境没有安装 {pkg}。"
        f"请联系运维安装后重试 —— **不要猜测文件内容**,也不要假装已经读过了")


# ── 表格 ────────────────────────────────────────────────────────────

def _decode(content: bytes) -> str:
    """文本解码。中文 CSV 现场最常见的就是 GBK,一律按 UTF-8 读会整篇变乱码 ——
    而乱码不会报错,模型会对着一堆问号认真分析。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def read_csv(content: bytes, *, delimiter: str | None = None) -> list[dict]:
    text = _decode(content)
    if delimiter is None:
        try:                                    # 让 csv 自己嗅分隔符(逗号/制表符/分号)
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise FileReadError("这个 CSV 没有数据行(可能只有表头,或者文件是空的)")
    return [{(k or "").strip(): v for k, v in r.items()} for r in rows[:MAX_TABLE_ROWS]]


def read_excel(content: bytes, *, sheet: str | None = None) -> tuple[list[dict], list[str]]:
    """读 xlsx。返回 (行, 全部 sheet 名)。

    多 sheet 时默认读第一个,并把所有 sheet 名一起回去 —— 不然模型不知道还有别的表,
    会把第一个 sheet 当成整个文件。
    """
    try:
        import openpyxl
    except ImportError:                         # pragma: no cover
        _need("openpyxl", "openpyxl", "Excel 文件")
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    names = list(wb.sheetnames)
    ws = wb[sheet] if sheet and sheet in names else wb[names[0]]
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if not header:
        raise FileReadError(f"工作表「{ws.title}」是空的")
    cols = [str(h).strip() if h is not None else f"列{i + 1}" for i, h in enumerate(header)]
    rows: list[dict] = []
    for r in it:
        if r is None or all(v is None for v in r):
            continue                            # 跳过整行空白(Excel 里非常常见)
        rows.append({c: v for c, v in zip(cols, r)})
        if len(rows) >= MAX_TABLE_ROWS:
            break
    wb.close()
    if not rows:
        raise FileReadError(f"工作表「{ws.title}」只有表头,没有数据行")
    return rows, names


# ── 文档 ────────────────────────────────────────────────────────────

def read_docx(content: bytes) -> str:
    """Word 正文 + 表格。表格转成 Markdown —— 合同/报表类 Word 的关键信息大半在表里,
    只抽段落等于把最该读的那部分丢了。"""
    try:
        import docx
    except ImportError:                         # pragma: no cover
        _need("docx", "python-docx", "Word 文件")
    d = docx.Document(io.BytesIO(content))
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for i, tbl in enumerate(d.tables, 1):
        rows = [[c.text.strip().replace("|", "\\|") for c in row.cells] for row in tbl.rows]
        if not rows:
            continue
        md = ["| " + " | ".join(rows[0]) + " |",
              "| " + " | ".join("---" for _ in rows[0]) + " |"]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        parts.append(f"\n【表格 {i}】\n" + "\n".join(md))
    text = "\n\n".join(parts)
    if not text.strip():
        raise FileReadError("这个 Word 文件里没有可提取的文字(可能整篇是图片,"
                            "如果是扫描件请改用图片格式上传走 OCR)")
    return text[:MAX_DOC_CHARS]


def read_pdf(content: bytes) -> str:
    """PDF 文字。按页标出页码 —— 引用时能说清「第几页」,而不是含糊地说「文件里提到」。

    三个后端按顺序试:pdfplumber(带表格) → pypdf(纯文字) → pdftotext(命令行)。
    任何一个在就能用,不为了一种格式绑死一个库。
    """
    pages: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                t = (page.extract_text() or "").strip()
                if t:
                    pages.append(f"【第 {i} 页】\n{t}")
    except ImportError:
        try:
            import pypdf

            for i, page in enumerate(pypdf.PdfReader(io.BytesIO(content)).pages, 1):
                t = (page.extract_text() or "").strip()
                if t:
                    pages.append(f"【第 {i} 页】\n{t}")
        except ImportError:                     # pragma: no cover
            _need("pypdf", "pypdf 或 pdfplumber", "PDF 文件")
    except Exception as exc:                    # noqa: BLE001
        raise FileReadError(f"PDF 解析失败:{type(exc).__name__}: {exc}") from exc
    text = "\n\n".join(pages)
    if not text.strip():
        raise FileReadError("这个 PDF 里没有可提取的文字 —— 多半是扫描件(整页都是图片)。"
                            "请把它转成图片上传,走 OCR 识别")
    return text[:MAX_DOC_CHARS]


def read_text(content: bytes) -> str:
    text = _decode(content)
    if not text.strip():
        raise FileReadError("文件是空的")
    return text[:MAX_DOC_CHARS]


# ── 图片 OCR ────────────────────────────────────────────────────────

def read_image_ocr(content: bytes, *, lang: str = "chi_sim+eng") -> str:
    """图片里的文字。

    lang 默认中英混排。**没装中文语言包时明说**,不要默默退回英文 ——
    退回英文的话,一张中文截图会认出一堆毫无意义的字母,而模型会认真分析那堆字母。
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:                         # pragma: no cover
        _need("pytesseract", "pytesseract 与 Pillow", "图片(OCR)")
    try:
        img = Image.open(io.BytesIO(content))
    except Exception as exc:                    # noqa: BLE001
        raise FileReadError(f"这不是一张能打开的图片:{exc}") from exc
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception:                           # noqa: BLE001 - 取不到就按请求的来
        have = set()
    if have:
        want = [p for p in lang.split("+") if p in have]
        if not want:
            raise FileReadError(
                f"OCR 引擎没有装所需语言包(需要 {lang},现有 {'、'.join(sorted(have)) or '无'})。"
                "中文识别请让运维安装 tesseract-ocr-chi-sim;"
                "**不要拿英文包硬认中文** —— 认出来的会是一堆无意义字母")
        lang = "+".join(want)
    text = pytesseract.image_to_string(img, lang=lang).strip()
    if not text:
        raise FileReadError("图里没认出文字。可能是纯图形(图表/照片),"
                            "也可能太模糊 —— 如果是图表截图,请改为提供原始数据")
    return text[:MAX_DOC_CHARS]


# ── 统一入口 ────────────────────────────────────────────────────────

def read(name: str, content: bytes, *, sheet: str | None = None,
         lang: str = "chi_sim+eng") -> dict:
    """读一个文件。返回 {"kind", ...}:

      kind="table" → {"rows", "sheets"}     交给数据留存,给模型一个句柄
      kind="doc"   → {"text"}               长了由调用方走摘要
      kind="image" → {"text"}               OCR 结果
    """
    if not content:
        raise FileReadError("文件是空的")
    if len(content) > MAX_FILE_BYTES:
        raise FileReadError(
            f"文件 {len(content) / 1048576:.1f}MB,超过 {MAX_FILE_BYTES // 1048576}MB 上限。"
            "这么大的量建议走数据源接入,而不是一次对话读进来")
    s, kind = suffix(name), kind_of(name)
    if kind == "unsupported":
        raise FileReadError(
            f"不支持的文件类型 {s or '(无后缀)'}。支持:"
            f"表格 {'/'.join(sorted(TABLE_SUFFIXES))}、"
            f"文档 {'/'.join(sorted(DOC_SUFFIXES))}、"
            f"图片 {'/'.join(sorted(IMAGE_SUFFIXES))}")
    if kind == "table":
        if s in (".csv", ".tsv"):
            rows = read_csv(content, delimiter="\t" if s == ".tsv" else None)
            return {"kind": "table", "rows": rows, "sheets": []}
        if s == ".xls":
            try:
                import xlrd                     # noqa: F401
            except ImportError:                 # pragma: no cover
                raise FileReadError(
                    "读不了老版 .xls(Excel 97-2003):当前环境没有安装 xlrd。"
                    "最省事的办法是请用户用 Excel 另存为 .xlsx 再上传") from None
        rows, sheets = read_excel(content, sheet=sheet)
        return {"kind": "table", "rows": rows, "sheets": sheets}
    if kind == "image":
        return {"kind": "image", "text": read_image_ocr(content, lang=lang)}
    if s == ".docx":
        return {"kind": "doc", "text": read_docx(content)}
    if s == ".pdf":
        return {"kind": "doc", "text": read_pdf(content)}
    return {"kind": "doc", "text": read_text(content)}
