# -*- coding: utf-8 -*-
"""问题与资产的「看得见 / 删得掉」回归。

这一组钉的都是现场反馈过的故障,共同点是**没有报错,只是结果不对**:

- 集团把仪表板登记成集团级(给所有景区共用),景区账号发起提问时报
  「资产不存在或无权访问:fa9f3b14-…」—— 可见范围漏了 scenic_id IS NULL 这一档;
- 后台把问题删了,前台照样看得到、点得到 —— 删除只删了一张表;
- 没挂三级标签的问题被兜进「未分类」分支,而管理端一直按「没挂标签 = 前台看不到」提示运营;
- 解读/报告建完之后不出现在对应板块里,因为三级标签要手工再选一遍。
"""
from __future__ import annotations

import asyncio
import contextlib
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base
from cogni_agent.common.time import now_cst

SCENIC = "PFTSCA01018088"
OTHER = "PFTSCB02000052"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh_db():
    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    yield


def _actor(role, scenic, user_id="u-1"):
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    return qsvc.Actor(user_id=user_id, scenic_id=scenic, role=role, source="console")


# ══════════════════════════════════════════════════════════════════════
# 一、集团级资产对所有景区可见
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_group_level_asset_is_visible_to_scenic_accounts():
    """现场那句「资产不存在或无权访问:fa9f3b14-…」的根因。

    集团把仪表板登记成集团级(不绑景区)= 所有景区共用。景区账号发起提问时
    按 asset_id 取资产,这里判不过,整条请求就断在取资产上 —— 而看板明明是配给他用的。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset, QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c1", name="连接", owner_user_id="u-admin",
                             visibility="group", scenic_id=None, active=True))
        db.add(QbiAsset(id="a-group", connection_id="c1", kind="dashboard", ref_id="p1",
                        name="九龙潭名胜风景区", owner_user_id="u-admin", scenic_id=None))
        db.add(QbiAsset(id="a-mine", connection_id="c1", kind="dashboard", ref_id="p2",
                        name="本景区看板", owner_user_id="u-admin", scenic_id=SCENIC))
        db.add(QbiAsset(id="a-other", connection_id="c1", kind="dashboard", ref_id="p3",
                        name="别的景区看板", owner_user_id="u-admin", scenic_id=OTHER))
        await db.commit()

    me = _actor("scenic_operator", SCENIC)
    assert (await qsvc.get_asset(me, "a-group")).name == "九龙潭名胜风景区"   # 集团级:能用
    assert (await qsvc.get_asset(me, "a-mine")).name == "本景区看板"
    with pytest.raises(Exception):
        await qsvc.get_asset(me, "a-other")                                  # 别的景区:仍然不可见

    ids = {a["id"] for a in await qsvc.list_assets(me)}
    assert ids == {"a-group", "a-mine"}, "列表与逐条取资产必须同一口径"


@run_async
async def test_running_against_a_group_level_asset_end_to_end():
    """整条执行链路都要过 —— 上一条只验了 get_asset,而现场错在**发起时**。

    `_prepare` 里还有一次 get_asset(把 asset 展开成 page_id/cube_id),
    它在首事件之后,所以不是 HTTP 500 而是流里的一个 error 事件 ——
    前端把它渲染成一条「资产不存在或无权访问:…」的回复气泡,正是截图里的样子。
    只补 get_asset 的单测抓不到这一层,必须真跑一遍。
    """
    import httpx
    from cryptography.fernet import Fernet

    from cogni_agent.config import get_settings
    from cogni_agent.tool_center_service.mcp import vault
    from cogni_agent.tool_center_service.quickbi import client as qclient
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from tests.quickbi import mock_quickbi_server as mock

    settings = get_settings()
    tc = dict(settings.raw.get("tool_center") or {})
    if not tc.get("credential_encrypt_key"):
        tc["credential_encrypt_key"] = Fernet.generate_key().decode()
        settings.raw["tool_center"] = tc
        vault._fernet_singleton = None
    q = dict(settings.raw.get("quickbi") or {})
    q.update({"trial_api_key": mock.API_KEY, "trial_api_secret": mock.API_SECRET,
              "report_poll_seconds": 0, "snapshot_poll_seconds": 0,
              "default_connection": {"user_token": ""}})
    settings.raw["quickbi"] = q
    _orig = qclient.default_transport_factory
    qclient.default_transport_factory = lambda: httpx.ASGITransport(app=mock.app)
    mock.STATE.__init__()
    try:
        admin = _actor("super_admin", None, user_id="u-admin")
        conn = await qsvc.save_connection(admin, {
            "name": "主连接", "use_trial": True, "visibility": "group",
            "user_token": mock.USER_TOKEN, "scenic_id": ""})
        asset = await qsvc.register_dashboard(admin, conn.id, mock.PAGE_ID,
                                              "九龙潭名胜风景区", scenic_id=None)
        assert asset.get("scenic_id") is None, "这条测试的前提是资产登记成集团级"

        app_user = _actor("scenic_operator", SCENIC, user_id="u-app")
        events = [e async for e in qsvc.run_stream(
            app_user, connection_id=conn.id, capability="dashboard_qa",
            question="年龄分布:按标签值看占比的分布",
            target={"asset_id": asset["id"]}, scenic_id=SCENIC)]
        errors = [e.get("message") for e in events if e.get("type") == "error"]
        done = next(e for e in events if e.get("type") == "done")
        assert not errors, errors
        assert done["status"] == "success", done.get("error")
    finally:
        qclient.default_transport_factory = _orig


# ══════════════════════════════════════════════════════════════════════
# 二、前台问题树:没挂标签的不展示
# ══════════════════════════════════════════════════════════════════════

async def _add_issue(db, issue_id, *, scenic=SCENIC, labels=True, **kw):
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    row = ScenicAgentIssueDetail(
        issue_id=issue_id, business_class=kw.pop("business_class", "quick_bi"),
        scenic_id=scenic, title=kw.pop("title", "本月售票总量"),
        issue=kw.pop("issue", "本月各平台销售额"), capability=kw.pop("capability", "dataset_qa"),
        visibility="group", active=True,
        **({"topic": "票务", "topic_id": "ticket",
            "dimension": "销售", "dimension_id": "sales",
            "metric": "销售额", "metric_id": "amount"} if labels else {}),
        **kw)
    db.add(row)
    await db.commit()
    return row


@run_async
async def test_unlabeled_issues_do_not_show_in_the_app_tree():
    """管理端一直提示「没挂标签 = 前台看不到」,后端却把它们兜进「未分类」——
    两边说的不是一回事,运营照着管理端的说法配,前台冒出一堆未分类。"""
    from cogni_agent.gateway_service import issue_service as issues

    async with common_db.get_sessionmaker()() as db:
        await _add_issue(db, "issue_labeled", labels=True)
        await _add_issue(db, "issue_bare", labels=False, title="没挂标签的问题")
        tree = await issues.business_issue_list(db, SCENIC)

    assert [t["topicId"] for t in tree] == ["ticket"]
    assert "uncategorized" not in str(tree) and "未分类" not in str(tree)


@run_async
async def test_app_tree_only_shows_own_scenic_plus_group_level():
    """集团用户全可见;景区用户只看本景区 + 集团级(集团级是配给所有景区共用的)。"""
    from cogni_agent.gateway_service import issue_service as issues

    async with common_db.get_sessionmaker()() as db:
        await _add_issue(db, "issue_mine", scenic=SCENIC)
        await _add_issue(db, "issue_group", scenic=None, title="集团通用")
        await _add_issue(db, "issue_other", scenic=OTHER, title="别的景区")

        mine = str(await issues.business_issue_list(db, SCENIC))
        assert "issue_mine" in mine and "issue_group" in mine and "issue_other" not in mine
        # 集团账号(scenic 为空)= 看全部
        every = str(await issues.business_issue_list(db, None))
        assert all(i in every for i in ("issue_mine", "issue_group", "issue_other"))


def test_app_tree_scenic_param_can_only_narrow():
    """景区账号传别的景区的 scenicId 要被拒,而不是拿到那个景区的问题。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    class U:
        def __init__(self, s): self.scenic_id = s

    assert binding.effective_scenic(U(SCENIC), "") == SCENIC
    with pytest.raises(Exception):
        binding.effective_scenic(U(SCENIC), OTHER)


