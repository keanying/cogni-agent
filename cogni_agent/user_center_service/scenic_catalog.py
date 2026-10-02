"""景区主数据目录:以 `dim_prd_scenic_base_yr_f` 为唯一真源。

**三张景区表的分工(本次改造后)**

  dim_prd_scenic_base_yr_f  景区主数据(候选池)。由外部主数据系统同步进来,平台只读。
                            景区叫什么、几A、在哪个省市、主类型 —— 一律以它为准。
  sys_scenics               已登记到本平台的景区(是 dim 的子集)。存的是**平台侧**的东西:
                            是否开通服务(is_enabled)、套餐配置(plan_config)、同步时间。
  usr_users.scenic_id       用户绑定的景区,取值必须是 dim 里存在的景区 ID。

改造前「系统设置-组织管理」读的是 gov_scenic_orgs,那张表只能手工一条条登记 ——
所以页面上永远只有人工敲进去的那一两条,看起来像「景区没同步过来」。
0078 之后那张表已并入 sys_scenics 并删除,登记改成从主数据里挑。

**为什么用 sa.table() 而不是 ORM 模型**:dim 表有 60 多列、且由外部系统维护,
一旦写成 ORM 模型,启动时的结构体检就会要求模型和库**逐列**一致 ——
外部加一列我们这边就起不来。这里只声明用得到的那几列,读不到的列一概不碰。
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import NotFound
from cogni_agent.common.logging import get_logger
from cogni_agent.user_center_service.models import SysScenic, User

log = get_logger("user_center_service.scenic_catalog")

# 景区主数据维表的轻量声明(只列出平台会用到的列;dim 表本身有 60+ 列)。
# 注意:这张表**没有** numeri_core_ 前缀 —— 它是数仓侧的表名,建表语句见迁移 0012。
DIM = sa.table(
    "dim_prd_scenic_base_yr_f",
    sa.column("scenic_id"),
    sa.column("scenic_name"),
    sa.column("scenic_alias"),
    sa.column("scenic_grade_level"),
    sa.column("province_name"),
    sa.column("city_name"),
    sa.column("district_name"),
    sa.column("scenic_main_type"),
    sa.column("scenic_sub_type"),
    sa.column("open_status_type"),
    sa.column("daily_capacity_max_qty"),
    sa.column("scenic_operator_name"),
    sa.column("scenic_tel_consult"),
)


def _row_dict(r) -> dict:
    """主数据行 → 统一的对外结构(前端按这份字段名渲染)。"""
    return {
        "scenic_id": r.scenic_id,
        "scenic_name": r.scenic_name,
        "scenic_alias": r.scenic_alias,
        "scenic_grade_level": r.scenic_grade_level,
        "province_name": r.province_name,
        "city_name": r.city_name,
        "district_name": r.district_name,
        "scenic_main_type": r.scenic_main_type,
        "scenic_sub_type": r.scenic_sub_type,
        "open_status_type": r.open_status_type,
        "daily_capacity_max_qty": r.daily_capacity_max_qty,
        "scenic_operator_name": r.scenic_operator_name,
        "scenic_tel_consult": r.scenic_tel_consult,
        # 平台侧状态:registered=是否已登记到 sys_scenics,is_enabled=是否开通服务
        "registered": bool(getattr(r, "registered", False)),
        "is_enabled": bool(getattr(r, "is_enabled", False)),
        "user_count": int(getattr(r, "user_count", 0) or 0),
    }


def _user_count_sub():
    """按景区统计用户数的子查询(组织管理列表要显示「这个景区有几个账号」)。"""
    return (
        sa.select(User.scenic_id, sa.func.count(User.id).label("cnt"))
        .where(User.scenic_id.isnot(None))
        .group_by(User.scenic_id)
        .subquery()
    )


async def search_catalog(
    db: AsyncSession,
    *,
    keyword: str | None = None,
    province: str | None = None,
    grade: str | None = None,
    registered: bool | None = None,
    enabled: bool | None = None,
    only_scenic_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """检索景区主数据(候选池),并标注每条在平台侧是否已登记/已开通。

    `registered=False` 就是「还没登记的景区」—— 登记弹窗默认用它,
    免得运营在几百条里去数哪些已经加过了。

    `enabled=True` = 已登记**且已开通**。各处的景区下拉(给连接/资产/问题绑景区、
    主题解读配哪个景区)用的都该是这个:停用的景区绑上去就是一条配了也不生效的记录,
    列在下拉里只会让人以为它能用 —— 现场就是这么选到一个已经停用的景区的。

    `only_scenic_id` = 把结果限死在这一个景区(景区账号用:主数据一万多条,
    它只该看见自己那一条)。
    """
    uc = _user_count_sub()
    stmt = (
        sa.select(
            DIM.c.scenic_id, DIM.c.scenic_name, DIM.c.scenic_alias, DIM.c.scenic_grade_level,
            DIM.c.province_name, DIM.c.city_name, DIM.c.district_name,
            DIM.c.scenic_main_type, DIM.c.scenic_sub_type, DIM.c.open_status_type,
            DIM.c.daily_capacity_max_qty, DIM.c.scenic_operator_name, DIM.c.scenic_tel_consult,
            SysScenic.scenic_id.isnot(None).label("registered"),
            sa.func.coalesce(SysScenic.is_enabled, sa.false()).label("is_enabled"),
            sa.func.coalesce(uc.c.cnt, 0).label("user_count"),
        )
        .select_from(DIM)
        .outerjoin(SysScenic, SysScenic.scenic_id == DIM.c.scenic_id)
        .outerjoin(uc, uc.c.scenic_id == DIM.c.scenic_id)
    )
    if keyword:
        like = f"%{keyword}%"
        # 景区 ID 也参与匹配:运维手上常常只有一个 PFTSC... 编号
        stmt = stmt.where(sa.or_(DIM.c.scenic_name.ilike(like), DIM.c.scenic_alias.ilike(like),
                                 DIM.c.scenic_id.ilike(like)))
    if province:
        stmt = stmt.where(DIM.c.province_name == province)
    if grade:
        stmt = stmt.where(DIM.c.scenic_grade_level == grade)
    if registered is True:
        stmt = stmt.where(SysScenic.scenic_id.isnot(None))
    elif registered is False:
        stmt = stmt.where(SysScenic.scenic_id.is_(None))
    if enabled is True:
        stmt = stmt.where(SysScenic.is_enabled.is_(True))
    elif enabled is False:
        stmt = stmt.where(sa.func.coalesce(SysScenic.is_enabled, sa.false()).is_(False))
    if only_scenic_id:
        stmt = stmt.where(DIM.c.scenic_id == only_scenic_id)

    total = (await db.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar() or 0
    page_size = max(1, min(int(page_size or 20), 200))
    # 已登记的排在最前:主数据可能有几百个景区,而平台真正开通的往往只有几个 ——
    # 按省份排的话,自己开通的那几个会淹在一堆没登记的里面,每次都得先搜一遍
    stmt = (stmt.order_by(SysScenic.scenic_id.isnot(None).desc(),
                          DIM.c.province_name.nullslast(), DIM.c.scenic_name)
                .offset(max(0, (max(1, page) - 1) * page_size)).limit(page_size))
    rows = (await db.execute(stmt)).all()
    return [_row_dict(r) for r in rows], int(total)


async def get_catalog(db: AsyncSession, scenic_id: str) -> dict:
    """单个景区的主数据明细(Quick BI 连接绑景区、景区详情页都用它)。"""
    stmt = (
        sa.select(
            *DIM.c,
            SysScenic.scenic_id.isnot(None).label("registered"),
            sa.func.coalesce(SysScenic.is_enabled, sa.false()).label("is_enabled"),
        )
        .select_from(DIM)
        .outerjoin(SysScenic, SysScenic.scenic_id == DIM.c.scenic_id)
        .where(DIM.c.scenic_id == scenic_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise NotFound(f"景区主数据里没有这个景区:{scenic_id}")
    return _row_dict(row)


async def provinces(db: AsyncSession) -> list[str]:
    """主数据里出现过的省份(筛选下拉用)。"""
    rows = await db.execute(
        sa.select(DIM.c.province_name).where(DIM.c.province_name.isnot(None)).distinct()
        .order_by(DIM.c.province_name))
    return [r[0] for r in rows if r[0]]


async def grades(db: AsyncSession) -> list[str]:
    """主数据里出现过的景区等级(5A/4A/…)。"""
    rows = await db.execute(
        sa.select(DIM.c.scenic_grade_level).where(DIM.c.scenic_grade_level.isnot(None)).distinct()
        .order_by(DIM.c.scenic_grade_level))
    return [r[0] for r in rows if r[0]]


async def register(db: AsyncSession, scenic_ids: list[str], *, enable: bool = True) -> dict:
    """把主数据里的景区登记到平台(写 sys_scenics),名称/等级一律取主数据。

    已登记的会被跳过而不是报错 —— 批量勾选时里面混进一条已登记的很正常,
    为这个整批失败只会让人重新勾一遍。主数据里查无此景区则拒绝:
    平台侧凭空多出一个主数据没有的景区,后面所有按 scenic_id 的关联都对不上。
    """
    ids = [s for s in {str(i).strip() for i in scenic_ids} if s]
    if not ids:
        return {"registered": 0, "skipped": 0, "items": []}

    found = {r.scenic_id: r for r in (await db.execute(
        sa.select(DIM.c.scenic_id, DIM.c.scenic_name, DIM.c.scenic_grade_level)
        .where(DIM.c.scenic_id.in_(ids)))).all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound("景区主数据里没有这些景区:" + ", ".join(missing))

    exists = {r[0] for r in (await db.execute(
        sa.select(SysScenic.scenic_id).where(SysScenic.scenic_id.in_(ids)))).all()}

    created = []
    for sid, r in found.items():
        if sid in exists:
            continue
        db.add(SysScenic(scenic_id=sid, scenic_name=r.scenic_name or sid,
                         scenic_grade_level=r.scenic_grade_level,
                         is_enabled=enable, plan_config={}, synced_at=datetime.utcnow()))
        created.append(sid)
    await db.commit()
    log.info(f"景区登记:新增 {len(created)} 条,跳过已登记 {len(exists)} 条")
    return {"registered": len(created), "skipped": len(exists), "items": created}


async def sync_from_dim(db: AsyncSession, scenic_ids: list[str] | None = None) -> dict:
    """用主数据刷新已登记景区的名称/等级(主数据改了名,平台这边跟上)。

    只覆盖名称与等级:is_enabled / plan_config 是平台侧自己的状态,主数据不该动它们。
    """
    stmt = sa.select(SysScenic)
    if scenic_ids:
        stmt = stmt.where(SysScenic.scenic_id.in_(list(scenic_ids)))
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return {"updated": 0, "missing": []}

    dim_rows = {r.scenic_id: r for r in (await db.execute(
        sa.select(DIM.c.scenic_id, DIM.c.scenic_name, DIM.c.scenic_grade_level)
        .where(DIM.c.scenic_id.in_([r.scenic_id for r in rows])))).all()}

    updated, missing = 0, []
    for row in rows:
        d = dim_rows.get(row.scenic_id)
        if d is None:
            # 主数据里已经没有这个景区了:不删平台记录(用户还绑在上面),只报出来让人处理
            missing.append(row.scenic_id)
            continue
        if row.scenic_name != (d.scenic_name or row.scenic_name) or row.scenic_grade_level != d.scenic_grade_level:
            row.scenic_name = d.scenic_name or row.scenic_name
            row.scenic_grade_level = d.scenic_grade_level
            updated += 1
        row.synced_at = datetime.utcnow()
    await db.commit()
    return {"updated": updated, "missing": missing}
