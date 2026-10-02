"""
上下文记忆:跨轮次保留「关于这次对话/这个人」的提炼事实。

**与 Checkpointer 的区别**(两者都叫「记忆」,最容易混):

| | Checkpointer | 本模块 |
|---|---|---|
| 存什么 | 完整消息历史 | 提炼出的结构化事实 |
| 长度 | 很长 | 很短,每条一行 |
| 会被裁剪吗 | **会**(超长时裁掉早期消息) | **不会**,每轮完整注入 |

这个区别是关键:消息历史被裁剪后,「这个人是销售总监」会跟着丢;而记忆不会。

**与工具实参的区别**:工具参数是「这次调用的事实」(由模型按 Schema 给出或
由前置工具解析注入);本模块是「关于这个人/这次对话的偏好与背景」,
进的是提示词而不是工具参数。

**为什么不自动全量抽取**:让模型每轮总结「学到了什么」会引入额外调用,
而且很容易把一次性的东西(「这次先看华南区」)误存成长期偏好,越积越脏。
现在是工具通过 `_remember` **显式声明**,或调接口写入。
**宁可少记,不要记错** —— 记错的信息会一直污染后续对话,
而用户完全不知道模型为什么「突然就认定」他关心某件事。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger

log = get_logger("control_plane.memory")

SCOPE_CONVERSATION = "conversation"
SCOPE_USER = "user"

# 单作用域最多保留多少条。记忆会进提示词,无限增长会把上下文吃光,
# 而且几十条「事实」堆在一起,模型反而抓不住重点。
_MAX_PER_SCOPE = 12


class ContextMemory(Base):
    __tablename__ = table_name("cp_context_memory")
    # 唯一约束**必须声明在模型上**,不能只写在迁移 SQL 里 ——
    # 全新库走 create_all 建表,不执行迁移里的 CREATE UNIQUE INDEX,
    # 索引就丢了;而 upsert 的 ON CONFLICT 依赖它,
    # 结果是「记忆写入全部静默失败」(异常被 log.debug 吞掉,正常跑根本看不见)。
    __table_args__ = (
        UniqueConstraint("scope", "owner_id", "label", name="uq_ctx_mem"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: f"mem_{uuid.uuid4().hex[:16]}")
    scope: Mapped[str] = mapped_column(String(16), index=True)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)   # conversation_id 或 user_id
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(64))                  # 「用户角色」
    value: Mapped[str] = mapped_column(Text)                        # 「销售总监」
    source: Mapped[str] = mapped_column(String(16), default="model")  # model / user / system
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


async def remember(*, scope: str, owner_id: str, label: str, value: str,
                   scenic_id: str | None = None, source: str = "model") -> None:
    """记一条。同一 (scope, owner, label) **覆盖更新** ——
    「关注周期」只该有一个值,用户说了 Q3 就该覆盖 Q2,
    两条并存会让模型自己猜用哪个。"""
    label, value = (label or "").strip()[:64], str(value or "").strip()[:2000]
    if not label or not value or not owner_id:
        return
    try:
        async with get_sessionmaker()() as db:
            await db.execute(pg_insert(ContextMemory).values(
                id=f"mem_{uuid.uuid4().hex[:16]}", scope=scope, owner_id=owner_id,
                scenic_id=scenic_id, label=label, value=value, source=source,
            ).on_conflict_do_update(
                # 按**列**匹配而不是按约束名:库里的 uq_ctx_mem 可能是
                # CREATE UNIQUE INDEX 建的「唯一索引」,而 ON CONFLICT ON CONSTRAINT
                # 只认「约束」—— 两者在 PG 里不等价,用约束名会报
                # 「constraint does not exist」。按列匹配对索引和约束都成立。
                index_elements=["scope", "owner_id", "label"],
                set_={"value": value, "source": source, "updated_at": func.now()},
            ))
            await db.commit()
            await _trim(db, scope, owner_id)
    except Exception:  # noqa: BLE001
        # 记忆写失败不该让一次本来成功的对话变成失败
        log.debug(f"记忆写入失败:{scope}/{owner_id}/{label}", exc_info=True)


async def _trim(db: AsyncSession, scope: str, owner_id: str) -> None:
    rows = (await db.execute(
        select(ContextMemory.id)
        .where(ContextMemory.scope == scope, ContextMemory.owner_id == owner_id)
        .order_by(ContextMemory.updated_at.desc()).offset(_MAX_PER_SCOPE)
    )).scalars().all()
    if rows:
        await db.execute(delete(ContextMemory).where(ContextMemory.id.in_(rows)))
        await db.commit()


async def recall(db: AsyncSession, *, conversation_id: str | None,
                 user_id: str | None) -> list[dict]:
    """取出本次可用的记忆。

    **用户级在前、对话级在后** —— 后者更具体,放后面让模型优先采信
    (「这次只看华南区」应该盖过「一般关注全国」)。
    """
    out: list[dict] = []
    for scope, owner in ((SCOPE_USER, user_id), (SCOPE_CONVERSATION, conversation_id)):
        if not owner:
            continue
        rows = (await db.execute(
            select(ContextMemory)
            .where(ContextMemory.scope == scope, ContextMemory.owner_id == owner)
            .order_by(ContextMemory.updated_at.desc()).limit(_MAX_PER_SCOPE)
        )).scalars().all()
        out.extend({"id": r.id, "label": r.label, "value": r.value,
                    "scope": r.scope, "source": r.source} for r in rows)
    return out


def as_prompt_block(items: list[dict]) -> str:
    """拼成注入系统提示的一段。空列表返回空串 —— 不要留一个空标题。"""
    if not items:
        return ""
    lines = "\n".join(f"- {i['label']}:{i['value']}" for i in items)
    return ("\n\n【已知上下文】(来自此前对话,**若与本次请求冲突以本次为准**)\n" + lines)


async def forget(db: AsyncSession, memory_id: str) -> None:
    """删除一条。**必须能删** —— 记错的信息会一直污染后续对话,
    而用户完全不知道模型为什么「突然就认定」他关心某件事。"""
    await db.execute(delete(ContextMemory).where(ContextMemory.id == memory_id))
    await db.commit()


async def capture_from_result(result: dict | None, *, conversation_id: str | None,
                              user_id: str | None, scenic_id: str | None) -> int:
    """从工具返回里提取 `_remember` 声明并落库。

    约定:`{"_remember": [{"label":"关注周期","value":"Q2 2025","scope":"conversation"}]}`
    """
    if not isinstance(result, dict):
        return 0
    items = result.get("_remember")
    if not isinstance(items, list):
        return 0

    n = 0
    for it in items[:5]:          # 一次最多 5 条,防刷屏式写入
        if not isinstance(it, dict):
            continue
        scope = it.get("scope") or SCOPE_CONVERSATION
        if scope not in (SCOPE_CONVERSATION, SCOPE_USER):
            scope = SCOPE_CONVERSATION
        owner = conversation_id if scope == SCOPE_CONVERSATION else user_id
        if not owner:
            continue
        await remember(scope=scope, owner_id=owner, label=str(it.get("label") or ""),
                       value=str(it.get("value") or ""), scenic_id=scenic_id)
        n += 1
    return n
