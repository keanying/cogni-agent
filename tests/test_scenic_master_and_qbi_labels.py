# -*- coding: utf-8 -*-
"""两件事的回归:Quick BI 固定问题挂三级标签、景区表收敛到主数据。

**为什么要有这个文件**(都是「后台配了、前台没有」这一类最难查的问题):
- 固定问题 0079 起直接存在统一问题表里(business_class=quick_bi),老表已删除。
  这里钉死:管理端保存的固定问题就是前台读的那一行,且没挂三级标签就上不了问题树;
- 组织管理原先读 gov_scenic_orgs(只能手工登记),页面上永远只有人工敲的那一两条。
  这里钉死:景区列表来自主数据维表 dim_prd_scenic_base_yr_f,名称以主数据为准。

不依赖 tests/conftest.py(沙箱无 asyncpg),与 tests/quickbi 同款自建写法。
"""
from __future__ import annotations

import asyncio
import functools

import pytest
import sqlalchemy as sa

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as sc  # noqa: F401  注册全部模型
from cogni_agent.common.db import Base
from cogni_agent.common.errors import NotFound, ValidationError
from cogni_agent.gateway_service import issue_service as issues
from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
from cogni_agent.tool_center_service.quickbi import service as svc
from cogni_agent.user_center_service import scenic_catalog, scenics as scenics_service
from cogni_agent.user_center_service.models import SysScenic, User

# 0012 建的景区主数据维表没有 ORM 模型(它由数仓同步,列还会变),
# 所以这里按迁移里的核心列自己建一张同名表来跑用例。
DIM_DDL = """
CREATE TABLE IF NOT EXISTS dim_prd_scenic_base_yr_f (
    scenic_id                VARCHAR(64) PRIMARY KEY,
    scenic_name              VARCHAR(255),
    scenic_alias             VARCHAR(255),
    scenic_grade_level       VARCHAR(16),
    province_name            VARCHAR(64),
    city_name                VARCHAR(64),
    district_name            VARCHAR(64),
    scenic_main_type         VARCHAR(32),
    scenic_sub_type          VARCHAR(64),
    open_status_type         VARCHAR(32),
    daily_capacity_max_qty   INTEGER,
    scenic_operator_name     VARCHAR(255),
    scenic_tel_consult       VARCHAR(128)
)
"""

DIM_ROWS = [
    ("PFTSCA01018088", "泰宁九龙潭生态旅游区", "九龙潭", "4A", "福建省", "三明市", "泰宁县",
     "自然风光", "峡谷", "all", 8000, "泰宁文旅", "0598-1234567"),
    ("PFTSCA01018099", "武夷山国家公园", "武夷山", "5A", "福建省", "南平市", "武夷山市",
     "自然风光", "山岳", "all", 30000, "武夷文旅", "0599-7654321"),
    ("PFTSCB02010001", "上海科技馆", None, "4A", "上海市", "上海市", "浦东新区",
     "文化场馆", "科技馆", "all", 12000, "上海文旅", "021-00000000"),
]


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
            await conn.exec_driver_sql("DROP TABLE IF EXISTS dim_prd_scenic_base_yr_f")
            await conn.exec_driver_sql(DIM_DDL)
            for r in DIM_ROWS:
                await conn.exec_driver_sql(
                    "INSERT INTO dim_prd_scenic_base_yr_f (scenic_id, scenic_name, scenic_alias,"
                    " scenic_grade_level, province_name, city_name, district_name, scenic_main_type,"
                    " scenic_sub_type, open_status_type, daily_capacity_max_qty, scenic_operator_name,"
                    " scenic_tel_consult) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", r)
    asyncio.run(reset())
    yield


LABEL = {"topic": "票务", "topic_id": "ticketing",
         "dimension": "售票概况", "dimension_id": "ticketingOverview",
         "metric": "总量", "metric_id": "totalVolume"}


def _actor() -> "svc.Actor":
    return svc.Actor(user_id="u-admin", scenic_id=None, role="super_admin")


async def _connection():
    return await svc.save_connection(_actor(), {
        "name": "测试连接", "server_domain": "https://bi.aliyun.com",
        "api_key": "k", "api_secret": "s", "user_token": "t", "visibility": "group"})


# ── ① Quick BI 固定问题 × 三级标签 ─────────────────────────────────────

