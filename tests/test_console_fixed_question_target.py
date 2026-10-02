# -*- coding: utf-8 -*-
"""操练台:选中固定问题后,能力与目标数据要回填成这一问真正要跑的配置。

现场:问题配的是「九龙潭-票务-供应商品(票种)日报表」这个数据集(存为 cube_id),
操练台选中问题后能力跳到了「解读」,目标数据却停在「按景区自动关联」——
回填只认 asset_id / file_id。控件显示的和实际要跑的对不上。
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


def test_mapping_runs_against_the_real_module():
    tsx = _tsx()
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "fixed_target.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_console_uses_it_when_picking_a_question_and_reopening_a_session():
    s = CONSOLE.read_text(encoding="utf-8")
    assert "setQbiTarget(fixedTargetValue(f.target, qbiAssets));" in s, "选中固定问题"
    assert "setQbiTarget(fixedTargetValue(item.target, qbiAssets));" in s, "重开会话"
    assert "a.asset_id ? `asset:${a.asset_id}` : a.file_id" not in s, "老的只认两种的写法不能留"


def test_bound_target_is_visible_and_sent():
    s = CONSOLE.read_text(encoding="utf-8")
    assert "{boundTargetLabel && <option value={qbiTarget}>{boundTargetLabel}</option>}" in s, \
        "不在列表里的绑定目标要单独列出来,否则下拉框显示成「按景区自动关联」"
    assert ": directTarget(qbiTarget)" in s, "直连的数据集 / 仪表板要真的传给后端"
