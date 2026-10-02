# -*- coding: utf-8 -*-
"""工具中心列表(Tool / MCP / Skill / Agent):卡片更紧凑、查看/修改用弹窗、卡片显示修改信息。

现场诉求:「窗格可以再小一些,然后点击查看/修改是否可以做成弹窗模式;
展示最近修改时间,修改人,归属这些信息」。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "src"
LISTS = {"tool": "ToolList.tsx", "mcp": "MCPList.tsx", "skill": "SkillList.tsx", "agent": "AgentList.tsx"}
MODAL_PAGES = ["ToolDetail", "ToolNew", "MCPDetail", "MCPNew", "SkillDetail", "SkillEdit",
               "AgentDetail", "AgentNew"]


def _tsx():
    for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx"):
        if c and Path(c).exists():
            return c
    return None


def test_nav_logic_runs_against_the_real_module():
    tsx = _tsx()
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "modal_nav.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("kind,fname", LISTS.items())
def test_list_opens_detail_and_edit_in_modal_and_shows_meta(kind, fname):
    s = (WEB / "pages" / "Tools" / fname).read_text(encoding="utf-8")
    assert f"openInModal(navigate, location, `/tools/{kind}/detail?id=" in s, "卡片点开 = 弹窗查看"
    assert f"openInModal(navigate, location, `/tools/{kind}/edit?id=" in s, "齿轮 = 弹窗修改"
    assert f"navigate(`/tools/{kind}/detail?id=" not in s and f"navigate(`/tools/{kind}/edit?id=" not in s
    assert "<CardMeta item=" in s, "卡片底部显示最近修改 / 修改人 / 归属"
    assert "className={CARD_GRID}" in s, "统一的紧凑网格(2xl 四列)"
    assert "gap-6" not in s.split("CARD_GRID", 1)[1][:200]
    assert "[location.key]" in s, "弹窗关闭回到列表要重新拉数据"


def test_card_grid_is_denser_and_meta_uses_backend_fields():
    s = (WEB / "components" / "common" / "CardMeta.tsx").read_text(encoding="utf-8")
    assert "2xl:grid-cols-4" in s
    for f in ("updated_at", "updated_by_name", "scenic_name", "owner_name"):
        assert f"item.{f}" in s, f


@pytest.mark.parametrize("page", MODAL_PAGES)
def test_pages_shown_in_modal_use_modal_aware_navigate(page):
    s = (WEB / "pages" / "Tools" / f"{page}.tsx").read_text(encoding="utf-8")
    assert "const navigate = useModalNavigate();" in s
    assert "useNavigate" not in s, "漏换的话弹窗里点「编辑」会整页跳走"


def test_app_renders_list_as_background_and_modal_on_top():
    s = (WEB / "App.tsx").read_text(encoding="utf-8")
    assert "<Routes location={background || location}>" in s
    for kind in LISTS:
        for view in ("detail", "edit"):
            assert f'<Route path="/tools/{kind}/{view}" element={{<RouteModal>' in s, (kind, view)
    m = (WEB / "components" / "common" / "RouteModal.tsx").read_text(encoding="utf-8")
    # 遮罩带 backdrop-blur / transform 会让页面里的二级弹窗(fixed)以它为定位基准而被裁掉
    head = m.split('data-route-modal', 1)[0].rsplit("<div", 1)[1]
    assert "backdrop-blur" not in head and "translate" not in head


# ── v3.54.1:弹窗里的详情页 —— 顶部固定、各栏固定高度各自滚动;请求头明文密钥打码 ──

def test_mask_secret_runs_against_the_real_module():
    tsx = _tsx()
    if not tsx:
        pytest.skip("没有 tsx")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "mask_secret.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("page", ["ToolDetail", "SkillDetail", "AgentDetail"])
def test_detail_pages_fix_header_and_scroll_columns_in_modal(page):
    s = (WEB / "pages" / "Tools" / f"{page}.tsx").read_text(encoding="utf-8")
    assert "const inModal = useInRouteModal();" in s
    assert "inModal ? 'shrink-0 space-y-3'" in s, "头部固定(不参与滚动)"
    assert "lg:flex-1 lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]" in s, "内容区占满剩余高度"
    assert s.count("lg:h-full lg:overflow-y-auto") >= 2, "中栏、右栏各自滚动"
    assert "lg:h-full lg:flex lg:flex-col" in s, "版本列表卡片撑满栏高、列表区内部滚动"


def test_route_modal_provides_in_modal_flag():
    m = (WEB / "components" / "common" / "RouteModal.tsx").read_text(encoding="utf-8")
    assert "<InRouteModal.Provider value={true}>" in m and "export const useInRouteModal" in m
    assert 'className="flex-1 min-h-0 overflow-y-auto' in m


def test_tool_detail_masks_plaintext_keys():
    s = (WEB / "pages" / "Tools" / "ToolDetail.tsx").read_text(encoding="utf-8")
    assert "<HighlightVars text={maskSecret(k, String(v))} />" in s
    assert "JSON.stringify(maskSecretsDeep(viewTool.type_config), null, 2)" in s
    assert "JSON.stringify(viewTool.type_config" not in s
