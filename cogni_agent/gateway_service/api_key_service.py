"""
API Key 管理服务:创建、列表、吊销、数据库级校验。

对外 AI API 入口的核心鉴权链路:
- create_api_key: 生成 Key → 存哈希到 DB → 返回明文(仅此一次)
- validate_api_key: 从 Header 中提取 Key → 查 DB 校验 → 返回绑定的 Agent 权限
- list_api_keys: 按 scenic_id 列出
- revoke_api_key: 吊销
"""
from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker, utcnow
from cogni_agent.common.errors import ApiKeyInvalid, AgentNotAllowed
from cogni_agent.common.logging import get_logger
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.agent.models import Agent, ApiKey

log = get_logger("gateway_service.api_key_service")

API_KEY_PREFIX = "sk-zhilv-"


# ═══════════════════════════════════════════════════════════════
# Key 生成与哈希
# ═══════════════════════════════════════════════════════════════

def _generate_raw_key() -> tuple[str, str, str]:
    """
    生成 API Key。
    返回: (明文key, SHA256哈希, 展示前缀如 sk-zhilv-abc...xyz)
    """
    raw = f"{API_KEY_PREFIX}{secrets.token_hex(16)}"
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:14] + "..." + raw[-4:]
    return raw, key_hash, prefix


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════
# CRUD
# ═══════════════════════════════════════════════════════════════

async def create_api_key(
    scenic_id: str | None,
    created_by: str,
    agent_allowed_ids: list[str] | None = None,
    expires_at: datetime | None = None,
) -> dict:
    """
    创建 API Key,返回明文(仅此一次)。
    scenic_id=None 表示集团级 Key(可访问所有景区)。
    """
    raw, key_hash, prefix = _generate_raw_key()
    async with get_sessionmaker()() as db:
        key = ApiKey(
            key_hash=key_hash,
            prefix_display=prefix,
            scenic_id=scenic_id,
            agent_allowed_ids=agent_allowed_ids or [],
            created_by=created_by,
            expires_at=expires_at,
        )
        db.add(key)
        await db.commit()
        await db.refresh(key)
        log.info(f"api_key created id={key.id} prefix={prefix} scenic_id={scenic_id}")
        return {
            "id": key.id,
            "key": raw,  # 明文，仅此一次
            "prefix_display": prefix,
            "scenic_id": scenic_id,
            "agent_allowed_ids": key.agent_allowed_ids,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "created_at": key.created_at.isoformat() if key.created_at else None,
        }


async def list_api_keys(scenic_id: str | None) -> list[dict]:
    """列出指定 scenic_id 下的 API Keys(不返回哈希)。"""
    async with get_sessionmaker()() as db:
        stmt = select(ApiKey).where(ApiKey.scenic_id == scenic_id).order_by(ApiKey.created_at.desc())
        result = await db.execute(stmt)
        keys = result.scalars().all()
        return [
            {
                "id": k.id,
                "prefix_display": k.prefix_display,
                "scenic_id": k.scenic_id,
                "agent_allowed_ids": k.agent_allowed_ids,
                "revoked": k.revoked,
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "created_at": k.created_at.isoformat() if k.created_at else None,
            }
            for k in keys
        ]


async def revoke_api_key(key_id: str, scenic_id: str | None) -> bool:
    """吊销 API Key,需校验 scenic_id 归属。"""
    async with get_sessionmaker()() as db:
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.scenic_id == scenic_id)
        key = (await db.execute(stmt)).scalar_one_or_none()
        if not key:
            return False
        key.revoked = True
        await db.commit()
        log.info(f"api_key revoked id={key_id}")
        return True


# ═══════════════════════════════════════════════════════════════
# 运行时鉴权（从 Header 提取 Key → 校验 → 返回权限信息）
# ═══════════════════════════════════════════════════════════════

