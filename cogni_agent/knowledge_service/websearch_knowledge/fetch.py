"""网络知识:域名白名单 + 常驻站点档案(官方站点定期增量抓取存档)。"""
from __future__ import annotations

import uuid
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name


class WebWhitelist(Base):
    __tablename__ = table_name("kb_web_whitelist")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    domain: Mapped[str] = mapped_column(String(255))
    scenic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None=集团级通用白名单


class WebSiteArchive(Base):
    """常驻站点档案:针对反复查询的官方站点,定期增量抓取存档,检索时优先命中档案而非每次实时爬取。"""

    __tablename__ = table_name("kb_web_site_archive")

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    site_url: Mapped[str] = mapped_column(String(512))
    crawl_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def is_domain_whitelisted(url: str, whitelist_domains: list[str]) -> bool:
    host = urlparse(url).netloc
    return any(host.endswith(d) for d in whitelist_domains)