@run_async
async def test_fixed_question_is_stored_in_unified_issue_table():
    """管理端保存的固定问题,直接就是统一问题表里的一行(前台读的就是它)。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
    conn = await _connection()

    saved = await svc.save_fixed(_actor(), {
        "connection_id": conn.id, "title": "今日售票总量",
        "question": "今天卖了多少张票", "capability": "dataset_qa",
        "target": {"cube_id": "cube-1", "cube_name": "票务明细"},
        "topic_id": "ticketing", "dimension_id": "ticketingOverview", "metric_id": "totalVolume",
    })
    # 中文名由后端按标签表回填,前端只传编码
    assert saved["topic"] == "票务" and saved["dimension"] == "售票概况" and saved["metric"] == "总量"
    assert saved["id"].startswith("issue_"), "对外的 id 就是统一表的 issue_id"
    assert saved["quickbi_link_id"] == conn.id, "connection_id 与 quickbiLinkId 是同一个值,两个名字都给"
    # 目标数据摊成编码 + 名称两列,前台列表不必解析 JSON
    assert (saved["target_data_id"], saved["target_data_name"]) == ("cube-1", "票务明细")

    async with common_db.get_sessionmaker()() as db:
        row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.issue_id == saved["id"]))).scalars().first()
        assert row is not None, "固定问题必须落在统一问题表,否则 APP 端看不到"
        assert row.business_class == "quick_bi"
        assert (row.topic_id, row.dimension_id, row.metric_id) == \
               ("ticketing", "ticketingOverview", "totalVolume")
        assert row.connection_id == conn.id
        assert row.issue == "今天卖了多少张票"
        assert row.owner_user_id == "u-admin" and row.visibility == "group"


@run_async
async def test_fixed_question_reachable_by_legacy_id():
    """历史配置里存的是老表 UUID(工具配置、运行记录都在用),换表后不能失效。"""
    async with common_db.get_sessionmaker()() as db:
        db.add(ScenicAgentIssueDetail(
            issue_id="issue_legacy01", business_class="quick_bi", connection_id="conn-x",
            title="老问题", issue="昨天卖了多少票", capability="dataset_qa", ask_type="qa",
            owner_user_id="u-admin", visibility="group", active=True,
            source_question_id="8f2c1d90-0000-4000-8000-000000000001"))
        await db.commit()

    got = await svc.get_fixed(_actor(), "8f2c1d90-0000-4000-8000-000000000001")
    assert got.issue_id == "issue_legacy01"


@run_async
async def test_business_issue_list_sees_admin_saved_question():
    """在管理端建完,APP 的问题树里立刻就有 —— 这正是并表要解决的问题。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
    conn = await _connection()
    await svc.save_fixed(_actor(), {
        "connection_id": conn.id, "question": "今天卖了多少张票", "capability": "dataset_qa",
        "topic_id": "ticketing", "dimension_id": "ticketingOverview", "metric_id": "totalVolume"})

    async with common_db.get_sessionmaker()() as db:
        tree = await issues.business_issue_list(db, None)
    assert tree and tree[0]["topic"] == "票务"
    dims = tree[0][tree[0]["topicId"]]
    metrics = dims[0][dims[0]["dimensionId"]]
    issues_of_metric = metrics[0][metrics[0]["metricId"]][0]["issues"]
    assert issues_of_metric[0]["quickbiLinkId"] == conn.id


@run_async
async def test_fixed_question_rejects_unknown_and_partial_label():
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
    conn = await _connection()
    base = {"connection_id": conn.id, "question": "问一句", "capability": "dataset_qa"}

    # 只选了两级 —— 挂不上树,保存时就该拦住
    with pytest.raises(ValidationError) as e1:
        await svc.save_fixed(_actor(), {**base, "topic_id": "ticketing",
                                        "dimension_id": "ticketingOverview"})
    assert "三级" in str(e1.value)

    # 三级都给了但组合不存在
    with pytest.raises(ValidationError) as e2:
        await svc.save_fixed(_actor(), {**base, "topic_id": "ticketing",
                                        "dimension_id": "ticketingOverview", "metric_id": "nope"})
    assert "不存在" in str(e2.value)


