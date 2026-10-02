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
ALL_SUFFIXES = QA_SUFFIXES | REPORT_SUFFIXES

MIME = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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


# ── 主题解读报告:按主题挑快照里的表 ─────────────────────────────────
#
# 报告引用文件最多带 REPORT_REF_LIMIT 份。以前是「快照里的前 5 张表」,不看主题:
# 景区只登记了一块大看板(客群 / 票务 / 舆情 / 渠道全在上面)时,五个主题拿到的
# 都是看板最前面那 5 张 —— 运行 dc25bb69「【主题解读】OTA直销」上传的是
# 本地客群占比 / 有孩家庭 / 年龄分布 / 人生阶段 / 星级酒店偏好,一张渠道图都没有,
# 报告只能拿别的主题的数硬写,卡片标题却是本主题。
# 现在按图表标题里的关键词挑本主题的表;一张都对不上(看板本来就是本主题专属、
# 图表名起得不规范)才退回原来的「前 N 张」。
REPORT_REF_LIMIT = 5
THEME_TABLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "客群": ("客群", "客流", "客源", "画像", "年龄", "性别", "男女", "人生阶段", "有孩", "有车", "家庭", "职业",
             "学历", "收入", "消费", "偏好", "本地", "省内", "省外", "省份", "城市", "出游", "距离",
             "职住", "居住", "工作地", "到访", "旅游", "TGI"),
    "票务": ("票", "销售", "销量", "收入", "营收", "金额", "订单", "退款", "入园", "核销", "产品"),
    "舆情": ("舆情", "评论", "评价", "口碑", "好评", "中评", "差评", "情绪", "情感", "满意", "词云",
             "负面", "正面", "正向", "负向", "总评", "得分", "评分", "关键词"),
    "OTA直销": ("OTA", "直销", "渠道", "分销", "携程", "美团", "飞猪", "同程", "去哪儿", "抖音",
               "核销", "客单价", "订单"),
}
THEME_PREFIX = "【主题解读】"
_THEME_NAME_TO_DOMAIN = {"客群": "客群", "票务": "票务", "舆情": "舆情", "OTA直销": "OTA直销", "概览": "全域"}


def theme_domain_of(question: str) -> str | None:
    """「【主题解读】客群」→ 客群;不是主题解读 → None(按原样取前 N 张)。"""
    q = (question or "").strip()
    if not q.startswith(THEME_PREFIX):
        return None
    rest = q[len(THEME_PREFIX):].split()          # 固定问题发给上游时可能在后面追加了「口径要求」
    return _THEME_NAME_TO_DOMAIN.get(rest[0]) if rest else None


def theme_hits(title: str, domain: str | None) -> int:
    """标题里命中了几个本主题关键词(没有该主题的关键词表 → 0)。"""
    return sum(1 for k in THEME_TABLE_KEYWORDS.get(domain or "", ()) if k in str(title or ""))


def pick_theme_tables(tables: list, domain: str | None, limit: int = REPORT_REF_LIMIT) -> list:
    """按主题挑表:标题命中本主题关键词的优先(按命中数排序、同分保持看板顺序)。

    - 没给主题 / 概览(全域)/ 一张都没命中 → 看板前 limit 张(原行为);
    - 命中了就**只用命中的**,不拿别的主题的表凑数 —— 凑数正是卡片跑题的来源。
    """
    keys = THEME_TABLE_KEYWORDS.get(domain or "")
    if not keys:
        return tables[:limit]
    scored = []
    for i, t in enumerate(tables):
        title = str(t[0] or "")
        hits = sum(1 for k in keys if k in title)
        if hits:
            scored.append((-hits, i, t))
    if not scored:
        return tables[:limit]
    scored.sort(key=lambda x: (x[0], x[1]))
    keep = sorted(scored[:limit], key=lambda x: x[1])   # 挑完按看板原顺序排,读起来跟看板一致
    return [t for _, _, t in keep]


def markdown_to_csv_files(md: str, *, base_name: str, limit: int = REPORT_REF_LIMIT,
                          domain: str | None = None) -> list[tuple[str, bytes]]:
    """仪表板快照 Markdown → 若干 CSV(每张表一个),用作小Q报告的引用文件。

    domain = 主题数据域(客群 / 票务 / 舆情 / OTA直销):给了就按主题挑表,见 pick_theme_tables。
    """
    return tables_to_csv_files(pick_theme_tables(markdown_tables(md), domain, limit), base_name=base_name)


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
