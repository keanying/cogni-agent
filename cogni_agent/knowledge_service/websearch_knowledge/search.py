"""网络知识:搜索引擎配置。与工具中心·Tool 的 HTTP 类型区别见 README——本模块面向"检索非结构化信息"。"""
from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("knowledge_service.websearch")


class WebSearchConfig(Base):
    __tablename__ = table_name("kb_web_search_config")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(32), default="bochaai")  # bochaai / searxng / bing
    api_key_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 旧字段,保留兼容
    # 密钥密文。以前 api_key 只能写在 .env 里,意味着换个搜索服务要登服务器改文件重启;
    # 现在可以在页面上维护,Fernet 加密存库,读取时才解密,接口永不回显明文。
    api_key_cipher: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)  # searxng 自建实例需要
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, default=21600)
    scope: Mapped[str] = mapped_column(String(16), default="group")
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def web_search(query: str, *, top_k: int = 5) -> list[dict]:
    """按 config.yaml: knowledge.websearch.provider 分发到具体 SDK/HTTP 调用。
    统一返回 [{"title","url","snippet"}, ...],供 router.py 融合进检索结果。
    未配置 api_key(bochaai/bing)或 base_url(searxng)时返回空列表并记录警告,
    不抛异常阻断整个对话请求——搜索失败应该降级而不是让整轮回答失败。"""
    cfg = await _effective_config()
    provider = cfg.get("provider", "bochaai")
    try:
        if provider == "bochaai":
            return await _search_bochaai(query, cfg, top_k)
        if provider == "searxng":
            return await _search_searxng(query, cfg, top_k)
        if provider == "bing":
            return await _search_bing(query, cfg, top_k)
        log.warning(f"未知网络知识搜索引擎 provider={provider}")
        return []
    except Exception:  # noqa: BLE001 - 搜索失败降级为空结果,不影响主流程
        log.exception(f"网络知识检索失败(provider={provider})")
        return []


async def _search_bochaai(query: str, cfg: dict, top_k: int) -> list[dict]:
    api_key = cfg.get("api_key")
    if not api_key:
        log.warning("websearch.provider=bochaai 但未配置 api_key,跳过")
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "summary": True, "count": top_k},
        )
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("data") or {}).get("webPages", {}).get("value", [])
    return [{"title": r.get("name"), "url": r.get("url"), "snippet": r.get("snippet")} for r in results[:top_k]]


async def _search_searxng(query: str, cfg: dict, top_k: int) -> list[dict]:
    base_url = cfg.get("base_url")
    if not base_url:
        log.warning("websearch.provider=searxng 但未配置 base_url,跳过")
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/search", params={"q": query, "format": "json"})
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")} for r in results[:top_k]]


async def _search_bing(query: str, cfg: dict, top_k: int) -> list[dict]:
    api_key = cfg.get("api_key")
    if not api_key:
        log.warning("websearch.provider=bing 但未配置 api_key,跳过")
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            headers={"Ocp-Apim-Subscription-Key": api_key},
            params={"q": query, "count": top_k},
        )
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("webPages") or {}).get("value", [])
    return [{"title": r.get("name"), "url": r.get("url"), "snippet": r.get("snippet")} for r in results[:top_k]]


async def _effective_config() -> dict:
    """生效的搜索引擎配置:**页面上维护的(数据库)优先于 config.yaml**。

    这样换搜索服务/换密钥不需要登服务器改 .env 再重启。库里没有启用中的配置时,
    回落到 config.yaml,保证旧部署行为不变。
    """
    from sqlalchemy import select as _select

    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.tool_center_service.mcp.vault import decrypt_credential

    file_cfg = dict(get_settings().get("knowledge", "websearch", default={}) or {})
    try:
        async with get_sessionmaker()() as db:
            row = (await db.execute(
                _select(WebSearchConfig).where(WebSearchConfig.active.is_(True))
                .order_by(WebSearchConfig.created_at.desc())
            )).scalars().first()
    except Exception:  # noqa: BLE001 - 数据库不可用时退回文件配置,不让检索整个瘫掉
        return file_cfg
    if row is None:
        return file_cfg

    merged = {**file_cfg, "provider": row.provider,
              "cache_ttl_seconds": row.cache_ttl_seconds}
    if row.base_url:
        merged["base_url"] = row.base_url
    if row.api_key_cipher:
        try:
            merged["api_key"] = decrypt_credential(row.api_key_cipher)
        except Exception:  # noqa: BLE001
            log.exception(f"网络知识配置 {row.id} 的 api_key 解密失败,回落到文件配置里的 key")
    return merged