# ══════════════════════════════════════════════════════════════════════
# 三、删除是全局的,而且要删干净
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_delete_issue_also_clears_source_prewarm_and_follows():
    """删完之后前台不该再看得到、点得到。

    原先只删 scenic_agent_issue_detail 一行,于是:
      ① 来源问题库行还在 → 下次保存问题库会把它**原地重建**;
      ② 预热缓存还在 → 概要卡片与预热直出继续命中;
      ③ 关注列表按文本存 → 列表照列,点下去反查不到 issue,掉回通用对话。
    """
    from sqlalchemy import select

    from cogni_agent.control_plane_service.models import AppFollowedQuestion
    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.gateway_service.models import AppPrewarmCache, AppQuestionBank

    async with common_db.get_sessionmaker()() as db:
        bank = AppQuestionBank(id="q-src", scenic_id=None, theme_code="ticket",
                               question_text="本月各平台销售额", time_period="today",
                               priority="P1", return_format="auto", active=True)
        db.add(bank)
        db.add(AppPrewarmCache(scenic_id=SCENIC, question_id="q-src", task_id="t-1",
                               data_as_of=now_cst(), answer="预热答案"))
        db.add(AppFollowedQuestion(user_id="u-app", question_text="本月各平台销售额"))
        await db.commit()
        await _add_issue(db, "issue_del", source_question_id="q-src")

        await issues.delete_issue(db, "issue_del")

    async with common_db.get_sessionmaker()() as db:
        assert (await db.execute(select(AppQuestionBank.id))).scalars().all() == []
        assert (await db.execute(select(AppPrewarmCache.id))).scalars().all() == []
        assert (await db.execute(select(AppFollowedQuestion.id))).scalars().all() == []
        assert str(await issues.business_issue_list(db, SCENIC)) == "[]"


@run_async
async def test_deleted_issue_is_not_resurrected_by_question_bank_sync():
    """删除是**全局**的:不该被下一次问题库同步悄悄复活。"""
    from sqlalchemy import select

    from cogni_agent.gateway_service import issue_service as issues
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.gateway_service.models import AppQuestionBank

    async with common_db.get_sessionmaker()() as db:
        bank = AppQuestionBank(id="q-src", scenic_id=None, theme_code="ticket",
                               question_text="本月各平台销售额", time_period="today",
                               priority="P1", return_format="auto", active=True)
        db.add(bank)
        await db.commit()
        await _add_issue(db, "issue_del", source_question_id="q-src")
        await issues.delete_issue(db, "issue_del")

    async with common_db.get_sessionmaker()() as db:
        left = (await db.execute(select(ScenicAgentIssueDetail.issue_id))).scalars().all()
        assert left == [], left


