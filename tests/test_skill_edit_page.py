# -*- coding: utf-8 -*-
"""Skill 编辑页 · 工具接入:错位、批量、默认最新版本、吸顶。

现场截图:工具接入页里「版本历史」的内容压在工具列表上;27 个已接入工具全显示「未钉版」,
要一个个下拉选版本;列表很长,往下翻时保存/发布和页签都滚走了。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "src" / "pages" / "Tools" / "SkillEdit.tsx"


def _src() -> str:
    return PAGE.read_text(encoding="utf-8")


def _tsx() -> str | None:
    for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx"):
        if c and Path(c).exists():
            return c
    return None


def test_pin_logic_runs_against_the_real_module():
    tsx = _tsx()
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "skill_pins.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_columns_obey_their_height_instead_of_spilling_over():
    """栅格子项默认 min-height:auto,不加 min-h-0 就会被内容撑高、溢出到下面的卡片上。"""
    s = _src()
    assert "gap-8 h-[600px]" not in s
    assert s.count('className="flex min-h-0 flex-col gap-3') == 2, "两栏都要 min-h-0"
    assert s.count("min-h-0 flex-1 overflow-y-auto overscroll-contain") == 2, "两个列表都在内部滚动"
    assert "lg:h-[calc(100vh-250px)]" in s


def test_version_panel_lives_only_in_the_version_tab():
    s = _src()
    tab = s[s.index('<TabsContent value="version">'):]
    tab = tab[:tab.index("</TabsContent>")]
    assert "<VersionPanel" in tab
    assert s.count("<VersionPanel") == 1, "不能再挂在所有页签下面(那样工具接入页总要整页滚动)"


def test_header_and_tabs_are_sticky():
    s = _src()
    sticky = s[s.index('<div className="sticky -top-6'):s.index("</TabsList>")]
    assert "发布版本" in sticky and "保存草稿" in sticky and "<TabsList" in sticky


def test_batch_select_all_on_search_results():
    s = _src()
    assert "toggleAllFiltered(filteredTools.map((t) => t.id), allFilteredOn)" in s
    assert "el.indeterminate" in s, "部分选中要显示半选"
    assert "confirmDialog(" in s, "批量取消会丢掉已填的使用说明,要先确认"


def test_new_and_loaded_bindings_default_to_latest_published():
    s = _src()
    assert "fillLatestPins(prev, map)" in s, "进页面时未钉版的默认最新"
    assert "await ensurePinOptions(fresh)" in s, "新勾上的工具要补拉版本候选"
    assert "disabled={v.status !== 'published'}" in s, "已下线/未发布的版本不可选"
