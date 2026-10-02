# -*- coding: utf-8 -*-
"""角色 × 景区 的权限矩阵(组织管理里的六个角色)。

    集团三角色(super_admin / group_admin / group_developer)  不受限:增 / 改 / 删,跨景区
    集团只读(group_viewer)                                  只能看
    景区管理员(scenic_admin)                                 本景区:增 / 改 / 删
    景区操作员(scenic_operator)                              本景区:增 / 改,**不能删**

两条细则单独钉:
- 景区角色动不了**集团级**内容(scenic_id 为空的那批,所有景区共用);
- 判定按景区而不是按创建者 —— 换个管理员接手要能管得了前任建的东西。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import permissions as perm
from cogni_agent.common import schema as sc  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.common import db as common_db
from cogni_agent.common.errors import Forbidden

GROUP3 = ["super_admin", "group_admin", "group_developer"]
SCENIC = "PFTSCA01018088"
OTHER = "PFTSCA01018099"


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


# ── 矩阵本身 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", GROUP3)
def test_group_three_roles_are_unrestricted(role):
    assert perm.can_create(role) and perm.can_delete(role) and perm.can_manage_all(role)
    # 跨景区、集团级内容都能动
    perm.assert_can_write_obj(role, None, SCENIC, "问题")
    perm.assert_can_delete_obj(role, None, None, "问题")


def test_group_viewer_is_read_only():
    assert not perm.can_create("group_viewer") and not perm.can_delete("group_viewer")
    with pytest.raises(Forbidden):
        perm.assert_can_write("group_viewer", "问题")
    with pytest.raises(Forbidden):
        perm.assert_can_delete("group_viewer", "问题")


def test_scenic_admin_full_within_own_scenic():
    assert perm.can_create("scenic_admin") and perm.can_delete("scenic_admin")
    perm.assert_can_write_obj("scenic_admin", SCENIC, SCENIC, "问题")
    perm.assert_can_delete_obj("scenic_admin", SCENIC, SCENIC, "问题")
    # 别的景区不行
    with pytest.raises(Forbidden) as e:
        perm.assert_can_write_obj("scenic_admin", SCENIC, OTHER, "问题")
    assert OTHER in str(e.value)
    # 集团级内容(所有景区共用)也不行 —— 改一次全场跟着变
    with pytest.raises(Forbidden) as e2:
        perm.assert_can_write_obj("scenic_admin", SCENIC, None, "问题")
    assert "集团级" in str(e2.value)


def test_scenic_operator_can_write_but_not_delete():
    assert perm.can_create("scenic_operator")
    assert not perm.can_delete("scenic_operator"), "操作员不能删除"
    perm.assert_can_write_obj("scenic_operator", SCENIC, SCENIC, "问题")
    with pytest.raises(Forbidden) as e:
        perm.assert_can_delete_obj("scenic_operator", SCENIC, SCENIC, "问题")
    assert "景区管理员" in str(e.value), "要告诉他去找谁删,而不是只说没权限"


def test_unbound_scenic_account_is_told_to_get_bound():
    """景区角色没绑景区 = 配置没做完,报错要说清楚去哪儿绑。"""
    with pytest.raises(Forbidden) as e:
        perm.assert_can_write_obj("scenic_admin", None, SCENIC, "问题")
    assert "组织管理" in str(e.value)


def test_capabilities_payload_for_frontend():
    cap = perm.capabilities("scenic_operator", SCENIC)
    assert cap["can_create"] is True and cap["can_delete"] is False
    assert cap["is_group"] is False and cap["scope_scenic_id"] == SCENIC
    assert cap["role_label"] == "景区操作员"

    cap2 = perm.capabilities("group_viewer", None)
    assert cap2["can_create"] is False and cap2["can_delete"] is False and cap2["is_group"] is True


# ── 落到真实写入口:Quick BI 固定问题 ────────────────────────────────

async def _seed(db, scenic=None):
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    db.add(QbiConnection(id="c1", name="Quick BI", owner_user_id="u-admin",
                         visibility="group", scenic_id=None, active=True, is_default=True))
    db.add(ScenicAgentIssueDetail(
        issue_id="issue_1", business_class="quick_bi", connection_id="c1",
        scenic_id=scenic, title="本月售票总量", issue="本月各平台销售额",
        capability="dataset_qa", owner_user_id="u-other", visibility="group", active=True))
    await db.commit()


def _actor(role, scenic):
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    return qsvc.Actor(user_id=f"u-{role}", scenic_id=scenic, role=role, source="console")


@run_async
async def test_scenic_admin_can_edit_a_colleagues_question_in_same_scenic():
    """判定按景区不按创建者:换个管理员接手,要能管得了前任建的问题。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=SCENIC)
    out = await qsvc.save_fixed(_actor("scenic_admin", SCENIC), {"title": "改过的标题"}, "issue_1")
    assert out["title"] == "改过的标题"