# ══════════════════════════════════════════════════════════════════════
# 四、解读 / 报告自动挂到对应板块
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_model_instruction_is_editable_on_a_fixed_question():
    """口径说明要能按问题单独设:主题解读那五条的口径原先写死在代码里,改不了。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c1", name="连接", owner_user_id="u-1",
                             visibility="group", scenic_id=None, active=True))
        await db.commit()

    me = _actor("super_admin", None, user_id="u-1")
    out = await qsvc.save_fixed(me, {
        "connection_id": "c1", "question": "【主题解读】客群", "capability": "report",
        "domain": "客群", "model_instruction": "围绕客群结构,先结论后数据,最后给 2-3 条建议"})
    assert out["model_instruction"].startswith("围绕客群结构")

    out2 = await qsvc.save_fixed(me, {"model_instruction": "改过的口径"}, out["id"])
    assert out2["model_instruction"] == "改过的口径"


# ══════════════════════════════════════════════════════════════════════
# 五、只发了问题原文也要认得出来
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_issue_is_matched_by_text_when_no_id_is_sent():
    """关注问题、历史消息里点回去这些入口只存了**文本**,没有 issue_id。

    认不出来的话有两种表现,都很糟:一条配在 Quick BI 上的问题被本地 Agent 接走,
    答出来一个看着像模像样、其实没查过数的结果;或者前端干脆拦在
    「暂无可用智能体,请联系管理员分发」上 —— 而用户点的明明是一条配好的问题。
    """
    from cogni_agent.gateway_service import scenic_binding as binding

    async with common_db.get_sessionmaker()() as db:
        await _add_issue(db, "issue_text", scenic=SCENIC,
                         title="年龄分布", issue="年龄分布:按标签值看占比的分布")
        await _add_issue(db, "issue_group", scenic=None,
                         title="集团通用", issue="集团通用的问题")
        await _add_issue(db, "issue_other", scenic=OTHER,
                         title="别家的", issue="别的景区的问题")

        hit = await binding.match_issue_by_text(db, SCENIC, "年龄分布:按标签值看占比的分布")
        assert hit is not None and hit.issue_id == "issue_text"
        assert (await binding.match_issue_by_text(db, SCENIC, "年龄分布")).issue_id == "issue_text"
        # 集团级的也认得(所有景区共用);别的景区的认不出来
        assert (await binding.match_issue_by_text(db, SCENIC, "集团通用的问题")).issue_id == "issue_group"
        assert await binding.match_issue_by_text(db, SCENIC, "别的景区的问题") is None
        assert await binding.match_issue_by_text(db, SCENIC, "一句没配过的自由提问") is None


def test_app_chat_falls_back_to_text_matching():
    """/app/chat 的解析顺序:issue_id → question_id → **问题原文**。
    最后这一档必须在源码里 —— 少了它,上面那条匹配能力等于没接上。"""
    import inspect

    from cogni_agent.gateway_service import routes_app

    src = inspect.getsource(routes_app.app_chat)
    assert "binding.match_issue_by_text" in src
    assert "theme_of_message" not in src, "主题解读兜底已移除:主题卡片不再依赖主题解读配置"


# ══════════════════════════════════════════════════════════════════════
# 六、景区账号用本景区的 Quick BI 连接
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_scenic_member_can_use_any_connection_bound_to_their_scenic():
    """现场那句「Quick BI 连接不存在或无权访问:290987f7-…」。

    连接绑着这个景区、名字就叫这个人的名字,她却用不了 —— 因为可见范围是 private。
    归属景区本身就是一次授权:管理员把连接绑到这个景区,意思就是「这个景区的人用它」。
    private 的本意是「**没绑景区**时只有创建者能用」,不该拿来否决一条明确绑给你的连接。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        for i, vis in enumerate(("private", "scenic", "group", "public")):
            db.add(QbiConnection(id=f"mine-{vis}", name=f"本景区/{vis}", owner_user_id="u-admin",
                                 visibility=vis, scenic_id=SCENIC, active=True))
            db.add(QbiConnection(id=f"other-{vis}", name=f"别的景区/{vis}", owner_user_id="u-admin",
                                 visibility=vis, scenic_id=OTHER, active=True))
            db.add(QbiConnection(id=f"grp-{vis}", name=f"集团级/{vis}", owner_user_id="u-admin",
                                 visibility=vis, scenic_id=None, active=True))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    async with common_db.get_sessionmaker()() as db:
        for vis in ("private", "scenic", "group", "public"):
            assert await qsvc._row_visible(db, me, f"mine-{vis}"), f"本景区 {vis} 应当可用"
        # 别的景区的一律不可见 —— 例外只有 public:那是管理员显式选的「全平台共用」,
        # 不是疏忽,所以照它的字面意思办
        for vis in ("private", "scenic", "group"):
            assert not await qsvc._row_visible(db, me, f"other-{vis}"), f"别的景区 {vis} 不该可见"
        assert await qsvc._row_visible(db, me, "other-public"), "public 是显式的全平台共用"
        # 集团级:group/public 是共用的;scenic 在集团级上是无意义组合,按 group 处理
        for vis in ("group", "public", "scenic"):
            assert await qsvc._row_visible(db, me, f"grp-{vis}"), f"集团级 {vis} 应当可用"
        assert not await qsvc._row_visible(db, me, "grp-private"), "集团级 private 仍然只有创建者能用"


@run_async
async def test_invisible_connection_says_why():
    """看不到的时候要说清**为什么**。

    一句「不存在或无权访问」让人无从下手:是 ID 错了?连接被删了?还是权限问题?
    三种猜法方向完全不同,而运维手上只有这一句话。
    """
    from cogni_agent.common.errors import NotFound
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection, QbiGrant

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-other", name="别家的连接", owner_user_id="u-admin",
                             visibility="group", scenic_id=OTHER, active=True))
        db.add(QbiConnection(id="c-priv", name="私有连接", owner_user_id="u-admin",
                             visibility="private", scenic_id=None, active=True))
        db.add(QbiConnection(id="c-wl", name="白名单连接", owner_user_id="u-admin",
                             visibility="group", scenic_id=None, active=True))
        db.add(QbiGrant(connection_id="c-wl", user_id="someone-else"))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    with pytest.raises(NotFound) as e1:
        await qsvc.get_connection(me, "c-other")
    assert OTHER in str(e1.value) and "跨景区" in str(e1.value)

    with pytest.raises(NotFound) as e2:
        await qsvc.get_connection(me, "c-priv")
    assert "private" in str(e2.value) and "归属景区" in str(e2.value)

    with pytest.raises(NotFound) as e3:
        await qsvc.get_connection(me, "c-wl")
    assert "白名单" in str(e3.value) and "分发中心" in str(e3.value)

    with pytest.raises(NotFound) as e4:
        await qsvc.get_connection(me, "根本不存在的 id")
    assert "不存在" in str(e4.value)


