"""
系统设置(M7-09~11):模型与预算、组织管理。

模型配置支持两层来源——config.yaml: runtime.models 是"部署时固定"的模型清单
(改了需要重启进程才生效),这里的 LlmModelConfig 表是"运行时可由 group_admin/
super_admin 在管理后台增删改"的补充清单,llm/client.py 解析模型名时先查这张表,
查不到再退回 config.yaml。这样不需要为了新增一个模型就改配置文件重启服务。

预算护栏是真实的:budget.py::check_org_daily_budget() 会查 gov_llm_usage 当日
汇总,超过硬预算(BudgetThreshold.hard_limit_tokens)直接熔断当前任务(抛
GuardrailTripped),超过告警阈值(alert_threshold_pct)只记录一条告警日志,
不阻断。

组织管理是只读列表 + 开通/停用开关(功能清单原文:"数据来源于业务主数据同步,
系统内不做编辑,仅可开通/停用某景区的平台服务")。这份"同步进来的景区主数据"就是
维表 dim_prd_scenic_base_yr_f,开通状态落在 sys_scenics 上 —— 路由见本服务
routes.py 的 /settings/orgs,取数见 user_center_service/scenic_catalog.py。
(原先这里另有一张只能手工登记的 gov_scenic_orgs,已由迁移 0078 并入 sys_scenics 并删除。)
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger
from cogni_agent.tool_center_service.mcp.vault import decrypt_credential, encrypt_credential

log = get_logger("governance_event_bus_service.settings")


class LlmModelConfig(Base):
    __tablename__ = table_name("gov_llm_model_configs")

    # 自增 UUID 主键:身份与展示名解耦,name 降级为普通唯一字段(可改名)。
    # 业务记录(spec.model_override / gov_llm_usage.model)持久化的是 model_id(供应商 ID),
    # 不再引用 name,因此改名不会破坏历史归因。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_cipher: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 默认模型:未指定模型的任务用它。**每种类型各有一个默认**,由 set_default_model 保证
    # 同类型最多一个 true。放在库里而不是只认 config.yaml 的 default_model,是为了在页面上
    # 就能切换 —— 改配置文件要登服务器 + 重启,而切默认模型是很常见的运营操作(比如某家服务商挂了)。
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 模型类型:chat(对话模型)/embedding(嵌入模型,向量入库/检索用)。两种类型在同一个
    # 清单里统一管理、各自添加,解析时按类型各取各的默认。
    model_type: Mapped[str] = mapped_column(String(16), default="chat")
    # 供应商模型 ID(如 deepseek-chat / doubao-embedding / ep-xxx):运行时真正拿去调用
    # 的标识。name 是给人看的展示名,model_id 是给模型看的调用 ID,两者允许不同。
    model_id: Mapped[str] = mapped_column(String(128), default="")
    # 兼容字段:0010 曾用 is_embedding 标记嵌入模型,0019 起改由 model_type 表达。
    # 保留列避免破坏已有数据,新代码不再读写它。
    is_embedding: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[str] = mapped_column(String(255), default="")
    # 真实计费单价(元/百万 token,可空):手填后按该价计费;未填自动查 litellm 价格表,再兜底估算
    input_price_cny_per_m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    output_price_cny_per_m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    created_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BudgetThreshold(Base):
    __tablename__ = table_name("gov_budget_thresholds")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    scope: Mapped[str] = mapped_column(String(16), default="group")  # group / scenic
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    daily_token_budget: Mapped[int] = mapped_column(Integer, default=1000000)
    alert_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ScenicOrg(gov_scenic_orgs)已于 0078 删除:它是一张只能手工登记的景区表,
# 与 sys_scenics(平台侧登记)、dim_prd_scenic_base_yr_f(景区主数据)三份数据打架 ——
# 「系统设置-组织管理」里只看得到人工敲进去的那一两条,看起来像景区没同步过来。
# 现在组织管理直接读主数据 + sys_scenics,见 user_center_service/scenic_catalog.py。


class ModelPrice(Base):
    """模型单价配置表(替代 litellm 实时拉取的计费表)。

    单价统一在此配置(元/百万 token),由「系统设置 · 模型单价」卡片管理;
    cost.py 按 model_id 精确匹配取价,不再依赖 litellm 内置价格表/外部网络。
    model_id 与 gov_llm_model_configs.model_id 对应,也可为 config.yaml 静态模型
    (runtime.models)配置单价。
    """

    __tablename__ = table_name("gov_llm_model_prices")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    model_id: Mapped[str] = mapped_column(String(128), unique=True)
    input_price_cny_per_m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    output_price_cny_per_m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


async def get_active_model_configs(db: AsyncSession) -> dict[str, dict]:
    """供 llm/client.py 解析模型时先查这张表;返回 {模型id: {name, provider, base_url, api_key, model_type, model_id}}。

    以**不可变的 id** 作为 dict key —— 展示名 name 是给人看的别名、允许页面改名,
    不能当身份标识;name / model_id 都放进值里,由解析链按「id → name → model_id」
    的顺序做别名匹配。
    """
    rows = (await db.execute(select(LlmModelConfig).where(LlmModelConfig.active.is_(True)))).scalars().all()
    out = {}
    for r in rows:
        api_key = None
        if r.api_key_cipher:
            try:
                api_key = decrypt_credential(r.api_key_cipher)
            except Exception:  # noqa: BLE001
                log.exception(f"模型 {r.name} 的 api_key 解密失败")
        out[r.id] = {"id": r.id, "name": r.name, "provider": r.provider, "base_url": r.base_url,
                     "api_key": api_key, "model_type": r.model_type, "model_id": r.model_id or r.name,
                     "input_price_cny_per_m": float(r.input_price_cny_per_m) if r.input_price_cny_per_m is not None else None,
                     "output_price_cny_per_m": float(r.output_price_cny_per_m) if r.output_price_cny_per_m is not None else None}
    return out


async def upsert_model_config(name: str, provider: str, base_url: str | None, api_key: str | None,
                              created_by: str, model_type: str = "chat", model_id: str | None = None,
                              config_id: str | None = None,
                              input_price_cny_per_m: float | None = None,
                              output_price_cny_per_m: float | None = None) -> None:
    """新增或更新模型配置。

    `name` 是展示名(给人看的,可叫中文别名),`model_id` 是供应商提供的调用 ID
    (如 deepseek-chat / ep-xxx),运行时用 `model_id` 去调模型。两者允许不同。

    定位语义:传 `config_id` 表示编辑已存在的模型(按 id 定位,name 可改);不传表示新增。
    `name` 是唯一字段(展示名不可重复),但不是主键,改名不会影响历史归因 ——
    业务记录持久化的是 `model_id`,改名后解析链仍能按 model_id 兜底匹配。

    **该类型的第一个模型会自动设为默认。** 否则用户"在页面配好模型和 Key,却发现执行时
    仍然用的是 config.yaml 里那个不存在的默认模型",而报错信息(未知模型 'xxx')
    完全指不到「你需要再点一下设为默认」这一步。这是实际踩过的坑。

    **对话模型与嵌入模型(向量入库/检索用)在同一个清单里统一管理**,用 `model_type`
    区分(chat / embedding)。两种类型各自维护自己的默认模型。
    """
    if model_type not in ("chat", "embedding"):
        raise ValueError(f"不支持的模型类型: {model_type!r}(仅支持 chat / embedding)")
    model_id = (model_id or name).strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空")
    async with get_sessionmaker()() as db:
        existing = await db.get(LlmModelConfig, config_id) if config_id else None
        if existing is None and config_id:
            raise ValueError(f"模型配置不存在: {config_id}")
        if existing:
            # 编辑:改名时校验新展示名不与其它模型冲突(唯一字段)
            if name.strip() != existing.name:
                dup = (await db.execute(
                    select(LlmModelConfig).where(
                        LlmModelConfig.name == name.strip(),
                        LlmModelConfig.id != existing.id,
                    ).limit(1)
                )).scalars().first()
                if dup:
                    raise ValueError(f"模型名称已存在: {name.strip()}")
            cipher = encrypt_credential(api_key) if api_key else existing.api_key_cipher
            old_type = existing.model_type
            existing.name, existing.provider = name.strip(), provider
            existing.base_url, existing.api_key_cipher = base_url, cipher
            existing.model_type = model_type
            existing.model_id = model_id
            existing.input_price_cny_per_m = input_price_cny_per_m
            existing.output_price_cny_per_m = output_price_cny_per_m
            if model_type != old_type:
                # 类型变更:旧类型里它若曾是默认,清掉;新类型若无默认则自动顶上
                await db.execute(update(LlmModelConfig).where(
                    LlmModelConfig.id != existing.id,
                    LlmModelConfig.model_type == old_type,
                ).values(is_default=False))
                existing.is_default = False
                is_first = (await db.execute(
                    select(LlmModelConfig).where(LlmModelConfig.model_type == model_type).limit(1)
                )).scalars().first() is None
                if is_first:
                    existing.is_default = True
        else:
            # 新增:展示名唯一,与现有模型重名直接报错
            dup = (await db.execute(
                select(LlmModelConfig).where(LlmModelConfig.name == name.strip()).limit(1)
            )).scalars().first()
            if dup:
                raise ValueError(f"模型名称已存在: {name.strip()}")
            is_first = (await db.execute(
                select(LlmModelConfig).where(LlmModelConfig.model_type == model_type).limit(1)
            )).scalars().first() is None
            cipher = encrypt_credential(api_key) if api_key else None
            db.add(LlmModelConfig(name=name.strip(), provider=provider, base_url=base_url,
                                  api_key_cipher=cipher, created_by=created_by,
                                  model_type=model_type, model_id=model_id, is_default=is_first,
                                  input_price_cny_per_m=input_price_cny_per_m,
                                  output_price_cny_per_m=output_price_cny_per_m))
        await db.commit()
    if not config_id and is_first:
        log.info(f"模型 {name!r} 是 {model_type} 类型的第一个配置,已自动设为默认")


async def set_default_model(db, config_id: str) -> None:
    """把某个模型设为它所属类型的默认模型(按 id 定位)。

    先清同类型其他模型的标记再置 true,放在同一个事务里 —— 否则中途失败会留下
    "两个默认"或"一个默认都没有"的状态,后者会让所有未指定模型的任务直接失败。
    对话与嵌入两种类型各有各的默认,互不影响。
    """
    from sqlalchemy import update as _update

    target = await db.get(LlmModelConfig, config_id)
    if target is None:
        raise NotFound(f"模型配置不存在: {config_id}")
    if not target.active:
        raise Forbidden(f"模型 {target.name} 已停用,不能设为默认")
    await db.execute(_update(LlmModelConfig).where(
        LlmModelConfig.id != config_id,
        LlmModelConfig.model_type == target.model_type,
    ).values(is_default=False))
    target.is_default = True
    await db.commit()


async def get_default_model_id(db, model_type: str = "chat") -> str | None:
    """库里配置的某类型默认模型的 **id**(模型配置主键,默认查对话模型)。

    返回 id 而不是展示名:name 允许页面改名,id 不可变,解析链拿 id 作默认值后
    按主键直接命中 db 清单,不依赖任何可能变化的别名。没有则返回 None,
    由调用方回落到 config.yaml。
    """
    from sqlalchemy import select as _select

    row = (await db.execute(
        _select(LlmModelConfig).where(
            LlmModelConfig.model_type == model_type,
            LlmModelConfig.is_default.is_(True),
            LlmModelConfig.active.is_(True),
        )
    )).scalars().first()
    return row.id if row else None


async def list_model_prices(db: AsyncSession) -> list[ModelPrice]:
    """返回全部模型单价配置(供「系统设置 · 模型单价」卡片展示)。"""
    from sqlalchemy import select as _select

    rows = (await db.execute(_select(ModelPrice).order_by(ModelPrice.model_id))).scalars().all()
    return list(rows)


async def get_model_price(db: AsyncSession, model_id: str) -> ModelPrice | None:
    """按供应商模型 ID 精确查单价配置。"""
    from sqlalchemy import select as _select

    return (await db.execute(_select(ModelPrice).where(ModelPrice.model_id == model_id))).scalars().first()


async def upsert_model_price(
    db: AsyncSession,
    model_id: str,
    input_price_cny_per_m: float | None,
    output_price_cny_per_m: float | None,
    updated_by: str,
) -> ModelPrice:
    """新增或更新某个 model_id 的单价(元/百万 token)。两个单价都可空,空则计费回落到兜底。"""
    row = await get_model_price(db, model_id)
    if row is None:
        row = ModelPrice(
            model_id=model_id,
            input_price_cny_per_m=input_price_cny_per_m,
            output_price_cny_per_m=output_price_cny_per_m,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.input_price_cny_per_m = input_price_cny_per_m
        row.output_price_cny_per_m = output_price_cny_per_m
        row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_model_price(db: AsyncSession, model_id: str) -> bool:
    """删除某个 model_id 的单价配置,计费回落到模型手填/兜底。返回是否存在并删除。"""
    from sqlalchemy import delete as _delete

    res = await db.execute(_delete(ModelPrice).where(ModelPrice.model_id == model_id))
    await db.commit()
    return res.rowcount > 0