@run_async
async def test_scenic_operator_cannot_delete_question():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=SCENIC)
    # 能改
    await qsvc.save_fixed(_actor("scenic_operator", SCENIC), {"title": "操作员改的"}, "issue_1")
    # 不能删
    with pytest.raises(Forbidden) as e:
        await qsvc.delete_fixed(_actor("scenic_operator", SCENIC), "issue_1")
    assert "景区管理员" in str(e.value)


@run_async
async def test_scenic_role_cannot_touch_group_level_question():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=None)      # 集团级问题
    with pytest.raises(Forbidden):
        await qsvc.save_fixed(_actor("scenic_admin", SCENIC), {"title": "想改集团的"}, "issue_1")
    with pytest.raises(Forbidden):
        await qsvc.delete_fixed(_actor("scenic_admin", SCENIC), "issue_1")


@run_async
async def test_scenic_role_cannot_touch_other_scenic_question():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=OTHER)
    with pytest.raises(Exception):        # 别的景区的问题连查都查不到(scope 先挡住)
        await qsvc.save_fixed(_actor("scenic_admin", SCENIC), {"title": "越界"}, "issue_1")


@run_async
async def test_group_viewer_cannot_write_anything():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=SCENIC)
    with pytest.raises(Forbidden):
        await qsvc.save_fixed(_actor("group_viewer", None), {"title": "只读也想改"}, "issue_1")
    with pytest.raises(Forbidden):
        await qsvc.delete_fixed(_actor("group_viewer", None), "issue_1")


@run_async
async def test_group_developer_can_delete_any_scenic_question():
    from cogni_agent.tool_center_service.quickbi import service as qsvc

    async with common_db.get_sessionmaker()() as db:
        await _seed(db, scenic=OTHER)
    await qsvc.delete_fixed(_actor("group_developer", None), "issue_1")


# ══════════════════════════════════════════════════════════════════════
# 复审:之前建的那些写接口,是不是也按这套矩阵来
#
# 这一段针对的是「当时只拦了 group_viewer、别的什么都没判」的那批接口 ——
# 列表里看不见的东西,拿到 id 照样能改能删,矩阵等于没生效。
# ══════════════════════════════════════════════════════════════════════

def _console_user(role, scenic=None):
    from cogni_agent.gateway_service.deps import CurrentUser

    return CurrentUser(user_id=f"u-{role}", scenic_id=scenic, role=role)


