"""v1.0 APP 端：竞品需求提交业务逻辑。"""
from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import NotFound
from cogni_agent.common.logging import get_logger
from cogni_agent.user_center_service.models import CompetitorRequest

log = get_logger("gateway.competitor_service")

_EXTRA_PREVIEW_MAX_LENGTH = 500


async def create_request(
    db: AsyncSession,
    user_id: str,
    scenic_id: str | None,
    schema_version: int = 1,
    extra: dict | None = None,
) -> CompetitorRequest:
    """创建竞品需求提交记录。业务字段全部由 extra 承载，schema_version 标记其结构版本。"""
    record = CompetitorRequest(
        user_id=user_id,
        scenic_id=scenic_id,
        schema_version=schema_version,
        extra=extra or {},
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    log.info(f"competitor request {record.id} created by user {user_id} (schema_v{schema_version})")
    return record


async def list_requests(
    db: AsyncSession,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> tuple[list[dict], int]:
    """查询用户的提交记录列表（分页 + 可选 status 筛选）。"""
    conditions = [CompetitorRequest.user_id == user_id]
    if status:
        conditions.append(CompetitorRequest.status == status)

    count_stmt = select(func.count()).select_from(CompetitorRequest).where(*conditions)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    list_stmt = (
        select(CompetitorRequest)
        .where(*conditions)
        .order_by(CompetitorRequest.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(list_stmt)
    rows = result.scalars().all()

    items = [
        {
            "id": r.id,
            "schema_version": r.schema_version,
            "extra": _preview_extra(r.extra),
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
    return items, total


async def get_request_detail(
    db: AsyncSession, request_id: str, user_id: str
) -> dict:
    """获取单条提交详情。只能查看自己的记录。"""
    stmt = select(CompetitorRequest).where(CompetitorRequest.id == request_id)
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise NotFound("记录不存在")
    if record.user_id != user_id:
        raise NotFound("记录不存在")  # 防用户枚举：自己的 vs 他人的统一返回 404

    return {
        "id": record.id,
        "schema_version": record.schema_version,
        "extra": record.extra if isinstance(record.extra, dict) else {},
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _preview_extra(extra: dict | None) -> dict:
    """为列表返回截断后的 extra，避免列表拉回太大的 JSON。"""
    if not extra or not isinstance(extra, dict):
        return {}
    # 对 feature_desc / remark 等长文本字段做截断
    result = dict(extra)
    for key in ("feature_desc", "remark", "description"):
        if key in result and isinstance(result[key], str) and len(result[key]) > _EXTRA_PREVIEW_MAX_LENGTH:
            result[key] = result[key][:_EXTRA_PREVIEW_MAX_LENGTH] + "..."
    return result