# ══════════════════════════════════════════════════════════════════════
# 七、集团角色在管理端看得到全部连接
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_group_roles_see_every_connection_including_others_private():
    """「我切换我自己为超级管理员也不行」。

    上一条(六)修的是景区账号用不了绑给自己景区的 private 连接;这条修的是另一头:
    景区管理员建的那条 private 连接,**超级管理员在后台也看不见** ——
    可见范围里集团角色只放行 group/public,别人的 private 一律不在列表里。
    两头都看不见,于是一条连接谁都改不了,只能去数据库里改。

    private 的本意是「别人别用我的凭证」,不是「集团管不着」。集团角色(含只读)
    本来就该看见全平台的配置 —— 看得见才谈得上帮人把归属景区/可见范围改对。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        # 就是现场那条:景区管理员本人建的、private、绑着自己的景区
        db.add(QbiConnection(id="yin-private", name="尹开英", owner_user_id="u-yin",
                             visibility="private", scenic_id=SCENIC, active=True))
        db.add(QbiConnection(id="other-private", name="别家私有", owner_user_id="u-x",
                             visibility="private", scenic_id=OTHER, active=True))
        db.add(QbiConnection(id="loose-private", name="没绑景区的私有", owner_user_id="u-x",
                             visibility="private", scenic_id=None, active=True))
        await db.commit()

    for role in ("super_admin", "group_admin", "group_developer", "group_viewer"):
        me = _actor(role, None, user_id="u-boss")
        async with common_db.get_sessionmaker()() as db:
            for cid in ("yin-private", "other-private", "loose-private"):
                assert await qsvc._row_visible(db, me, cid), f"{role} 应当看得到 {cid}"
        # 看得见 = 能取到,而不是抛「连接不存在或无权访问」
        v = await qsvc.get_connection(me, "yin-private")
        assert v.name == "尹开英"

    # 景区账号的口径不变:别家的 private 仍然看不到(这条修的是集团侧,不是把闸门拆了)
    scenic = _actor("scenic_admin", SCENIC, user_id="u-yin")
    async with common_db.get_sessionmaker()() as db:
        assert await qsvc._row_visible(db, scenic, "yin-private")
        assert not await qsvc._row_visible(db, scenic, "other-private")
        assert not await qsvc._row_visible(db, scenic, "loose-private")


@run_async
async def test_group_roles_are_implicitly_whitelisted():
    """**集团用户都算在白名单里**(含集团只读)。

    白名单是给景区之间做隔离的:「这条连接只给这几个人用」说的是景区侧的人,
    不是集团侧的管理员。原先只豁免超管,于是有一种死配置 ——
    一条集团级连接分配给了某个人,那人离职/换岗之后连集团管理员都用不了它,
    也就没人能把它重新分配出去,只能进数据库改。

    景区侧的口径不变:不在名单里、也不归你景区,就是用不了。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection, QbiGrant

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-wl2", name="集团级白名单连接", owner_user_id="u-x",
                             visibility="group", scenic_id=None, active=True))
        db.add(QbiGrant(connection_id="c-wl2", user_id="someone-else"))
        await db.commit()

    async with common_db.get_sessionmaker()() as db:
        for role in ("super_admin", "group_admin", "group_developer", "group_viewer"):
            assert await qsvc._row_visible(db, _actor(role, None, "u-boss"), "c-wl2"), \
                f"{role} 天然在白名单里"
        # 景区侧照旧:白名单是拿来隔离景区的,这一条没放开
        assert not await qsvc._row_visible(db, _actor("scenic_admin", OTHER, "u-out"), "c-wl2"), \
            "景区账号不在名单里、连接也不归它景区 —— 仍然用不了"


@run_async
async def test_no_connection_can_end_up_usable_by_nobody():
    """**任何一条连接都至少有人能用** —— 改归属/改可见范围不该改出一条死配置。

    这是前面几条的总闸:景区侧看归属景区,集团侧永远能用。少了任何一边,
    都会出现「谁都用不了、也就谁都改不了、只能进数据库改」的连接 —— 现场已经撞到两次。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection, QbiGrant

    combos = []
    for vis in ("private", "scenic", "group", "public"):
        for tag, sid in (("bound", SCENIC), ("grp", None)):
            for wl in (False, True):
                cid = f"{vis}-{tag}-{'wl' if wl else 'open'}"
                combos.append((cid, vis, sid, wl))

    async with common_db.get_sessionmaker()() as db:
        for cid, vis, sid, wl in combos:
            # owner 一律设成一个「已经不在了」的人:死配置正是这么来的
            db.add(QbiConnection(id=cid, name=cid, owner_user_id="u-gone",
                                 visibility=vis, scenic_id=sid, active=True))
            if wl:
                db.add(QbiGrant(connection_id=cid, user_id="u-also-gone"))
        await db.commit()

    # 平台上真实存在的各类账号(不含已离开的 owner)
    actors = [_actor(r, None, f"u-{r}") for r in
              ("super_admin", "group_admin", "group_developer", "group_viewer")]
    actors += [_actor("scenic_admin", SCENIC, "u-s1"), _actor("scenic_operator", SCENIC, "u-s2"),
               _actor("scenic_admin", OTHER, "u-s3")]

    async with common_db.get_sessionmaker()() as db:
        for cid, vis, sid, wl in combos:
            who = [a.role for a in actors if await qsvc._row_visible(db, a, cid)]
            assert who, f"{cid}(visibility={vis} scenic={sid} 白名单={wl})谁都用不了"


# ══════════════════════════════════════════════════════════════════════
# 八、改了角色/归属,当场生效(不用重新登录)
# ══════════════════════════════════════════════════════════════════════

def _token(user_id: str, role: str, scenic_id, iat=None):
    """签一个与登录接口同构的 access_token(故意带上**旧**的角色/景区)。"""
    import time

    from jose import jwt

    from cogni_agent.config import get_settings

    cfg = get_settings().get("user_center", default={})
    return jwt.encode({"sub": user_id, "role": role, "scenic_id": scenic_id,
                       "iat": int(iat if iat is not None else time.time()),
                       "exp": int(time.time()) + 3600},
                      cfg["jwt_secret"], algorithm=cfg.get("jwt_algorithm", "HS256"))


@run_async
async def test_role_and_scenic_changes_take_effect_without_relogin():
    """「这个我切换我自己为超级管理员也不行」。

    JWT 是登录那一刻的快照。管理员在「组织管理」里把自己改成超级管理员之后,
    手里的令牌还带着 scenic_admin —— 于是改了不生效,而且**一声不响**,
    只是照旧角色判权限。要重新登录才生效,没人猜得到,界面上也没有任何地方这么说。

    现在角色与归属景区一律以用户表为准(那一行本来就要读,用来判凭据是否变更)。
    """
    from cogni_agent.gateway_service.deps import get_current_user
    from cogni_agent.user_center_service.models import User

    async with common_db.get_sessionmaker()() as db:
        db.add(User(id="u-yin", name="尹开英", account="yin", phone="13000000001",
                    password_hash="x", scenic_id=SCENIC, role="scenic_admin", is_active=True))
        await db.commit()

    tok = _token("u-yin", "scenic_admin", SCENIC)      # 登录时签的:景区管理员
    me = await get_current_user(f"Bearer {tok}")
    assert (me.role, me.scenic_id) == ("scenic_admin", SCENIC)

    # 管理员把他改成超级管理员(集团级,不绑景区)—— 令牌一个字没变
    async with common_db.get_sessionmaker()() as db:
        u = await db.get(User, "u-yin")
        u.role, u.scenic_id = "super_admin", None
        await db.commit()

    me = await get_current_user(f"Bearer {tok}")
    assert me.role == "super_admin", "改了角色要当场生效,不该等到重新登录"
    assert me.scenic_id is None, "归属景区同理"

    # 换到另一个景区也是当场生效
    async with common_db.get_sessionmaker()() as db:
        u = await db.get(User, "u-yin")
        u.role, u.scenic_id = "scenic_admin", OTHER
        await db.commit()
    me = await get_current_user(f"Bearer {tok}")
    assert (me.role, me.scenic_id) == ("scenic_admin", OTHER)


@run_async
async def test_disabled_account_cannot_keep_using_an_old_token():
    """停用只写了库,不拦令牌的话,被停用的账号还能拿旧令牌用到过期 —— 「停用」形同虚设。"""
    from cogni_agent.common.errors import Unauthorized
    from cogni_agent.gateway_service.deps import get_current_user
    from cogni_agent.user_center_service.models import User

    async with common_db.get_sessionmaker()() as db:
        db.add(User(id="u-off", name="离职的人", account="off", phone="13000000002",
                    password_hash="x", scenic_id=SCENIC, role="scenic_operator", is_active=True))
        await db.commit()

    tok = _token("u-off", "scenic_operator", SCENIC)
    assert (await get_current_user(f"Bearer {tok}")).user_id == "u-off"

    async with common_db.get_sessionmaker()() as db:
        u = await db.get(User, "u-off")
        u.is_active = False
        await db.commit()

    with pytest.raises(Unauthorized):
        await get_current_user(f"Bearer {tok}")


# ══════════════════════════════════════════════════════════════════════
# 九、问题指着一条已被删除的连接
# ══════════════════════════════════════════════════════════════════════

DEAD_CONN = "290987f7-30d8-4e80-aa8d-c93103969571"


async def _issue_pointing_at(conn_id: str | None, scenic=SCENIC):
    """建一条指着 conn_id 的 Quick BI 问题。

    返回 (主键, 业务主键) —— 统一问题表的 id 是自增 BIGINT,而发起时用的是 issue_id。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    async with common_db.get_sessionmaker()() as db:
        row = ScenicAgentIssueDetail(
            title="【主题解读】OTA直销", issue="【主题解读】OTA直销",
            business_class="quick_bi", capability="report", connection_id=conn_id,
            scenic_id=scenic, active=True)
        db.add(row)
        await db.commit()
        return row.id, row.issue_id