@run_async
async def test_fixed_question_edit_clears_label():
    """把三级选择器清空 = 从问题树上摘下来:六列一起置空。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
    conn = await _connection()
    saved = await svc.save_fixed(_actor(), {
        "connection_id": conn.id, "question": "今天卖了多少张票", "capability": "dataset_qa",
        "topic_id": "ticketing", "dimension_id": "ticketingOverview", "metric_id": "totalVolume"})

    cleared = await svc.save_fixed(_actor(), {"topic_id": "", "dimension_id": "", "metric_id": ""},
                                   saved["id"])
    assert cleared["topic"] is None and cleared["metric_id"] is None

    async with common_db.get_sessionmaker()() as db:
        row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.issue_id == saved["id"]))).scalars().first()
        assert row.topic_id is None and row.metric is None


@run_async
async def test_delete_fixed_question_removes_the_row():
    conn = await _connection()
    saved = await svc.save_fixed(_actor(), {
        "connection_id": conn.id, "question": "退票率多少", "capability": "dataset_qa"})
    await svc.delete_fixed(_actor(), saved["id"])
    async with common_db.get_sessionmaker()() as db:
        left = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.issue_id == saved["id"]))).scalars().first()
        assert left is None, "删了固定问题,前台不该还挂着一个点不动的问题"


@run_async
async def test_list_fixed_can_filter_unlabeled():
    """「还没挂标签」要能单独筛 —— 这些就是前台看不到的那一批。"""
    async with common_db.get_sessionmaker()() as db:
        await issues.create_label(db, LABEL)
    conn = await _connection()
    await svc.save_fixed(_actor(), {"connection_id": conn.id, "question": "A", "capability": "dataset_qa"})
    await svc.save_fixed(_actor(), {"connection_id": conn.id, "question": "B", "capability": "dataset_qa",
                                    "topic_id": "ticketing", "dimension_id": "ticketingOverview",
                                    "metric_id": "totalVolume"})
    unlabeled = await svc.list_fixed(_actor(), unlabeled=True)
    assert [q["question"] for q in unlabeled] == ["A"]
    labeled = await svc.list_fixed(_actor(), metric_id="totalVolume")
    assert [q["question"] for q in labeled] == ["B"]


@run_async
async def test_fixed_question_can_bind_scenic_and_filter_by_it():
    """固定问题要能绑景区(集团角色),并能按景区筛 —— 否则多景区的问题很快混成一片。"""
    conn = await _connection()
    await svc.save_fixed(_actor(), {"connection_id": conn.id, "question": "本景区今日客流",
                                    "capability": "dataset_qa", "scenic_id": "PFTSCA01018088"})
    await svc.save_fixed(_actor(), {"connection_id": conn.id, "question": "集团级模板问题",
                                    "capability": "dataset_qa"})

    # 按景区筛:本景区 + 集团级(scenic_id 为空)都要带出来,
    # 否则集团配的模板问题在每个景区都看不到 —— 那就等于没配
    got = await svc.list_fixed(_actor(), scenic_id="PFTSCA01018088")
    assert {q["question"] for q in got} == {"本景区今日客流", "集团级模板问题"}

    other = await svc.list_fixed(_actor(), scenic_id="PFTSCA01018099")
    assert {q["question"] for q in other} == {"集团级模板问题"}


@run_async
async def test_scenic_role_cannot_move_question_to_another_scenic():
    """景区账号不能把问题挂到别的景区名下。"""
    scenic_actor = svc.Actor(user_id="u-scenic", scenic_id="PFTSCA01018088", role="scenic_admin")
    # 景区账号看不到 group 可见的连接,所以用它自己建一条(private)来跑这条用例
    conn = await svc.save_connection(scenic_actor, {
        "name": "景区自建连接", "server_domain": "https://bi.aliyun.com",
        "api_key": "k", "api_secret": "s", "user_token": "t"})
    # 自己建的问题(归属自己景区),改别的字段可以,唯独不能改归属景区
    mine = await svc.save_fixed(scenic_actor, {"connection_id": conn.id, "question": "本景区问一句",
                                               "capability": "dataset_qa"})
    assert mine["scenic_id"] == "PFTSCA01018088"
    with pytest.raises(Exception) as e:
        await svc.save_fixed(scenic_actor, {"scenic_id": "PFTSCA01018099"}, mine["id"])
    assert "归属景区" in str(e.value)


# ── ② 景区表收敛到主数据 ───────────────────────────────────────────────

@run_async
async def test_catalog_lists_master_data_with_registration_flag():
    async with common_db.get_sessionmaker()() as db:
        items, total = await scenic_catalog.search_catalog(db)
        assert total == 3, "候选池就是主数据全量,不该只有登记过的那几条"
        assert all(i["registered"] is False for i in items)
        assert {i["province_name"] for i in items} == {"福建省", "上海市"}

        # 关键字同时匹配名称、别名与景区 ID
        by_alias, _ = await scenic_catalog.search_catalog(db, keyword="九龙潭")
        by_id, _ = await scenic_catalog.search_catalog(db, keyword="PFTSCB02")
        assert by_alias[0]["scenic_id"] == "PFTSCA01018088"
        assert by_id[0]["scenic_name"] == "上海科技馆"


@run_async
async def test_catalog_puts_registered_scenics_first_and_can_filter():
    """已登记的排最前 + 「只看已登记」要真的生效。

    主数据可能有几百个景区,平台真正开通的往往只有几个 —— 按省份排的话,
    自己开通的那几个会淹在一堆没登记的里面,每次都得先搜一遍。
    """
    async with common_db.get_sessionmaker()() as db:
        # 「上海科技馆」按省份排在最后(上海市),登记之后必须跳到第一条
        await scenic_catalog.register(db, ["PFTSCB02010001"])
        items, _ = await scenic_catalog.search_catalog(db)
        assert items[0]["scenic_id"] == "PFTSCB02010001" and items[0]["registered"] is True

        only, total = await scenic_catalog.search_catalog(db, registered=True)
        assert total == 1 and [i["scenic_id"] for i in only] == ["PFTSCB02010001"]

        rest, _ = await scenic_catalog.search_catalog(db, registered=False)
        assert "PFTSCB02010001" not in {i["scenic_id"] for i in rest}


@run_async
async def test_register_takes_name_from_master_data_and_is_idempotent():
    async with common_db.get_sessionmaker()() as db:
        r1 = await scenic_catalog.register(db, ["PFTSCA01018088"])
        assert r1["registered"] == 1
        row = await db.get(SysScenic, "PFTSCA01018088")
        assert row.scenic_name == "泰宁九龙潭生态旅游区" and row.scenic_grade_level == "4A"

        # 再点一次不该报错(批量勾选里混进已登记的很正常)
        r2 = await scenic_catalog.register(db, ["PFTSCA01018088", "PFTSCA01018099"])
        assert (r2["registered"], r2["skipped"]) == (1, 1)

        # 主数据里没有的景区一律拒绝:凭空多出来的 scenic_id 后面全对不上
        with pytest.raises(NotFound):
            await scenic_catalog.register(db, ["NOT-IN-MASTER"])


@run_async
async def test_scenic_list_name_follows_master_data():
    """主数据改了名,管理端列表立刻跟着变(平台侧存的旧名不再是展示来源)。"""
    async with common_db.get_sessionmaker()() as db:
        await scenic_catalog.register(db, ["PFTSCA01018088"])
        await db.execute(sa.text("UPDATE dim_prd_scenic_base_yr_f SET scenic_name = '泰宁九龙潭'"
                                 " WHERE scenic_id = 'PFTSCA01018088'"))
        await db.commit()

        items, total = await scenics_service.list_scenics(db)
        assert total == 1
        assert items[0]["scenic_name"] == "泰宁九龙潭"
        assert items[0]["province_name"] == "福建省" and items[0]["in_master_data"] is True


@run_async
async def test_toggle_registers_scenic_on_the_fly():
    """组织管理里点「开通」的对象可能还没登记过 —— 按主数据自动补登记,而不是报不存在。"""
    async with common_db.get_sessionmaker()() as db:
        result = await scenics_service.update_scenic_status(db, "PFTSCA01018099", True)
        assert result["is_enabled"] is True
        row = await db.get(SysScenic, "PFTSCA01018099")
        assert row is not None and row.scenic_name == "武夷山国家公园"

        with pytest.raises(NotFound):
            await scenics_service.update_scenic_status(db, "NOT-IN-MASTER", True)


@run_async
async def test_sync_from_dim_reports_missing_master_rows():
    async with common_db.get_sessionmaker()() as db:
        await scenic_catalog.register(db, ["PFTSCA01018088"])
        await db.execute(sa.text("UPDATE dim_prd_scenic_base_yr_f SET scenic_name = '改了名'"
                                 " WHERE scenic_id = 'PFTSCA01018088'"))
        await db.commit()
        assert (await scenic_catalog.sync_from_dim(db))["updated"] == 1

        # 主数据里被删掉的景区:平台记录保留(用户还绑着),但要报出来
        db.add(SysScenic(scenic_id="GHOST", scenic_name="幽灵景区", is_enabled=False, plan_config={}))
        await db.commit()
        assert (await scenic_catalog.sync_from_dim(db))["missing"] == ["GHOST"]


@run_async
async def test_list_scenics_keeps_platform_only_rows_visible():
    """主数据里查不到的登记记录仍要显示(显示旧名 + 缺失角标),不能直接消失。"""
    async with common_db.get_sessionmaker()() as db:
        db.add(SysScenic(scenic_id="GHOST", scenic_name="幽灵景区", is_enabled=True, plan_config={}))
        db.add(User(id="u-1", account="a1", name="张三", phone="13000000001",
                    password_hash="x", role="scenic_admin", scenic_id="GHOST"))
        await db.commit()

        items, _ = await scenics_service.list_scenics(db)
        ghost = [i for i in items if i["scenic_id"] == "GHOST"][0]
        assert ghost["scenic_name"] == "幽灵景区"
        assert ghost["in_master_data"] is False
        assert ghost["user_count"] == 1