@run_async
async def test_tool_center_manage_is_by_scenic_not_by_creator():
    """工具/技能/Agent 的改删:按景区判,不按「谁建的」判。"""
    from cogni_agent.tool_center_service import routes as tc

    # 同景区别人建的 → 景区管理员能改能删
    assert tc._can_manage("u-someone-else", SCENIC, _console_user("scenic_admin", SCENIC))
    assert tc._can_remove("u-someone-else", SCENIC, _console_user("scenic_admin", SCENIC))
    # 景区操作员:能改,不能删
    assert tc._can_manage("u-someone-else", SCENIC, _console_user("scenic_operator", SCENIC))
    assert not tc._can_remove("u-someone-else", SCENIC, _console_user("scenic_operator", SCENIC))
    # 别的景区的 → 都不行
    assert not tc._can_manage("u-x", OTHER, _console_user("scenic_admin", SCENIC))
    # 集团级(scenic 为空)→ 景区角色只能用不能改
    assert not tc._can_manage("u-x", None, _console_user("scenic_admin", SCENIC))
    # 集团只读:什么都不行;集团三角色:都行
    assert not tc._can_manage("u-x", SCENIC, _console_user("group_viewer", None))
    for role in GROUP3:
        assert tc._can_remove("u-x", OTHER, _console_user(role, None))


@run_async
async def test_version_and_assignment_routes_follow_the_matrix():
    """版本发布 / 用户指派原先一个角色判定都没有 —— 现在按它所属资源的景区判。"""
    from cogni_agent.tool_center_service import routes as tc
    from cogni_agent.tool_center_service.skill.models import Skill

    async with common_db.get_sessionmaker()() as db:
        db.add(Skill(id="sk_1", name="客流分析", tool_ids=[], owner_user_id="u-admin",
                     visibility="group", scenic_id=SCENIC))
        await db.commit()

    async with common_db.get_sessionmaker()() as db:
        # 集团只读:发布不了
        with pytest.raises(Forbidden):
            await tc._assert_resource_manage(db, "skill", "sk_1", _console_user("group_viewer", None))
        # 别的景区的景区管理员:动不了
        with pytest.raises(Forbidden):
            await tc._assert_resource_manage(db, "skill", "sk_1", _console_user("scenic_admin", OTHER))
        # 本景区管理员:发布可以,解除指派(删除动作)也可以
        await tc._assert_resource_manage(db, "skill", "sk_1", _console_user("scenic_admin", SCENIC))
        await tc._assert_resource_manage(db, "skill", "sk_1",
                                         _console_user("scenic_admin", SCENIC), removing=True)
        # 本景区操作员:能发布,不能解除指派
        await tc._assert_resource_manage(db, "skill", "sk_1", _console_user("scenic_operator", SCENIC))
        with pytest.raises(Forbidden) as e:
            await tc._assert_resource_manage(db, "skill", "sk_1",
                                             _console_user("scenic_operator", SCENIC), removing=True)
        assert "景区管理员" in str(e.value)


@run_async
async def test_revoke_qbi_grant_needs_delete_right():
    """解除连接分配是删除动作:景区操作员做不了(分配本身可以)。"""
    from cogni_agent.tool_center_service.quickbi import service as qsvc
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async with common_db.get_sessionmaker()() as db:
        db.add(QbiConnection(id="c_s", name="本景区连接", owner_user_id="u-admin",
                             visibility="scenic", scenic_id=SCENIC, active=True))
        await db.commit()
        with pytest.raises(Forbidden) as e:
            await qsvc._assert_can_grant(db, _actor("scenic_operator", SCENIC), "c_s", removing=True)
        assert "景区管理员" in str(e.value) or "不能删除" in str(e.value)
        # 改(分配)是可以的
        await qsvc._assert_can_grant(db, _actor("scenic_operator", SCENIC), "c_s")


def test_system_settings_allows_all_three_group_roles():
    """系统设置原先漏了集团开发:同一个「登记景区」动作两条路径两套口径。"""
    from cogni_agent.governance_event_bus_service import routes as gov

    for role in GROUP3:
        gov._require_group_admin(_console_user(role, None))
    for role in ("group_viewer", "scenic_admin", "scenic_operator"):
        with pytest.raises(Forbidden):
            gov._require_group_admin(_console_user(role, SCENIC if role.startswith("scenic") else None))