@run_async
async def test_question_bound_to_a_deleted_connection_falls_back_by_scenic():
    """现场那句「Quick BI 连接不存在:290987f7-…」。

    注意它和之前那句**不是一个错**:少了「或无权访问」—— 这是 `db.get` 返回 None,
    也就是数据库里真的没有这一行。连接被删了,而指着它的问题照样存着那串 id
    (裸 String,没有外键级联),于是问题还在、景区还在、人也没问题,一点就报错,
    而那串 id 运营查都查不到(行已经删了)。

    APP 端早有这道解析(scenic_binding.connection_usable),**管理端没有** ——
    所以同一条问题从 APP 点能跑,从 cogni-agent 页面点就报错。现在放在 run_stream 底下,两边同一套。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-live", name="本景区在用的连接", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    # 已删除 → 按景区重挑
    assert await qsvc.usable_connection_id(me, DEAD_CONN, SCENIC) == "c-live"
    # 已停用 → 同样重挑(停用的连接一样跑不了,只是报错的话换一句)
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-off", name="停用的", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=False))
        await db.commit()
    assert await qsvc.usable_connection_id(me, "c-off", SCENIC) == "c-live"
    # 还能用的那条不动
    assert await qsvc.usable_connection_id(me, "c-live", SCENIC) == "c-live"
    # 一条都没有时返回 None(由调用方给「去哪配」的提示,而不是抛一串 id)
    other = _actor("scenic_admin", OTHER, user_id="u-x")
    assert await qsvc.usable_connection_id(other, DEAD_CONN, OTHER) is None


@run_async
async def test_deleting_a_connection_in_use_is_refused():
    """删连接不能把一批问题删成「点了就报错」。

    原先 delete_connection 直接 `db.delete(row)`,不看有没有人引用 —— 现场那条死引用
    就是这么来的。现在默认拒绝并说清被谁引用;确实要删就 force,
    同时把问题上的连接清空(之后按景区自动重挑),而不是留下一个指着已删除连接的死 id。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-doomed", name="要删的连接", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        await db.commit()
    pk, _ = await _issue_pointing_at("c-doomed")

    boss = _actor("super_admin", None, user_id="u-root")
    with pytest.raises(ValidationError) as e:
        await qsvc.delete_connection(boss, "c-doomed")
    assert "引用" in str(e.value) and "问题" in str(e.value)

    # 强制删:连接没了,但问题上的连接被清空,不会留下死 id
    r = await qsvc.delete_connection(boss, "c-doomed", force=True)
    assert r["issues_cleared"] == 1
    async with common_db.get_sessionmaker()() as db:
        assert (await db.get(ScenicAgentIssueDetail, pk)).connection_id is None
        assert await db.get(QbiConnection, "c-doomed") is None


@run_async
async def test_run_stream_routes_the_question_connection_through_the_resolver():
    """钉住**接线**,不只是解析函数本身。

    解析函数写好了但没接到 run_stream 上,现场表现和没写一模一样 ——
    这条就是防这个:走 run_stream,断言报的是「这个景区还没有可用的连接(去哪配)」,
    而不是那句谁都看不懂的「Quick BI 连接不存在:<一串 id>」。
    """
    from cogni_agent.common.errors import NotFound, ValidationError
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    # 景区 OTHER 下一条连接都没有,问题却指着一条已被删除的连接
    _, issue_id = await _issue_pointing_at(DEAD_CONN, scenic=OTHER)

    me = _actor("scenic_admin", OTHER, user_id="u-x")
    with pytest.raises(ValidationError) as e:
        async for _ in qsvc.run_stream(me, connection_id=None, capability=None, question="",
                                       fixed_question_id=issue_id):
            break
    msg = str(e.value)
    assert "还没有可用的 Quick BI 连接" in msg, msg
    assert DEAD_CONN not in msg, "别再把那串查不到的 id 甩给运营"

    # 顺带确认它不是 NotFound(「连接不存在」那条分支)
    try:
        async for _ in qsvc.run_stream(me, connection_id=None, capability=None, question="",
                                       fixed_question_id=issue_id):
            break
    except NotFound:
        pytest.fail("不该再走「Quick BI 连接不存在」那条分支")
    except ValidationError:
        pass


