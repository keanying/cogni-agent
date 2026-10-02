# -*- coding: utf-8 -*-
"""思考过程「版本溯源」:只列本轮实际调用到的工具,其余已绑定的折叠成一个数量。

现场:技能绑了一串工具,版本溯源一展开就把每个工具的 pin 版本全列了,
真正调到的一两个淹没在里面。完整数据仍在 resolve_provenance 事件里(导出不受影响)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "web" / "src" / "pages" / "Console.tsx"


def _tsx():
    for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx"):
        if c and Path(c).exists():
            return c
    return None


def test_split_runs_against_the_real_module():
    tsx = _tsx()
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "provenance_view.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_console_renders_only_used_tools():
    s = CONSOLE.read_text(encoding="utf-8")
    assert "splitProvenanceTools(s?.tools, usedToolKeys(allSteps))" in s
    assert "renderTimelineStep(step, idx, msg.steps!.length, thinking, msg.steps)" in s, "要把本轮步骤传进去"
    assert "Object.entries(s.tools).map(" not in s, "不能再把全部工具直接铺开"
    assert "个已绑定工具" in s and "<details" in s, "未用到的折叠成数量"
    assert "case 'latest_published'" in s, "跟随最新要有中文文案"
