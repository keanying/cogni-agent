"""组织管理服务层:景区列表查询与开通/停用。

**数据来源**:景区叫什么、几A、在哪个省市,一律以主数据维表 `dim_prd_scenic_base_yr_f`
为准(见 scenic_catalog.py 里对三张表分工的说明);`sys_scenics` 只存平台侧状态
(是否开通、套餐、同步时间)。所以这里的列表是「已登记景区 ⟕ 主数据」——
主数据改了名,管理端立刻跟着变,不必再手工改一遍。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import NotFound
from cogni_agent.user_center_service.models import SysScenic, User
from cogni_agent.user_center_service.scenic_catalog import DIM


async def list_scenics(
    db: AsyncSession,
    keyword: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """分页查询已登记景区,联主数据取权威名称/等级/属地,并统计各景区用户数。

    Returns:
        (items, total) — items 为 ScenicInfo 字典列表
    """
    # 子查询:按 scenic_id 统计用户数
    user_count_sub = (
        select(User.scenic_id, func.count(User.id).label("cnt"))
        .where(User.scenic_id.isnot(None))
        .group_by(User.scenic_id)
        .subquery()
    )

    # 主查询:已登记景区 ⟕ 主数据 ⟕ 用户数。
    # 名称/等级用 COALESCE(主数据, 平台侧):主数据里查不到的(比如主数据还没同步过来)
    # 仍然显示平台侧存的旧值,而不是显示成空白 —— 空白会被当成「数据丢了」。
    base_stmt = (
        select(
            SysScenic.scenic_id,
            func.coalesce(DIM.c.scenic_name, SysScenic.scenic_name).label("scenic_name"),
            func.coalesce(DIM.c.scenic_grade_level, SysScenic.scenic_grade_level).label("scenic_grade_level"),
            DIM.c.province_name,
            DIM.c.city_name,
            DIM.c.scenic_main_type,
            DIM.c.scenic_id.isnot(None).label("in_master_data"),
            SysScenic.is_enabled,
            func.coalesce(user_count_sub.c.cnt, 0).label("user_count"),
        )
        .select_from(SysScenic)
        .outerjoin(DIM, DIM.c.scenic_id == SysScenic.scenic_id)
        .outerjoin(user_count_sub, SysScenic.scenic_id == user_count_sub.c.scenic_id)
    )

    # 筛选:名称同时匹配主数据名与平台侧名(改过名的景区两个名字都该能搜到)
    if keyword:
        like = f"%{keyword}%"
        base_stmt = base_stmt.where(or_(DIM.c.scenic_name.ilike(like),
                                        SysScenic.scenic_name.ilike(like),
                                        SysScenic.scenic_id.ilike(like)))
    if is_enabled is not None:
        base_stmt = base_stmt.where(SysScenic.is_enabled == is_enabled)

    # 统计总数
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    # 分页
    offset = (page - 1) * page_size
    base_stmt = base_stmt.order_by(func.coalesce(DIM.c.scenic_name, SysScenic.scenic_name)) \
                         .offset(offset).limit(page_size)
    result = await db.execute(base_stmt)
    rows = result.all()

    items = [
        {
            "scenic_id": row.scenic_id,
            "scenic_name": row.scenic_name,
            "scenic_grade_level": row.scenic_grade_level,
            "province_name": row.province_name,
            "city_name": row.city_name,
            "scenic_main_type": row.scenic_main_type,
            # 主数据里找不到这条 = 平台侧有、数仓没有,多半是主数据没同步或景区已下线,
            # 前端会打一个「主数据缺失」的角标,提醒去核对而不是默默显示旧名字
            "in_master_data": bool(row.in_master_data),
            "is_enabled": row.is_enabled,
            "user_count": row.user_count,
        }
        for row in rows
    ]
    return items, total


async def update_scenic_status(
    db: AsyncSession,
    scenic_id: str,
    is_enabled: bool,
) -> dict:
    """开通/停用景区平台服务。

    景区还没登记过(sys_scenics 里没有)但主数据里有 —— 直接按主数据补登记再置状态。
    组织管理页面现在列的是主数据,点「开通」的对象很可能就是一条还没登记的景区,
    这里若还报「景区不存在」,用户会以为是系统坏了。
    """
    scenic = await db.get(SysScenic, scenic_id)
    if not scenic:
        from cogni_agent.user_center_service import scenic_catalog

        await scenic_catalog.register(db, [scenic_id], enable=is_enabled)  # 主数据没有会抛 NotFound
        scenic = await db.get(SysScenic, scenic_id)
        if not scenic:
            raise NotFound(f"景区 {scenic_id} 不存在")

    scenic.is_enabled = is_enabled
    await db.commit()
    await db.refresh(scenic)

    return {
        "scenic_id": scenic.scenic_id,
        "scenic_name": scenic.scenic_name,
        "scenic_grade_level": scenic.scenic_grade_level,
        "is_enabled": scenic.is_enabled,
        "user_count": 0,  # 简化:不查用户数
    }