# ══════════════════════════════════════════════════════════════════════
# 十、资产 / 历史报告绑在一条已删除的连接上
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_asset_on_a_deleted_connection_says_what_to_do():
    """现场:24 条数据资产全指着一条已删除的连接。

    资产不是「历史留痕」—— 报告/解读的目标数据就是它。原先的提示是
    「属于另一个 Quick BI 连接(290987f7-…),请切换到该连接」,而那条连接已经删了,
    切不过去,那串 id 也查不到任何东西。两种情况下一步完全不同,得分开说。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-live", name="尹开英", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        db.add(QbiConnection(id="c-other", name="另一套凭证", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    view = await qsvc.get_connection(me, "c-live")

    # 连接还在 → 提示切过去,并且说的是**名字**不是 id
    with pytest.raises(qsvc.QuickBIError) as e1:
        await qsvc._same_connection(view, "c-other", "资产「票务-景区当日客流」")
    assert "另一套凭证" in str(e1.value) and "切换" in str(e1.value)

    # 连接已删 → 说清「已被删除」和改绑怎么做,不再叫人去切一条不存在的连接
    with pytest.raises(qsvc.QuickBIError) as e2:
        await qsvc._same_connection(view, DEAD_CONN, "资产「票务-景区当日客流」")
    msg = str(e2.value)
    assert "已被删除" in msg and "改绑" in msg
    assert "请切换到该连接" not in msg, "切不过去,别这么提示"


@run_async
async def test_old_report_whose_connection_is_gone_still_opens():
    """主题解读卡片会把「我上次跑过的那次」拉出来刷新 —— 连接删了之后这一刷就炸。

    那份报告明明早就跑完、结果存在库里。一条历史记录拉不到连接,最多是
    「刷不出更新的进度」,不该让整张卡片打不开。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection, QbiRun

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-live", name="尹开英", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        db.add(QbiRun(id="r-old", connection_id=DEAD_CONN, capability="report",
                      question="【主题解读】OTA直销", status="running", chat_id="chat-1",
                      scenic_id=SCENIC, user_id="u-yin", source="console"))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    r = await qsvc.refresh_report(me, "r-old")
    assert r["status"] == "failed"
    assert "已被删除" in (r.get("error") or "")
    assert DEAD_CONN not in (r.get("error") or ""), "别把那串查不到的 id 甩给用户"

    # 流式入口同理:给一个收尾的 done,而不是抛异常把整条流打断
    evts = [e async for e in qsvc.stream_report(me, "r-old")]
    assert evts[-1]["type"] == "done" and evts[-1]["status"] == "failed"


@run_async
async def test_deleting_an_asset_in_use_is_refused():
    """删资产和删连接是同一个毛病:裸 id、无级联、删了不报错,直到有人点。

    资产 id 存在问题行的 target_data_id / target 里。删掉资产,那些问题就变成
    「点了就报错」,而报错给的是一串已经查不到的 id ——
    现场那句「资产不存在或无权访问:fa9f3b14-…」就是这个家族的。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset, QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c-live", name="尹开英", owner_user_id="u-admin",
                             visibility="group", scenic_id=SCENIC, active=True))
        db.add(QbiAsset(id="a-1", connection_id="c-live", kind="dashboard",
                        name="票务-景区当日客流", ref_id="p-1", scenic_id=SCENIC,
                        owner_user_id="u-admin"))
        # 两种指法都要认:列上的 target_data_id,和 target JSON 里的 asset_id
        db.add(ScenicAgentIssueDetail(title="票务解读", issue="票务解读",
                                      business_class="quick_bi", capability="report",
                                      connection_id="c-live", target_data_id="a-1",
                                      scenic_id=SCENIC, active=True))
        db.add(ScenicAgentIssueDetail(title="票务解读2", issue="票务解读2",
                                      business_class="quick_bi", capability="report",
                                      connection_id="c-live", target={"asset_id": "a-1"},
                                      scenic_id=SCENIC, active=True))
        await db.commit()

    boss = _actor("super_admin", None, user_id="u-root")
    with pytest.raises(ValidationError) as e:
        await qsvc.delete_asset(boss, "a-1")
    assert "2 条问题" in str(e.value), str(e.value)

    # force 是 cascade 的旧名,保留兼容:连带删掉那两条问题
    r = await qsvc.delete_asset(boss, "a-1", force=True)
    assert r["deleted"]["问题"] == 2
    async with common_db.get_sessionmaker()() as db:
        assert await db.get(QbiAsset, "a-1") is None


# ══════════════════════════════════════════════════════════════════════
# 十一、删连接前:能继承的转移,强绑定的停用
# ══════════════════════════════════════════════════════════════════════

async def _conn(cid, name, *, api_key="ak-1", domain="https://bi.aliyun.com", scenic=SCENIC):
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        # 密文列由服务层加密写入;这里直接构造行,给 api_key(明文列)即可 ——
        # 「是不是同一个工作空间」正是按 server_domain + api_key 判的
        db.add(QbiConnection(id=cid, name=name, owner_user_id="u-admin", visibility="group",
                             scenic_id=scenic, active=True, server_domain=domain,
                             api_key=api_key, use_trial=False))
        await db.commit()


@run_async
async def test_transfer_inherits_within_the_same_workspace():
    """同一个 Quick BI 工作空间(同域名 + 同 api_key)→ 资产整体继承,继续可用。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-old", "旧连接")
    await _conn("c-new", "新连接")                      # 同 api_key = 同工作空间
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id="a-1", connection_id="c-old", kind="dashboard", name="票务看板",
                        ref_id="p-1", owner_user_id="u-admin", scenic_id=SCENIC))
        await db.commit()

    boss = _actor("super_admin", None, user_id="u-root")
    r = await qsvc.transfer_connection(boss, "c-old", "c-new")
    assert r["inherited"] is True
    assert r["moved"].get("数据资产") == 1 and not r["disabled"].get("数据资产")

    async with common_db.get_sessionmaker()() as db:
        a = await db.get(QbiAsset, "a-1")
        assert a.connection_id == "c-new" and a.active is True