def test_generic_question_template_is_group_level():
    """APP 运营的「通用问题模板」是 scenic_id IS NULL 的集团级内容:景区角色只能用。"""
    for role in ("scenic_admin", "scenic_operator"):
        with pytest.raises(Forbidden) as e:
            perm.assert_can_write_obj(role, SCENIC, None, "通用问题模板")
        assert "集团级" in str(e.value)
    for role in GROUP3:
        perm.assert_can_write_obj(role, None, None, "通用问题模板")


def test_no_write_route_ships_without_a_permission_check():
    """守门的守门人:管理端每个写接口(POST/PUT/PATCH/DELETE)都得判一次权限。

    这条是为了防「下次再漏」——上面那些漏判的接口不是写错了,是**压根没写**,
    而漏没漏在代码评审里看不出来(少一行和多一行长得一样)。这里按源码扫一遍:
    路由函数体里必须出现某个权限判定的名字,否则测试直接点名是哪个接口。

    确实不需要判的(纯读的辅助接口、连通性探测),列在 _EXEMPT 里并写明原因。
    """
    import inspect

    from cogni_agent.governance_event_bus_service import routes as gov_routes
    from cogni_agent.knowledge_service import routes as kb_routes
    from cogni_agent.tool_center_service import routes as tc_routes
    from cogni_agent.tool_center_service.quickbi import routes_admin as qbi_routes

    # 判定函数名:出现任意一个即视为判过(服务层自己判的,列它的服务函数名)
    MARKERS = (
        "_can_write", "_can_manage", "_can_remove", "_assert_writable", "_assert_deletable",
        "_assert_scope", "_assert_write", "_assert_delete", "_assert_resource_manage",
        "_assert_version_manage", "_require_group_admin", "_can_delete", "assert_can_write",
        "assert_can_delete", "assert_in_scope", "perm.", "guard(", "svc.",
    )
    # 豁免:写方法但不改数据 / 权限在服务层按 actor 判
    _EXEMPT = {
        "/tools/preview": "试运行,不落库",
        "/tools/from-api-doc/parse": "只把接口文档解析出来给人看一眼,不落库(落库的是 /tools/from-api-doc)",
        "/tools/parse-fields": "只把一段字段表解析给人看一眼,不落库、不读任何数据",
        "/tools/{tool_id}/test": "连通性探测,不落库",
        "/mcp-servers/{server_id}/ping": "连通性探测,不落库",
        "/mcp-servers/{server_id}/discover": "只读取远端工具清单,不落库",
        "/eval-runs": "跑一遍评测出报告,不改配置",
        "/approvals/{approval_id}/resolve": "审批动作自带审批人校验",
        "/versions/{version_id}/precheck": "发布预检,只算依赖不改状态(真发布在 publish 上判)",
        "/web-search-config/{cfg_id}/test": "拿已存配置真发一次检索,不落库",
        "/vector-db-configs/test": "连通性探测,不落库",
        "/bases/{kb_id}/retrieve-test": "检索试跑,不落库",
    }

    missing = []
    for mod in (tc_routes, kb_routes, gov_routes, qbi_routes):
        for route in mod.router.routes:
            methods = getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
            if not methods:
                continue
            path = route.path.replace(mod.router.prefix, "", 1) or route.path
            if path in _EXEMPT:
                continue
            try:
                src = inspect.getsource(route.endpoint)
            except (OSError, TypeError):      # 取不到源码就不瞎判
                continue
            if not any(m in src for m in MARKERS):
                missing.append(f"{sorted(methods)[0]} {mod.router.prefix}{path}")

    assert not missing, "这些写接口没有任何权限判定:\n  " + "\n  ".join(missing)


# ══════════════════════════════════════════════════════════════════════
# 读侧:后台列表/详情也必须按账号的景区收口
#
# 写门禁管「能不能改」,但「列表里出现了别的景区的东西」同样是越权 ——
# 而且更隐蔽:没有报错,只是多看见了。最常见的形态是:
#   列表筛得好好的,详情却是一句 db.get(Model, id) —— 拿到 ID 就能读。
# ══════════════════════════════════════════════════════════════════════

