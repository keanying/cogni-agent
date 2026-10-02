# -*- coding: utf-8 -*-
"""五个主题的解读必须**各对各的**,不许串台。

现场:「总览」Tab 显示的是客群的解读,「票务」Tab 显示的是舆情的解读。两个独立的原因:

1. **前端拿错主题**:数据页「总览」Tab 写的是 `<AiSummary theme="passenger_flow" />`,
   于是「总览」和「客流画像」两个 Tab 显示同一份客群解读,而两个 Tab 的图表完全不同。
2. **后端绑错看板**:`_theme_target` 的注释白纸黑字写着「挑不到就返回空 target,
   宁可让 Quick BI 自己选表,也不要硬塞一份不相关的数据」,而代码写的是
   `a = hit or rows[0]` —— 注释说不塞,代码照塞不误。票务主题于是被绑到舆情看板上,
   报告老老实实按舆情看板写,结构完整、数字也对(对的是舆情的数),标题却写着「票务」。
   这种错运营一眼看不出来,只会觉得模型答跑题了。

第三件事是**缓存**:上面两条修完,前台还会继续回放改绑之前那份错报告,最长 30 天 ——
代码改对了、现象一点没变,而没人猜得到是缓存。所以回放要认 target。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from cogni_agent.gateway_service import scenic_binding as sb

UNI = Path("/home/claude/work/uni/digital-brain-uni-v1.1.1")


class _Asset:
    def __init__(self, name, aid="a1", ref="p1"):
        self.name, self.id, self.ref_id = name, aid, ref


class _DB:
    """只喂 _theme_target 需要的那一次查询。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        rows = self._rows

        class _R:
            @staticmethod
            def scalars():
                class _S:
                    @staticmethod
                    def all():
                        return rows
                return _S()
        return _R()


async def _pick(names, domain):
    return await sb._theme_target(_DB([_Asset(n, f"a{i}", f"p{i}")
                                       for i, n in enumerate(names)]), "conn1", "sc1", domain)


class TestThemeTargetNeverGuesses:
    @pytest.mark.asyncio
    async def test_no_name_match_binds_nothing(self):
        """名字都对不上 -> 空 target,交给 Quick BI 自己选表。

        旧代码在这里返回 rows[0] —— 那正是票务主题被绑到舆情看板上的那一行。
        """
        target, data_id, name = await _pick(["景区舆情分析看板", "客群画像看板"], "票务")
        assert target == {} and data_id is None and name is None

    @pytest.mark.asyncio
    async def test_exact_domain_still_matches(self):
        target, _, name = await _pick(["景区舆情分析看板", "票务经营看板"], "票务")
        assert name == "票务经营看板" and target["page_id"] == "p1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("assets,domain,expect", [
        (["门票销售分析"], "票务", "门票销售分析"),
        (["售票日报"], "票务", "售票日报"),
        (["经营综合驾驶舱"], "全域", "经营综合驾驶舱"),
        (["总览大屏"], "全域", "总览大屏"),
        (["游客画像"], "客群", "游客画像"),
        (["口碑评价分析"], "舆情", "口碑评价分析"),
        (["渠道分销概况"], "OTA直销", "渠道分销概况"),
    ])
    async def test_operators_own_naming_is_recognised(self, assets, domain, expect):
        """运营给看板起名不会照着我们的 domain 字段起。只认一个词,命中率低得可怜,
        而一旦不命中,旧代码就退回「任一仪表板」—— 低命中率直接变成高串台率。"""
        _, _, name = await _pick(assets, domain)
        assert name == expect

    @pytest.mark.asyncio
    async def test_never_returns_another_themes_board(self):
        """五个主题两两之间都不许串。"""
        boards = {"全域": "综合驾驶舱", "客群": "客群画像看板",
                  "票务": "门票销售看板", "舆情": "舆情口碑看板",
                  "OTA直销": "OTA渠道看板"}
        for domain, own in boards.items():
            _, _, name = await _pick(list(boards.values()), domain)
            assert name == own, f"主题「{domain}」绑到了「{name}」"

    @pytest.mark.asyncio
    async def test_single_registered_board_is_still_bound(self):
        """只登记了一块看板:没有「挑错」这回事 —— 它是这个景区仅有的那份数据。

        名字往往就是景区名(「九龙潭名胜风景区」),说明不了它不覆盖某个主题。
        真正危险的是**多块看板里挑错一块**,那才会出现「票务卡片里是舆情报告」。
        """
        _, _, name = await _pick(["九龙潭名胜风景区"], "票务")
        assert name == "九龙潭名胜风景区"

    @pytest.mark.asyncio
    async def test_two_unrelated_boards_bind_nothing(self):
        """一旦有得挑,挑不中就别挑 —— 这条和上一条的边界就在「有没有第二块」。"""
        assert await _pick(["九龙潭名胜风景区", "泰宁大金湖"], "票务") == ({}, None, None)

    @pytest.mark.asyncio
    async def test_no_assets_at_all(self):
        assert await _pick([], "票务") == ({}, None, None)

    @pytest.mark.asyncio
    async def test_no_domain(self):
        assert await _pick(["随便一块看板"], None) == ({}, None, None)

    def test_every_theme_has_aliases(self):
        """新增主题时别忘了给它配叫法,否则它会静默退化成「永远绑不上」。"""
        for t in sb.THEMES:
            assert t["domain"] in sb._DOMAIN_ALIASES, f"主题 {t['code']} 的 domain 没配叫法"