@run_async
async def test_transfer_across_workspaces_disables_instead_of_moving():
    """不同工作空间 → 资产**停用**,不是硬转过去。

    资产存的 ref_id 是 Quick BI 那边的 pageId,换一套凭证指不到任何东西。
    硬转过去只是把死引用挪个地方 —— 停用是看得见、可恢复的,而死引用点了才报错。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset, QbiFile

    await _conn("c-old", "旧连接", api_key="ak-1")
    await _conn("c-new", "别家的连接", api_key="ak-2")   # 不同 api_key = 不同工作空间
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id="a-1", connection_id="c-old", kind="dashboard", name="票务看板",
                        ref_id="p-1", owner_user_id="u-admin", scenic_id=SCENIC))
        db.add(QbiFile(id="f-1", connection_id="c-old", file_name="客流.xlsx",
                       owner_user_id="u-admin", scenic_id=SCENIC))
        await db.commit()

    boss = _actor("super_admin", None, user_id="u-root")
    r = await qsvc.transfer_connection(boss, "c-old", "c-new")
    assert r["inherited"] is False
    assert r["disabled"].get("数据资产") == 1
    # 上传文件是强绑定(fileId 绑 user_token),同工作空间也继承不了 —— 一律停用
    assert r["disabled"].get("上传文件") == 1

    async with common_db.get_sessionmaker()() as db:
        a = await db.get(QbiAsset, "a-1")
        assert a.active is False and "工作空间" in (a.disabled_reason or "")
        assert (await db.get(QbiFile, "f-1")).active is False

    # 停用的资产:看得见(列表里还在),但用不了,而且要说清为什么
    with pytest.raises(Exception) as e:
        await qsvc.get_asset(boss, "a-1")
    assert "已停用" in str(e.value)


@run_async
async def test_continuing_an_old_session_whose_connection_is_gone():
    """截图里的那一幕:打开一条 9/18 的老会话继续问,撞上「连接不存在」。

    我一开始把「会话」归成了历史留痕 —— 错的。**接着聊就是一次新发起**,
    它用的是会话行上粘着的那条连接。同样的问题新开一个会话反而正常,
    差别就在这里。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiSession

    await _conn("c-live", "尹开英")
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiSession(id="s-old", connection_id=DEAD_CONN, title="年龄分布",
                          capability="dashboard_qa", scenic_id=SCENIC,
                          user_id="u-yin", target={}))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    # 解析出能用的那条,并把会话更正过来(免得每轮重算)
    assert await qsvc.usable_connection_id(me, DEAD_CONN, SCENIC) == "c-live"

    evts = []
    with contextlib.suppress(Exception):
        async for e in qsvc.chat(me, "s-old", "OTA雷达图最近是怎么分布的"):
            evts.append(e)
            if len(evts) > 3:
                break
    async with common_db.get_sessionmaker()() as db:
        assert (await db.get(QbiSession, "s-old")).connection_id == "c-live", \
            "会话上的死连接要被更正,而不是每轮都撞一次"


# ══════════════════════════════════════════════════════════════════════
# 十二、目标数据决定用哪条连接
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_picking_a_board_switches_the_connection_automatically():
    """选了哪块板,连接就没有第二种选法 —— 不该再让人先把连接挑对。

    现场:两块仪表板,操练台的下拉里只出现一块。因为下拉按「当前连接」筛资产,
    而另一块登记在别的连接下 —— 看起来像「这块板没登记」,它却在数据资产页里好好列着。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    await _conn("c-b", "连接B")
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id="board-b", connection_id="c-b", kind="dashboard", name="九龙潭名胜风景区",
                        ref_id="p-9", owner_user_id="u-admin", scenic_id=SCENIC))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    # 当前连接是 A,但选的板属于 B → 自动切到 B
    got = await qsvc.connection_for_target(me, {"asset_id": "board-b"}, "c-a")
    assert got == "c-b"
    # 没选目标数据时不动
    assert await qsvc.connection_for_target(me, {}, "c-a") == "c-a"
    # 本来就对得上时也不动
    assert await qsvc.connection_for_target(me, {"asset_id": "board-b"}, "c-b") == "c-b"


@run_async
async def test_auto_switch_still_checks_the_actor_can_use_that_connection():
    """自动切连接不能变成「借目标数据越权换凭证」—— 切过去的那条本来就得是他能用的。"""
    from cogni_agent.common.errors import NotFound
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    await _conn("c-mine", "本景区的", scenic=SCENIC)
    await _conn("c-theirs", "别的景区的", scenic=OTHER)
    async with common_db.get_sessionmaker()() as db:
        # 集团级资产(所有景区可见),但它登记在别的景区专属的连接下
        db.add(QbiAsset(id="a-x", connection_id="c-theirs", kind="dashboard", name="别家的板",
                        ref_id="p-x", owner_user_id="u-admin", scenic_id=None))
        await db.commit()

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    with pytest.raises(NotFound):
        await qsvc.connection_for_target(me, {"asset_id": "a-x"}, "c-mine")


@run_async
async def test_assets_are_listed_for_group_and_own_scenic():
    """集团用户看得到全部;景区用户看得到「集团级 + 自己景区」,别人的看不到。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id="a-grp", connection_id="c-a", kind="dashboard", name="泰宁双景区大数据平台",
                        ref_id="p-1", owner_user_id="u-admin", scenic_id=None))
        db.add(QbiAsset(id="a-mine", connection_id="c-a", kind="dashboard", name="九龙潭名胜风景区",
                        ref_id="p-2", owner_user_id="u-admin", scenic_id=SCENIC))
        db.add(QbiAsset(id="a-other", connection_id="c-a", kind="dashboard", name="别家的板",
                        ref_id="p-3", owner_user_id="u-admin", scenic_id=OTHER))
        await db.commit()

    boss = _actor("super_admin", None, user_id="u-root")
    assert {a["id"] for a in await qsvc.list_assets(boss)} == {"a-grp", "a-mine", "a-other"}

    me = _actor("scenic_admin", SCENIC, user_id="u-yin")
    ids = {a["id"] for a in await qsvc.list_assets(me, scenic_id=SCENIC)}
    assert ids == {"a-grp", "a-mine"}, "集团级 + 本景区的都要看得到,别家的看不到"


# ══════════════════════════════════════════════════════════════════════
# 十三、删仪表板 = 连带删它下面的数据集与问题
# ══════════════════════════════════════════════════════════════════════

async def _board_with_cubes(aid, name, cubes, conn="c-a", scenic=SCENIC):
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id=aid, connection_id=conn, kind="dashboard", name=name, ref_id=f"p-{aid}",
                        owner_user_id="u-admin", scenic_id=scenic,
                        meta={"cubes": [{"cube_id": c, "cube_name": c} for c in cubes]}))
        await db.commit()


async def _dataset(aid, cube, conn="c-a", scenic=SCENIC):
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiAsset(id=aid, connection_id=conn, kind="dataset", name=f"集-{cube}", ref_id=cube,
                        owner_user_id="u-admin", scenic_id=scenic))
        await db.commit()


