"""把一份 Markdown 报告压成**一行能给人看的中文**(APP 首页「AI总结」卡片那一行)。

这一行长期是错的,两种错法都出现在现场截图里:

1. **显示的是计划,不是结论。** 报告正文常常以一段执行计划开头
   (`- **1. 景区舆情数据多维统计…**` / `- **1.1**: 使用 \\`researcher\\` 在互联网上搜索…`),
   旧实现直接取"正文第一句",于是卡片上写的是「使用 researcher 在互联网上搜索…」——
   用户看到的是系统打算干什么,而不是它查出了什么。也有的报告以模型的自言自语开头
   (「首先,我需要判断用户的意图类型…这属于类型C」),同样一个字的结论都没有。
2. **原样吐出 Markdown。** 卡片是纯文本槽位,`**`、反引号、行首 `-` 会原封不动显示出来,
   看着像乱码。

做法:先找**摘要/结论类锚点**(`# 摘要`、`【主要发现】`…),从那儿取第一句 —— 报告作者
已经把结论放在那儿了,这比任何启发式都准。找不到锚点才退而求其次:跳过计划提纲行,
取第一句像人话的正文。最后统一去掉 Markdown 标记。

一个刻意的克制:**这里不概括、不改写**,只做挑选和清洗。卡片上的那句话必须是报告里
原样写着的话 —— 由这一层去"总结"就等于凭空造一个没人核对过的结论,而它正好出现在
用户最可能只看一眼的地方。
"""
from __future__ import annotations

import re

__all__ = ["strip_markdown", "summary_line"]

# 摘要/结论类锚点。顺序即优先级:先要结论,拿不到才要背景。
# 「基础信息」放最后 —— 它是背景铺垫,作为卡片那一行聊胜于无,但不该盖过真正的发现。
_ANCHORS = ("核心结论", "主要发现", "摘要", "概要", "总结", "结论", "核心观点", "要点", "基础信息")
_ANCHOR_RE = re.compile(
    r"(?P<head>#{1,6}\s*)?\*{0,2}(?P<lb>[【\[])?\s*(?P<word>" + "|".join(_ANCHORS)
    + r")\s*(?P<rb>[】\]])?\*{0,2}\s*[:：]?",
)

# 计划提纲行:`- **1. xxx**` / `  - **1.1**: xxx` / `2、xxx` / `### 3.2 xxx`
_OUTLINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?\*{0,2}#{0,6}\s*\d+(?:\.\d+)*\s*\*{0,2}\s*[.、:：)\]]")
# 表格行 / 分隔线 / 引用 / 纯标记行
_NOISE_RE = re.compile(r"^\s*(?:\||[-=*_]{3,}|>|#{1,6}\s*$)")

_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{2,3}|~~)")
_LEADING_MARK_RE = re.compile(r"^[\s\-*+•·#>|]+")


def strip_markdown(text: str) -> str:
    """把一段 Markdown 压成纯文本。只去标记,不动文字本身。"""
    s = _FENCE_RE.sub(" ", text or "")
    s = _LINK_RE.sub(r"\1", s)          # [文字](url) -> 文字;图片同理
    s = _INLINE_CODE_RE.sub(r"\1", s)   # `researcher` -> researcher
    s = _EMPHASIS_RE.sub("", s)         # ** / *** / __ / ~~
    s = _LEADING_MARK_RE.sub("", s)
    s = re.sub(r"[ \t　]+", " ", s)
    return s.strip()


def _first_sentence(text: str, limit: int) -> str:
    """取第一句。中文句号优先,其次分号/换行;都没有就按长度截断。"""
    body = strip_markdown(text)
    if not body:
        return ""
    for sep, keep in (("。", True), ("！", True), ("？", True),
                      ("\n", False), (";", False), (";", False)):
        idx = body.find(sep)
        if 0 < idx <= limit:
            return body[: idx + (1 if keep else 0)].strip()
    if len(body) <= limit:
        return body
    # 硬截断时补省略号,让人知道这句话没说完,而不是以为报告就写了这么多
    return body[:limit].rstrip() + "…"


def _looks_like_prose(line: str) -> bool:
    """像正文,不像提纲/表格/标记。"""
    s = line.strip()
    if len(s) < 12:                      # 太短的多半是小标题
        return False
    if _NOISE_RE.match(s) or _OUTLINE_RE.match(s):
        return False
    return True


def _after_anchor(text: str) -> str:
    """找到摘要/结论类锚点,返回它后面的内容(按 _ANCHORS 的优先级挑)。

    锚点可能**贴在正文里没有换行** —— 现场那份报告就是
    `…优化建议。泰宁双景区OTA与直销渠道效能对比及结构优化策略# 摘要-   国内景区…`,
    按行扫根本找不到它。所以在全文上正则找,不依赖换行。
    """
    best: tuple[int, int, int] | None = None   # (是否标题形态, 锚点优先级, 命中位置)
    for m in _ANCHOR_RE.finditer(text):
        # **标题形态优先**(`### 【主要发现】` / `# 摘要`),裸提及排在后面。
        # 报告开头常有一段模型自言自语,里面会把报告结构复述一遍
        # (「类型C的输出报告结构:- 主要发现:数据呈现的核心规律、核心指标变化。」)——
        # 那是它在说自己**打算**写什么,不是发现本身。按位置取第一个命中,
        # 正好会取到这句复述,卡片上于是出现一句放之四海而皆准的空话。
        is_heading = 0 if (m.group("head") or (m.group("lb") and m.group("rb"))) else 1
        key = (is_heading, _ANCHORS.index(m.group("word")), m.end())
        if best is None or key[:2] < best[:2]:
            best = key
    return text[best[2]:] if best else ""


def summary_line(text: str, limit: int = 120) -> str:
    """报告正文 → 卡片那一行纯文本。取不到就返回空串(让前端显示「正在生成」而不是半句垃圾)。"""
    if not (text or "").strip():
        return ""
    body = _FENCE_RE.sub(" ", text)

    tail = _after_anchor(body)
    if tail:
        # 锚点后面可能紧跟标题剩余部分/列表符号,逐行找第一句像人话的
        for line in tail.splitlines():
            if _looks_like_prose(line):
                got = _first_sentence(line, limit)
                if got:
                    return got
        got = _first_sentence(tail, limit)
        if got:
            return got

    # 没有锚点:跳过计划提纲和噪音行,取第一句正文
    for line in body.splitlines():
        if _looks_like_prose(line):
            got = _first_sentence(line, limit)
            if got:
                return got
    return _first_sentence(body, limit)