def test_read_scope_helpers():
    """集团看全部;景区看本景区 + 集团级;没绑景区的景区账号只看得到集团级。"""
    for role in GROUP3 + ["group_viewer"]:
        assert perm.read_scope(role, None) is None
        assert perm.can_read_obj(role, None, OTHER)
    assert perm.read_scope("scenic_admin", SCENIC) == SCENIC
    # 集团级(归属为空)的内容景区角色看得见 —— 「能用不能改」的「能用」靠的就是这条
    assert perm.can_read_obj("scenic_admin", SCENIC, None)
    assert perm.can_read_obj("scenic_operator", SCENIC, SCENIC)
    assert not perm.can_read_obj("scenic_admin", SCENIC, OTHER)
    # 没绑景区:只剩集团级,而不是「什么都看得到」
    assert not perm.can_read_obj("scenic_admin", None, OTHER)
    with pytest.raises(Exception):
        perm.assert_can_read("scenic_admin", SCENIC, OTHER, "景区配置")


def test_scenic_read_clause_sql_shape():
    """拼出来的 WHERE 必须真的带上景区条件(集团角色才是恒真)。"""
    import sqlalchemy as sa

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail as D

    group = str(perm.scenic_read_clause("group_admin", None, D.scenic_id).compile(
        compile_kwargs={"literal_binds": True}))
    scenic = str(perm.scenic_read_clause("scenic_admin", SCENIC, D.scenic_id).compile(
        compile_kwargs={"literal_binds": True}))
    assert group == "true"
    assert SCENIC in scenic and "IS NULL" in scenic
    assert isinstance(perm.scenic_read_clause("group_viewer", None, D.scenic_id), sa.sql.elements.True_)


def test_issue_list_scenic_param_can_only_narrow():
    """?scenic_id= 只能收窄。

    原先是 `scenic_id if scenic_id is not None else user.scenic_id` ——
    传别人的景区就看别人的问题;传**空串**更糟:`"" is not None` 为真,
    下游对空值是「不加条件」,于是整个平台的问题全出来。
    """
    from cogni_agent.gateway_service import scenic_binding as binding

    class U:
        def __init__(self, scenic): self.scenic_id = scenic

    # 景区账号:空串/不传都回落到自己景区,而不是「全部」
    assert binding.effective_scenic(U(SCENIC), "") == SCENIC
    assert binding.effective_scenic(U(SCENIC), None) == SCENIC
    assert binding.effective_scenic(U(SCENIC), SCENIC) == SCENIC
    with pytest.raises(Exception):
        binding.effective_scenic(U(SCENIC), OTHER)
    # 集团账号:不传 = 集团级,传谁就是谁
    assert binding.effective_scenic(U(None), None) is None
    assert binding.effective_scenic(U(None), OTHER) == OTHER


def test_tool_center_detail_reads_are_scoped():
    """工具中心的详情门禁:别的景区的资源一律当作不存在(404,不是 403)。"""
    from cogni_agent.common.errors import NotFound
    from cogni_agent.tool_center_service import routes as tc

    me = _console_user("scenic_admin", SCENIC)
    tc._assert_visible(me, "u-other", SCENIC, "scenic", "Skill")          # 本景区:可见
    tc._assert_visible(me, "u-other", None, "group", "Skill")             # 集团级:可见
    tc._assert_visible(me, "u-other", OTHER, "public", "Skill")           # public:本来就给所有景区用
    tc._assert_visible(me, me.user_id, OTHER, "private", "Skill")         # 自己建的:可见
    with pytest.raises(NotFound):
        tc._assert_visible(me, "u-other", OTHER, "scenic", "Skill")       # 别的景区:不存在
    for role in GROUP3 + ["group_viewer"]:
        tc._assert_visible(_console_user(role, None), "u-other", OTHER, "scenic", "Skill")


