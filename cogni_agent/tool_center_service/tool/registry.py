"""工具注册、版本管理、可见性过滤查询(G-AUTH v3:四级可见范围 + 定向授权)。"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.tool_center_service.tool.models import ResourceGrant, Tool

GROUP_ROLES = {"super_admin", "group_admin", "group_developer", "group_viewer"}


async def list_visible_tools(db: AsyncSession, user_id: str, role: str, scenic_id: str | None, keyword: str | None = None) -> list[Tool]:
    """
    可见性过滤 SQL(四级:private/scenic/group/public)——
    owner_user_id=当前用户
    OR visibility=public
    OR (visibility=group AND 当前角色属于集团角色)
    OR (visibility=scenic AND scenic_id=当前用户所在景区)
    OR 存在 tc_grants 授权记录(按人 或 按景区全员)。
    """
    is_group_role = role in GROUP_ROLES
    grant_subq = (
        select(ResourceGrant.resource_id)
        .where(
            ResourceGrant.resource_type == "tool",
            or_(
                (ResourceGrant.grantee_type == "user") & (ResourceGrant.grantee_id == user_id),
                (ResourceGrant.grantee_type == "scenic_all") & (ResourceGrant.grantee_id == (scenic_id or "")),
            ),
        )
    )
    conditions = [Tool.owner_user_id == user_id, Tool.visibility == "public", Tool.id.in_(grant_subq)]
    if is_group_role:
        conditions.append(Tool.visibility == "group")
    if scenic_id:
        conditions.append((Tool.visibility == "scenic") & (Tool.scenic_id == scenic_id))

    stmt = select(Tool).where(or_(*conditions), Tool.deleted_at.is_(None))  # 软删除归档的不再可见
    if keyword:
        stmt = stmt.where(Tool.name.ilike(f"%{keyword}%"))
    # 列表顺序必须稳定:无 ORDER BY 时 PostgreSQL 按物理堆顺序返回,行 UPDATE(如发布/启停)
    # 后位置会变,导致每次刷新顺序不同。统一按创建时间倒序 + id 兜底,保证确定性。
    stmt = stmt.order_by(Tool.created_at.desc(), Tool.id.desc())
    return list((await db.execute(stmt)).scalars())


async def test_permission(db: AsyncSession, tool_id: str, user_id: str, role: str, scenic_id: str | None, action: str) -> tuple[bool, str]:
    """M5-10 权限测试面板背后的逻辑——模拟某用户对某工具的某个操作是否会被放行。"""
    tool = await db.get(Tool, tool_id)
    if not tool:
        return False, "工具不存在"
    if not tool.active:
        return False, "工具已被禁用"
    if tool.locked and action in ("edit", "manage") and tool.owner_user_id != user_id:
        return False, "工具已锁定,仅创建者或 admin 可编辑"
    if tool.owner_user_id == user_id:
        return True, "创建者本人,默认全部权限"
    if tool.visibility == "public":
        return True, "全平台公开,允许调用"
    if tool.visibility == "group" and role in GROUP_ROLES:
        return True, "集团内共享,当前为集团角色"
    if tool.visibility == "scenic" and scenic_id and tool.scenic_id == scenic_id:
        return True, "本景区共享范围内"
    grant_stmt = select(ResourceGrant).where(
        ResourceGrant.resource_type == "tool",
        ResourceGrant.resource_id == tool_id,
        or_(
            (ResourceGrant.grantee_type == "user") & (ResourceGrant.grantee_id == user_id),
            (ResourceGrant.grantee_type == "scenic_all") & (ResourceGrant.grantee_id == (scenic_id or "")),
        ),
    )
    grant = (await db.execute(grant_stmt)).scalar_one_or_none()
    if grant:
        return True, f"存在定向授权记录(permission={grant.permission})"
    return False, "不在可见范围内,也没有定向授权记录"
