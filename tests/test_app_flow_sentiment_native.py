# -*- coding: utf-8 -*-
"""APP「客流画像」「舆情」两个 Tab:从嵌 Quick BI 链接(iframe)改为对接数据平台真实接口原生渲染。

客群 1.18 ~ 1.26、舆情 1.27 ~ 1.31;排版按运营给的 UI(客群 8 个子页签 / 舆情 5 个周期),
风格沿用 APP;接口取不到的模块整块丢弃;UI 里的「速览 / 解读 / 洞察小结」叙述不做。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNI = pathlib.Path("/home/claude/work/uni/digital-brain-uni-v1.1.1/src")


def _tsx():
    for c in (shutil.which("tsx"), "/home/claude/.npm-global/bin/tsx"):
        if c and pathlib.Path(c).exists():
            return c
    return None


def test_view_models_run_against_the_real_modules():
    tsx = _tsx()
    if not tsx or not UNI.exists():
        pytest.skip("没有 tsx / uni 工程")
    r = subprocess.run([tsx, str(ROOT / "tests" / "web" / "uni" / "views.check.ts")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_dashboard_no_longer_embeds_bi_links():
    if not UNI.exists():
        pytest.skip("uni 工程不在当前检出里")
    s = (UNI / "pages" / "dashboard" / "index.vue").read_text(encoding="utf-8")
    assert "<iframe" not in s and "fetchQuickBiLink" not in s and "getQuickBiLink" not in s
    assert '<FlowProfile v-if="activeTab === \'flow\'" :scenic-id="scenicId" :time-gran="activeReport" />' in s
    assert '<SentimentPanel :scenic-id="scenicId" />' in s
    assert "const reportChips = computed(() => profilePeriodList)" in s, "报告周期 = 客群接口的 timeGran"


def test_api_layer_covers_all_new_endpoints_and_never_hardcodes_key():
    if not UNI.exists():
        pytest.skip("uni 工程不在当前检出里")
    api = (UNI / "api" / "digitalBrain.js").read_text(encoding="utf-8")
    for path in ("workHomeLocation", "visitPref", "coreMetric", "tripProfile", "sourceRegion", "popularDestination",
                 "spendingProfile", "regionSwitch", "basicProfile", "opinion/baseCore",
                 "opinion/commentPeriodDistribution", "opinion/dimTagScore",
                 "opinion/channelCommentDistribution", "opinion/emotionWordCloud"):
        assert f"/v1/data/digitalBrain/api/{path}'" in api, path
    for f in ("api/digitalBrain.js", "utils/profileData.js", "utils/opinionData.js", "utils/chartOptions.js",
              "components/dashboard/FlowProfile.vue", "components/dashboard/SentimentPanel.vue"):
        assert "pfk_" not in (UNI / f).read_text(encoding="utf-8"), f"{f} 里不能出现数据平台密钥"
    # 失败转空数组放在缓存外面:一次网络抖动不能把 [] 缓存住
    assert "return cached(params).catch(" in api


def test_narratives_from_the_ui_are_not_built():
    if not UNI.exists():
        pytest.skip("uni 工程不在当前检出里")
    for f in ("utils/profileData.js", "utils/opinionData.js"):
        s = (UNI / f).read_text(encoding="utf-8")
        for word in ("核心发现", "画像速览", "洞察小结", "消费行为解读"):
            assert f"title: '{word}" not in s and f"'{word}'" not in s, (f, word)


def test_flow_tab_has_no_entry_or_in_park_cards():
    """客流画像 Tab 去掉「入园人数」「当前在园人数」两张卡(产品截图标注「不要」),也不再调 1.1 / 1.7。"""
    if not UNI.exists():
        pytest.skip("uni 工程不在当前检出里")
    s = (UNI / "pages" / "dashboard" / "index.vue").read_text(encoding="utf-8")
    tpl = s.split("<script", 1)[0]
    assert 'class="entry-card"' not in tpl and 'class="in-park-card"' not in tpl
    assert "await fetchFlowData()" not in s
    # 竞品对比入口、报告周期、AI 总结仍保留
    assert "goToDataCompare" in tpl and 'theme="passenger_flow"' in tpl


def test_profile_and_sentiment_follow_ticket_typography():
    """客群 / 舆情的指标卡、标题、时段按钮、图表字号与票务一致(DIN 48rpx 数值、30rpx 标题、24rpx 胶囊、#1989fa)。"""
    if not UNI.exists():
        pytest.skip("uni 工程不在当前检出里")
    import re
    kpi = (UNI / "components" / "dashboard" / "ProfileKpis.vue").read_text(encoding="utf-8")
    rev = (UNI / "components" / "dashboard" / "RevenueMetrics.vue").read_text(encoding="utf-8")
    grab = lambda src, cls: re.search(r"\." + cls + r" \{([^}]*)\}", src, re.S).group(1)
    for cls in ("metric-value", "metric-label"):
        a = re.sub(r"\s+", " ", grab(kpi, cls))
        for prop in ("font-size: 48rpx", "font-size: 24rpx", "'DIN Alternate'", "color: #24272e", "color: #646566"):
            if prop in re.sub(r"\s+", " ", grab(rev, cls)):
                assert prop in a, (cls, prop)
    assert "Georgia" not in kpi and "serif;" not in kpi, "不能再用衬线字体"
    blocks = (UNI / "components" / "dashboard" / "ProfileBlocks.vue").read_text(encoding="utf-8")
    assert "pb-bar" not in blocks, "标题不加色条(票务 BlockTitle 没有)"
    for f in ("SentimentPanel.vue", "FlowProfile.vue"):
        src = (UNI / "components" / "dashboard" / f).read_text(encoding="utf-8")
        assert "padding: 12rpx 24rpx" in src and "background: #1989fa" in src and "linear-gradient" not in src, f
    opts = (UNI / "utils" / "chartOptions.js").read_text(encoding="utf-8")
    assert "#4e8ef7" not in opts and "fontSize: 10," not in opts and "fontSize: 9," not in opts
    idx = (UNI / "pages" / "dashboard" / "index.vue").read_text(encoding="utf-8")
    assert 'variant="banner"' not in idx, "舆情的 AI 总结与其它 Tab 同一个卡片样式"