async def validate_api_key(raw_key: str) -> dict:
    """
    校验 API Key 有效性,返回:
    {
        key_id, scenic_id, allowed_agent_ids, rate_limit_qps, rate_limit_daily
    }
    
    校验项:
    1. 前缀格式 sk-zhilv-*
    2. Key 哈希存在于 DB
    3. 未被吊销
    4. 未过期
    """
    if not raw_key or not raw_key.startswith(API_KEY_PREFIX):
        raise ApiKeyInvalid("API Key 格式无效")

    key_hash = _hash_key(raw_key)
    async with get_sessionmaker()() as db:
        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash)
        key = (await db.execute(stmt)).scalar_one_or_none()

        if not key:
            raise ApiKeyInvalid("API Key 不存在")

        if key.revoked:
            raise ApiKeyInvalid("API Key 已被吊销")

        if key.expires_at and key.expires_at < utcnow():
            raise ApiKeyInvalid("API Key 已过期")

        # 聚合限流信息：取绑定 Agent 中最宽松的限制（实际会更精细）
        # v1.1.8:限流已版本化,读最新已发布快照优先、主表初值兜底,与线上内容一致
        rate_limit_qps = 5
        rate_limit_daily = 10000
        if key.agent_allowed_ids:
            agent_stmt = select(Agent).where(Agent.id.in_(key.agent_allowed_ids))
            agents = (await db.execute(agent_stmt)).scalars().all()
            if agents:
                snaps = await ver.latest_published_snapshots(db, "agent", [a.id for a in agents])

                def _live(a: Agent, field: str):
                    s = snaps.get(a.id) or {}
                    return s[field] if field in s and s[field] is not None else getattr(a, field)

                rate_limit_qps = max(_live(a, "rate_limit_qps") for a in agents)
                rate_limit_daily = max(_live(a, "rate_limit_daily") for a in agents)

        return {
            "key_id": key.id,
            "scenic_id": key.scenic_id,
            "allowed_agent_ids": key.agent_allowed_ids,
            "rate_limit_qps": rate_limit_qps,
            "rate_limit_daily": rate_limit_daily,
        }


async def check_agent_access(key_info: dict, agent_id: str) -> dict:
    """
    校验 API Key 是否有权调用指定 Agent。
    返回 Agent 基本信息,不含敏感字段。
    """
    async with get_sessionmaker()() as db:
        stmt = select(Agent).where(Agent.id == agent_id)
        agent = (await db.execute(stmt)).scalar_one_or_none()

        if not agent:
            raise AgentNotAllowed(f"Agent {agent_id} 不存在")

        if not agent.active:
            raise AgentNotAllowed(f"Agent {agent_id} 已禁用")

        allowed = key_info["allowed_agent_ids"]
        if allowed and agent_id not in allowed:
            raise AgentNotAllowed(f"该 API Key 无权调用 Agent {agent_id}")

        # v1.1.8:内容字段(描述/人格/绑定/限流)读最新已发布快照优先,主表仅兜底初值。
        # API Key 代表 agent「当前线上能力」;创建后发布过新版,不应再返回创建时初值。
        snap = (await ver.latest_published_snapshots(db, "agent", [agent_id])).get(agent_id) or {}

        def _f(key: str, fallback):
            return snap[key] if key in snap and snap[key] is not None else fallback

        bound_skill_ids = agent.bound_skill_ids
        pins = snap.get("bound_skills")
        if pins:
            bound_skill_ids = [p["skill_id"] for p in pins
                               if isinstance(p, dict) and p.get("skill_id")]
        elif snap.get("bound_skill_ids"):  # 兼容旧格式快照(纯 id 数组)
            bound_skill_ids = list(snap["bound_skill_ids"])

        return {
            "id": agent.id,
            "name": agent.name,
            "description": _f("description", None),  # 主表 description 已删列(0045),无快照即空
            "persona_desc": _f("persona_desc", agent.persona_desc),
            "tone_style": _f("tone_style", agent.tone_style),
            "bound_skill_ids": bound_skill_ids,
            "rate_limit_qps": _f("rate_limit_qps", agent.rate_limit_qps),
            "rate_limit_daily": _f("rate_limit_daily", agent.rate_limit_daily),
        }