def test_no_admin_read_route_ships_without_scoping():
    """守门的守门人(读侧):管理端每个 GET 都得判一次范围,或者在豁免表里写明为什么不用。

    这条和写侧那条是一对。读侧的漏判不会报错,只会「多看见」——
    所以更需要机器来盯:少写一行和多写一行,在代码评审里长得一模一样。
    """
    import inspect

    from cogni_agent.gateway_service import routes_app_ops as ops_routes
    from cogni_agent.governance_event_bus_service import routes as gov_routes
    from cogni_agent.knowledge_service import routes as kb_routes
    from cogni_agent.tool_center_service import routes as tc_routes
    from cogni_agent.tool_center_service.quickbi import routes_admin as qbi_routes
    from cogni_agent.user_center_service import routes as uc_routes

    # 判过范围的标志:自己筛的、调了带范围的服务函数的、或按角色整条拦掉的
    MARKERS = (
        "_scoped", "_read_clause", "_assert_read", "_assert_visible", "_assert_can_view_user",
        "_assert_resource_visible", "_assert_record_scope", "_readable_datasource",
        "_require_admin", "_require_group_admin", "can_manage_all", "_assert_scope",
        "list_visible_tools", "test_permission", "_visible_skills", "_visible_agents",
        "effective_scenic", "user.scenic_id", "scope_scenic", "perm.", "svc.", "actor_of",
        "_can_manage", "_assert_can_grant", "scenic_id=user.scenic_id",
        # 行内直接按角色拦掉的(仅 super_admin / group_admin 可看)也算判过 ——
        # 景区角色根本进不来,自然不存在跨景区的问题
        "user.role",
    )
    # 豁免:模型压根没有景区归属(全平台共用的配置 / 静态表 / 自己的东西)
    _EXEMPT = {
        "/tools/templates": "静态模板字典",
        "/quickbi/overview": "静态能力清单",
        "/roles": "角色表,全平台一套",
        "/auth/public-key": "登录前调用",
        "/auth/login-policy": "登录策略,全平台一套",
        "/auth/permissions": "看自己的权限",
        "/auth/me": "看自己",
        "/auth/capabilities": "看自己",
        "/model-providers": "模型供应商,全平台一套",
        "/settings/model-providers": "静态供应商清单",
        "/vector-db-configs": "向量库配置无景区概念,全平台一套",
        "/vector-storage-configs": "同上",
        "/web-site-archive": "站点档案无景区列",
        "/metrics": "指标定义表,全平台一套",
        "/dimension-labels": "三级标签树,全平台共用(写侧已按集团级拦)",
        "/questions": "通用问题模板,硬筛 scenic_id IS NULL,所有景区都该看见",
        "/eval-cases": "评测用例无景区列",
        "/eval-runs": "同上",
        "/eval-runs/{batch_id}": "同上",
        "/tools/builtin": "系统内置工具的静态清单(代码里写死的那些),不读库、"
                          "不含任何租户数据 —— 所有景区看到的是同一份",
    }

    missing = []
    for mod in (tc_routes, kb_routes, gov_routes, qbi_routes, ops_routes, uc_routes):
        prefix = mod.router.prefix
        for route in mod.router.routes:
            if "GET" not in getattr(route, "methods", set()):
                continue
            path = route.path.replace(prefix, "", 1) or route.path
            if path in _EXEMPT:
                continue
            try:
                src = inspect.getsource(route.endpoint)
            except (OSError, TypeError):
                continue
            if not any(m in src for m in MARKERS):
                missing.append(f"GET {prefix}{path}")

    assert not missing, ("这些管理端读接口没有任何范围判定(要么按景区筛,要么在 _EXEMPT 里"
                         "写明为什么不用):\n  " + "\n  ".join(missing))