@run_async
async def test_deleting_a_board_cascades_to_its_datasets_and_questions():
    """删仪表板要把它下面的数据集与问题一起删掉,并且**先把清单摆出来**。

    数据集是板里用到的那些表,问题是打在板/这些表上的 —— 单独删板、把它们留着,
    就是又一批「点了才报错」的死引用。而一句「确定删除吗」不说清会连带删掉多少东西,
    人按下确定时并不知道自己同意了什么。
    """
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    await _board_with_cubes("b-1", "九龙潭名胜风景区", ["cube-1", "cube-2"])
    await _dataset("d-1", "cube-1")
    await _dataset("d-2", "cube-2")
    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(title="打在板上的问题", issue="q1", business_class="quick_bi",
                                      target_data_id="b-1", scenic_id=SCENIC, active=True))
        db.add(ScenicAgentIssueDetail(title="打在集上的问题", issue="q2", business_class="quick_bi",
                                      target={"asset_id": "d-1"}, scenic_id=SCENIC, active=True))
        await db.commit()

    boss = _actor("super_admin", None, user_id="u-root")

    plan = await qsvc.asset_delete_plan(boss, "b-1")
    assert plan["needs_confirm"] is True
    assert {d["name"] for d in plan["datasets"]} == {"集-cube-1", "集-cube-2"}
    assert len(plan["questions"]) == 2

    # 没确认(cascade=False)时拒绝,并把代价说出来
    with pytest.raises(ValidationError) as e:
        await qsvc.delete_asset(boss, "b-1")
    assert "2 个数据集" in str(e.value) and "2 条问题" in str(e.value)

    r = await qsvc.delete_asset(boss, "b-1", cascade=True)
    assert r["deleted"]["数据集"] == 2 and r["deleted"]["问题"] == 2

    async with common_db.get_sessionmaker()() as db:
        for aid in ("b-1", "d-1", "d-2"):
            assert await db.get(QbiAsset, aid) is None
        from sqlalchemy import func, select as _select
        n = (await db.execute(_select(func.count()).select_from(ScenicAgentIssueDetail))).scalar_one()
        assert n == 0, "APP 与管理端读的是同一张表,删了就都不显示"


@run_async
async def test_cascade_keeps_datasets_shared_with_another_board():
    """别的板也在用的数据集**不能**跟着删 —— 那块板下面会凭空少一个集,而它的人毫不知情。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    await _board_with_cubes("b-1", "板一", ["cube-1", "cube-share"])
    await _board_with_cubes("b-2", "板二", ["cube-share"])
    await _dataset("d-own", "cube-1")
    await _dataset("d-share", "cube-share")

    boss = _actor("super_admin", None, user_id="u-root")
    plan = await qsvc.asset_delete_plan(boss, "b-1")
    assert {d["id"] for d in plan["datasets"]} == {"d-own"}, "共用的 d-share 不该在删除清单里"

    await qsvc.delete_asset(boss, "b-1", cascade=True)
    async with common_db.get_sessionmaker()() as db:
        assert await db.get(QbiAsset, "d-own") is None
        assert await db.get(QbiAsset, "d-share") is not None
        assert await db.get(QbiAsset, "b-2") is not None


# ══════════════════════════════════════════════════════════════════════
# 十四、数据集登记时记下「来自哪块仪表板」
# ══════════════════════════════════════════════════════════════════════

@run_async
async def test_batch_register_records_the_source_board():
    """按仪表板拉、多选登记,并把来源板记下来。

    在此之前这层归属是**猜**的(拿板 meta.cubes 的 cube_id 反查集的 ref_id),
    而它现在是有后果的 —— 删板要连带删集,归属算错就是删错东西。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    await _board_with_cubes("b-1", "九龙潭名胜风景区", ["cube-1", "cube-2", "cube-3"])

    boss = _actor("super_admin", None, user_id="u-root")

    # 板里用到哪些集,以及哪些登记过了
    r = await qsvc.board_cubes(boss, "b-1")
    assert {c["cube_id"] for c in r["items"]} == {"cube-1", "cube-2", "cube-3"}
    assert all(c["registered"] is False for c in r["items"])

    out = await qsvc.register_datasets(
        boss, "c-a", [{"cube_id": "cube-1"}, {"cube_id": "cube-2"}], source_asset_id="b-1")
    assert out["created"] == 2

    async with common_db.get_sessionmaker()() as db:
        from sqlalchemy import select as _select
        rows = (await db.execute(_select(QbiAsset).where(QbiAsset.kind == "dataset"))).scalars().all()
        assert {r.source_asset_id for r in rows} == {"b-1"}
        # 归属景区跟着板走 —— 板绑了景区、集却留在集团级,是最容易配漏的一处
        assert {r.scenic_id for r in rows} == {SCENIC}

    # 再拉一次:已登记的要标出来,重复登记要跳过而不是整批失败
    r2 = await qsvc.board_cubes(boss, "b-1")
    assert {c["cube_id"] for c in r2["items"] if c["registered"]} == {"cube-1", "cube-2"}
    again = await qsvc.register_datasets(
        boss, "c-a", [{"cube_id": "cube-1"}, {"cube_id": "cube-3"}], source_asset_id="b-1")
    assert again["created"] == 1 and again["skipped"] == 1


@run_async
async def test_cascade_uses_the_recorded_source_even_when_cubes_are_shared():
    """记了来源之后,共用 cube 也不会把归属算错。

    反查做不到这一点:两块板都写着 cube-share,反查只能二选一或都排除。
    """
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    await _conn("c-a", "连接A")
    await _board_with_cubes("b-1", "板一", ["cube-share"])
    await _board_with_cubes("b-2", "板二", ["cube-share"])
    # 明确登记在板一名下(即使两块板都用到这个 cube)
    await qsvc.register_datasets(_actor("super_admin", None, "u-root"), "c-a",
                                 [{"cube_id": "cube-share", "name": "共用集"}], source_asset_id="b-1")

    boss = _actor("super_admin", None, user_id="u-root")
    assert {d["name"] for d in (await qsvc.asset_delete_plan(boss, "b-1"))["datasets"]} == {"共用集"}
    assert (await qsvc.asset_delete_plan(boss, "b-2"))["datasets"] == [], "板二没登记过它,不该算在板二名下"

    await qsvc.delete_asset(boss, "b-2", cascade=True)
    async with common_db.get_sessionmaker()() as db:
        from sqlalchemy import select as _select
        left = (await db.execute(_select(QbiAsset).where(QbiAsset.kind == "dataset"))).scalars().all()
        assert len(left) == 1, "删板二不该动板一名下的集"