class TestCachedRunMustMatchTarget:
    def test_recent_success_run_accepts_strict_target(self):
        import inspect

        sig = inspect.signature(sb.recent_success_run)
        assert "strict_target" in sig.parameters
        assert "expect_target_data_id" in sig.parameters
        src = inspect.getsource(sb.recent_success_run)
        assert 'QbiRun.target["page_id"]' in src

    @pytest.mark.parametrize("rel,needle", [
        ("cogni_agent/gateway_service/routes_app.py", "strict_target=True"),
        ("cogni_agent/cronjobs/theme_reports.py", "strict_target=True"),
        ("cogni_agent/gateway_service/routes_app_ops.py", "strict_target=True"),
    ])
    def test_all_replay_paths_use_it(self, rel, needle):
        """前台回放、定时预热、运维手动重跑 —— 三条路必须同口径,
        漏一条就会出现「这边说跑过了、那边说没有」的来回打架。"""
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert needle in src


@pytest.mark.skipif(not UNI.exists(), reason="uni 工程不在当前检出里")
class TestAppTabsMapToTheirOwnTheme:
    @staticmethod
    def _dashboard() -> str:
        return (UNI / "src" / "pages" / "dashboard" / "index.vue").read_text(encoding="utf-8")

    def test_five_tabs_five_distinct_themes(self):
        themes = re.findall(r'<AiSummary\s+theme="([a-z_]+)"', self._dashboard())
        assert sorted(themes) == sorted(
            ["overview", "ticket", "passenger_flow", "public_opinion", "ota_direct_sales"]), (
            f"五个 Tab 应各对一个主题,实际是 {themes}"
        )

    def test_no_theme_is_used_twice(self):
        themes = re.findall(r'<AiSummary\s+theme="([a-z_]+)"', self._dashboard())
        assert len(themes) == len(set(themes)), "有两个 Tab 指向同一个主题,必然有一个在串台"

    def test_overview_tab_asks_for_overview(self):
        src = self._dashboard()
        i = src.index("activeTab === 'revenue'")     # 总览段落在票务 Tab 之前
        assert 'theme="overview"' in src[:i]
        assert 'theme="passenger_flow"' not in src[:i], "总览 Tab 拿的是客群主题的解读"

    def test_themes_match_the_backend_codes(self):
        codes = {t["code"] for t in sb.THEMES}
        themes = set(re.findall(r'<AiSummary\s+theme="([a-z_]+)"', self._dashboard()))
        assert themes <= codes, f"前端用了后端不认识的主题码:{themes - codes}"

    def test_card_strips_markdown_client_side(self):
        src = (UNI / "src" / "components" / "dashboard" / "AiSummary.vue").read_text(encoding="utf-8")
        assert "function plainText" in src, "卡片是纯文本槽位,前端也要兜一道"
        assert "plainText(view.value.sentence" in src


class TestAmbiguousBoardNames:
    """看板名同时带着两个主题的词时,不能按登记顺序取第一个命中的。

    「游客评价分析」:「游客」命中客群、「评价」命中舆情。以前按顺序取第一个命中的,
    客群主题就绑到这块舆情看板上 —— APP 客流画像的 AI 总结里写出来的是「评论量峰值 284 条」。
    """

    @pytest.mark.asyncio
    async def test_prefers_board_that_only_looks_like_this_theme(self):
        _, _, name = await _pick(["游客评价分析", "客群画像看板"], "客群")
        assert name == "客群画像看板"

    @pytest.mark.asyncio
    async def test_ambiguous_board_not_taken_by_the_weaker_theme(self):
        # 只有这一块带「游客」的舆情看板 + 别的看板:客群不该去抢它
        assert await _pick(["游客评价分析", "门票销售看板"], "客群") == ({}, None, None)
        _, _, name = await _pick(["游客评价分析", "门票销售看板"], "舆情")
        assert name == "游客评价分析"
