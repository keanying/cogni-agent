# -*- coding: utf-8 -*-
"""APP 首页/数据页那张「AI总结」卡片:显示的必须是**本主题的结论**,而且是纯文本。

现场截图里它同时犯了三个错:

1. **显示的是计划,不是结论。** 卡片上写着
   「- **1.1**: 使用 `researcher` 在互联网上搜索"九龙潭名胜风景区 游客画像…"」——
   那是报告开头那段执行计划。用户看到的是系统**打算**干什么,一个结论都没有。
   另一类报告以模型的自言自语开头(「首先,我需要判断用户的意图类型…这属于类型C」),
   同样一个字的结论都没有。
2. **原样吐 Markdown。** 卡片是纯文本槽位,`**`、反引号、行首 `-` 原封不动显示出来。
3. **串台。** 「总览」Tab 显示的是客群的解读,「票务」Tab 显示的是舆情的解读。

下面用**现场真实报告原文**(tests/fixtures/theme_report_answers.json,从两份运行记录
导出的 answer 正文)钉住 1 和 2;3 在 test_theme_binding.py 里。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cogni_agent.gateway_service.summary_line import strip_markdown, summary_line

_FIX = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "theme_report_answers.json")
    .read_text(encoding="utf-8"))


class TestRealReports:
    """两份现场报告 —— 它们正是卡片出错时显示的那两份。"""

    def test_plan_first_report_shows_a_finding_not_the_plan(self):
        """开头是执行计划的那份:必须跳过计划,取到「摘要」里的第一条结论。"""
        got = summary_line(_FIX["plan_first"])
        assert got, "取不到就该返回空串让前端显示「正在生成」,但这份报告里明明有结论"
        # 旧实现取到的是这句(计划的第一行)
        assert "使用互联网搜索" not in got
        assert "景区渠道行业基准" not in got
        assert not got.startswith("1."), "不能以提纲编号开头"
        # 摘要区第一条:带数字、是结论
        assert "5.48亿" in got

    def test_thinking_first_report_skips_the_models_monologue(self):
        """开头是模型自言自语的那份:必须落到【主要发现】,而不是它复述的报告结构。"""
        got = summary_line(_FIX["thinking_first"])
        assert got
        assert "我需要判断" not in got and "类型C" not in got
        # 「主要发现:数据呈现的核心规律、核心指标变化。」是它在说**打算**写什么,
        # 一句放之四海而皆准的空话 —— 真正的发现在后面那个 ### 【主要发现】 里
        assert got != "数据呈现的核心规律、核心指标变化。"
        assert "客群结构演变" in got

    @pytest.mark.parametrize("key", ["plan_first", "thinking_first"])
    def test_no_markdown_leaks_through(self, key):
        got = summary_line(_FIX[key])
        for mark in ("**", "`", "](", "##"):
            assert mark not in got, f"卡片是纯文本槽位,不该出现 {mark!r}"
        assert not got.lstrip().startswith("-")

    @pytest.mark.parametrize("key", ["plan_first", "thinking_first"])
    def test_stays_within_one_line(self, key):
        got = summary_line(_FIX[key])
        assert "\n" not in got
        assert len(got) <= 121, "一行卡片放不下更多,超了会被截断成半句"

    @pytest.mark.parametrize("key", ["plan_first", "thinking_first"])
    def test_the_line_is_quoted_verbatim_from_the_report(self, key):
        """只挑选和清洗,**不概括、不改写**。

        由这一层去「总结」就等于凭空造一个没人核对过的结论,而它正好出现在
        用户最可能只看一眼的地方。所以取到的每个字都必须在原文里找得到。
        """
        body = strip_markdown(_FIX[key].replace("\n", " "))
        got = summary_line(_FIX[key]).rstrip("…")
        assert got in body, "卡片上的句子必须是报告里原样写着的话"


class TestAnchorPriority:
    def test_heading_anchor_beats_a_bare_mention(self):
        text = ("类型C的输出报告结构:\n- 主要发现:数据呈现的核心规律。\n"
                "### 【主要发现】\n- 客群明显向上班族集中,占比升至69.72%。\n")
        assert summary_line(text) == "客群明显向上班族集中,占比升至69.72%。"

    def test_conclusion_wins_over_background(self):
        """【基础信息】是背景铺垫,不该盖过真正的发现。"""
        text = ("### 【基础信息】\n- 识别为客群画像及消费偏好数据,涵盖十个指标模块。\n"
                "### 【主要发现】\n- 高端住宿需求激增,星级酒店偏好达110.54%。\n")
        assert "高端住宿" in summary_line(text)

    def test_background_used_when_there_is_no_finding(self):
        text = "### 【基础信息】\n- 识别为客群画像及消费偏好数据,涵盖十个指标模块。\n"
        assert "客群画像" in summary_line(text)

    def test_inline_heading_without_newline_is_found(self):
        """现场那份报告的 `# 摘要` 是**贴在正文里没有换行**的,按行扫根本找不到它。"""
        text = "…最后整合生成主题解读报告。渠道效能对比# 摘要-   在线预订用户规模已达5.48亿人。"
        assert summary_line(text) == "在线预订用户规模已达5.48亿人。"


class TestNoAnchorFallback:
    def test_outline_lines_are_skipped(self):
        text = ("- **1. 数据检索**\n"
                "  - **1.1**: 使用 `researcher` 搜索年龄结构。\n"
                "客流同比下降12%,主要集中在31-45岁人群。\n")
        got = summary_line(text)
        assert got == "客流同比下降12%,主要集中在31-45岁人群。"

    def test_tables_and_rules_are_skipped(self):
        text = "| 指标 | 值 |\n|---|---|\n| 客流 | 90 |\n\n本期客流为90人,较昨日下降。\n"
        assert summary_line(text) == "本期客流为90人,较昨日下降。"

    def test_code_fences_are_dropped(self):
        text = "```python\nprint('x')\n```\n本期票房收入703元,环比增长37.6%。\n"
        assert summary_line(text) == "本期票房收入703元,环比增长37.6%。"


class TestEdges:
    def test_empty_returns_empty_not_junk(self):
        """取不到就返回空串 —— 前端据此显示「正在生成」,而不是半句垃圾。"""
        for junk in ("", "   ", None):
            assert summary_line(junk) == ""

    def test_long_single_sentence_is_truncated_with_ellipsis(self):
        text = "客" * 400 + "。"
        got = summary_line(text)
        assert got.endswith("…"), "硬截断要补省略号,否则看的人以为报告就写了这么多"
        assert len(got) <= 121

    def test_only_outline_returns_something_rather_than_nothing(self):
        """全篇只有提纲时不强求 —— 有总比空白强,但不能崩。"""
        summary_line("- **1. 只有计划**\n  - **1.1**: 做点什么。\n")


class TestStripMarkdown:
    def test_removes_marks_keeps_text(self):
        assert strip_markdown("- **1.1**: 使用 `researcher` 搜索") == "1.1: 使用 researcher 搜索"

    def test_links_keep_their_text(self):
        assert strip_markdown("详见[报告](http://x/y)") == "详见报告"

    def test_plain_chinese_is_untouched(self):
        s = "客群明显向“上班族”集中,占比升至69.72%。"
        assert strip_markdown(s) == s


class TestWiredIn:
    """光有函数不够 —— 卡片那几个槽位都得过一遍。"""

    @staticmethod
    def _src() -> str:
        return (Path(__file__).resolve().parents[1]
                / "cogni_agent" / "gateway_service" / "routes_app.py").read_text(encoding="utf-8")

    def test_insight_sentence_uses_it(self):
        assert "return summary_line(text, limit)" in self._src()

    def test_cached_sentence_is_stripped(self):
        src = self._src()
        assert "strip_markdown(cache.summary)" in src, "预热摘要同样可能带标记"

    def test_theme_card_subtitle_is_stripped(self):
        assert "subtitle=(strip_markdown(cache.summary)" in self._src()
