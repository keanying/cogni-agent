"""
代码编辑器着色层的两条硬约束。

编辑器是「透明 textarea 叠在着色 <pre> 上」的结构,两层必须**逐字符对齐**。
历史上这里出过两个 bug,都不会报错、只会让人觉得"编辑器很难用":

1. 占位符暂存被数字正则破坏 -> 着色层把自己的 HTML 片段吐进代码里
   (`400 font-semibold">import sys`)
2. 着色用了 font-semibold / px-0.5 -> 粗体比常规体宽、内边距占位置,
   着色层每遇到一个关键字就比 textarea 宽一点,**越往后累积越多**,
   表现是"点击位置和光标落点不一样,行越长偏得越厉害"

所以这里从源码层面钉死:着色只能改颜色。
"""
from __future__ import annotations

import re
from pathlib import Path

EDITOR = Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "common" / "CodeEditor.tsx"
MARKDOWN = Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "common" / "Markdown.tsx"


def _highlight_classes(src: str) -> list[str]:
    """取出 CLASS_OF 映射里所有的样式类。"""
    m = re.search(r"const CLASS_OF[^=]*=\s*\{(.*?)\};", src, re.S)
    assert m, "找不到 CLASS_OF 着色样式表"
    return re.findall(r"'([^']+)'", m.group(1))


def test_highlight_styles_are_color_only():
    """着色层只能改颜色。任何影响字符宽度/位置的属性都会让光标错位。"""
    forbidden = ("font-bold", "font-semibold", "font-medium", "font-black",
                 "px-", "pl-", "pr-", "tracking-", "text-lg", "text-xl", "italic")
    for cls in _highlight_classes(EDITOR.read_text(encoding="utf-8")):
        for bad in forbidden:
            assert bad not in cls, (
                f"着色类 {cls!r} 含有 {bad!r} —— 它会改变字符宽度,导致光标与文字错位。"
                f"着色层只能用 text-<color> 类。"
            )


def test_no_placeholder_stashing():
    """不能再用「占位符暂存」那套做法:数字正则会破坏占位符本身。"""
    src = EDITOR.read_text(encoding="utf-8")
    assert "\\u0000" not in src, "不要用 \\u0000 占位符暂存,数字正则会把它破坏掉"
    assert "const stash" not in src, "不要用 stash 暂存已着色片段,改用单遍分词"


def test_uses_single_pass_tokenizer():
    src = EDITOR.read_text(encoding="utf-8")
    assert "CLASS_OF" in src and "flushPlain" in src, "着色应为单遍分词实现"


def test_shared_box_pins_all_metrics():
    """两层的盒模型必须逐项写死,不能一边用 Tailwind 类一边靠默认值。"""
    src = EDITOR.read_text(encoding="utf-8")
    m = re.search(r"const SHARED_BOX[^=]*=\s*\{(.*?)\};", src, re.S)
    assert m, "找不到 SHARED_BOX"
    box = m.group(1)
    for key in ("fontFamily", "fontSize", "lineHeight", "padding", "border",
                "letterSpacing", "tabSize", "fontVariantLigatures"):
        assert key in box, f"SHARED_BOX 缺少 {key} —— 两层该属性不一致就会错位"


def test_no_rows_attribute_on_textarea():
    """rows 会参与布局撑高 textarea,而着色层是 absolute inset-0 跟随容器 ——
    两层高度不一致就会各自滚动。高度只能由容器控制。"""
    src = EDITOR.read_text(encoding="utf-8")
    ta = src[src.index("<textarea"):src.index("/>", src.index("<textarea"))]
    assert "rows=" not in ta, "textarea 不能带 rows,高度由外层容器的 minHeight 控制"


def test_markdown_highlight_also_color_only():
    """消息里的代码块用的是同一套思路,同样不能改宽度。"""
    src = MARKDOWN.read_text(encoding="utf-8")
    block = src[src.index("function highlightCode"):src.index("const CopyButton")]
    for bad in ("font-semibold", "font-bold", "px-0.5", "px-1"):
        assert bad not in block, f"Markdown 代码块着色不应包含 {bad!r}"


def test_markdown_does_not_rely_on_typography_plugin():
    """prose 系列类来自 @tailwindcss/typography,本项目没装 —— 写了是空类,
    而 preflight 会把标题/列表样式清掉,表现为"渲染了但看起来像纯文本"。"""
    src = MARKDOWN.read_text(encoding="utf-8")
    assert "prose-sm" not in src and 'className="prose' not in src, \
        "不要依赖 prose 类,本项目未安装 @tailwindcss/typography"
    for el in ("strong:", "li:", "hr:", "ul:", "ol:", "h1:", "table:"):
        assert el in src, f"缺少 {el} 的显式样式 —— preflight 会把它的默认样式清掉"
