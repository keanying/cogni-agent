"""模型厂商与模型配置 CRUD 服务。"""
from __future__ import annotations

import math

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.user_center_service.models import ModelConfig, ModelProvider
from cogni_agent.user_center_service.schemas import (
    ModelConfigCreate,
    ModelConfigResponse,
    ModelConfigUpdate,
    ModelProviderCreate,
    ModelProviderResponse,
    ModelProviderUpdate,
)


# ═══════════════════════════════════════════════════════════════
# 模型厂商 CRUD
# ═══════════════════════════════════════════════════════════════

async def create_provider(db: AsyncSession, req: ModelProviderCreate) -> ModelProvider:
    provider = ModelProvider(
        name=req.name,
        provider_type=req.provider_type,
        base_url=req.base_url,
        api_key=req.api_key,
        description=req.description,
        is_enabled=req.is_enabled,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


async def list_providers(
    db: AsyncSession,
    keyword: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ModelProviderResponse], int]:
    base = select(ModelProvider)
    count_base = select(func.count(ModelProvider.id))

    if keyword:
        base = base.where(ModelProvider.name.ilike(f"%{keyword}%"))
        count_base = count_base.where(ModelProvider.name.ilike(f"%{keyword}%"))
    if is_enabled is not None:
        base = base.where(ModelProvider.is_enabled == is_enabled)
        count_base = count_base.where(ModelProvider.is_enabled == is_enabled)

    total = (await db.execute(count_base)).scalar() or 0

    rows = (await db.execute(base.order_by(ModelProvider.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()

    items = []
    for p in rows:
        # 脱敏 api_key: 只展示前 8 位
        masked_key = p.api_key[:8] + "****" if p.api_key and len(p.api_key) > 8 else p.api_key
        items.append(ModelProviderResponse(
            id=p.id,
            name=p.name,
            provider_type=p.provider_type,
            base_url=p.base_url,
            api_key=masked_key,
            description=p.description or "",
            is_enabled=p.is_enabled,
            created_at=p.created_at.isoformat() if p.created_at else None,
            updated_at=p.updated_at.isoformat() if p.updated_at else None,
        ))
    return items, total


async def get_provider(db: AsyncSession, provider_id: str) -> ModelProvider | None:
    return (await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))).scalar_one_or_none()


async def update_provider(db: AsyncSession, provider_id: str, req: ModelProviderUpdate) -> ModelProvider | None:
    provider = await get_provider(db, provider_id)
    if not provider:
        return None
    updates = req.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(provider, k, v)
    await db.commit()
    await db.refresh(provider)
    return provider


async def delete_provider(db: AsyncSession, provider_id: str) -> bool:
    provider = await get_provider(db, provider_id)
    if not provider:
        return False
    # 检查是否有关联模型
    count = (await db.execute(select(func.count(ModelConfig.id)).where(ModelConfig.provider_id == provider_id))).scalar() or 0
    if count > 0:
        raise ValueError(f"该厂商下仍有 {count} 个模型配置，请先删除模型配置")
    await db.delete(provider)
    await db.commit()
    return True


# ═══════════════════════════════════════════════════════════════
# 模型配置 CRUD
# ═══════════════════════════════════════════════════════════════

def _model_to_response(m: ModelConfig, provider: ModelProvider | None = None) -> ModelConfigResponse:
    return ModelConfigResponse(
        id=m.id,
        name=m.name,
        provider_id=m.provider_id,
        provider_name=provider.name if provider else "",
        provider_type=provider.provider_type if provider else "",
        model_name=m.model_name,
        description=m.description or "",
        is_enabled=m.is_enabled,
        is_default=m.is_default,
        extra_params=m.extra_params or {},
        created_at=m.created_at.isoformat() if m.created_at else None,
        updated_at=m.updated_at.isoformat() if m.updated_at else None,
    )


async def create_model_config(db: AsyncSession, req: ModelConfigCreate) -> ModelConfig:
    # 如果设为默认，先取消其他模型的默认标记
    if req.is_default:
        await db.execute(
            update(ModelConfig).where(ModelConfig.is_default.is_(True)).values(is_default=False)
        )
    model = ModelConfig(
        name=req.name,
        provider_id=req.provider_id,
        model_name=req.model_name,
        description=req.description,
        is_enabled=req.is_enabled,
        is_default=req.is_default,
        extra_params=req.extra_params,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def list_model_configs(
    db: AsyncSession,
    keyword: str | None = None,
    provider_id: str | None = None,
    is_enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ModelConfigResponse], int]:
    # JOIN 获取 provider 名称
    base = select(ModelConfig, ModelProvider.name, ModelProvider.provider_type).join(
        ModelProvider, ModelConfig.provider_id == ModelProvider.id, isouter=True
    )
    count_base = select(func.count(ModelConfig.id))

    if keyword:
        base = base.where(ModelConfig.name.ilike(f"%{keyword}%"))
        count_base = count_base.where(ModelConfig.name.ilike(f"%{keyword}%"))
    if provider_id:
        base = base.where(ModelConfig.provider_id == provider_id)
        count_base = count_base.where(ModelConfig.provider_id == provider_id)
    if is_enabled is not None:
        base = base.where(ModelConfig.is_enabled == is_enabled)
        count_base = count_base.where(ModelConfig.is_enabled == is_enabled)

    total = (await db.execute(count_base)).scalar() or 0

    rows = (await db.execute(
        base.order_by(ModelConfig.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )).all()

    items = []
    for model, p_name, p_type in rows:
        items.append(ModelConfigResponse(
            id=model.id,
            name=model.name,
            provider_id=model.provider_id,
            provider_name=p_name or "",
            provider_type=p_type or "",
            model_name=model.model_name,
            description=model.description or "",
            is_enabled=model.is_enabled,
            is_default=model.is_default,
            extra_params=model.extra_params or {},
            created_at=model.created_at.isoformat() if model.created_at else None,
            updated_at=model.updated_at.isoformat() if model.updated_at else None,
        ))
    return items, total


async def get_model_config(db: AsyncSession, model_id: str) -> ModelConfigResponse | None:
    row = (await db.execute(
        select(ModelConfig, ModelProvider.name, ModelProvider.provider_type)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id, isouter=True)
        .where(ModelConfig.id == model_id)
    )).one_or_none()

    if not row:
        return None
    model, p_name, p_type = row
    return _model_to_response(model, ModelProvider(name=p_name or "", provider_type=p_type or ""))


async def update_model_config(db: AsyncSession, model_id: str, req: ModelConfigUpdate) -> ModelConfigResponse | None:
    row = (await db.execute(
        select(ModelConfig, ModelProvider.name, ModelProvider.provider_type)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id, isouter=True)
        .where(ModelConfig.id == model_id)
    )).one_or_none()

    if not row:
        return None
    model, _, _ = row

    # 如果设为默认，先取消其他模型的默认标记
    if req.is_default:
        await db.execute(
            update(ModelConfig).where(ModelConfig.is_default.is_(True), ModelConfig.id != model_id).values(is_default=False)
        )

    updates = req.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(model, k, v)

    await db.commit()
    await db.refresh(model)
    return await get_model_config(db, model_id)


async def delete_model_config(db: AsyncSession, model_id: str) -> bool:
    row = (await db.execute(select(ModelConfig).where(ModelConfig.id == model_id))).scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def get_enabled_models(db: AsyncSession) -> list[dict]:
    """获取所有启用的模型配置（供 LLM Client 使用，返回全量数据含 api_key）。"""
    rows = (await db.execute(
        select(ModelConfig, ModelProvider)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id)
        .where(ModelConfig.is_enabled.is_(True), ModelProvider.is_enabled.is_(True))
        .order_by(ModelConfig.is_default.desc(), ModelConfig.created_at.desc())
    )).all()

    models = []
    for model, provider in rows:
        models.append({
            "name": model.model_name,
            "display_name": model.name,
            "provider": provider.provider_type,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "extra_params": model.extra_params or {},
            "is_default": model.is_default,
        })
    return models
