"""Quick BI 集成的业务编排层(带持久化)。

职责:
  - 连接管理(加密存储、可见范围、默认连接、环境变量连接、试用凭证兜底)
  - 数据资产(仪表板 / 数据集登记)与上传文件(按能力懒上传到 Quick BI 并缓存 fileId)
  - 统一执行入口 run_stream():建运行记录 → 调能力 → 事件透传 → 收尾落库
  - 三个场景:固定问题(run_fixed / batch_run)、对话(chat)、报告(start_report / refresh_report)

所有函数都自己开短会话(get_sessionmaker),不依赖请求级 db —— 流式响应期间
FastAPI 的依赖可能已经释放,且报告轮询可能持续数十分钟,不能长期占用连接。
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import ipaddress
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sqlalchemy import delete as sa_delete
from sqlalchemy import false, func, or_, select, true, update
from sqlalchemy.orm import defer

from cogni_agent.common.db import get_sessionmaker, utcnow
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError, ZhilvError
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.tool_center_service.quickbi import capabilities as cap
from cogni_agent.tool_center_service.quickbi import filesupport as fs
from cogni_agent.tool_center_service.quickbi.client import DEFAULT_SERVER_DOMAIN, ClientConfig, QuickBIClient
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
from cogni_agent.tool_center_service.quickbi.events import AnswerSplitter, ResultAccumulator, with_channel
from cogni_agent.tool_center_service.quickbi.trace import RunTrace
from cogni_agent.tool_center_service.quickbi.models import (
    QbiAsset, QbiConnection, QbiFile, QbiGrant, QbiPrompt, QbiRun, QbiScenicDataset, QbiSession,
)

log = get_logger("tool_center_service.quickbi")

GROUP_ROLES = {"super_admin", "group_admin", "group_developer", "group_viewer"}
MANAGER_ROLES = {"super_admin", "group_admin", "group_developer"}
ENV_CONNECTION_ID = "env-default"
CAPABILITY_KEYS = set(cap.CAPABILITIES)
MAX_ROWS_PER_CHART = 1000


# ═══════════════════════════ 配置 & 身份 ═══════════════════════════

def qbi_settings() -> dict:
    return get_settings().get("quickbi", default={}) or {}


def _setting(key: str, default: Any) -> Any:
    v = qbi_settings().get(key)
    if v in (None, ""):
        return default
    try:
        return type(default)(v) if isinstance(default, (int, float)) and not isinstance(default, bool) else v
    except (TypeError, ValueError):
        return default


def _as_bool(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


DEFAULT_ALLOWED_DOMAINS = ("*.aliyun.com", "*.aliyuncs.com")


def check_server_domain(domain: str) -> str:
    """server_domain 白名单:默认只允许阿里云域名,私有化部署在 quickbi.allowed_domains 追加。

    连接会带着签名头(及试用凭证)访问该地址,放任任意地址 = 可把凭证/签名导向内网或攻击者主机。
    """
    from urllib.parse import urlparse

    dom = (domain or "").strip().rstrip("/") or DEFAULT_SERVER_DOMAIN
    u = urlparse(dom)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValidationError("server_domain 必须是 http(s)://域名 形式")
    if u.path not in ("", "/") or u.query or u.username:
        raise ValidationError("server_domain 只填协议 + 域名(如 https://bi.aliyun.com),不要带路径/参数")
    host = u.hostname.lower()
    allowed = list(DEFAULT_ALLOWED_DOMAINS) + [str(x).lower() for x in (qbi_settings().get("allowed_domains") or [])]
    if not any(host == pat or fnmatch.fnmatch(host, pat) or (pat.startswith("*.") and host == pat[2:])
               for pat in allowed):
        raise ValidationError(f"server_domain {host} 不在白名单内(config.yaml quickbi.allowed_domains),"
                              "私有化部署请先由管理员加入白名单")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            if host not in allowed:
                raise ValidationError("server_domain 不允许使用内网 IP")
    except ValueError:
        pass
    return f"{u.scheme}://{u.netloc}"


@dataclass
class Actor:
    user_id: str
    scenic_id: str | None
    role: str
    source: str = "console"            # console / api / agent
    api_key_id: str | None = None
    # 显式信任的资源 id(连接 / 资产 / 文件 / 固定问题)。只由服务端写入:执行引擎调用 quickbi 工具时,
    # 工具配置里的引用已按「工具所有者」身份校验过;报告刷新时运行记录的连接已按运行归属校验过。
    # 模型传入的 file_id / asset_id 不在其中,仍按调用者身份校验。
    trusted_refs: frozenset = frozenset()

    def trusts(self, ref: str | None) -> bool:
        return bool(ref) and ref in self.trusted_refs

    def with_trust(self, *refs: str | None) -> "Actor":
        return Actor(**{**self.__dict__, "trusted_refs": self.trusted_refs | {r for r in refs if r}})

    @property
    def is_group(self) -> bool:
        # 集团级 API Key(scenic_id 为空)等同集团角色:可见集团内全部 Quick BI 资产
        return self.role in GROUP_ROLES or (self.source == "api" and self.scenic_id is None)

    @property
    def can_manage_all(self) -> bool:
        return self.role in MANAGER_ROLES


@dataclass
class ConnView:
    id: str
    name: str
    cfg: ClientConfig
    row: QbiConnection | None = None
    api_enabled: bool = True
    is_default: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def virtual(self) -> bool:
        return self.row is None

    def client(self, *, on_raw=None, on_http=None) -> QuickBIClient:
        return QuickBIClient(self.cfg, on_raw=on_raw, on_http=on_http)

    def secrets(self) -> list[str]:
        """这一路调用会用到的凭证字面值。留痕按值打码要用 ——
        user_token 会原样出现在 userId / oapiUserId 这种看不出是凭证的字段里。"""
        return [self.cfg.user_token, self.cfg.api_secret, self.cfg.api_key]


def _trial_pair() -> tuple[str, str]:
    s = qbi_settings()
    return str(s.get("trial_api_key") or ""), str(s.get("trial_api_secret") or "")


def _base_cfg(**kw) -> ClientConfig:
    return ClientConfig(
        timeout=float(_setting("http_timeout_seconds", 60)),
        stream_timeout=float(_setting("stream_timeout_seconds", 600)),
        verify_ssl=_as_bool(qbi_settings().get("verify_ssl", True)),
        **kw,
    )


def env_connection() -> ConnView | None:
    """config.yaml quickbi.default_connection(环境变量注入)构成的只读虚拟连接。"""
    d = qbi_settings().get("default_connection") or {}
    token = str(d.get("user_token") or "")
    if not token:
        return None
    ak, sk = str(d.get("api_key") or ""), str(d.get("api_secret") or "")
    trial = False
    if not ak and not sk:
        ak, sk = _trial_pair()
        trial = bool(ak and sk)
    cfg = _base_cfg(server_domain=str(d.get("server_domain") or DEFAULT_SERVER_DOMAIN), api_key=ak,
                    api_secret=sk, user_token=token, locale=str(d.get("locale") or "zh_CN"))
    return ConnView(id=ENV_CONNECTION_ID, name=str(d.get("name") or "环境变量默认连接"), cfg=cfg,
                    api_enabled=bool(d.get("api_enabled", True)), is_default=False, extra={"trial": trial})


def _encrypt(v: str) -> str:
    from cogni_agent.tool_center_service.mcp.vault import encrypt_credential
    try:
        return encrypt_credential(v)
    except RuntimeError as exc:
        raise ValidationError(f"无法加密保存 Quick BI 凭证:{exc}") from exc


def _decrypt(v: str | None) -> str:
    if not v:
        return ""
    from cogni_agent.tool_center_service.mcp.vault import decrypt_credential
    try:
        return decrypt_credential(v)
    except Exception as exc:  # noqa: BLE001
        raise QuickBIError("Quick BI 凭证解密失败(CREDENTIAL_FERNET_KEY 可能已变更),请重新保存连接", kind="config") from exc


def view_of(row: QbiConnection) -> ConnView:
    ak, sk = row.api_key or "", _decrypt(row.api_secret_cipher)
    trial = False
    if (not ak or not sk) and row.use_trial:
        ak, sk = _trial_pair()
        trial = True
    cfg = _base_cfg(server_domain=row.server_domain or DEFAULT_SERVER_DOMAIN, api_key=ak, api_secret=sk,
                    user_token=_decrypt(row.user_token_cipher), locale=row.default_locale or "zh_CN")
    return ConnView(id=row.id, name=row.name, cfg=cfg, row=row, api_enabled=row.api_enabled,
                    is_default=row.is_default, extra={"trial": trial})


def _grant_clause(actor: Actor):
    """连接分配闸门(0075)。

    白名单是**按连接**生效的:某个连接一旦被分配给过至少一个人,它就只对被分配的人开放;
    一条分配都没有的连接维持原样(景区 + 可见性)。这样上线当天不会有人突然用不了 ——
    要收紧哪个连接,给它分配一次就行。

    **集团角色天然在白名单里**(含集团只读)。白名单是给景区之间做隔离的 ——
    「这条连接只给这几个人用」说的是景区侧的人,不是集团侧的管理员。原先只豁免超管,
    于是出现一种死配置:一条集团级连接被分配给了某个人,那个人离职/换岗之后,
    连集团管理员都用不了它,也就没人能把它重新分配出去 —— 只能进数据库改。
    配合「归属景区即授权」,任何一条连接都至少有人能用:景区侧看归属,集团侧永远能用。

    开放平台 API Key 不是「人」,走连接自身的 api_enabled + 景区,不受本闸门约束。
    """
    from cogni_agent.common import permissions as perm

    if actor.source != "console" or perm.is_group_role(actor.role):
        return true()
    restricted = select(QbiGrant.connection_id).distinct()
    mine = select(QbiGrant.connection_id).where(QbiGrant.user_id == actor.user_id)
    clause = or_(QbiConnection.id.notin_(restricted), QbiConnection.id.in_(mine))
    if actor.scenic_id:
        # 绑到本景区的连接不进白名单闸门:归属景区本身就是一次授权,
        # 再要求逐人分配一遍,等于改一次归属就得把整个景区的人重新分配一轮
        clause = or_(clause, QbiConnection.scenic_id == actor.scenic_id)
    return clause


def _visible_clause(actor: Actor):
    """谁能看到这条连接。

    口径就一句话:**连接绑到哪个景区,那个景区的人就能用**。
    原先只认 visibility=scenic,于是管理员把一条 group 连接绑到某景区之后,
    该景区的账号反而看不到它了 —— 改完归属再发起提问就失败,
    而报错是「连接不存在或无权访问」,没人会往「可见范围」上想。
    """
    from cogni_agent.common import permissions as perm

    if actor.source == "console" and perm.is_group_role(actor.role):
        # 集团角色(含只读)在管理端看得到**全部**连接,不再按 visibility 过滤。
        #
        # 原先这里只放行 group/public,于是出现一条谁都修不了的连接:
        # 景区管理员建了一条 private 连接绑到自己景区 —— 他自己用不了(private 否决了归属景区,
        # 已在下面修掉),而超级管理员去后台想帮他改,连接压根不在列表里,报「连接不存在或无权访问」。
        # 两头都看不见,就只能去数据库里改。
        # private 的本意是「别人别用我的凭证」,不是「集团管不着」—— 集团角色本来就该看见全平台的配置。
        return _grant_clause(actor)
    conds = [QbiConnection.owner_user_id == actor.user_id, QbiConnection.visibility == "public"]
    if actor.is_group:
        conds.append(QbiConnection.visibility == "group")
    if actor.scenic_id:
        # 绑到我景区的连接:**一律可用,不再看 visibility**。
        #
        # 归属景区本身就是一次授权 —— 管理员把连接绑到这个景区,意思就是「这个景区的人用它」。
        # 原先这里还要求 visibility != private,于是出现一种谁也想不到的组合:
        # 连接绑着你的景区、名字就叫你的名字,你却用不了,报「连接不存在或无权访问」。
        # private 的本意是「没绑景区时只有创建者能用」,不该拿来否决一条明确绑给你的连接。
        conds.append(QbiConnection.scenic_id == actor.scenic_id)
        # 集团级连接(未绑景区):group/public 是所有景区共用的那条默认连接。
        # scenic 这个取值在集团级连接上是个**无意义组合**(没绑景区,却说"景区内可见"),
        # 现场配出来就是一条谁都用不了的连接 —— 按 group 处理,而不是让它静悄悄失效。
        conds.append(QbiConnection.scenic_id.is_(None)
                     & QbiConnection.visibility.in_(("group", "public", "scenic")))
    if actor.source == "api":
        # 开放平台:集团级 Key(scenic_id 为空)可用全部已开放 API 的连接;景区 Key 只能用本景区/公开连接
        base = QbiConnection.api_enabled.is_(True) & (QbiConnection.visibility != "private")
        if actor.scenic_id is None:
            return base
        return base & or_(QbiConnection.scenic_id == actor.scenic_id, QbiConnection.visibility == "public")
    # 可见性(谁的资源)与分配(分给了谁)是两件事,两个都得过
    return or_(*conds) & _grant_clause(actor)


def _can_write(row, actor: Actor) -> bool:
    """能不能改这条记录:按**角色 + 景区**判,不再按创建者。

    原先是「谁建的谁能改」,于是同景区换个管理员接手就动不了前任建的东西,只能找超管 ——
    而景区管理员本来就该管得了本景区的全部内容。集团级记录(scenic_id 为空)景区角色不能改:
    那是所有景区共用的一份,改一次全场跟着变。
    """
    from cogni_agent.common import permissions as perm

    if actor.source != "console" or not perm.can_create(actor.role):
        return False
    if perm.can_manage_all(actor.role):
        return True
    if perm.is_group_role(actor.role):      # group_viewer 已被 can_create 拦下
        return False
    return bool(actor.scenic_id) and row.scenic_id == actor.scenic_id


def _can_delete(row, actor: Actor) -> bool:
    """能不能删:在 _can_write 的基础上再要求角色有删除权(景区操作员没有)。"""
    from cogni_agent.common import permissions as perm

    return _can_write(row, actor) and perm.can_delete(actor.role)


def connection_dict(v: ConnView) -> dict:
    r = v.row
    return {
        "id": v.id, "name": v.name, "virtual": v.virtual,
        **v.cfg.masked(),
        "has_api_secret": bool(v.cfg.api_secret), "has_user_token": bool(v.cfg.user_token),
        "use_trial": bool(r.use_trial) if r else bool(v.extra.get("trial")),
        "trial_active": bool(v.extra.get("trial")),
        "default_locale": v.cfg.locale, "is_default": v.is_default, "api_enabled": v.api_enabled,
        "active": r.active if r else True,
        "status": r.status if r else "env", "status_message": r.status_message if r else None,
        "checked_at": r.checked_at.isoformat() if r and r.checked_at else None,
        "visibility": r.visibility if r else "group", "scenic_id": r.scenic_id if r else None,
        "owner_user_id": r.owner_user_id if r else None,
        "missing": v.cfg.missing(),
        "created_at": r.created_at.isoformat() if r and r.created_at else None,
    }


async def list_connections(actor: Actor) -> list[ConnView]:
    async with get_sessionmaker()() as db:
        stmt = select(QbiConnection).where(_visible_clause(actor), QbiConnection.active.is_(True))
        rows = (await db.execute(stmt.order_by(QbiConnection.is_default.desc(), QbiConnection.created_at))).scalars()
        views = []
        for r in rows:
            try:
                views.append(view_of(r))
            except QuickBIError as exc:
                v = ConnView(id=r.id, name=r.name, cfg=_base_cfg(server_domain=r.server_domain, api_key=r.api_key),
                             row=r, api_enabled=r.api_enabled, is_default=r.is_default, extra={"error": str(exc)})
                views.append(v)
    env = env_connection()
    if env and _env_allowed(actor, env):
        views.append(env)
    return views


def _env_allowed(actor: Actor, env: ConnView) -> bool:
    """环境变量连接相当于集团级共享账号:仅集团角色 / 集团级 API Key / 执行引擎(工具已授权)可用。"""
    if actor.trusts(ENV_CONNECTION_ID):
        return True
    if actor.source == "api":
        return env.api_enabled and actor.scenic_id is None
    return actor.role in GROUP_ROLES


async def get_connection(actor: Actor, connection_id: str | None, *, write: bool = False) -> ConnView:
    """按 id 取连接;id 为空时取默认连接(显式默认 > 第一个可见 > 环境变量连接)。"""
    if connection_id == ENV_CONNECTION_ID:
        env = env_connection()
        if env is None:
            raise NotFound("未配置环境变量默认连接(QUICKBI_USER_TOKEN 等)")
        if write:
            raise Forbidden("环境变量连接为只读,请修改 .env 后重启")
        if not _env_allowed(actor, env):
            raise Forbidden("环境变量连接仅对集团角色 / 集团级 API Key 开放")
        return env
    async with get_sessionmaker()() as db:
        if connection_id:
            row = await db.get(QbiConnection, connection_id)
            if row is None:
                raise NotFound(f"Quick BI 连接不存在:{connection_id}")
            if not actor.trusts(row.id) and not await _row_visible(db, actor, row.id):
                # 说清**为什么**看不到。一句「不存在或无权访问」让人无从下手:
                # 是 ID 错了?连接被删了?还是权限问题?—— 三种猜法方向完全不同。
                raise NotFound(await _why_invisible(db, actor, row))
            if write and not _can_write(row, actor):
                raise Forbidden("只有连接创建者或集团管理员/开发者可以修改该连接")
            if not row.active and not write:
                raise ValidationError(f"Quick BI 连接「{row.name}」已停用")
            return view_of(row)
    views = await list_connections(actor)
    if not views:
        raise NotFound("还没有可用的 Quick BI 连接,请先到「工具中心 · Quick BI」新建连接")
    # 默认连接:自己设的默认 > 他人共享的默认 > 第一个可见
    own = [v for v in views if v.is_default and v.row is not None and v.row.owner_user_id == actor.user_id]
    return (own or [v for v in views if v.is_default] or views)[0]


async def _why_invisible(db, actor: Actor, row: QbiConnection) -> str:
    """这条连接为什么对当前账号不可见 —— 直接把下一步怎么办说出来。"""
    from sqlalchemy import select as _select

    if row.scenic_id and actor.scenic_id and row.scenic_id != actor.scenic_id:
        return (f"Quick BI 连接「{row.name}」绑定的是景区 {row.scenic_id},"
                f"而当前账号绑定的是 {actor.scenic_id},不能跨景区使用这套凭证")
    if row.scenic_id and not actor.scenic_id:
        return (f"Quick BI 连接「{row.name}」是景区 {row.scenic_id} 专属的,"
                "当前账号未绑定景区;请在「系统设置 · 组织管理」给账号绑定景区")
    granted = (await db.execute(_select(QbiGrant.id).where(
        QbiGrant.connection_id == row.id).limit(1))).scalar_one_or_none()
    if granted is not None:
        return (f"Quick BI 连接「{row.name}」已启用白名单(按人分配),当前账号不在名单里;"
                "请到「分发中心」把它分配给这个账号")
    if row.visibility == "private":
        return (f"Quick BI 连接「{row.name}」的可见范围是 private(仅创建者),"
                "且没有绑定到任何景区;请把它的归属景区绑到你的景区,或把可见范围改成 group")
    return (f"Quick BI 连接「{row.name}」对当前账号不可见(可见范围 {row.visibility});"
            "请在「工具中心 · Quick BI · 连接配置」调整它的归属景区或可见范围")


# 加速(预热)的默认值。问题上没单独配时用它们。
# 刷新 30 分钟、存储 60 分钟:存储时间刻意比刷新时间长一倍 ——
# 两者相等的话,上一份刚好过期、下一份还没跑完的那个瞬间会漏成现跑,
# 而那正是用户最容易撞上的时刻(整点前后)。
_PREWARM_DEFAULT_INTERVAL_MIN = 30
_PREWARM_DEFAULT_TTL_MIN = 60


def prewarm_settings(issue) -> tuple[bool, int, int]:
    """这条问题的加速配置:(开没开, 刷新分钟, 存储分钟)。"""
    enabled = bool(getattr(issue, "prewarm_enabled", False))
    interval = int(getattr(issue, "prewarm_interval_minutes", None)
                   or _setting("prewarm_interval_minutes", _PREWARM_DEFAULT_INTERVAL_MIN))
    ttl = int(getattr(issue, "prewarm_ttl_minutes", None)
              or _setting("prewarm_ttl_minutes", _PREWARM_DEFAULT_TTL_MIN))
    return enabled, max(1, interval), max(1, ttl)


def _fixed_ids(issue) -> list[str]:
    """一条固定问题在运行记录里可能以哪几个 id 出现。

    统一表的业务主键是 issue_id,但历史运行记录里存的是老表的 UUID
    (source_question_id)。只按 issue_id 找缓存的话,老问题刚迁过来那阵子
    一条都命中不了 —— 而且不报错,只是「加速开了但从来没快过」,很难被发现。
    """
    ids = [getattr(issue, "issue_id", None), getattr(issue, "source_question_id", None)]
    return [str(i) for i in ids if i]


async def cached_run(issue, scenic_id: str | None, ttl_minutes: int,
                     question: str | None = None) -> QbiRun | None:
    """这条问题在这个景区最近一次成功的运行,且还在存储时间内。

    issue 传整行(不是 id):要同时按 issue_id 和历史 UUID 去找,见 _fixed_ids。

    **景区必须一起比**:公开的固定问题可以被不同景区问,拿 A 景区的结果回放给 B 景区,
    是把别人的数据给了他 —— 而且不报错,看起来就是一份正常的回答。

    **问题原文也要比**:固定问题支持 {{变量}}(如 {{month}}),同一条问题填不同的月份
    是两个问题。只按 id 比的话,问 8 月会拿到 7 月的数 —— 数字看着完全正常,
    没人会怀疑。填出来的文本对不上就当没缓存,老老实实现跑。

    没有单独的缓存表:结果本来就整份存在 tc_qbi_runs 里。多一份存储就多一处会不同步,
    而缓存与源数据不同步的表现是「看到的是旧数,还没人察觉」。
    """
    from datetime import timedelta

    ids = _fixed_ids(issue)
    if not ids:
        return None
    since = utcnow() - timedelta(minutes=max(1, ttl_minutes))
    async with get_sessionmaker()() as db:
        stmt = select(QbiRun).where(
            QbiRun.fixed_question_id.in_(ids),
            QbiRun.status == "success",
            QbiRun.created_at >= since,
        )
        if question is not None:
            stmt = stmt.where(QbiRun.question == question)
        # scenic_id 为空(集团级)时只认同样为空的那些,不要把某个景区的结果当集团级的
        stmt = stmt.where(QbiRun.scenic_id == scenic_id) if scenic_id \
            else stmt.where(QbiRun.scenic_id.is_(None))
        return (await db.execute(
            stmt.order_by(QbiRun.created_at.desc()).limit(1))).scalars().first()


def _replay_events(run: QbiRun, question: str) -> list[dict]:
    """把一次成功的运行回放成事件流。

    形状与真跑一遍完全一致(run → answer → done),前端不需要为「缓存命中」
    写第二套渲染 —— 写了就会两套慢慢长歪,而歪掉的那套通常是没人天天看的那套。
    """
    result = run.result or {}
    return [
        {"type": "run", "run_id": run.id, "capability": run.capability, "routed": False,
         "connection_id": run.connection_id, "connection_name": "", "locale": "",
         "label": cap.CAPABILITIES.get(run.capability, {}).get("label", run.capability),
         "capability_source": "prewarm", "sent_question": question,
         # 明确标出这是回放,不是刚跑的 —— 界面可以显示「预热结果 · 12:30 生成」,
         # 不标的话用户会以为这就是实时数据
         "from_cache": True, "cached_at": run.created_at.isoformat() if run.created_at else None},
        {"type": "answer", "text": run.answer or ""},
        {"type": "done", "run_id": run.id, "status": "success", "capability": run.capability,
         "answer": run.answer, "error": None, "error_code": None,
         "duration_ms": 0, "chat_id": run.chat_id, "report_url": run.report_url,
         "chart_count": len((result.get("charts") or [])),
         "usage": result.get("usage") or {}, "trace_id": result.get("trace_id"),
         "from_cache": True, "cached_at": run.created_at.isoformat() if run.created_at else None,
         "result": result},
    ]


async def usable_connection_id(actor: Actor, connection_id: str | None,
                               scenic_id: str | None) -> str | None:
    """问题/资产上写着的那条连接现在还能用吗?不能用就按景区换一条。

    为什么需要这一步:连接 id 是**存在行里**的(固定问题、主题解读、资产、工具配置),
    而连接本身会被删、会被改绑到别的景区、会被停用 —— 存着的那个 id 不会跟着变。
    于是出现一种一点就废的状态:问题还在、景区还在、人也没问题,一发起就报
    「Quick BI 连接不存在:290987f7-…」,而那串 id 对运营毫无意义,
    查也查不到(行已经删了),只能来找开发。

    APP 端早就有这道解析(scenic_binding.connection_usable),**管理端没有** ——
    同一条问题,从 APP 点能跑,从管理端点就报错。现在放在 run_stream 底下,两边同一套。

    返回 None 表示这个景区一条能用的连接都没有,由调用方给出「去哪配」的提示。
    """
    cid = (connection_id or "").strip() or None
    if cid:
        async with get_sessionmaker()() as db:
            row = await db.get(QbiConnection, cid)
        # 还在、没停用、且没被改绑到别的景区 —— 三者都满足才继续用它
        if row is not None and row.active and (
                not row.scenic_id or not scenic_id or row.scenic_id == scenic_id):
            return row.id
        log.warning("Quick BI 连接 %s 对景区 %s 已不可用(%s),按景区重新解析",
                    cid, scenic_id or "(集团级)",
                    "已删除" if row is None else ("已停用" if not row.active else "已改绑其它景区"))
    # 重挑:本景区的优先(同景区多条时默认连接优先),其次集团级的;都按「当前账号看得见」过滤。
    # **挑不到就返回 None,不拿「第一条看得见的」凑数**:集团账号看得见所有景区的连接,
    # 凑数凑到的多半是别的景区的那条 —— 用别人的 user_token 问数,拿到的仍是一份像模像样的
    # 答案,只是数据是别的景区的。这种静默串数据比报错难查得多。
    views = [v for v in await list_connections(actor) if v.row is not None]
    if scenic_id:
        # 景区数据集映射是显式配置(运营确认过这个景区的数据在哪条连接上),排在推断之前
        mapped = await scenic_dataset_row(scenic_id)
        if mapped is not None and any(v.id == mapped.connection_id and v.row.active for v in views):
            return mapped.connection_id
    for want in ([scenic_id] if scenic_id else []) + [None]:
        hits = sorted((v for v in views if v.row.scenic_id == want), key=lambda v: not v.is_default)
        if hits:
            return hits[0].id
    return None


async def _row_visible(db, actor: Actor, conn_id: str) -> bool:
    stmt = select(QbiConnection.id).where(QbiConnection.id == conn_id, _visible_clause(actor))
    return (await db.execute(stmt)).scalar_one_or_none() is not None


# ── 景区数据集映射(0091) ──────────────────────────────────────────────
# 一景区一行:用哪条连接 + 哪个数据集取数 + 景区级预热开关。**显式配置**,所以能被当默认值用
# (问题没指定目标数据时取这里的数据集、问题绑的连接失效时优先换成这里的连接);
# 没配映射的景区维持原逻辑,不猜别的景区的资产。

def scenic_dataset_dict(r: QbiScenicDataset, connection_name: str | None = None) -> dict:
    return {"id": r.id, "scenic_id": r.scenic_id, "connection_id": r.connection_id,
            "connection_name": connection_name, "cube_id": r.cube_id, "cube_name": r.cube_name,
            "prewarm_enabled": bool(r.prewarm_enabled), "source": (r.meta or {}).get("source"),
            "updated_by": r.updated_by,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None}


async def scenic_dataset_row(scenic_id: str | None) -> QbiScenicDataset | None:
    sid = (scenic_id or "").strip()
    if not sid:
        return None
    async with get_sessionmaker()() as db:
        return (await db.execute(select(QbiScenicDataset).where(
            QbiScenicDataset.scenic_id == sid))).scalars().first()


async def scenic_prewarm_allowed(scenic_id: str | None) -> bool:
    """这个景区允许加速预热吗:有映射且关了预热 = 不允许;没映射 = 维持原样(允许)。"""
    row = await scenic_dataset_row(scenic_id)
    return row is None or bool(row.prewarm_enabled)


def _assert_group_manager(actor: Actor) -> None:
    # 映射是平台级取数资源台账:改一行就改了这个景区所有问题的默认数据,只给集团管理角色
    if not actor.can_manage_all:
        raise Forbidden("只有集团管理员 / 开发者可以改动景区数据集映射")


async def list_scenic_datasets(actor: Actor) -> list[dict]:
    """映射清单。集团账号看全部,景区账号只看自己景区那一行(只读)。"""
    async with get_sessionmaker()() as db:
        stmt = select(QbiScenicDataset).order_by(QbiScenicDataset.scenic_id)
        if not actor.is_group:
            stmt = stmt.where(QbiScenicDataset.scenic_id == (actor.scenic_id or ""))
        rows = (await db.execute(stmt)).scalars().all()
        names = {i: n for i, n in (await db.execute(select(QbiConnection.id, QbiConnection.name))).all()}
    return [scenic_dataset_dict(r, names.get(r.connection_id)) for r in rows]


async def upsert_scenic_dataset(actor: Actor, *, scenic_id: str, connection_id: str, cube_id: str,
                                cube_name: str | None = None, prewarm_enabled: bool | None = None) -> dict:
    """新增 / 修改一个景区的映射。

    连接必须存在且启用(映射指向一条死连接是纯错误);数据集名没给时按连接实时清单补一次 ——
    补不到也不拦(对方系统暂时不可用时,运营仍需要能先把映射配起来,填错了列表上看得见、改就是)。
    连接与景区**不强制一致**:有的景区就是借集团账号的连接取数。
    """
    _assert_group_manager(actor)
    sid, cid, cube = (str(x or "").strip() for x in (scenic_id, connection_id, cube_id))
    if not sid or not cid or not cube:
        raise ValidationError("景区、连接、数据集都不能为空")
    view = await get_connection(actor, cid)
    if view.row is None:
        raise ValidationError("环境变量连接不能用于景区映射,请先在「连接配置」新建一条")
    if not view.row.active:
        raise ValidationError(f"连接「{view.name}」已停用")
    name = (cube_name or "").strip()
    if not name:
        try:
            async with view.client() as qbi:
                name = next((c["cube_name"] for c in await cap.list_cubes(qbi) if c["cube_id"] == cube), "")
        except QuickBIError as exc:
            log.warning("补数据集名称失败(不影响保存):%s", exc)
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(QbiScenicDataset).where(
            QbiScenicDataset.scenic_id == sid))).scalars().first()
        if row is None:
            row = QbiScenicDataset(scenic_id=sid, prewarm_enabled=True if prewarm_enabled is None else prewarm_enabled)
            db.add(row)
        elif prewarm_enabled is not None:
            row.prewarm_enabled = prewarm_enabled
        row.connection_id, row.cube_id = cid, cube
        row.cube_name = (name or row.cube_name or "")[:255]
        row.meta = {**(row.meta or {}), "source": "manual"}
        row.updated_by = actor.user_id
        await db.commit()
        await db.refresh(row)
        return scenic_dataset_dict(row, view.name)


async def set_scenic_dataset_prewarm(actor: Actor, scenic_id: str, enabled: bool) -> dict:
    _assert_group_manager(actor)
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(QbiScenicDataset).where(
            QbiScenicDataset.scenic_id == scenic_id))).scalars().first()
        if row is None:
            raise NotFound(f"景区 {scenic_id} 还没有数据集映射")
        row.prewarm_enabled, row.updated_by = bool(enabled), actor.user_id
        await db.commit()
        await db.refresh(row)
        return scenic_dataset_dict(row)


async def delete_scenic_dataset(actor: Actor, scenic_id: str) -> None:
    """删映射。已经跑出来的运行记录不动(那是历史),只是之后这个景区不再按映射取默认数据。"""
    _assert_group_manager(actor)
    async with get_sessionmaker()() as db:
        await db.execute(sa_delete(QbiScenicDataset).where(QbiScenicDataset.scenic_id == scenic_id))
        await db.commit()


# ── 口径说明库(0091) ──────────────────────────────────────────────────
# 库里的一条 = 一段可复用的口径 / 约束。导入是**抄**进固定问题的 model_instruction,不是引用:
# 引用会让「改一次库」静默改写所有正在跑的问题,而口径变更恰恰需要先在一条问题上验证。

def prompt_dict(p: QbiPrompt, actor: Actor | None = None) -> dict:
    d = {"id": p.id, "name": p.name, "content": p.content, "description": p.description,
         "owner_user_id": p.owner_user_id, "scenic_id": p.scenic_id, "visibility": p.visibility,
         "created_at": p.created_at.isoformat() if p.created_at else None,
         "updated_at": p.updated_at.isoformat() if p.updated_at else None}
    if actor is not None:
        d["can_write"] = p.owner_user_id == actor.user_id or actor.can_manage_all
    return d


def _prompt_scope(actor: Actor):
    """可见范围与固定问题同口径:集团全放行;否则 自己的 / public / 本景区 / 集团级共享。"""
    if actor.is_group:
        return true()
    conds = [QbiPrompt.owner_user_id == actor.user_id, QbiPrompt.visibility == "public",
             QbiPrompt.scenic_id.is_(None) & QbiPrompt.visibility.in_(("group", "public"))]
    if actor.scenic_id:
        conds.append((QbiPrompt.scenic_id == actor.scenic_id) & QbiPrompt.visibility.in_(("scenic", "group")))
    return or_(*conds)


async def list_prompts(actor: Actor, keyword: str | None = None) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(QbiPrompt).where(_prompt_scope(actor))
        if keyword and keyword.strip():
            k = f"%{keyword.strip()}%"
            stmt = stmt.where(or_(QbiPrompt.name.ilike(k), QbiPrompt.content.ilike(k),
                                  QbiPrompt.description.ilike(k)))
        # 最近维护过的排前面:最常改的口径往往也是最该用的那份
        rows = (await db.execute(stmt.order_by(QbiPrompt.updated_at.desc(), QbiPrompt.created_at.desc())
                                 .limit(200))).scalars().all()
        return [prompt_dict(p, actor) for p in rows]


async def save_prompt(actor: Actor, data: dict, prompt_id: str | None = None) -> dict:
    from cogni_agent.common import permissions as perm

    perm.assert_can_write(actor.role, "口径说明")
    if data.get("visibility") is not None and data["visibility"] not in ("private", "scenic", "group", "public"):
        raise ValidationError("visibility 只能是 private / scenic / group / public")
    async with get_sessionmaker()() as db:
        if prompt_id:
            p = await db.get(QbiPrompt, prompt_id)
            if p is None:
                raise NotFound("口径说明不存在")
            if p.owner_user_id != actor.user_id and not actor.can_manage_all:
                raise Forbidden("只有创建者或集团管理员 / 开发者可以修改")
        else:
            for k, label in (("name", "名称"), ("content", "内容")):
                if not str(data.get(k) or "").strip():
                    raise ValidationError(f"{label}不能为空")
            p = QbiPrompt(owner_user_id=actor.user_id, scenic_id=actor.scenic_id,
                          visibility="group" if actor.is_group else "scenic", name="", content="")
            db.add(p)
        for k in ("name", "content", "description", "visibility"):
            v = data.get(k)
            if v is None or (k in ("name", "content") and not str(v).strip()):
                continue            # 名称与正文 NOT NULL:收到空串保持原值,不把已有内容清空
            setattr(p, k, str(v).strip() if k != "content" else v)
        p.name = p.name[:128]
        await db.commit()
        await db.refresh(p)
        return prompt_dict(p, actor)


async def delete_prompt(actor: Actor, prompt_id: str) -> None:
    async with get_sessionmaker()() as db:
        p = await db.get(QbiPrompt, prompt_id)
        if p is None:
            raise NotFound("口径说明不存在")
        if p.owner_user_id != actor.user_id and not actor.can_manage_all:
            raise Forbidden("只有创建者或集团管理员 / 开发者可以删除")
        # 抄进固定问题的那些已经是问题自己的文本,不受影响 —— 这也是不做外键引用的收益
        await db.delete(p)
        await db.commit()


# ── 连接分配(0075:Quick BI 进分发中心) ──────────────────────────────────
# 分配的是「能不能用这套凭证」,不是「用哪一版」—— 连接没有版本概念,
# 所以单开一张表(tc_qbi_grants),但在分发中心里与 Agent/Skill 并排。

async def _assert_can_grant(db, actor: Actor, connection_id: str,
                            *, removing: bool = False) -> QbiConnection:
    """能分配这个连接的人 = 对它有写权限的人(集团三角色,或本景区的景区角色)。

    为什么不是「谁能看见谁就能分」:看得见只说明能用,分配是把它给别人 ——
    那是管理动作,得由管得了这个连接的人来做。
    解除分配是删除动作,再多一道:景区操作员不能做(removing=True)。
    """
    from cogni_agent.common import permissions as perm

    row = await db.get(QbiConnection, connection_id)
    if row is None:
        raise NotFound(f"Quick BI 连接不存在:{connection_id}")
    if removing:
        if not _can_delete(row, actor):
            perm.assert_can_delete_obj(actor.role, actor.scenic_id, row.scenic_id, "连接分配")
            raise Forbidden("无权解除该连接的分配")
    elif not _can_write(row, actor):
        perm.assert_can_write_obj(actor.role, actor.scenic_id, row.scenic_id, "连接分配")
        raise Forbidden("无权分配该连接")
    return row


async def _grant_dict(db, row: QbiGrant) -> dict:
    from cogni_agent.user_center_service.models import User

    u = await db.get(User, row.user_id)
    return {
        "id": row.id, "connection_id": row.connection_id, "user_id": row.user_id,
        "user_name": getattr(u, "name", None), "user_account": getattr(u, "account", None),
        "user_role": getattr(u, "role", None), "user_scenic_id": getattr(u, "scenic_id", None),
        "note": row.note, "created_by": row.created_by,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


async def list_grants(actor: Actor, connection_id: str) -> dict:
    """某个连接分配给了哪些人。空列表 = 该连接尚未启用白名单(按景区+可见性判定)。"""
    async with get_sessionmaker()() as db:
        await _assert_can_grant(db, actor, connection_id)
        rows = (await db.execute(
            select(QbiGrant).where(QbiGrant.connection_id == connection_id)
            .order_by(QbiGrant.created_at.desc())
        )).scalars().all()
        return {"items": [await _grant_dict(db, r) for r in rows],
                "restricted": len(rows) > 0}


async def grant_connection(actor: Actor, connection_id: str, user_id: str,
                           note: str = "") -> dict:
    """把连接分配给某个用户(重复分配 = 更新备注,幂等)。"""
    from cogni_agent.user_center_service.models import User

    if not (user_id or "").strip():
        raise ValidationError("必须指定被分配的用户")
    async with get_sessionmaker()() as db:
        row = await _assert_can_grant(db, actor, connection_id)
        target = await db.get(User, user_id)
        if target is None:
            raise NotFound("用户不存在")
        existed = (await db.execute(select(QbiGrant).where(
            QbiGrant.connection_id == connection_id, QbiGrant.user_id == user_id))).scalars().first()
        first = existed is None and (await db.execute(
            select(QbiGrant.id).where(QbiGrant.connection_id == connection_id).limit(1)
        )).scalar_one_or_none() is None
        if existed is None:
            existed = QbiGrant(connection_id=connection_id, user_id=user_id, note=note,
                               created_by=actor.user_id)
            db.add(existed)
        elif note:
            existed.note = note
        await db.commit()
        await db.refresh(existed)
        # 通知要说清分给了谁(姓名+账号),不能只写「该用户」
        who = f"用户「{target.name or user_id}」" + (f"({target.account})" if target.account else "")
        msg = f"已把「{row.name}」分配给{who}"
        if first:
            # 第一条分配会把这个连接从「按景区可见」切成白名单,说清楚,别让人事后才发现
            msg += "。这是该连接的第一条分配 —— 它从现在起只对被分配的人开放,其他人将不可见"
        log.info(f"quickbi grant {connection_id} -> {user_id}(first={first})")
        return {"ok": True, "grant": await _grant_dict(db, existed), "message": msg,
                "restricted": True}


async def revoke_grant(actor: Actor, grant_id: str) -> dict:
    """解除分配。解除最后一条 = 该连接回到「按景区+可见性」判定(不再是白名单)。"""
    async with get_sessionmaker()() as db:
        row = await db.get(QbiGrant, grant_id)
        if row is None:
            raise NotFound("分配记录不存在")
        conn = await _assert_can_grant(db, actor, row.connection_id, removing=True)
        conn_id = row.connection_id
        from cogni_agent.user_center_service.models import User
        u = await db.get(User, row.user_id)
        who = (f"用户「{u.name or row.user_id}」" + (f"({u.account})" if u.account else "")
               if u is not None else "该用户")
        await db.delete(row)
        await db.commit()
        left = len((await db.execute(
            select(QbiGrant.id).where(QbiGrant.connection_id == conn_id))).scalars().all())
        msg = f"已解除{who}对「{conn.name}」的分配"
        if left == 0:
            msg += "。该连接已无任何分配,回到按景区/可见性判定(所有可见的人都能用)"
        log.info(f"quickbi revoke {grant_id}(conn={conn_id}, left={left})")
        return {"ok": True, "message": msg, "restricted": left > 0}


async def grants_by_user(actor: Actor, target_user_id: str) -> dict:
    """人视角:该用户对每个连接的状态 —— granted / not_granted / open。

    open = 该连接还没启用白名单(零分配),按景区+可见性判定,不需要分配也能用。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.user_center_service.models import User

    async with get_sessionmaker()() as db:
        target = await db.get(User, target_user_id)
        if target is None:
            raise NotFound("用户不存在")
        # 只能看本景区的人:原先谁都能拿别的景区的 user_id 来查,
        # 连带把那个人的姓名/账号/手机号一起返回
        if not actor.is_group and target.id != actor.user_id:
            if not actor.scenic_id or (target.scenic_id or "") != actor.scenic_id:
                raise NotFound("用户不存在")
        # 连接范围也要收:这条原先是 select(...).where(active) —— 整个模块里唯一
        # 绕开可见性判定的读,于是全平台的连接名/归属/是否默认全被列出来。
        # 「列表要能看见全部状态」说的是 granted/not_granted/open 三态都要展示,
        # 不是「所有人的连接都要展示」。
        conn_stmt = select(QbiConnection).where(QbiConnection.active.is_(True))
        if not actor.is_group:
            conn_stmt = conn_stmt.where(
                perm.scenic_read_clause(actor.role, actor.scenic_id, QbiConnection.scenic_id))
        rows = (await db.execute(conn_stmt.order_by(QbiConnection.created_at))).scalars().all()
        grants = (await db.execute(select(QbiGrant))).scalars().all()
        mine = {g.connection_id: g for g in grants if g.user_id == target_user_id}
        restricted = {g.connection_id for g in grants}
        # 管理员只管得到自己能管的连接,但三态都要看得见,所以这里不按 _can_write 过滤,
        # 只在写操作(分配/解除)那一步拦。
        items = []
        for c in rows:
            g = mine.get(c.id)
            state = "granted" if g else ("not_granted" if c.id in restricted else "open")
            items.append({
                "connection_id": c.id, "name": c.name, "visibility": c.visibility,
                "scenic_id": c.scenic_id, "is_default": c.is_default,
                "api_enabled": c.api_enabled, "state": state,
                "grant_id": g.id if g else None, "note": g.note if g else None,
                "granted_count": len([x for x in grants if x.connection_id == c.id]),
            })
        return {"user": {"id": target.id, "name": target.name, "account": target.account,
                         "phone": target.phone, "role": target.role, "scenic_id": target.scenic_id},
                "items": items}


async def grant_counts(connection_ids: list[str] | None = None) -> dict[str, int]:
    """各连接的分配人数(分发中心徽标用;0 = 未启用白名单)。"""
    async with get_sessionmaker()() as db:
        stmt = select(QbiGrant.connection_id)
        if connection_ids:
            stmt = stmt.where(QbiGrant.connection_id.in_(connection_ids))
        out: dict[str, int] = {}
        for cid in (await db.execute(stmt)).scalars().all():
            out[cid] = out.get(cid, 0) + 1
        return out


async def save_connection(actor: Actor, data: dict, connection_id: str | None = None) -> ConnView:
    from cogni_agent.common import permissions as perm

    perm.assert_can_write(actor.role, "Quick BI 连接")
    async with get_sessionmaker()() as db:
        if connection_id:
            row = await db.get(QbiConnection, connection_id)
            if row is None:
                raise NotFound("连接不存在")
            if not _can_write(row, actor):
                from cogni_agent.common import permissions as perm

                perm.assert_can_write_obj(actor.role, actor.scenic_id, row.scenic_id, "Quick BI 连接")
        else:
            row = QbiConnection(owner_user_id=actor.user_id, scenic_id=actor.scenic_id,
                                visibility="group" if actor.is_group else "private", status="pending")
            db.add(row)
        if data.get("visibility") is not None:
            if data["visibility"] not in ("private", "scenic", "group", "public"):
                raise ValidationError("visibility 只能是 private / scenic / group / public")
            if not actor.is_group and data["visibility"] in ("group", "public"):
                raise Forbidden("景区角色只能在 private / scenic 间选择可见范围")
        if data.get("default_locale") is not None and data["default_locale"] not in ("zh_CN", "en_US"):
            raise ValidationError("default_locale 只能是 zh_CN / en_US")
        # 绑定景区:集团账号可以把连接挂到指定景区(该景区的人才看得到 / 才能用)。
        # 景区账号不允许改 —— 否则它能把自己的连接挂到别的景区名下。
        # 空串 = 解绑回集团级。
        if data.get("scenic_id") is not None:
            if not actor.is_group:
                raise Forbidden("只有集团角色可以变更连接归属景区")
            row.scenic_id = str(data["scenic_id"]).strip() or None
        for key in ("name", "default_locale", "use_trial", "api_enabled", "active", "visibility"):
            if data.get(key) is not None:
                setattr(row, key, data[key])
        if data.get("server_domain") is not None:
            dom = check_server_domain(str(data["server_domain"]))
            if connection_id and dom != (row.server_domain or "").rstrip("/"):
                # 换了服务地址:已保存的机密不能被带到新地址,必须随本次请求重新填写
                if data.get("api_secret") is None:
                    row.api_secret_cipher = None
                if data.get("user_token") is None:
                    row.user_token_cipher = None
            row.server_domain = dom
        if data.get("api_key") is not None:
            row.api_key = str(data["api_key"]).strip()
        # 机密字段:None = 不修改;"" = 清空;其余加密保存
        for key, col in (("api_secret", "api_secret_cipher"), ("user_token", "user_token_cipher")):
            if data.get(key) is not None:
                val = str(data[key]).strip()
                setattr(row, col, _encrypt(val) if val else None)
        if not row.name:
            row.name = "Quick BI"
        if row.server_domain is None or not str(row.server_domain).strip():
            row.server_domain = DEFAULT_SERVER_DOMAIN
        if data.get("is_default"):
            await _clear_default(db, actor)
            row.is_default = True
        row.status, row.status_message = "pending", None
        await db.commit()
        await db.refresh(row)
        return view_of(row)


async def _clear_default(db, actor: Actor) -> None:
    """默认连接是「按人」的偏好:普通用户只清自己的默认;集团管理员/开发者清全部可写的默认。"""
    stmt = select(QbiConnection).where(QbiConnection.is_default.is_(True))
    if not actor.can_manage_all:
        stmt = stmt.where(QbiConnection.owner_user_id == actor.user_id)
    for r in (await db.execute(stmt)).scalars():
        if _can_write(r, actor):
            r.is_default = False


async def set_default(actor: Actor, connection_id: str) -> None:
    async with get_sessionmaker()() as db:
        row = await db.get(QbiConnection, connection_id)
        if row is None or not _can_write(row, actor):
            raise Forbidden("无权设置该连接为默认")
        await _clear_default(db, actor)
        row.is_default = True
        await db.commit()


async def connection_references(db, connection_id: str) -> dict[str, int]:
    """还有谁指着这条连接 —— 删之前得先知道会弄坏什么。

    连接 id 存在六张表里(问题/资产/文件/会话/运行记录/分配),都是裸的 String(36),
    没有外键级联。删了连接,这些行照样存着那串 id,而且**不报错、不失效**,
    直到有人去点那条问题 —— 才炸一句「Quick BI 连接不存在:<一串 id>」,
    那串 id 已经查不到了(行删了),运营只能来找开发。
    """
    from sqlalchemy import func, select as _select

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    counts: dict[str, int] = {}
    for label, model in (("问题 / 主题解读", ScenicAgentIssueDetail), ("数据资产", QbiAsset),
                         ("上传文件", QbiFile), ("会话", QbiSession), ("运行记录", QbiRun),
                         ("分配记录", QbiGrant)):
        n = (await db.execute(_select(func.count()).select_from(model).where(
            model.connection_id == connection_id))).scalar_one()
        if n:
            counts[label] = int(n)
    return counts


# 「可转移」= 转过去之后还能用的那三类。会话/运行记录是历史留痕(当时用的就是那条连接,
# 改了反而失真),分配记录是白名单(连接都没了,名单本身就该清掉,不是转移)。
_TRANSFERABLE = ("数据资产", "上传文件", "问题 / 主题解读")


def same_workspace(a: ConnView, b: ConnView) -> bool:
    """两条连接是不是同一个 Quick BI 工作空间。

    这是「能不能继承」的唯一判据。资产存的 ref_id 是 Quick BI 那边的 pageId / cubeId,
    只在同一个工作空间里有效;换成另一个空间的凭证,那串 id 什么都指不到。
    工作空间由 server_domain + api_key 确定(api_secret/user_token 只是同一空间里的不同身份),
    所以「同域名 + 同 api_key」= 可继承,否则只能停用。

    判不准时**宁可判成不可继承** —— 停用是看得见、可恢复的;
    转过去打不开则是又一批「点了才报错」的死引用,而那正是这套流程要根治的东西。
    """
    if not a.cfg.api_key or not b.cfg.api_key:
        return False
    return (a.cfg.server_domain or "").rstrip("/") == (b.cfg.server_domain or "").rstrip("/") \
        and a.cfg.api_key == b.cfg.api_key


async def connection_transfer_plan(actor: Actor, connection_id: str) -> dict:
    """删之前先算一笔账:名下有哪些东西要转、可以转给谁。

    给界面用 —— 点「删除」先看到「名下有 24 个资产需转移」,而不是删完才发现一批看板全废了。
    """
    view = await get_connection(actor, connection_id, write=True)   # 能删它的人才看得到这笔账
    async with get_sessionmaker()() as db:
        refs = await connection_references(db, connection_id)
        from sqlalchemy import select as _select

        assets = (await db.execute(_select(QbiAsset).where(
            QbiAsset.connection_id == connection_id).order_by(QbiAsset.name).limit(200))).scalars().all()
    # 候选:当前账号能用的其它连接。按人名展示 —— 连接本来就是一套「某个人的凭证」
    targets = [{"id": v.id, "name": v.name,
                "scenic_id": (v.row.scenic_id if v.row else None),
                "active": (v.row.active if v.row else True),
                # 同工作空间 = 资产能继承;否则转过去也打不开,只能停用 —— 界面要把这点先讲清楚
                "can_inherit": same_workspace(view, v)}
               for v in await list_connections(actor)
               if v.id != connection_id and v.row is not None and v.row.active]
    return {
        "connection": {"id": view.id, "name": view.name,
                       "scenic_id": view.row.scenic_id if view.row else None},
        "references": refs,
        "transferable": {k: v for k, v in refs.items() if k in _TRANSFERABLE},
        "history": {k: v for k, v in refs.items() if k not in _TRANSFERABLE},
        "assets": [{"id": a.id, "name": a.name, "kind": a.kind} for a in assets],
        "targets": targets,
        "needs_transfer": any(refs.get(k) for k in _TRANSFERABLE),
    }


async def transfer_connection(actor: Actor, connection_id: str, to_connection_id: str,
                              *, owner_user_id: str | None = None) -> dict:
    """把这条连接名下的资产/文件/问题整体转到另一条连接。

    这里有两个不同的「转移给谁」,分清很重要:

    - **接手的连接**(to_connection_id,必填):资产绑的是一套凭证(user_token),
      能不能打开取决于那套凭证有没有这个仪表板的权限 —— 这一项决定「转过去之后还能不能用」。
    - **归属人**(owner_user_id,可选):谁负责这批资产,纯记账,不影响能不能打开。

    只改归属人不改连接的话,资产照样打不开,所以连接那一项没有默认值。

    会话/运行记录不动:那是「当时用哪条连接跑的」,改了就不是史实了。
    """
    if connection_id == to_connection_id:
        raise ValidationError("转移目标不能是这条连接本身")
    src = await get_connection(actor, connection_id, write=True)
    dst = await get_connection(actor, to_connection_id, write=True)
    if dst.row is not None and not dst.row.active:
        raise ValidationError(f"目标连接「{dst.name}」已停用,转过去一样用不了")
    # 跨景区转移要拦:两个景区的凭证互相看不到对方的仪表板,转过去就是一批打不开的资产
    s_scenic = src.row.scenic_id if src.row else None
    d_scenic = dst.row.scenic_id if dst.row else None
    if s_scenic and d_scenic and s_scenic != d_scenic:
        raise ValidationError(
            f"「{src.name}」属于景区 {s_scenic},而「{dst.name}」属于 {d_scenic} —— "
            "跨景区转移过去的资产一样打不开(两套凭证看不到对方的仪表板)")

    from sqlalchemy import update as _update

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    inherit = same_workspace(src, dst)
    moved: dict[str, int] = {}
    disabled: dict[str, int] = {}
    async with get_sessionmaker()() as db:
        # ── 问题:纯指针,永远可以转 ──
        res = await db.execute(_update(ScenicAgentIssueDetail)
                               .where(ScenicAgentIssueDetail.connection_id == connection_id)
                               .values(connection_id=to_connection_id))
        if res.rowcount:
            moved["问题 / 主题解读"] = int(res.rowcount)

        # ── 资产:同一个工作空间才继承得了(ref_id 是那边的 pageId/cubeId) ──
        values: dict = {"connection_id": to_connection_id}
        if owner_user_id:
            values["owner_user_id"] = owner_user_id
        if inherit:
            values.update({"active": True, "disabled_reason": None})
            res = await db.execute(_update(QbiAsset)
                                   .where(QbiAsset.connection_id == connection_id).values(**values))
            if res.rowcount:
                moved["数据资产"] = int(res.rowcount)
        else:
            res = await db.execute(_update(QbiAsset)
                                   .where(QbiAsset.connection_id == connection_id)
                                   .values(**values, active=False,
                                           disabled_reason=f"原连接已删除,「{dst.name}」不是同一个 Quick BI 工作空间,"
                                                           f"仪表板/数据集 id 在这套凭证下打不开"))
            if res.rowcount:
                disabled["数据资产"] = int(res.rowcount)

        # ── 上传文件:远端 fileId 直接绑在 user_token 上,换凭证必然失效 —— 一律停用 ──
        fvalues: dict = {"connection_id": to_connection_id, "active": False,
                         "disabled_reason": "原连接已删除;上传文件的远端 fileId 绑定原凭证,"
                                            "换一条连接无法继承,请重新上传"}
        if owner_user_id:
            fvalues["owner_user_id"] = owner_user_id
        res = await db.execute(_update(QbiFile)
                               .where(QbiFile.connection_id == connection_id).values(**fvalues))
        if res.rowcount:
            disabled["上传文件"] = int(res.rowcount)
        await db.commit()

    log.info("Quick BI 连接 %s → %s(同工作空间=%s):转移 %s,停用 %s",
             connection_id, to_connection_id, inherit, moved, disabled)
    parts = []
    if moved:
        parts.append("已转移 " + "、".join(f"{k} {v} 条" for k, v in moved.items()))
    if disabled:
        parts.append("已停用 " + "、".join(f"{k} {v} 条" for k, v in disabled.items()) + "(换凭证继承不了)")
    return {"ok": True, "from": src.name, "to": dst.name, "inherited": inherit,
            "moved": moved, "disabled": disabled,
            "message": ";".join(parts) if parts else "这条连接名下没有需要处理的内容"}


async def delete_connection(actor: Actor, connection_id: str, *, force: bool = False) -> dict:
    """删除连接。**还有东西指着它时默认拒绝** —— 除非显式 force。

    原先是直接 `db.delete(row)`,不看有没有人引用。于是删一条连接就能把一批问题变成
    「点了就报错」的状态,而报错只给一串已经查不到的 id。现场就是这么来的。
    force=True 时仍然删,但把指着它的**问题**上的 connection_id 清空 ——
    清空之后发起会走「按景区重挑一条」(见 usable_connection_id),而不是指着一个死 id。
    资产/文件/会话/运行记录是历史留痕,只读不改,保留原样。
    """
    async with get_sessionmaker()() as db:
        row = await db.get(QbiConnection, connection_id)
        if row is None:
            raise NotFound("连接不存在")
        # 删除比修改更严:景区操作员不能删,景区管理员只能删本景区绑定的连接
        if not _can_delete(row, actor):
            from cogni_agent.common import permissions as perm

            perm.assert_can_delete_obj(actor.role, actor.scenic_id, row.scenic_id, "Quick BI 连接")
        refs = await connection_references(db, connection_id)
        blocking = {k: v for k, v in refs.items() if k in ("问题 / 主题解读", "数据资产")}
        if blocking and not force:
            detail = "、".join(f"{k} {v} 条" for k, v in blocking.items())
            raise ValidationError(
                f"连接「{row.name}」还被 {detail} 引用,删了这些会变成「点了就报错」。"
                "请先把它们改绑到别的连接;确实要删就用强制删除(会把问题上的连接清空,"
                "之后按景区自动重挑一条)")
        cleared = 0
        if force:
            from sqlalchemy import update as _update

            from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

            res = await db.execute(_update(ScenicAgentIssueDetail)
                                   .where(ScenicAgentIssueDetail.connection_id == connection_id)
                                   .values(connection_id=None))
            cleared = int(res.rowcount or 0)
        await db.delete(row)
        await db.commit()
    return {"ok": True, "references": refs, "issues_cleared": cleared}


async def check_connection(actor: Actor, connection_id: str | None) -> dict:
    view = await get_connection(actor, connection_id)
    started = time.perf_counter()
    try:
        async with view.client() as qbi:
            res = await cap.check_connection(qbi)
        ok, msg = True, f"连接正常,可问数数据集 {res['cube_count']} 个"
    except QuickBIError as exc:
        res, ok, msg = {"ok": False, "error": exc.to_dict()}, False, str(exc)
    if view.row is not None:
        async with get_sessionmaker()() as db:
            row = await db.get(QbiConnection, view.id)
            if row is not None:
                row.status, row.status_message, row.checked_at = ("ok" if ok else "failed"), msg[:1000], utcnow()
                await db.commit()
    return {**res, "ok": ok, "message": msg, "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "trial": bool(view.extra.get("trial"))}


async def list_cubes(actor: Actor, connection_id: str | None, keyword: str | None = None) -> list[dict]:
    view = await get_connection(actor, connection_id)
    async with view.client() as qbi:
        cubes = await cap.list_cubes(qbi)
    if keyword:
        cubes = [c for c in cubes if keyword.lower() in (c.get("cube_name") or "").lower()
                 or keyword in (c.get("cube_id") or "")]
    return cubes


# ═══════════════════════════ 资产:仪表板 / 数据集 ═══════════════════════════

def asset_dict(a: QbiAsset, *, full: bool = False) -> dict:
    meta = a.meta or {}
    d = {"id": a.id, "connection_id": a.connection_id, "kind": a.kind, "name": a.name, "ref_id": a.ref_id,
         # 绑定的景区要回显:提问按景区关联看板,列表上看不见绑给谁就没法核对
         "scenic_id": a.scenic_id,
         # 停用的资产照样列出来(不列的话人只会觉得「资产凭空没了」),但要带上为什么
         "active": bool(getattr(a, "active", True)),
         "disabled_reason": getattr(a, "disabled_reason", None),
         # 数据集从哪块板拉出来的:前端按它精确分组,没有(历史数据)才退回按 cube 反查
         "source_asset_id": getattr(a, "source_asset_id", None),
         # 自定义分类(0088):列表按它分组,编辑问题时按分类多选数据集
         "category": getattr(a, "category", None),
         "url": a.url, "created_at": a.created_at.isoformat() if a.created_at else None,
         "updated_at": a.updated_at.isoformat() if a.updated_at else None,
         # 图表数只算**带数据的图表**:结构里还有一堆容器 / 页签 / 文本组件(concat-container 之类),
         # 算进去的话「图表 93」对不上 Quick BI 解读里的「图表 59」,也对不上能解读的数量
         "chart_count": len(cap.data_charts(meta.get("charts"))),
         "component_count": len(meta.get("charts") or []), "cube_count": len(meta.get("cubes") or []),
         "cubes": [{"cube_id": c.get("cube_id"), "cube_name": c.get("cube_name")} for c in meta.get("cubes") or []]}
    if full:
        d["meta"] = meta
    return d


def _asset_scope(actor: Actor):
    """资产可见范围。

    **集团级资产(scenic_id 为空)对所有景区可见** —— 这一条原先漏了,后果是现场那句
    「资产不存在或无权访问:fa9f3b14-…」:集团把仪表板登记成集团级(给所有景区共用),
    景区账号发起提问时按 asset_id 取资产,这里判不过,整条请求就断在取资产上。
    与连接、问题一致的口径:集团级的东西景区**能用不能改**(改由 _can_write 拦)。
    """
    if actor.is_group:
        return true()
    if actor.scenic_id:
        return or_(QbiAsset.owner_user_id == actor.user_id,
                   QbiAsset.scenic_id == actor.scenic_id,
                   QbiAsset.scenic_id.is_(None))
    # 没绑景区的账号:只看得到集团级的和自己登记的
    return or_(QbiAsset.owner_user_id == actor.user_id, QbiAsset.scenic_id.is_(None))


async def list_assets(actor: Actor, connection_id: str | None = None, kind: str | None = None,
                      scenic_id: str | None = None) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(QbiAsset).where(_asset_scope(actor))
        if connection_id:
            stmt = stmt.where(QbiAsset.connection_id == connection_id)
        if kind:
            stmt = stmt.where(QbiAsset.kind == kind)
        if scenic_id:
            # 按景区筛时把集团级资产(scenic_id 为空)一并带上:它们对所有景区都适用,
            # 漏掉的话景区侧会出现「明明配了仪表板却选不到」。
            stmt = stmt.where(or_(QbiAsset.scenic_id == scenic_id, QbiAsset.scenic_id.is_(None)))
        rows = (await db.execute(stmt.order_by(QbiAsset.created_at.desc()))).scalars()
        return [asset_dict(a) for a in rows]


async def get_asset(actor: Actor, asset_id: str) -> QbiAsset:
    async with get_sessionmaker()() as db:
        a = await db.get(QbiAsset, asset_id)
        # 与 _asset_scope 同一套判定(集团级资产所有景区可见)——两处口径必须一致:
        # 列表里选得到、发起时却报「无权访问」,是最难自查的一类故障
        if a is None or not (actor.is_group or actor.trusts(asset_id)
                             or a.owner_user_id == actor.user_id
                             or a.scenic_id is None
                             or (actor.scenic_id and a.scenic_id == actor.scenic_id)):
            raise NotFound(f"资产不存在或无权访问:{asset_id}")
        # 停用的资产:看得到、但用不了,而且要说清**为什么** ——
        # 否则又回到「点了才报错,报错还看不懂」的老路上
        if not getattr(a, "active", True):
            raise ValidationError(
                f"数据资产「{a.name}」已停用:{a.disabled_reason or '原连接已删除,无法继承'}。"
                "请在「工具中心 · Quick BI · 数据资产」重新登记,或改用其它资产")
        return a


async def parse_dashboard(actor: Actor, connection_id: str | None, url: str) -> dict:
    view = await get_connection(actor, connection_id)
    async with view.client() as qbi:
        return await cap.fetch_dashboard(qbi, url)


def _bind_scenic(actor: Actor, scenic_id: str | None) -> str | None:
    """资产/执行要绑定的景区 ID。

    不传 = 跟随调用者所属景区(集团角色则为空,表示集团级资产)。显式传了就按传的走,
    但景区角色只能绑自己那个景区 —— 否则一个景区操作员可以把资产挂到别人的景区下面,
    而提问时正是按景区找资产的,等于跨景区串数据。
    """
    want = (scenic_id or "").strip() or None
    if want is None:
        return actor.scenic_id
    if not actor.is_group and want != actor.scenic_id:
        raise Forbidden(f"无权指定景区 {want}")
    return want


async def register_dashboard(actor: Actor, connection_id: str | None, url: str, name: str | None = None,
                             scenic_id: str | None = None) -> dict:
    view = await get_connection(actor, connection_id)
    async with view.client() as qbi:
        summary = await cap.fetch_dashboard(qbi, url)
    async with get_sessionmaker()() as db:
        existing = (await db.execute(select(QbiAsset).where(
            QbiAsset.connection_id == view.id, QbiAsset.kind == "dashboard",
            QbiAsset.ref_id == summary["page_id"]))).scalars().first()
        a = existing or QbiAsset(connection_id=view.id, kind="dashboard", ref_id=summary["page_id"],
                                 owner_user_id=actor.user_id)
        a.scenic_id = _bind_scenic(actor, scenic_id)
        a.name = (name or summary.get("name") or summary["page_id"])[:128]
        a.url = url[:1024]
        a.meta = summary
        if existing is None:
            db.add(a)
        await db.commit()
        await db.refresh(a)
        return asset_dict(a, full=True)


async def refresh_asset(actor: Actor, asset_id: str) -> dict:
    a = await get_asset(actor, asset_id)
    if a.kind != "dashboard":
        return asset_dict(a, full=True)
    return await register_dashboard(actor, a.connection_id, a.url or a.ref_id, a.name)


async def update_asset(actor: Actor, asset_id: str, *, name: str | None = None,
                       scenic_id: str | None = None, category: str | None = None) -> dict:
    """改已登记资产的显示名与归属景区。

    **为什么要能改**:归属是登记那一刻定的,而现实是先登记、后分景区 ——
    没有这个接口,改归属就只能「删了重登记」,而删掉的那一刻:
      ① 所有指着这个 asset_id 的问题(target.asset_id)全部失效,
         发起时报「资产不存在」,而运营根本不会把这件事和刚才那次删除联系起来;
      ② 主题解读的 target_data_id / target_data_name 也跟着对不上。
    所以这里改的是同一行(asset_id 不变),指着它的问题一条都不用动 ——
    顺带把问题上冗余的那两列(目标数据名)刷新一遍,列表显示才不会停在旧名字上。

    scenic_id 传空串 = 解绑回集团级(所有景区共用)。
    """
    from cogni_agent.common import permissions as perm
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    a = await get_asset(actor, asset_id)
    # 改归属按「改这条资产」判:集团三角色随意,景区角色只能动本景区的
    perm.assert_can_write_obj(actor.role, actor.scenic_id, a.scenic_id, "数据资产")
    want = None if scenic_id is None else ((scenic_id or "").strip() or None)
    if want is not None and want != a.scenic_id:
        # 目标景区也要在权限范围内 —— 否则景区角色能把资产推到别人景区下
        perm.assert_can_write_obj(actor.role, actor.scenic_id, want, "数据资产")
    async with get_sessionmaker()() as db:
        row = await db.get(QbiAsset, asset_id)
        if row is None:
            raise NotFound(f"资产不存在:{asset_id}")
        if name is not None and name.strip():
            row.name = name.strip()[:128]
        if scenic_id is not None:
            row.scenic_id = want
        if category is not None:
            # 空串 = 清掉分类(回到「未分类」)
            row.category = category.strip()[:64] or None
        await db.flush()
        # 指着这条资产的问题:刷新它们冗余存的「目标数据名称」。
        # 这一列只是给列表显示用的,不刷新不影响发起,但会一直显示旧名字 ——
        # 运营改完名字回列表一看没变,只会以为没保存上。
        await db.execute(update(ScenicAgentIssueDetail)
                         .where(ScenicAgentIssueDetail.target_data_id == row.ref_id)
                         .values(target_data_name=row.name))
        await db.commit()
        await db.refresh(row)
        log.info(f"资产 {asset_id} 归属更新为 {row.scenic_id or '集团级'},名称「{row.name}」")
        return asset_dict(row, full=True)


async def register_dataset(actor: Actor, connection_id: str | None, cube_id: str, name: str | None = None,
                           scenic_id: str | None = None, source_asset_id: str | None = None) -> dict:
    r = await register_datasets(actor, connection_id, [{"cube_id": cube_id, "name": name}],
                                scenic_id=scenic_id, source_asset_id=source_asset_id)
    return r["items"][0]


async def board_cubes(actor: Actor, board_asset_id: str) -> dict:
    """一块仪表板里用到的数据集,以及哪些已经登记过了。

    「拉取可问数数据集」原先拉的是**整条连接**下的全部 cube —— 一条连接上挂着两块板、
    几十个 cube,人得自己认哪个属于哪块板,认错了就是拿 A 板的集去问 B 板的问题
    (答得出来,还不报错)。板的结构里本来就写着自己用到哪些 cube,按它给就行。
    """
    a = await get_asset(actor, board_asset_id)
    if a.kind != "dashboard":
        raise ValidationError(f"「{a.name}」不是仪表板,没有「它用到哪些数据集」这一说")
    cubes = [{"cube_id": c.get("cube_id"), "cube_name": c.get("cube_name") or c.get("cube_id")}
             for c in (a.meta or {}).get("cubes") or [] if c.get("cube_id")]
    async with get_sessionmaker()() as db:
        from sqlalchemy import select as _select

        done = {r for (r,) in await db.execute(_select(QbiAsset.ref_id).where(
            QbiAsset.connection_id == a.connection_id, QbiAsset.kind == "dataset"))}
    return {"board": {"id": a.id, "name": a.name, "scenic_id": a.scenic_id},
            "items": [{**c, "registered": c["cube_id"] in done} for c in cubes]}


async def register_datasets(actor: Actor, connection_id: str | None, cubes: list[dict],
                            *, scenic_id: str | None = None,
                            source_asset_id: str | None = None, all_live: bool = False) -> dict:
    """批量登记数据集。

    为什么要批量:一块板动辄十几二十个数据集,一个一个点既慢又容易漏 ——
    漏掉的那个不会报错,只是到用的时候「怎么选不到这张表」。

    all_live:「一键登记全部」—— cubes 不用传,实时拉这条连接(当前 user_token)下全部可问数的
    数据集。走后端一次登记而不是前端逐个发请求:几十上百个逐个发,中途失败会留下一半且说不清
    哪些成功;已登记的照样按 cube_id 跳过,不会堆重复行。

    source_asset_id:这批集是从哪块板拉出来的。记下来之后,分组与「删板连带删集」
    都按它算,不再靠 meta.cubes 反查(共用 cube、板刷新结构都会把反查算错)。
    """
    view = await get_connection(actor, connection_id)
    board = None
    if source_asset_id:
        board = await get_asset(actor, source_asset_id)
        if board.connection_id != view.id:
            raise ValidationError(f"仪表板「{board.name}」不在这条连接下,数据集不能挂到它名下")
        # 归属景区默认跟着板走 —— 板绑了景区、集却留在集团级,是最容易配漏的一处
        if scenic_id is None:
            scenic_id = board.scenic_id

    if all_live and not cubes:
        async with view.client() as qbi:
            cubes = await cap.list_cubes(qbi)
        if not cubes:
            return {"items": [], "created": 0, "skipped": 0, "message": "这条连接下没有可问数的数据集"}
    want = [{"cube_id": str(c.get("cube_id")), "name": c.get("name") or c.get("cube_name")}
            for c in cubes if c.get("cube_id")]
    if not want:
        raise ValidationError("没有要登记的数据集")

    # 名字没给全时补一次(一次网络往返换全部名称,比逐个拉便宜)
    if any(not c["name"] for c in want):
        lookup: dict[str, str] = {c.get("cube_id"): c.get("cube_name")
                                  for c in ((board.meta or {}).get("cubes") or [])} if board else {}
        if any(not (c["name"] or lookup.get(c["cube_id"])) for c in want):
            try:
                async with view.client() as qbi:
                    lookup.update({c["cube_id"]: c["cube_name"] for c in await cap.list_cubes(qbi)})
            except QuickBIError:
                pass
        for c in want:
            c["name"] = c["name"] or lookup.get(c["cube_id"]) or c["cube_id"]

    async with get_sessionmaker()() as db:
        from sqlalchemy import select as _select

        exists = {r for (r,) in await db.execute(_select(QbiAsset.ref_id).where(
            QbiAsset.connection_id == view.id, QbiAsset.kind == "dataset",
            QbiAsset.ref_id.in_([c["cube_id"] for c in want])))}
        made = []
        for c in want:
            # 已登记的跳过而不是报错:多选时勾到一两个已登记的很正常,
            # 为此整批失败、让人回去一个个核对,纯属添乱
            if c["cube_id"] in exists:
                continue
            a = QbiAsset(connection_id=view.id, kind="dataset", ref_id=c["cube_id"],
                         name=c["name"][:128],
                         meta={"cubes": [{"cube_id": c["cube_id"], "cube_name": c["name"]}]},
                         owner_user_id=actor.user_id, scenic_id=_bind_scenic(actor, scenic_id),
                         source_asset_id=source_asset_id)
            db.add(a)
            made.append(a)
        await db.commit()
        for a in made:
            await db.refresh(a)
        items = [asset_dict(a, full=True) for a in made]
    return {"items": items, "created": len(items), "skipped": len(want) - len(items),
            "message": (f"已登记 {len(items)} 个数据集"
                        + (f",{len(want) - len(items)} 个此前已登记" if len(want) > len(items) else ""))}


async def clear_datasets(actor: Actor, connection_id: str | None) -> dict:
    """清空这条连接下已登记的数据集(「清空全部」):只动 kind=dataset,仪表板与上传文件不动。

    逐个走 delete_asset(不连带):还被固定问题当目标数据引用的、没权限删的,按条跳过并
    说明原因 —— 清空的目的是「重新登记一遍」,不是顺手把问题也删了。跳过的条数回给前端,
    否则点了「清空」还剩几条,会被当成 bug。
    """
    view = await get_connection(actor, connection_id)
    async with get_sessionmaker()() as db:
        ids = [a.id for a in (await db.execute(select(QbiAsset).where(
            _asset_scope(actor), QbiAsset.kind == "dataset", QbiAsset.connection_id == view.id))).scalars()]
    deleted = 0
    skipped: list[str] = []
    for aid in ids:
        try:
            await delete_asset(actor, aid)
            deleted += 1
        except (ValidationError, Forbidden, NotFound) as exc:
            skipped.append(str(exc))
    return {"deleted": deleted, "skipped": len(skipped), "reasons": skipped[:5]}


async def asset_references(db, asset_id: str) -> int:
    """还有几条问题拿它当目标数据。

    和连接同一个毛病:资产 id 存在问题行的 target_data_id / target 里,裸字符串,无级联。
    删了资产,问题照样指着它 —— 直到有人点,才报「资产不存在或无权访问:<id>」。
    """
    from sqlalchemy import func, or_ as _or, select as _select

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail as D

    n = (await db.execute(_select(func.count()).select_from(D).where(_or(
        D.target_data_id == asset_id,
        D.target["asset_id"].astext == asset_id,
    )))).scalar_one()
    return int(n)


async def _child_datasets(db, board: QbiAsset) -> list[QbiAsset]:
    """这块仪表板「自己的」数据集。

    板的 meta.cubes 里写着它用到哪些 cube,数据集资产的 ref_id 就是 cube id,按它归位。
    **同时被别的板用到的数据集不算自己的** —— 删这块板就把它一起删掉的话,
    另一块板下面会凭空少一个集,而那块板的人完全不知道发生了什么。
    """
    from sqlalchemy import select as _select

    # ① 登记时明确记了归属的(0083 之后都有)—— 这层是确定的,不做任何推断
    owned = (await db.execute(_select(QbiAsset).where(
        QbiAsset.kind == "dataset", QbiAsset.source_asset_id == board.id))).scalars().all()

    # ② 历史数据没有 source_asset_id,只能按 meta.cubes 反查。反查有两处会错,所以只对
    #    「没记归属」的行用,并且把别的板也用到的 cube 排除掉
    cube_ids = {c.get("cube_id") for c in (board.meta or {}).get("cubes") or [] if c.get("cube_id")}
    guessed: list[QbiAsset] = []
    if cube_ids:
        rows = (await db.execute(_select(QbiAsset).where(
            QbiAsset.connection_id == board.connection_id, QbiAsset.kind == "dataset",
            QbiAsset.source_asset_id.is_(None),
            QbiAsset.ref_id.in_(cube_ids)))).scalars().all()
        if rows:
            others = (await db.execute(_select(QbiAsset).where(
                QbiAsset.connection_id == board.connection_id, QbiAsset.kind == "dashboard",
                QbiAsset.id != board.id))).scalars().all()
            shared: set[str] = set()
            for o in others:
                shared |= {c.get("cube_id") for c in (o.meta or {}).get("cubes") or [] if c.get("cube_id")}
            guessed = [r for r in rows if r.ref_id not in shared]
    seen = {r.id for r in owned}
    return list(owned) + [r for r in guessed if r.id not in seen]


async def _questions_for_assets(db, asset_ids: list[str], cube_ids: list[str]):
    """拿这些资产当目标数据的问题(资产 id 与 cube id 两种指法都认)。"""
    from sqlalchemy import or_ as _or, select as _select

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail as D

    conds = []
    if asset_ids:
        conds += [D.target_data_id.in_(asset_ids), D.target["asset_id"].astext.in_(asset_ids)]
    if cube_ids:
        conds.append(D.target["cube_id"].astext.in_(cube_ids))
    if not conds:
        return []
    return (await db.execute(_select(D).where(_or(*conds)))).scalars().all()


async def asset_delete_plan(actor: Actor, asset_id: str) -> dict:
    """删这个资产会连带删掉什么 —— 删之前先给人看一眼。

    数据集与问题都是**挂在仪表板下面**的:数据集是板里用到的那些表,问题是打在这块板/这些表上的。
    单独删板、把集和问题留着,就是又一批「点了才报错」的死引用;而一句「确定删除吗」
    不告诉人会连带删掉 17 个集、12 条问题,他按下确定时并不知道自己同意了什么。
    """
    a = await get_asset(actor, asset_id)
    async with get_sessionmaker()() as db:
        board = await db.get(QbiAsset, asset_id)
        kids = await _child_datasets(db, board) if board.kind == "dashboard" else []
        ids = [asset_id] + [k.id for k in kids]
        cubes = [k.ref_id for k in kids] + ([board.ref_id] if board.kind == "dataset" else [])
        qs = await _questions_for_assets(db, ids, cubes)
    return {
        "asset": {"id": a.id, "name": a.name, "kind": a.kind, "scenic_id": a.scenic_id},
        "datasets": [{"id": k.id, "name": k.name} for k in kids],
        "questions": [{"id": q.issue_id, "title": q.title, "scenic_id": q.scenic_id} for q in qs],
        "counts": {"数据集": len(kids), "问题": len(qs)},
        "needs_confirm": bool(kids or qs),
    }


async def delete_asset(actor: Actor, asset_id: str, *, cascade: bool = False,
                       force: bool = False) -> dict:
    """删除已登记的数据资产。

    **数据集与问题是挂在仪表板下面的**,所以删板默认是**连带删**:板下面的数据集
    (只属于这块板的那些)、以及打在板/这些集上的问题,一起删掉。不连带的话,
    留下的就是又一批「点了才报错」的死引用 —— 前面几轮现场故障全是这么来的。

    但连带删必须**先让人看见**:`cascade=False` 时拒绝并说清会连带删掉几个集、几条问题
    (界面先调 asset_delete_plan 展示清单)。删完 APP 与管理端都不再显示 ——
    问题就在同一张表里,删了就没了。

    force 是 cascade 的旧名,保留兼容。
    """
    from cogni_agent.common import permissions as perm

    cascade = cascade or force
    a = await get_asset(actor, asset_id)
    # 景区管理员可以删本景区登记的资产(不限于自己登记的);操作员不能删;
    # 集团级资产(scenic_id 为空)只有集团角色能删
    perm.assert_can_delete_obj(actor.role, actor.scenic_id, a.scenic_id, "数据资产")

    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail as D

    async with get_sessionmaker()() as db:
        board = await db.get(QbiAsset, asset_id)
        if board is None:
            return {"ok": True, "deleted": {}}
        kids = await _child_datasets(db, board) if board.kind == "dashboard" else []
        ids = [asset_id] + [k.id for k in kids]
        cubes = [k.ref_id for k in kids] + ([board.ref_id] if board.kind == "dataset" else [])
        qs = await _questions_for_assets(db, ids, cubes)

        if (kids or qs) and not cascade:
            parts = []
            if kids:
                parts.append(f"{len(kids)} 个数据集")
            if qs:
                parts.append(f"{len(qs)} 条问题")
            raise ValidationError(
                f"删除「{a.name}」会同时删掉它下面的 {'、'.join(parts)}(APP 与管理端都不再显示)。"
                "确认后再执行删除")

        from sqlalchemy import delete as _delete

        deleted: dict[str, int] = {}
        if qs:
            await db.execute(_delete(D).where(D.id.in_([q.id for q in qs])))
            deleted["问题"] = len(qs)
        if kids:
            await db.execute(_delete(QbiAsset).where(QbiAsset.id.in_([k.id for k in kids])))
            deleted["数据集"] = len(kids)
        await db.delete(board)
        deleted["仪表板" if board.kind == "dashboard" else "数据集"] = \
            deleted.get("仪表板" if board.kind == "dashboard" else "数据集", 0) + 1
        await db.commit()

    log.info("删除资产 %s(%s),连带:%s", asset_id, a.name, deleted)
    return {"ok": True, "deleted": deleted,
            "message": "已删除 " + "、".join(f"{k} {v} 个" for k, v in deleted.items())}


# ═══════════════════════════ 上传文件 ═══════════════════════════

def file_dict(f: QbiFile, *, with_markdown: bool = True) -> dict:
    md_ready = bool(f.markdown) if with_markdown else fs.suffix(f.file_name) in fs.INSIGHT_SUFFIXES
    return {"id": f.id, "connection_id": f.connection_id, "file_name": f.file_name, "size": f.size,
            "sha256": f.sha256, "capabilities": fs.capabilities_for(f.file_name),
            "qa_ready": bool(f.qa_file_id), "report_ready": bool(f.report_file), "insight_ready": md_ready,
            "markdown_chars": len(f.markdown or "") if with_markdown else None, "detail": f.detail or {},
            "created_at": f.created_at.isoformat() if f.created_at else None}


async def save_file(actor: Actor, connection_id: str | None, name: str, content: bytes,
                    *, upload_now: bool = True) -> dict:
    try:
        fs.validate_upload(name, len(content))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    view = await get_connection(actor, connection_id)
    row = QbiFile(connection_id=view.id, file_name=name[:255], size=len(content),
                  sha256=hashlib.sha256(content).hexdigest(), content=content,
                  owner_user_id=actor.user_id, scenic_id=actor.scenic_id, detail={})
    warnings: list[str] = []
    if fs.suffix(name) in fs.INSIGHT_SUFFIXES:
        try:
            md = fs.to_markdown(name, content)
            row.markdown = md
            if len(md) > fs.MAX_INSIGHT_CHARS:
                warnings.append(f"数据转 Markdown 后 {len(md)} 字符,超过解读上限 {fs.MAX_INSIGHT_CHARS}")
        except ValueError as exc:
            warnings.append(f"本地解析失败(不影响问数/报告):{exc}")
    if upload_now and fs.suffix(name) in fs.QA_SUFFIXES:
        try:
            async with view.client() as qbi:
                up = await cap.upload_chat_file(qbi, name, content)
            row.qa_file_id, row.detail = up["file_id"], up["detail"]
        except QuickBIError as exc:
            warnings.append(f"上传到 Quick BI 问数失败,将在首次提问时重试:{exc}")
    async with get_sessionmaker()() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return {**file_dict(row), "warnings": warnings}


async def _load_file(actor: Actor, file_id: str) -> QbiFile:
    async with get_sessionmaker()() as db:
        f = await db.get(QbiFile, file_id)
        # 同资产:集团级(未绑景区)的上传文件是给所有景区当引用素材用的
        if f is None or not (actor.trusts(file_id) or actor.is_group
                             or f.owner_user_id == actor.user_id or f.scenic_id is None
                             or (actor.scenic_id and f.scenic_id == actor.scenic_id)):
            raise NotFound(f"文件不存在或无权访问:{file_id}")
        return f


async def list_files(actor: Actor, connection_id: str | None = None, limit: int = 100) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(QbiFile).options(defer(QbiFile.content), defer(QbiFile.markdown))
        if not actor.is_group:
            stmt = stmt.where(or_(QbiFile.owner_user_id == actor.user_id,
                                  QbiFile.scenic_id.is_(None),
                                  (QbiFile.scenic_id == actor.scenic_id) if actor.scenic_id else false()))
        if connection_id:
            stmt = stmt.where(QbiFile.connection_id == connection_id)
        rows = (await db.execute(stmt.order_by(QbiFile.created_at.desc()).limit(limit))).scalars()
        return [file_dict(f, with_markdown=False) for f in rows]


async def delete_file(actor: Actor, file_id: str) -> None:
    from cogni_agent.common import permissions as perm

    f = await _load_file(actor, file_id)
    # 自己传的文件始终能删(清理自己的上传不该受角色限制);其余按角色 + 景区判
    if f.owner_user_id != actor.user_id:
        perm.assert_can_delete_obj(actor.role, actor.scenic_id, f.scenic_id, "上传文件")
    async with get_sessionmaker()() as db:
        row = await db.get(QbiFile, file_id)
        if row is not None:
            await db.delete(row)
            await db.commit()


async def _update_file(file_id: str, **values) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(update(QbiFile).where(QbiFile.id == file_id).values(**values))
        await db.commit()


# ═══════════════════════════ 统一执行 ═══════════════════════════

def _compact(result: dict) -> dict:
    out = dict(result)
    charts = []
    for c in result.get("charts") or []:
        c = dict(c)
        if len(c.get("rows") or []) > MAX_ROWS_PER_CHART:
            c["rows"] = c["rows"][:MAX_ROWS_PER_CHART]
            c["truncated"] = True
        c.pop("html", None)
        charts.append(c)
    out["charts"] = charts
    for k in ("reasoning", "code"):
        if isinstance(out.get(k), str) and len(out[k]) > 50_000:
            out[k] = out[k][:50_000] + "…"
    return out


_FOLLOWUP = re.compile(r"^(那|那么|再|换成|改成|改为|按|只看|分别|其中|同比|环比|对比)|[呢吗]\s*[??]?$")


def _looks_like_followup(q: str) -> bool:
    q = (q or "").strip()
    return len(q) <= 24 and bool(_FOLLOWUP.search(q))


async def _same_connection(view: ConnView, owner_conn: str, what: str) -> None:
    """远端 fileId / 仪表板权限都绑定在某个连接(user_token)上,跨连接使用必然失败,提前说清楚。

    分两种情况说,因为**下一步完全不同**:
    - 那条连接还在 → 切过去就行;
    - 那条连接已经被删了 → 切不过去。原先照样提示「请切换到该连接」,
      而那个 id 已经查不到任何东西 —— 人对着一串不存在的 id 无从下手。
      现场 24 条资产全指着一条删掉的连接,就是这个样子。
    """
    if not owner_conn or owner_conn == view.id:
        return
    async with get_sessionmaker()() as db:
        exists = await db.get(QbiConnection, owner_conn)
    if exists is None:
        raise QuickBIError(
            f"{what}登记在一条**已被删除**的 Quick BI 连接下({owner_conn}),所以用不了。"
            f"请把它改绑到当前连接「{view.name}」—— 管理端「工具中心 · Quick BI · 数据资产」可以改归属,"
            "存量可用 scripts/check_quickbi_refs.py --fix --to <连接id> 批量改",
            kind="input")
    raise QuickBIError(f"{what}属于另一个 Quick BI 连接({exists.name}),请切换到该连接,或在当前连接下重新上传/登记",
                       kind="input")


async def _dashboard_insight(qbi: QuickBIClient, *, question: str, t: dict, charts: list | None,
                             locale: str) -> AsyncIterator[dict]:
    """仪表板解读:解析 pageId、拿到图表结构(登记过的资产直接用缓存),再按图表范围解读。"""
    page_id = t.get("page_id")
    if charts is None:
        # 没登记的看板(直接贴地址 / pageId):现拉一次结构,才知道有哪些图表、各在哪个数据集
        yield cap.status("dashboard", "解析仪表板结构…")
        summary = await cap.fetch_dashboard(qbi, page_id or t.get("dashboard"))
        page_id = page_id or summary.get("page_id")
        charts = summary.get("charts") or []
    elif not page_id:
        page_id = (await cap.resolve_page(qbi, t.get("dashboard")))["page_id"]
    async for e in cap.run_dashboard_insight(
            qbi, question=question, page_id=page_id, charts=charts, chart_ids=t.get("chart_ids"),
            locale=locale, poll_interval=float(_setting("snapshot_poll_seconds", 3))):
        yield e


async def _prepare(view: ConnView, qbi: QuickBIClient, actor: Actor, capability: str, target: dict,
                   question: str, locale: str, events: list) -> tuple[AsyncIterator[dict], dict]:
    """目标数据 → 能力生成器。返回 (generator, 规范化后的 target)。"""
    t = {k: v for k, v in (target or {}).items() if v not in (None, "", [])}
    asset = None
    if t.get("asset_id"):
        asset = await get_asset(actor, t["asset_id"])
        await _same_connection(view, asset.connection_id, f"资产「{asset.name}」")
        if asset.kind == "dashboard":
            t.setdefault("page_id", asset.ref_id)
            t.setdefault("dashboard_cubes", [c.get("cube_id") for c in (asset.meta or {}).get("cubes") or []])
            t.setdefault("asset_name", asset.name)
        elif asset.kind == "dataset":
            # target 里已经选了若干份(cube_ids)时不再把这份并进来:并进来会把多选
            # 压成单表(cube_id 优先于 cube_ids),运营勾的几份就只剩资产这一份
            if not t.get("cube_ids"):
                t.setdefault("cube_id", asset.ref_id)
    dashboard_ref = t.get("page_id") or t.get("dashboard")
    dashboard_charts = list((asset.meta or {}).get("charts") or []) if asset is not None and asset.kind == "dashboard" else None

    if capability == "dataset_qa":
        multi = [c for c in (t.get("cube_ids") or []) if c]
        if not t.get("cube_id") and len(multi) > 1 and t.get("cube_mode") != "pick_one":
            # 多选数据集问数(官方 multipleCubeIds):一次问,小Q在所选数据集里选一张或多张表回答。
            # 以前多选 = 候选,先智能选表**只挑一份**再问,跨数据集的问题答不全
            return cap.run_multi_cube_qa(qbi, question=question, cube_ids=multi, locale=locale), t
        if not t.get("cube_id") and dashboard_ref and not t.get("cube_ids"):
            # 选的是仪表板:按官方「仪表板问数」来(图表名命中用该图数据集,否则全部数据集多表问)
            return cap.run_dashboard_qa(qbi, question=question, dashboard=dashboard_ref,
                                        cube_ids=t.get("dashboard_cubes"), charts=dashboard_charts,
                                        chart_ids=t.get("chart_ids"), locale=locale), t
        cube_ids = t.get("cube_ids") or t.get("dashboard_cubes")
        return cap.run_dataset_qa(qbi, question=question, cube_id=t.get("cube_id"), cube_name=t.get("cube_name"),
                                  cube_ids=cube_ids, restrict=bool(t.get("restrict")) or bool(t.get("dashboard_cubes")),
                                  locale=locale), t

    if capability == "dashboard_qa":
        if not dashboard_ref and not t.get("cube_ids"):
            raise QuickBIError("仪表板问数需要指定仪表板(asset_id / page_id / dashboard 地址)", kind="input")
        # 对齐官方「小Q报表技能」:问题点到某张图 → 用该图的数据集;否则看板(所选组件)
        # 全部数据集走 multipleCubeIds 多表问数,由小Q选一张或多张表 —— 不再先挑一份。
        # 只有显式选了「挑一份数据集」(qa_scope=dataset)的存量配置才保留老跑法。
        return cap.run_dashboard_qa(qbi, question=question, dashboard=dashboard_ref,
                                    cube_ids=t.get("dashboard_cubes") or t.get("cube_ids"),
                                    cube_id=t.get("cube_id"), charts=dashboard_charts,
                                    chart_ids=t.get("chart_ids"),
                                    single_cube=t.get("qa_scope") == "dataset", locale=locale), t

    if capability == "file_qa":
        if not t.get("file_id"):
            raise QuickBIError("文件问数需要先上传文件并传入 file_id", kind="input")
        f = await _load_file(actor, t["file_id"])
        await _same_connection(view, f.connection_id, f"文件「{f.file_name}」")
        if not f.qa_file_id:
            events.append(cap.status("upload", f"正在上传「{f.file_name}」到 Quick BI…"))
            up = await cap.upload_chat_file(qbi, f.file_name, f.content or b"")
            await _update_file(f.id, qa_file_id=up["file_id"], detail=up["detail"])
            f.qa_file_id = up["file_id"]
        t["file_name"] = f.file_name
        return cap.run_file_qa(qbi, question=question, file_id=f.qa_file_id, locale=locale), t

    if capability == "insight":
        markdown = t.get("data")
        if t.get("file_id"):
            f = await _load_file(actor, t["file_id"])
            if not f.markdown:
                try:
                    f.markdown = fs.to_markdown(f.file_name, f.content or b"")
                except ValueError as exc:
                    raise QuickBIError(str(exc), kind="input") from exc
                await _update_file(f.id, markdown=f.markdown)
            markdown = f.markdown
            t["file_name"] = f.file_name
        # 显式给了数据集就走「先取数再解读」—— 哪怕 target 里还带着 asset_id/page_id
        # (常常是上一轮留下的:切成多选数据集时前端保留了其余键)。数据集是运营刚勾的,
        # 仪表板多半是残留,反过来压掉会让多选形同虚设。
        if not markdown and (t.get("cube_id") or t.get("cube_ids")):
            # 数据集(直接指定,或问题上绑的数据集资产):先按问题取数,再解读
            return cap.run_dataset_insight(qbi, question=question, cube_id=t.get("cube_id"),
                                           cube_ids=t.get("cube_ids"), restrict=bool(t.get("restrict")),
                                           locale=locale), t
        if not markdown and not dashboard_ref:
            raise QuickBIError("解读需要数据来源:file_id(上传文件)、asset_id/page_id/dashboard(仪表板)、"
                               "数据集(asset_id / cube_id)或 data(数据文本)", kind="input")
        if not markdown:
            # 仪表板解读按「图表范围」来:chart_ids 为空 = 全选(对齐 Quick BI 智能解读的「请确认图表范围」)。
            # 快照只覆盖当前页签,其它页签的图表按各自的数据集补取 —— 见 cap.run_dashboard_insight
            return _dashboard_insight(qbi, question=question, t=t, charts=dashboard_charts,
                                      locale=locale), t
        return cap.run_insight(qbi, question=question, markdown=markdown, dashboard=t.get("dashboard"),
                               page_id=t.get("page_id"), locale=locale,
                               poll_interval=float(_setting("snapshot_poll_seconds", 3))), t

    if capability == "report":
        async def _report_gen():
            uploads: list[dict] = []
            for fid in list(dict.fromkeys((t.get("file_ids") or []) + ([t["file_id"]] if t.get("file_id") else []))):
                f = await _load_file(actor, fid)
                await _same_connection(view, f.connection_id, f"文件「{f.file_name}」")
                if fs.suffix(f.file_name) not in fs.REPORT_SUFFIXES:
                    yield cap.status("upload", f"「{f.file_name}」不是报告支持的类型,已跳过")
                    continue
                if not f.report_file:
                    yield cap.status("upload", f"正在上传引用文件「{f.file_name}」…")
                    rf = await cap.upload_report_file(qbi, f.file_name, f.content or b"")
                    await _update_file(f.id, report_file=rf)
                    f.report_file = rf
                uploads.append(f.report_file)
            if dashboard_ref and t.get("use_dashboard_data", True):
                page_id = t.get("page_id") or (await cap.resolve_page(qbi, dashboard_ref))["page_id"]
                yield cap.status("snapshot", "正在拉取仪表板数据作为报告素材…")
                md = ""
                async for e in cap.dashboard_snapshot(qbi, page_id,
                                                      poll_interval=float(_setting("snapshot_poll_seconds", 3))):
                    if e["type"] == "snapshot":
                        md = e["markdown"]
                    else:
                        yield e
                base = re.sub(r"[\\/:*?\"<>|\s]+", "_", t.get("asset_name") or "仪表板")[:20]
                _domain = fs.theme_domain_of(question)
                if _domain and fs.THEME_TABLE_KEYWORDS.get(_domain):
                    # 主题解读(客群 / 票务 / 舆情 / OTA直销):只带本主题的图表。
                    # 快照只有当前页签,本主题的图在别的页签时按图表逐张补取;
                    # 两边都没有就报错,**不拿别的主题的表凑数**(那样写出来的报告标题是客群、内容是舆情)
                    tables: list = []
                    async for e in cap.theme_report_tables(qbi, domain=_domain, snapshot_md=md,
                                                           charts=dashboard_charts, locale=locale):
                        if e["type"] == "tables":
                            tables = e["tables"]
                        else:
                            yield e
                    if not tables:
                        raise QuickBIError(
                            f"绑定的仪表板「{t.get('asset_name') or dashboard_ref}」上没有找到「{_domain}」相关的图表数据"
                            f"(快照只含当前页签,其它页签里也没有名称对得上的图表,或取回来全是空值)。"
                            f"请在「Quick BI · 固定问题」里给这条主题解读换一块{_domain}看板,或先刷新资产结构",
                            kind="business", code="THEME_DATA_MISSING")
                    ups = await cap.report_uploads_from_tables(qbi, tables, base_name=base)
                    yield cap.status("upload", f"已把「{_domain}」相关的 {len(ups)} 张图表数据整理为引用文件:"
                                               + "、".join(str(x[0]) for x in tables))
                else:
                    ups = await cap.report_uploads_from_markdown(qbi, md, base_name=base)
                    yield cap.status("upload", f"已把仪表板数据整理为 {len(ups)} 个引用文件")
                uploads.extend(ups)
            yield {"type": "meta", "report_resources": [u.get("fileName") for u in uploads]}
            async for e in cap.run_report(
                qbi, question=question, uploads=uploads, locale=locale,
                web_search=bool(t.get("web_search", True)),
                poll_interval=float(_setting("report_poll_seconds", 10)),
                max_wait=float(t.get("max_wait_seconds") or _setting("report_max_wait_seconds", 1800)),
            ):
                yield e
                if e.get("type") == "report" and not e.get("finished") and not t.get("wait", True):
                    return  # 异步模式:拿到 chat_id 即返回,后续通过 refresh / stream 追踪
        return _report_gen(), t

    raise QuickBIError(f"未知能力:{capability},可选 {', '.join(sorted(CAPABILITY_KEYS))}", kind="input")


# ═══════════════════════ 报告的串行闸 ═══════════════════════
#
# 小Q 对同一个账号**只允许一个运行中的报告任务**。并发发起时它不会拒绝,而是
# 把后来者「接续」到前一个正在跑的任务上(create_report 的 reused 分支)——
# 于是 A 的请求去轮询 B 的报告,B 一结束 A 就收到一个对不上的状态,
# 报一句「报告异步生成状态已更新,请刷新页面重试」。
#
# 现场表现极具迷惑性:三条报告在 3 秒内先后发起,全部失败,耗时只有几秒;
# 而单独跑同一条要 8 分钟且成功。看运行记录会以为是那三条问题有问题。
#
# 所以按连接串行:一条连接同时只跑一个报告。等不了的调用方(APP 卡片那种
# 要立刻返回的)传 wait=False,直接告诉它「前面还有一个在跑」,而不是排进去。
_REPORT_LOCKS: dict[str, asyncio.Lock] = {}
# 队列深度上限:排太多没有意义(报告一条几分钟,排第五个的时候数据都变了)
_REPORT_WAITERS: dict[str, int] = {}
_REPORT_MAX_WAITERS = 3


def _report_lock(connection_id: str) -> asyncio.Lock:
    lock = _REPORT_LOCKS.get(connection_id)
    if lock is None:
        lock = _REPORT_LOCKS[connection_id] = asyncio.Lock()
    return lock


def report_busy(connection_id: str) -> bool:
    """这条连接上是否已经有报告在跑(给「要不要现在发起」的判断用)。"""
    lock = _REPORT_LOCKS.get(connection_id)
    return bool(lock and lock.locked())


@contextlib.asynccontextmanager
async def report_slot(connection_id: str, *, wait: bool = True):
    """占一个报告执行位。拿不到就抛 —— 由调用方决定是报错还是改口说「排队中」。"""
    lock = _report_lock(connection_id)
    if not wait and lock.locked():
        raise QuickBIError("该 Quick BI 连接上已有一个报告正在生成;"
                           "小Q 同一账号同时只能跑一个报告,请等前一个完成后再试",
                           kind="business", code="REPORT_BUSY")
    if lock.locked():
        waiting = _REPORT_WAITERS.get(connection_id, 0)
        if waiting >= _REPORT_MAX_WAITERS:
            raise QuickBIError(
                f"该连接上已有 {waiting} 个报告在排队,先跑完再发起;"
                "报告一条要几分钟,排太长时数据也已经变了",
                kind="business", code="REPORT_QUEUE_FULL")
        _REPORT_WAITERS[connection_id] = waiting + 1
        try:
            await lock.acquire()
        finally:
            _REPORT_WAITERS[connection_id] = max(0, _REPORT_WAITERS.get(connection_id, 1) - 1)
    else:
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


async def connection_for_target(actor: Actor, target: dict | None,
                                requested: str | None) -> str | None:
    """**目标数据决定用哪条连接**,而不是反过来。

    资产/文件是登记在某一条连接(= 一套 Quick BI 凭证)下的,ref_id 只有那套凭证认得。
    所以「选了哪块板」一旦确定,连接就没有第二种选法 —— 再让人先选连接、
    选错了还报一句「资产属于另一个 Quick BI 连接」,是把一件本来能自动做的事丢给了用户。
    现场表现更隐蔽:操练台按「当前连接」筛资产,于是**绑在别的连接下的那块板压根不出现在下拉里**,
    看起来像是「这块板没登记」,而它在数据资产页里好好列着。

    返回该用的连接 id;目标数据没指定资产/文件时,沿用调用方传的那个。
    """
    t = target or {}
    owner: str | None = None
    if t.get("asset_id"):
        owner = (await get_asset(actor, str(t["asset_id"]))).connection_id
    elif t.get("file_id") or t.get("file_ids"):
        fid = t.get("file_id") or (list(t.get("file_ids") or []) or [None])[0]
        if fid:
            owner = (await _load_file(actor, str(fid))).connection_id
    if not owner or owner == requested:
        return requested
    # 切过去之前先确认这个人本来就能用那条连接 —— 否则就成了「借目标数据越权换凭证」
    view = await get_connection(actor, owner)
    log.info("目标数据属于连接 %s(%s),本轮由 %s 切过去", owner, view.name, requested or "(默认)")
    return view.id


# ── 人工终止 ──────────────────────────────────────────────────────────────
# 运行记录停在 running 有两种成因:① 执行体已经不在了(发版/重启把它掐断,没机会走
# finally,成了僵尸);② 真的还在跑(上游 SSE 还在吐)。「终止」必须同时管住这两种:
# 改库管住 ①,置进程内标记让流自己退出管住 ② —— 只改库的话,那条还在跑的流 finalize
# 时会把状态写回 success/failed,把刚标好的「已终止」覆盖掉(「点了终止它又活了」)。
# 与 runtime_service.streaming 的取消标记同型:单进程内生效,别的副本上的流由它自己收尾。
_aborted_run_ids: set[str] = set()

# 终止后统一落成 failed + 这个 error_code:不新增状态,列表与统计口径都不用改,
# 详情里靠 error_code 与文案认出「这条是被人为终止的,不是跑挂了」。
ABORT_ERROR = "已由用户终止"
ABORT_CODE = "aborted"


def mark_run_aborted(run_id: str) -> None:
    _aborted_run_ids.add(run_id)


# 「目标数据给没给」的判定清单:任何一个有值就算给了。多选的 cube_ids、粘贴的 data 都要算 ——
# 漏一个就会把运营选好的数据当成没选
_TARGET_KEYS = ("asset_id", "page_id", "dashboard", "cube_id", "cube_ids", "cube_name",
                "file_id", "file_ids", "data")


def capability_source(capability: str | None, target: dict | None) -> str:
    """这一问/这一行**实际**的数据来源。

    不能直接取 `CAPABILITIES[capability]["source"]`:那是「这种能力通常吃什么」的静态描述
    (insight 写的是「仪表板 / 上传文件」),而解读同样吃数据集 —— 照静态值显示,数据集解读
    就成了「解读 · 仪表板 / 上传文件」,与事实正好相反。所以按 target 里真正填了什么来定;
    target 没有线索时才回落到静态 source。
    """
    t = target or {}
    cids = [c for c in (t.get("cube_ids") or []) if c] or ([t["cube_id"]] if t.get("cube_id") else [])
    if cids:
        return f"数据集 ×{len(cids)}" if len(cids) > 1 else "数据集"
    if t.get("page_id") or t.get("dashboard") or (t.get("asset_id") and t.get("asset_kind") == "dashboard"):
        return "仪表板"
    if t.get("file_id") or t.get("file_ids"):
        return "上传文件"
    if t.get("data"):
        return "数据文本"
    return cap.CAPABILITIES.get(capability or "", {}).get("source", "")


# ── 问句 / 口径里的「跑批时刻」变量 ────────────────────────────────────────
# 时间口径写死在问句里必然错:实时主题(票务/客流)要「今日截至此刻」,T+1 主题
# (舆情/OTA)今天能看到的最新完整数据只有昨天;而截止时间必须精确到**本次跑的那一刻**,
# 库里存不下、也没法提前写。所以这几个值由发起方注入:运营写占位符,这里替换。
# 管理台、APP、预热、开放平台都经过 run_stream —— 只在这一处替换,口径不会分叉。
RUNTIME_VAR_KEYS = ("截止时间", "统计日期", "昨日")


def render_runtime_vars(text: str | None, now=None) -> str | None:
    """把 {{截止时间}} / {{统计日期}} / {{昨日}} 换成本次运行时刻的真实值(东八区)。

    - 截止时间:YYYY-MM-DD HH:MM:SS = 本次取数的数据截止点
    - 统计日期:今天 YYYY-MM-DD(实时主题用)
    - 昨日    :昨天 YYYY-MM-DD(T+1 主题用)
    """
    if not text or "{{" not in text:
        return text
    from datetime import timedelta

    from cogni_agent.common.time import now_cst

    n = now or now_cst()
    today = n.date()
    for key, val in (("截止时间", n.strftime("%Y-%m-%d %H:%M:%S")),
                     ("统计日期", today.isoformat()),
                     ("昨日", (today - timedelta(days=1)).isoformat())):
        text = text.replace("{{" + key + "}}", val)
    return text


async def run_stream(
    actor: Actor, *, connection_id: str | None, capability: str | None, question: str,
    target: dict | None = None, mode: str = "direct", locale: str | None = None,
    session_id: str | None = None, fixed_question_id: str | None = None,
    context_question: str | None = None, report_async: bool | None = None,
    scenic_id: str | None = None, use_cache: bool = True, task_id: str | None = None,
) -> AsyncIterator[dict]:
    """统一执行入口:产出统一事件,首个事件为 run(含 run_id),最后一个为 done。

    task_id:触发这次运行的任务(APP 对话显式传;执行引擎里调用时不传,从 task_context 取)。

    report_async:路由结果为小Q报告时是否「创建即返回」(None = 沿用 target.wait,默认等待)。
    自动路由可能临时落到报告,非流式调用方应传 True,避免阻塞到报告生成完(可达 30 分钟)。

    fixed_question_id:只给 id 就能发起 —— 连接/能力/目标数据/问题文本都从固定问题带出来,
    调用方显式传的字段优先(例如同一条固定问题换个景区问)。

    scenic_id:这次提问归属哪个景区。它既落进运行记录(计量/审计按景区隔离),也决定
    没指定目标数据时去哪个景区的资产里找 —— 同一个问题在不同景区问的是不同的仪表板。

    use_cache:允不允许命中加速(预热)结果。默认允许;预热任务本身和列表上的「加速」
    按钮传 False —— 否则预热会读到自己上一次的结果,永远不去真跑,缓存就冻在那儿了。
    """
    target = dict(target or {})
    question = (question or "").strip()
    instruction: str | None = None
    fq = None
    if fixed_question_id:
        fq = await get_fixed(actor, fixed_question_id)
        question = question or (fq.issue or "")   # 统一表里问题原文叫 issue
        capability = capability or fq.capability
        # 固定问题上写死的连接可能已被删除/停用/改绑别的景区。调用方显式传的不动,
        # 从问题行上带出来的这条要过一遍「还能用吗」—— 否则就是上面那句
        # 「Quick BI 连接不存在:<一串 id>」,而运营查不到那串 id(行已经删了)。
        connection_id = connection_id or await usable_connection_id(
            actor, fq.connection_id, scenic_id or fq.scenic_id or actor.scenic_id)
        locale = locale or fq.locale
        # 口径说明:运营填的「真正给模型的约束」(取哪个窗口、什么算异常、答多长)。
        # 不带上它的话,像「【主题解读】OTA直销」这种按钮文案原样发给小Q,
        # 它只能照着五个字瞎猜 —— 答得很像回事,但不是要的那份报告。
        instruction = (getattr(fq, "model_instruction", None) or "").strip() or None
        if not target:
            target = dict(fq.target or {})
        # 公开的固定问题可能挂在别的景区名下。这种情况下它只是「问题模板」,
        # 景区应当跟着提问的人走,而不是让调用直接 403。
        if scenic_id is None and fq.scenic_id and (actor.is_group or fq.scenic_id == actor.scenic_id):
            scenic_id = fq.scenic_id
    if not question:
        raise ValidationError("问题不能为空")
    question = render_runtime_vars(question)
    instruction = render_runtime_vars(instruction)
    scenic_id = _bind_scenic(actor, scenic_id)
    # 加速(预热)命中:同一个景区、同一条问题,存储时间内已经跑成功过一次,直接回放。
    # 放在这里 —— 在「取连接」之前 —— 是有意的:预热过的问题在连接被删/停用的空档
    # 仍然答得出来(答的是上一次的真实结果,并且明确标了 from_cache),
    # 而不是把「后台配置断了」这件事直接甩到游客脸上。
    # 追问(context_question)不吃缓存:问的虽然是同一句,但上文不同,答案也该不同。
    if use_cache and fq is not None and not context_question:
        _on, _interval, _ttl = prewarm_settings(fq)
        if _on:
            hit = await cached_run(fq, scenic_id, _ttl, question)
            if hit is not None:
                log.info("加速命中:问题 %s 景区 %s 回放运行 %s", fq.issue_id, scenic_id, hit.id)
                for ev in _replay_events(hit, question):
                    yield ev
                return
    if fixed_question_id and not connection_id:
        raise ValidationError(
            f"景区 {scenic_id or '(集团级)'} 还没有可用的 Quick BI 连接 —— "
            f"问题「{question[:20]}」原先绑的那条已被删除或停用。"
            "请到「工具中心 · Quick BI · 连接配置」新建一条并绑到这个景区,或把问题改绑到现有连接")
    # 选了板/集/文件 → 连接跟着它走(见 connection_for_target)
    connection_id = await connection_for_target(actor, target, connection_id)
    view = await get_connection(actor, connection_id)
    # 不挑默认资产:没给目标数据就是没给,由能力层报「缺数据来源」(数据集问数例外:
    # 不指定本来就是让小Q自己智能选表)。以前会按景区兜底一个已登记资产,而判断「给没给」
    # 的清单只认单数 cube_id —— 多选的 cube_ids 被当成没给,兜底挂上一块看板,
    # 跑出来的是那块看板的快照:数看着正常,却不是运营选的数据。替用户做决定比报错更糟。
    # 景区数据集映射是**显式配置**(不是按名字猜):问题没指定任何目标数据、按某个景区发起时,
    # 用这个景区映射的数据集。只在同一条连接上才套用 —— 数据集 id 只在它那条连接的工作空间里有效。
    # 仪表板问数不套(它要的是看板,不是数据集),上传文件 / 粘贴数据本来就自带数据。
    if (scenic_id and capability not in ("dashboard_qa", "file_qa")
            and not any(target.get(k) for k in _TARGET_KEYS)):
        mapped = await scenic_dataset_row(scenic_id)
        if mapped is not None and mapped.connection_id == view.id:
            target.update({"cube_id": mapped.cube_id, "cube_name": mapped.cube_name or mapped.cube_id,
                           "from_scenic_map": True})
            log.info("景区 %s 按数据集映射取数:%s", scenic_id, mapped.cube_id)
    if actor.source == "api" and not view.api_enabled:
        raise Forbidden("该 Quick BI 连接未开放 API 调用")
    if target.get("asset_id") and not target.get("asset_kind"):
        target["asset_kind"] = (await get_asset(actor, target["asset_id"])).kind
    # 能力与目标数据对不上时按目标数据纠正,而不是抛一句
    # 「仪表板问数需要指定仪表板」—— 人刚刚明明选了目标数据,不会想到问题出在能力上。
    # 目标数据是他**显式选的**,能力常常只是上一轮留下的,所以以目标数据为准。
    _kind = target.get("asset_kind")
    if _kind == "dataset" and capability in ("dashboard_qa", "report"):
        log.info("目标数据是数据集,能力由 %s 纠正为 dataset_qa", capability)
        capability = "dataset_qa"
    elif _kind == "dashboard" and capability == "dataset_qa":
        log.info("目标数据是仪表板,能力由 dataset_qa 纠正为 dashboard_qa")
        capability = "dashboard_qa"
    if not capability or capability == "auto":
        capability = cap.route_capability(
            question, has_file=bool(target.get("file_id") or target.get("file_ids")),
            has_dashboard=bool(target.get("page_id") or target.get("dashboard") or
                               (target.get("asset_id") and target.get("asset_kind") == "dashboard")),
            has_dataset=bool(target.get("cube_id") or target.get("cube_ids")),
        )
        routed = True
    else:
        routed = False
    if capability not in CAPABILITY_KEYS:
        raise ValidationError(f"未知能力:{capability}")
    if capability == "report" and report_async is not None:
        target["wait"] = not report_async
    locale = locale or cap.detect_locale(question) or view.cfg.locale
    sent_question = question
    if context_question and capability in ("dataset_qa", "dashboard_qa", "file_qa") and _looks_like_followup(question):
        sent_question = f"{context_question};追问:{question}"
    if instruction and instruction not in sent_question:
        # 追加而不是替换:替换掉的话用户在固定问题上追问的那句就丢了。
        # question(留痕/展示)保持原样,只有发给上游的这份带口径。
        sent_question = f"{sent_question}\n口径要求:{instruction}"

    started = time.perf_counter()
    async with get_sessionmaker()() as db:
        from cogni_agent.common.task_context import current_task_id

        run = QbiRun(connection_id=view.id, capability=capability, mode=mode, question=question,
                     target={k: v for k, v in target.items() if k != "data"}, status="running",
                     session_id=session_id, fixed_question_id=fixed_question_id, source=actor.source,
                     user_id=actor.user_id, scenic_id=scenic_id, api_key_id=actor.api_key_id,
                     task_id=task_id or current_task_id())
        db.add(run)
        await db.commit()
        run_id = run.id
    acc = ResultAccumulator()
    splitter = AnswerSplitter()
    # 执行留痕:把上游原样发的和我们归一化之后的都留一份,排障时一键导出。
    # 默认开着 —— 出问题时再去开它就已经晚了,现场早就过去了。
    tracer = RunTrace(enabled=bool(_setting("capture_debug_trace", True)), secrets=view.secrets())
    state = {"status": "success", "err": None, "code": None, "count": 0, "chat_id": None,
             "report_finished": True, "finalized": False}

    async def finalize(status_: str) -> dict:
        result = _compact(acc.to_result())
        if run_id in _aborted_run_ids:
            # 终止与正常收尾撞车(用户在它快跑完那一下点的):以终止为准,
            # 否则流跑完后的 finalize(success) 会把刚标好的「已终止」覆盖掉
            status_ = state["status"] = "failed"
            state["err"] = state["err"] or ABORT_ERROR
            state["code"] = state["code"] or ABORT_CODE
        _aborted_run_ids.discard(run_id)
        duration = int((time.perf_counter() - started) * 1000)
        report_url = (acc.report or {}).get("report_url")
        await _update_run(
            run_id, status=status_, answer=acc.answer or None, result=result, error=state["err"],
            debug=tracer.to_dict(),
            error_code=(state["code"] or "")[:32] or None, trace_id=result.get("trace_id"),
            event_count=state["count"], duration_ms=duration, chat_id=state["chat_id"], report_url=report_url,
            finished_at=None if status_ == "running" else utcnow(),
        )
        if fixed_question_id:
            await _touch_fixed(fixed_question_id, run_id, status_)
        state["finalized"] = True
        return {"type": "done", "run_id": run_id, "status": status_, "capability": capability,
                "answer": acc.answer, "error": state["err"], "error_code": state["code"], "duration_ms": duration,
                "chat_id": state["chat_id"], "report_url": report_url, "chart_count": len(acc.charts),
                "usage": acc.usage, "trace_id": result.get("trace_id"),
                # 给模型/外部调用方的整段文本(回答 + 图表转 Markdown):只拿 answer 的话,
                # 纯图表类回答(问数常见)会变成空串
                "content": acc.as_llm_text()}

    def fail(message: str, code: str | None, kind: str) -> dict:
        state["status"], state["err"], state["code"] = "failed", message, code
        return {"type": "error", "message": message, "code": code, "kind": kind}

    try:
        yield {"type": "run", "run_id": run_id, "capability": capability, "routed": routed,
               "connection_id": view.id, "connection_name": view.name, "locale": locale,
               "label": cap.CAPABILITIES[capability]["label"],
               "capability_source": capability_source(capability, target),
               "sent_question": sent_question,
               "scenic_id": scenic_id, "fixed_question_id": fixed_question_id,
               "target": {k: v for k, v in target.items() if k != "data"},
               "trial": bool(view.extra.get("trial"))}
        try:
            # 报告类按连接串行(见 report_slot 的说明):小Q 同一账号只允许一个运行中的
            # 报告任务,并发发起时它会把后来者接续到前一个任务上,于是两边都拿到错的状态。
            # 非报告类(问数/解读)不受这条限制,照常并发。
            slot = (report_slot(view.id, wait=not report_async)
                    if capability == "report" else contextlib.nullcontext())
            async with slot, view.client(on_raw=tracer.add_raw, on_http=tracer.add_http) as qbi:
                pre: list[dict] = []
                gen, _ = await _prepare(view, qbi, actor, capability, target, sent_question, locale, pre)
                for e in pre:
                    state["count"] += 1
                    out = with_channel(e)
                    tracer.add_normalized(out)
                    yield out
                async for e in gen:
                    if run_id in _aborted_run_ids:
                        # 别处点了终止:退出循环即可 —— 退出 async with 会关掉上游连接,
                        # 后面 finalize 见到标记会把它收成 failed/aborted
                        state["status"], state["err"], state["code"] = "failed", ABORT_ERROR, ABORT_CODE
                        break
                    state["count"] += 1
                    if e.get("type") == "report":
                        state["chat_id"] = e.get("chat_id") or state["chat_id"]
                        state["report_finished"] = bool(e.get("finished"))
                        if state["report_finished"] and not e.get("success"):
                            state["status"], state["err"] = "failed", e.get("error") or "报告生成失败"
                        if state["chat_id"] and not e.get("finished"):
                            await _update_run(run_id, chat_id=state["chat_id"])
                    if e.get("type") == "report_progress":
                        continue
                    # 小Q常把「思考过程 …… 开始输出 …… 正式回答」塞在同一段 answer 文本里,
                    # 事件级的 channel 分不开;splitter 在文本层再切一刀,推理归推理、回答归回答。
                    for out in splitter.feed(e):
                        acc.add(out)
                        out = with_channel(out)
                        tracer.add_normalized(out)
                        yield out
            for out in splitter.flush():
                state["count"] += 1
                acc.add(out)
                out = with_channel(out)
                tracer.add_normalized(out)
                yield out
            if acc.errors and state["status"] == "success" and not acc.answer and not acc.charts:
                state["status"], state["err"] = "failed", acc.errors[-1]["message"]
                state["code"] = acc.errors[-1].get("code")
            # 一个字都没产出,却记成 success —— 这是最难查的一种「成功」:
            # 运行记录是绿的、耗时十几秒、没有任何报错,而用户那边是一张空白卡片,
            # 运营看着一条成功记录,完全不知道该查什么。
            # 报告类除外:它本来就是「创建即返回」,正文要等后台轮询收回来。
            elif (state["status"] == "success" and capability != "report"
                  and not acc.answer and not acc.charts and not acc.report):
                state["status"] = "failed"
                state["err"] = ("Quick BI 执行完成,但没有返回任何回答或图表。"
                                "常见原因:这个问题和所选数据对不上(小Q 判定「与数据集无关」),"
                                "或者问题里夹了与取数无关的话。"
                                "可以换一份目标数据,或把问题写得更贴近这份数据的字段再试。")
                state["code"] = "EMPTY_RESULT"
        except QuickBIError as exc:
            # 报告只要已经创建成功(拿到 chat_id),「跟踪这一程」的失败就不等于「报告失败」:
            # 远端还在生成,服务端的后台轮询会继续把结果收回来。
            # 判死只留给 Quick BI 自己给出的业务错误(额度用尽、无权限…)。
            if capability == "report" and state["chat_id"] and (exc.code == "REPORT_TIMEOUT" or exc.retryable):
                state["report_finished"] = False
                state["err"] = str(exc)
                yield with_channel({"type": "status", "stage": "report_tracking_interrupted",
                                    "message": f"{exc};报告仍在生成,系统会在后台继续获取结果"})
            else:
                yield with_channel(fail(str(exc), exc.code or exc.kind, exc.kind))
        except (ValidationError, NotFound, Forbidden) as exc:
            yield with_channel(fail(str(exc), exc.code, "input"))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要把运行记录收尾,不能留 running
            log.exception(f"quickbi run {run_id} 执行异常")
            yield with_channel(fail(f"内部错误:{exc}", "internal", "internal"))

        final_status = state["status"]
        if capability == "report" and final_status == "success" and not state["report_finished"]:
            final_status = "running"
            state["err"] = None      # 保持 running 的记录不该带着「超时」当失败原因
        done = await finalize(final_status)
        yield done
    finally:
        if not state["finalized"]:
            # 客户端断开(GeneratorExit)/ 任务取消(CancelledError)时也要收尾,不能把记录永远留在 running。
            # 报告已拿到 chat_id 的保持 running(远端仍在生成,可稍后刷新)。
            keep = capability == "report" and state["chat_id"]
            if not keep:
                state["status"] = "failed"
                state["err"] = state["err"] or "调用方已断开或任务被取消,执行中止"
                state["code"] = state["code"] or "cancelled"
            with contextlib.suppress(BaseException):
                await asyncio.shield(finalize("running" if keep else "failed"))


async def _update_run(run_id: str, **values) -> None:
    async with get_sessionmaker()() as db:
        run = await db.get(QbiRun, run_id)
        if run is None:
            return
        for k, v in values.items():
            if v is not None or k in ("finished_at",):
                setattr(run, k, v)
        await db.commit()


async def run_collect(actor: Actor, **kwargs) -> dict:
    """非流式:跑完整个流,返回 done 事件 + 结果。"""
    done: dict = {}
    run_id = None
    async for e in run_stream(actor, **kwargs):
        if e["type"] == "run":
            run_id = e["run_id"]
        elif e["type"] == "done":
            done = e
    return await get_run(actor, run_id) if run_id else done


def run_dict(r: QbiRun, *, full: bool = True) -> dict:
    d = {"id": r.id, "connection_id": r.connection_id, "capability": r.capability,
         "capability_label": cap.CAPABILITIES.get(r.capability, {}).get("label", r.capability),
         "capability_source": capability_source(r.capability, r.target),
         "mode": r.mode, "question": r.question, "target": r.target or {}, "status": r.status,
         "session_id": r.session_id, "fixed_question_id": r.fixed_question_id, "chat_id": r.chat_id,
         "report_url": r.report_url, "answer": r.answer, "error": r.error, "error_code": r.error_code,
         "trace_id": r.trace_id, "event_count": r.event_count, "duration_ms": r.duration_ms,
         "source": r.source, "user_id": r.user_id,
         # 本次提问归属的景区:计量/审计按景区隔离,回显它才能核对问的是哪个景区
         "scenic_id": r.scenic_id, "task_id": r.task_id,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "finished_at": r.finished_at.isoformat() if r.finished_at else None}
    if full:
        d["result"] = r.result or {}
    return d


def _run_scope(actor: Actor):
    if actor.source == "api":
        return QbiRun.api_key_id == actor.api_key_id
    if actor.is_group:
        return true()
    return or_(QbiRun.user_id == actor.user_id, (QbiRun.scenic_id == actor.scenic_id) if actor.scenic_id else false())


async def get_run(actor: Actor, run_id: str) -> dict:
    async with get_sessionmaker()() as db:
        stmt = select(QbiRun).where(QbiRun.id == run_id, _run_scope(actor))
        r = (await db.execute(stmt)).scalar_one_or_none()
        if r is None:
            raise NotFound(f"运行记录不存在:{run_id}")
        return run_dict(r)


async def run_debug(actor: Actor, run_id: str) -> dict:
    """一次运行的完整诊断包:配置(脱敏)+ 上游原样事件 + 归一化事件 + 最终结果分区。

    做给「问了之后把执行过程发给研发」这件事用的:一次导出,别人不用追问上下文。
    连接里的密钥不在这里面 —— 留痕本来就是要发出去的东西。
    """
    async with get_sessionmaker()() as db:
        r = (await db.execute(select(QbiRun).where(QbiRun.id == run_id, _run_scope(actor)))).scalar_one_or_none()
        if r is None:
            raise NotFound(f"运行记录不存在:{run_id}")
        conn = await db.get(QbiConnection, r.connection_id)
    trace = r.debug or {}
    return {
        "run": {
            "id": r.id, "capability": r.capability,
            "capability_label": cap.CAPABILITIES.get(r.capability, {}).get("label", r.capability),
            "capability_source": capability_source(r.capability, r.target),
            "mode": r.mode, "status": r.status, "question": r.question,
            "target": r.target or {}, "scenic_id": r.scenic_id, "session_id": r.session_id,
            "fixed_question_id": r.fixed_question_id, "chat_id": r.chat_id, "report_url": r.report_url,
            "error": r.error, "error_code": r.error_code, "trace_id": r.trace_id,
            "event_count": r.event_count, "duration_ms": r.duration_ms, "source": r.source,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        },
        # 连接只给「是哪个、连到哪」,不给任何凭证
        "connection": {"id": conn.id if conn else r.connection_id,
                       "name": conn.name if conn else "",
                       "server_domain": conn.server_domain if conn else "",
                       "use_trial": bool(conn.use_trial) if conn else None},
        "result": r.result or {},
        "events": {"http": trace.get("http", []),
                   "raw": trace.get("raw", []), "normalized": trace.get("normalized", []),
                   "dropped": trace.get("dropped", {}), "limits": trace.get("limits", {})},
        "captured": bool(trace),
    }


def run_debug_markdown(bundle: dict) -> str:
    """把诊断包渲染成一段可以直接贴给人看的 Markdown。

    JSON 更全,但贴进聊天窗口没人读得下去;这份是给人看的:先说结论性的元信息,
    再按顺序列事件,最后才是原始报文。
    """
    import json as _json

    run = bundle.get("run", {})
    conn = bundle.get("connection", {})
    res = bundle.get("result", {}) or {}
    ev = bundle.get("events", {}) or {}
    data = res.get("data") or {}
    thinking = res.get("thinking") or {}

    def fence(obj, limit=4000):
        t = _json.dumps(obj, ensure_ascii=False, indent=2)
        return t if len(t) <= limit else t[:limit] + "\n…(已截断)"

    lines = [
        f"# Quick BI 执行详情 · {run.get('capability_label') or run.get('capability')}",
        "",
        f"- 运行 ID:`{run.get('id')}`　状态:**{run.get('status')}**　耗时:{run.get('duration_ms')} ms",
        f"- 问题:{run.get('question')}",
        f"- 能力:{run.get('capability')}({run.get('capability_label')}"
        + (f" · {run.get('capability_source')}" if run.get("capability_source") else "") + ")",
        f"- 模式:{run.get('mode')}　来源:{run.get('source')}　景区:{run.get('scenic_id') or '—'}",
        f"- 连接:{conn.get('name')}({conn.get('server_domain')})",
        f"- 目标数据:`{_json.dumps(run.get('target') or {}, ensure_ascii=False)}`",
        f"- traceId:`{run.get('trace_id') or '—'}`　chatId:`{run.get('chat_id') or '—'}`",
    ]
    if run.get("error"):
        lines.append(f"- 错误:**{run.get('error')}**(code={run.get('error_code')})")
    lines += ["", "## 正式数据(给用户看的)", "",
              "### 回答", "", (data.get("answer") or "（空）"), ""]
    charts = data.get("charts") or []
    if charts:
        lines.append(f"### 图表 / 表格({len(charts)})")
        for c in charts[:5]:
            lines.append(f"- **{c.get('title')}** · {c.get('chart_type')} · {c.get('row_count')} 行 · "
                         f"字段 {[f.get('name') for f in (c.get('fields') or [])]}")
        lines.append("")
    lines += ["## 思考过程(排查用)", ""]
    for key, label in (("reasoning", "推理"), ("sql", "SQL"), ("code", "分析代码")):
        val = (thinking.get(key) or "").strip()
        if val:
            lines += [f"### {label}", "", "```", val[:3000], "```", ""]
    steps = thinking.get("steps") or []
    if steps:
        lines.append(f"### 执行步骤({len(steps)})")
        lines += [f"- {s.get('title')}:{(s.get('desc') or '')[:200]}" for s in steps[:30]]
        lines.append("")
    http = ev.get("http") or []
    if http:
        lines += ["## 接口原始一问一答(非流式)", "",
                  f"共 {len(http)} 次"
                  + (f",省略 {ev.get('dropped', {}).get('http', 0)} 次" if ev.get("dropped", {}).get("http") else ""),
                  ""]
        for h in http[:20]:
            lines += [f"### `{h.get('method')} {h.get('uri')}` → {h.get('status')}", ""]
            if h.get("request") is not None:
                lines += ["请求:", "", "```json", fence(h.get("request"), limit=1500), "```", ""]
            lines += ["响应原文:", "", "```", str(h.get("response") or "")[:3000], "```", ""]

    norm = ev.get("normalized") or []
    lines += ["## 归一化事件(平台内部)", "", f"共 {len(norm)} 条"
              + (f",省略 {ev.get('dropped', {}).get('normalized', 0)} 条" if ev.get("dropped", {}).get("normalized") else ""),
              "", "```json", fence(norm[:80]), "```", ""]
    raw = ev.get("raw") or []
    lines += ["## 上游原样事件(Quick BI 发来的)", "", f"共 {len(raw)} 条"
              + (f",省略 {ev.get('dropped', {}).get('raw', 0)} 条" if ev.get("dropped", {}).get("raw") else ""),
              "", "```json", fence(raw[:80], limit=8000), "```", ""]
    if not bundle.get("captured"):
        lines.append("> 这次没有留痕(quickbi.capture_debug_trace 关着,或这条记录是留痕功能之前跑的)。")
    return "\n".join(lines)


async def list_runs(actor: Actor, *, capability: str | None = None, mode: str | None = None,
                    status: str | None = None, session_id: str | None = None,
                    fixed_question_id: str | None = None, q: str | None = None,
                    task_id: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """运行记录列表(不带 result:正文 + 图表 + 留痕可达几 MB,单条走 get_run)。

    q:按问题名称模糊搜,同时匹配**运行问句正文**与**所属固定问题的标题** —— 固定问题运行
    存的问句是替换过 {{变量}}、拼过口径的文本,与列表里那条问题的标题不是同一段字,
    只搜问句会漏。每条回带 fixed_question_title,看得出是从哪条固定问题跑出来的。
    """
    D = _issue_model()
    async with get_sessionmaker()() as db:
        stmt = select(QbiRun).where(_run_scope(actor))
        for col, val in ((QbiRun.capability, capability), (QbiRun.mode, mode), (QbiRun.status, status),
                         (QbiRun.session_id, session_id), (QbiRun.fixed_question_id, fixed_question_id),
                         (QbiRun.task_id, (task_id or "").strip() or None)):
            if val:
                stmt = stmt.where(col == val)
        kw = (q or "").strip()
        if kw:
            like = f"%{kw}%"
            # 运行记录里存的可能是新的 issue_id,也可能是 0079 之前的老表 id(source_question_id)
            titled = select(D.issue_id).where(_fixed_base(), D.title.ilike(like))
            titled_old = select(D.source_question_id).where(_fixed_base(), D.title.ilike(like),
                                                            D.source_question_id.is_not(None))
            stmt = stmt.where(or_(QbiRun.question.ilike(like), QbiRun.fixed_question_id.in_(titled),
                                  QbiRun.fixed_question_id.in_(titled_old)))
        rows = list((await db.execute(
            stmt.order_by(QbiRun.created_at.desc()).offset(offset).limit(min(limit, 200)))).scalars())
        fq_ids = {r.fixed_question_id for r in rows if r.fixed_question_id}
        titles: dict[str, str] = {}
        if fq_ids:
            for iid, old, title in (await db.execute(
                    select(D.issue_id, D.source_question_id, D.title).where(
                        _fixed_base(), or_(D.issue_id.in_(fq_ids), D.source_question_id.in_(fq_ids))))).all():
                titles[iid] = title
                if old:
                    titles[old] = title
        items = []
        for r in rows:
            d = run_dict(r, full=False)
            d["fixed_question_title"] = titles.get(r.fixed_question_id or "")
            items.append(d)
        return {"items": items}


def _abort_title(run: QbiRun) -> str:
    """日志里认得出是哪一条:光给 run_id 没人知道终止的是哪一问。"""
    text = (run.question or "").strip().replace("\n", " ")
    return (text[:36] + "…") if len(text) > 36 else (text or run.id[:8])


async def abort_runs(actor: Actor, run_id: str | None = None, limit: int = 500) -> dict:
    """终止运行:可见范围内 status=running 的判为 failed(error_code=aborted)。

    run_id 为空 = 一键终止所有进行中的。只动 running,已收尾的不碰。两层动作缺一不可
    (见 _aborted_run_ids 处的说明):改库收僵尸,置标记让本进程里还在跑的那条立刻退出。
    报告类上游没有取消接口(Quick BI 只给了创建/轮询),终止的是平台不再跟踪它。
    """
    async with get_sessionmaker()() as db:
        scope = _run_scope(actor)
        stmt = select(QbiRun).where(QbiRun.status == "running", scope).order_by(QbiRun.created_at).limit(limit)
        if run_id:
            stmt = stmt.where(QbiRun.id == run_id)
        rows = list((await db.execute(stmt)).scalars().all())
        if run_id and not rows:
            one = (await db.execute(select(QbiRun).where(QbiRun.id == run_id, scope))).scalar_one_or_none()
            if one is None:
                raise NotFound(f"运行记录不存在:{run_id}")
            return {"terminated": 0, "ids": [], "status": one.status,
                    "hint": f"这条已经不是进行中了(当前 {one.status}),无需终止"}
        now = utcnow()
        for r in rows:
            r.status, r.error, r.error_code, r.finished_at = "failed", ABORT_ERROR, ABORT_CODE, now
            mark_run_aborted(r.id)
        has_report = any(r.capability == "report" for r in rows)
        await db.commit()
    if not rows:
        return {"terminated": 0, "ids": [], "hint": "没有进行中的运行需要终止"}
    return {"terminated": len(rows), "ids": [r.id for r in rows],
            "hint": ("已终止;报告类只是停止跟踪,Quick BI 侧可能仍在生成" if has_report
                     else "已终止:记录判为失败,详情里标了 error_code=aborted")}


async def abort_runs_stream(actor: Actor, ids: list[str] | None = None,
                            limit: int = 500) -> AsyncIterator[dict]:
    """批量终止(逐条 yield,供前端滚日志):一条失败不中断其余。

    「成功 N / 失败 M」回答不了「漏的是哪一条」,而漏一条恰恰是批量操作最该被看见的。
    """
    async with get_sessionmaker()() as db:
        stmt = (select(QbiRun).where(QbiRun.status == "running", _run_scope(actor))
                .order_by(QbiRun.created_at).limit(limit))
        if ids:
            stmt = stmt.where(QbiRun.id.in_(list(ids)[:limit]))
        rows = list((await db.execute(stmt)).scalars().all())
    total = len(rows)
    yield {"type": "start", "total": total,
           "message": f"开始终止 {total} 条进行中的运行" if total else "没有进行中的运行需要终止"}
    ok = fail = 0
    for i, run in enumerate(rows, 1):
        title = _abort_title(run)
        try:
            async with get_sessionmaker()() as db:
                cur = await db.get(QbiRun, run.id)
                if cur is None or cur.status != "running":
                    # 查出来到动手之间它可能已经自己收尾:用户要的是「别再是进行中」,这算达成
                    ok += 1
                    yield {"type": "item", "index": i, "total": total, "id": run.id, "title": title, "ok": True,
                           "message": f"已不是进行中(当前 {cur.status if cur else '已删除'}),无需终止"}
                    continue
                cur.status, cur.error, cur.error_code, cur.finished_at = "failed", ABORT_ERROR, ABORT_CODE, utcnow()
                mark_run_aborted(cur.id)
                await db.commit()
            ok += 1
            yield {"type": "item", "index": i, "total": total, "id": run.id, "title": title,
                   "ok": True, "message": "已终止"}
        except Exception as exc:  # noqa: BLE001 - 单条炸了也要让后面几条继续
            log.warning(f"批量终止第 {i} 条 {run.id[:8]} 失败:{exc}")
            fail += 1
            yield {"type": "item", "index": i, "total": total, "id": run.id, "title": title,
                   "ok": False, "message": str(exc)}
    yield {"type": "done", "total": total, "succeeded": ok, "failed": fail}


# ═══════════════════════════ 场景一:固定问题 ═══════════════════════════

# 预置数据域。**不是白名单** —— 现场可以直接填新的值,这里只是选择器里的默认候选,
# 免得每个人自己拍一个叫法(「客群」「人群」「用户画像」混着来就没法按域筛了)。
# 数据域候选。
#
# 「全域」排在最前:概览类的问题打的就是整块看板、不属于任何单一域,
# 原先没有这一项,运营只能留空(=未分类)或随便挑一个,两种都不对。
# 后面这几个与主题解读的 domain 对齐 —— 下面配了「OTA直销」主题,
# 上面的下拉里却只有「OTA」,就会出现同一件事两个写法,筛选时对不上。
PRESET_DOMAINS = ["全域", "客群", "票务", "舆情", "OTA直销",
                  "财务", "核销", "营销", "流量", "画像", "OTA"]

# 请求类别:与 Quick BI 的四个产品叫法一一对应,调用方按它拉清单。
#   问数 = 对着数据集/上传文件问;报表 = 对着已有仪表板问(小Q报表);
#   解读 = 小Q解读;报告 = 小Q报告(异步长任务)。
# 「问数」与「报表」分开,是因为对调用方来说这是两件事:前者随便问一个指标,
# 后者是针对某块已经做好的看板提问 —— 合成一类会让按类别取问题时混在一起。
ASK_TYPES = {"qa": "问数", "insight": "解读", "dashboard": "报表", "report": "报告"}
# capability → 默认 ask_type(没显式指定时按能力推,省得两处填一样的东西)
_ASK_TYPE_BY_CAP = {"dataset_qa": "qa", "file_qa": "qa", "dashboard_qa": "dashboard",
                    "insight": "insight", "report": "report"}


def default_ask_type(capability: str | None) -> str:
    return _ASK_TYPE_BY_CAP.get(capability or "", "qa")


# ══════════════════════ 固定问题(存储在统一问题表里) ══════════════════════
# 0079 起,「Quick BI 固定问题」就是统一问题表 scenic_agent_issue_detail 里
# business_class='quick_bi' 的那些行,老表 tc_qbi_fixed_questions 已删除。
#
# **为什么要并表**:同一件事配两张表,就一定会出现「管理端建了、APP 前台没有」——
# 因为前台读的是统一表,而管理端写的是老表,两边靠人记得去同步。现在只有一张表,
# 管理端存的就是前台读的。字段映射上只有两处改名:老表的 `question` 在统一表里叫
# `issue`,老表的 `id` 在统一表里叫 `issue_id`;对外返回时仍叫 id / question,
# 所以前端与开放平台的调用方都不用改。
#
# **老 ID 继续可用**:迁进来的行保留 source_question_id=老表 id,
# 历史的工具配置(tool_type 里存的 fixed_question_id)和运行记录都照样能查到。

_FIXED_ID_PREFIX = "issue_"


def _can(check, actor: Actor, q) -> bool:
    try:
        check(actor.role, actor.scenic_id, q.scenic_id, "固定问题")
        return True
    except Forbidden:
        return False


def fixed_dict(q: "IssueRow", actor: Actor | None = None) -> dict:
    """统一问题表的行 → 固定问题的对外结构(字段名保持老接口的样子)。

    传 actor 时额外给出 can_write / can_delete —— 与 save_fixed / delete_fixed 的鉴权同一口径,
    前端据此把编辑弹窗切成只读、灰掉上架开关,而不用在前端再抄一份角色白名单。
    """
    from cogni_agent.common import permissions as perm

    ask = q.ask_type or default_ask_type(q.capability)
    perms = ({"can_write": _can(perm.assert_can_write_obj, actor, q),
              "can_delete": _can(perm.assert_can_delete_obj, actor, q)} if actor is not None else {})
    return {**perms, "id": q.issue_id, "connection_id": q.connection_id,
            # 同一个值的两个名字:文档/前端叫 quickbiLinkId,库里叫 connection_id
            "quickbi_link_id": q.connection_id,
            "title": q.title, "question": q.issue,
            "capability": q.capability,
            "capability_label": cap.CAPABILITIES.get(q.capability, {}).get("label", q.capability),
            "capability_source": capability_source(q.capability, q.target),
            "target": q.target or {},
            # 目标数据的编码与名称:target 是 JSON 不便展示,列表直接用这两列
            "target_data_id": q.target_data_id, "target_data_name": q.target_data_name,
            "locale": q.locale, "category": q.category,
            # 三级标签:没挂标签的问题不会出现在 APP 的问题树里,所以列表里要能一眼看出
            "topic": q.topic, "topic_id": q.topic_id,
            "dimension": q.dimension, "dimension_id": q.dimension_id,
            "metric": q.metric, "metric_id": q.metric_id,
            "domain": q.domain, "ask_type": ask,
            "ask_type_label": ASK_TYPES.get(ask, ask),
            # 口径说明:发起时追加在问题后面发给模型(按钮文案要短,模型要的是约束)
            "model_instruction": q.model_instruction,
            "business_class": q.business_class,
            "scenic_id": q.scenic_id, "sort_order": q.sort_order,
            "active": q.active, "visibility": q.visibility, "owner_user_id": q.owner_user_id,
            # 加速(预热):分钟数为空表示跟随全局默认,前端显示占位而不是 0
            "prewarm_enabled": bool(q.prewarm_enabled),
            "prewarm_interval_minutes": q.prewarm_interval_minutes,
            "prewarm_ttl_minutes": q.prewarm_ttl_minutes,
            "last_run_id": q.last_run_id, "last_run_status": q.last_run_status,
            "last_run_at": q.last_run_time.isoformat() if q.last_run_time else None,
            # 老表的 id,便于与历史运行记录/工具配置对账(没迁移过来的新问题为空)
            "source_question_id": q.source_question_id,
            "created_at": q.create_time.isoformat() if q.create_time else None}


def _issue_model():
    """延迟导入统一问题表模型。

    放在函数里而不是模块顶部:gateway_service 会反过来 import 本模块(APP 端发起
    Quick BI 提问),顶部互相 import 会在启动时转成循环依赖。
    """
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail

    return ScenicAgentIssueDetail


def _fixed_base():
    """固定问题 = 统一问题表里 business_class='quick_bi' 的行。"""
    D = _issue_model()
    return D.business_class == "quick_bi"


def _fixed_scope(actor: Actor):
    D = _issue_model()
    if actor.is_group:
        return _fixed_base()
    conds = [D.owner_user_id == actor.user_id, D.visibility == "public"]
    if actor.trusted_refs:
        # 授权引用既可能给的是新的 issue_id,也可能是历史配置里的老 id
        refs = list(actor.trusted_refs)
        conds.append(or_(D.issue_id.in_(refs), D.source_question_id.in_(refs)))
    if actor.scenic_id:
        conds.append((D.scenic_id == actor.scenic_id) & D.visibility.in_(("scenic", "group")))
    # 集团级问题(scenic_id 为空)且可见范围是 group:所有景区账号都能用。
    # 少了这一条会很坑:管理员用超管账号建的固定问题默认就是「集团级 + group」,
    # 他自己在管理端看得见(is_group 直接放行),景区账号却一条都看不到 ——
    # 表现成「后台明明配了,APP 里是空的」。与资产/目录的口径(集团级可共用)对齐。
    conds.append(D.scenic_id.is_(None) & D.visibility.in_(("group", "public")))
    return _fixed_base() & or_(*conds)


def _validate_fixed(data: dict) -> None:
    if data.get("visibility") is not None and data["visibility"] not in ("private", "scenic", "group", "public"):
        raise ValidationError("visibility 只能是 private / scenic / group / public")
    if data.get("capability") and data["capability"] not in CAPABILITY_KEYS:
        raise ValidationError(f"未知能力:{data['capability']}")
    if data.get("question") is not None and not str(data["question"]).strip():
        raise ValidationError("问题不能为空")
    if data.get("ask_type") and data["ask_type"] not in ASK_TYPES:
        raise ValidationError(f"请求类别只能是 {' / '.join(ASK_TYPES)}")
    if data.get("domain") is not None and len(str(data["domain"])) > 32:
        raise ValidationError("数据域名称过长(最多 32 字)")
    # 三级标签要么三级全给、要么一个都不给。只给一半会在 APP 的问题树里挂不上去,
    # 而且半截路径没法归并 —— 与其存进去再排查「为什么前台看不到」,不如这里挡掉。
    codes = [str(data.get(k) or "").strip() for k in ("topic_id", "dimension_id", "metric_id")]
    if any(codes) and not all(codes):
        raise ValidationError("主题 / 维度 / 指标 必须三级都选,或三级都留空")
    # 加速的两个分钟数:留空 = 跟随全局默认,给了就必须是正整数。
    # 上限 7 天 —— 再长的「存储时间」本质上是「不再刷新」,那应该关掉加速而不是把它设成 30 天,
    # 否则界面上写着已开启,答的却是上个月的数。
    for key, label in (("prewarm_interval_minutes", "刷新时间"), ("prewarm_ttl_minutes", "存储时间")):
        raw = data.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{label}(分钟)必须是整数")
        if not 1 <= val <= 60 * 24 * 7:
            raise ValidationError(f"{label}(分钟)只能在 1 ~ 10080 之间")
        data[key] = val
    # 存储时间必须 >= 刷新时间:反过来的话每次刷新完没多久缓存就过期了,
    # 中间那段空窗全是现跑 —— 开了加速却没快,而且没有任何报错提示
    _iv, _ttl = data.get("prewarm_interval_minutes"), data.get("prewarm_ttl_minutes")
    if isinstance(_iv, int) and isinstance(_ttl, int) and _ttl < _iv:
        raise ValidationError("存储时间不能小于刷新时间,否则两次预热之间会出现没有缓存的空窗")


async def list_fixed(actor: Actor, *, connection_id: str | None = None, category: str | None = None,
                     active: bool | None = None, keyword: str | None = None,
                     domain: str | None = None, ask_type: str | None = None,
                     scenic_id: str | None = None, topic_id: str | None = None,
                     dimension_id: str | None = None, metric_id: str | None = None,
                     unlabeled: bool | None = None, prewarm: bool | None = None,
                     last_status: str | None = None) -> list[dict]:
    D = _issue_model()
    async with get_sessionmaker()() as db:
        stmt = select(D).where(_fixed_scope(actor))
        if connection_id:
            stmt = stmt.where(D.connection_id == connection_id)
        if category:
            stmt = stmt.where(D.category == category)
        if domain:
            stmt = stmt.where(D.domain == domain)
        if ask_type:
            stmt = stmt.where(D.ask_type == ask_type)
        if scenic_id:
            # 与资产同一口径:本景区 + 集团级(scenic_id 为空)的问题都能用
            stmt = stmt.where(or_(D.scenic_id == scenic_id, D.scenic_id.is_(None)))
        if topic_id:
            stmt = stmt.where(D.topic_id == topic_id)
        if dimension_id:
            stmt = stmt.where(D.dimension_id == dimension_id)
        if metric_id:
            stmt = stmt.where(D.metric_id == metric_id)
        if unlabeled:
            # 没挂标签的问题进不了 APP 的问题树 —— 单独筛出来好补
            stmt = stmt.where(D.metric_id.is_(None))
        if active is not None:
            stmt = stmt.where(D.active.is_(active))
        if prewarm is not None:
            stmt = stmt.where(D.prewarm_enabled.is_(prewarm))
        if last_status == "none":
            # 「从未运行」= 配完还没跑过的
            stmt = stmt.where(D.last_run_status.is_(None))
        elif last_status:
            stmt = stmt.where(D.last_run_status == last_status)
        if keyword:
            # 问题ID 也能搜:外部调用方(开放平台 fixed_question_id)与 APP 都按 id 定位,
            # 现场报问题时给的往往就是一串 id
            kw = keyword.strip()
            stmt = stmt.where(or_(D.title.ilike(f"%{kw}%"), D.issue.ilike(f"%{kw}%"),
                                  D.issue_id == kw, D.source_question_id == kw))
        rows = await db.execute(stmt.order_by(D.sort_order, D.create_time))
        return [fixed_dict(q, actor) for q in rows.scalars()]


async def question_catalog(actor: Actor, *, connection_id: str | None = None, scenic_id: str | None = None,
                           ask_type: str | None = None, domain: str | None = None,
                           include_assets: bool = True) -> dict:
    """对外目录:这个景区能问什么(固定问题)、能问哪些数据(数据集/仪表板)。

    给外部调用方用的「菜单」:先拉一次目录,知道有哪些问题、每个问题打在哪份数据上,
    之后要么直接 `fixed_question_id` 发起,要么拿 `cube_id` / `page_id` 自己组问题。
    所以每条都带上**可直接用于提问的 id**,而不只是一个名字。
    """
    scenic_id = _bind_scenic(actor, scenic_id)
    questions = await list_fixed(actor, connection_id=connection_id, active=True,
                                 domain=domain, ask_type=ask_type, scenic_id=scenic_id)
    assets: list[dict] = []
    if include_assets:
        assets = await list_assets(actor, connection_id, None, scenic_id=scenic_id)

    by_id = {a["id"]: a for a in assets} if assets else {}

    async def _resolve(target: dict) -> dict:
        """把固定问题的 target 翻译成「可直接发起提问的数据标识」。"""
        t = target or {}
        out: dict = {}
        asset = by_id.get(t.get("asset_id"))
        if asset is None and t.get("asset_id"):
            # 目录按景区筛过,问题绑的资产可能不在这一批里(集团级问题打在别的景区资产上),
            # 单独取一次而不是让这条问题的目标数据显示成空。
            try:
                a = await get_asset(actor, t["asset_id"])
                asset = asset_dict(a)
            except (NotFound, Forbidden):
                asset = None
        if asset:
            out = {"kind": asset["kind"], "asset_id": asset["id"], "name": asset["name"],
                   "ref_id": asset["ref_id"], "scenic_id": asset.get("scenic_id")}
            if asset["kind"] == "dashboard":
                out["page_id"] = asset["ref_id"]
                out["cubes"] = asset.get("cubes") or []
            else:
                out["cube_id"] = asset["ref_id"]
            if t.get("cube_ids"):                 # 多选:资产还在,但真正取数的是这几份
                out["cube_ids"] = list(t["cube_ids"])
        elif t.get("cube_ids") or t.get("cube_id"):
            cids = list(dict.fromkeys(list(t.get("cube_ids") or [])
                                      + ([t["cube_id"]] if t.get("cube_id") else [])))
            out = {"kind": "dataset", "cube_ids": cids,
                   "name": t.get("cube_name") or (cids[0] if cids else ""),
                   "ref_id": cids[0] if cids else ""}
            if len(cids) == 1:
                out["cube_id"] = cids[0]        # 只有一份时仍给 cube_id,老调用方不用改
        elif t.get("page_id") or t.get("dashboard"):
            ref = t.get("page_id") or t.get("dashboard")
            out = {"kind": "dashboard", "page_id": ref, "name": t.get("asset_name") or t.get("name") or ref,
                   "ref_id": ref}
        elif t.get("file_id"):
            out = {"kind": "file", "file_id": t["file_id"], "name": t.get("file_name") or t["file_id"]}
        return out

    items = []
    for q in questions:
        items.append({
            "id": q["id"], "title": q["title"], "question": q["question"],
            "domain": q["domain"], "ask_type": q["ask_type"], "ask_type_label": q["ask_type_label"],
            "capability": q["capability"], "capability_label": q["capability_label"],
            "category": q["category"], "scenic_id": q["scenic_id"], "connection_id": q["connection_id"],
            # 三级标签一并给出去:外部调用方按 主题/维度/指标 组织自己的问题菜单
            "topic": q["topic"], "topic_id": q["topic_id"],
            "dimension": q["dimension"], "dimension_id": q["dimension_id"],
            "metric": q["metric"], "metric_id": q["metric_id"],
            "target": await _resolve(q["target"]),
            "target_data_id": q["target_data_id"], "target_data_name": q["target_data_name"],
            # 口径说明:外部调用方自己组问题时也该带上它,否则同一条问题在不同接入方
            # 答出来的长度与角度都不一样
            "model_instruction": q.get("model_instruction"),
        })
    return {
        "scenic_id": scenic_id,
        "ask_types": [{"key": k, "label": v} for k, v in ASK_TYPES.items()],
        "domains": sorted({q["domain"] for q in questions if q["domain"]}) or [],
        "questions": items,
        "datasets": [{"asset_id": a["id"], "name": a["name"], "cube_id": a["ref_id"],
                      "scenic_id": a.get("scenic_id")} for a in assets if a["kind"] == "dataset"],
        "dashboards": [{"asset_id": a["id"], "name": a["name"], "page_id": a["ref_id"],
                        "scenic_id": a.get("scenic_id"), "cubes": a.get("cubes") or []}
                       for a in assets if a["kind"] == "dashboard"],
    }


async def list_domains(actor: Actor) -> list[str]:
    """预置数据域 + 库里已经用过的值(现场新增的那些),按预置顺序在前。"""
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            select(_issue_model().domain).where(_fixed_scope(actor),
                                                 _issue_model().domain.is_not(None)).distinct())
        used = {r[0] for r in rows if r[0]}
    return PRESET_DOMAINS + sorted(used - set(PRESET_DOMAINS))


# ═══════════════════ 固定问题 ↔ 统一问题表 的同步(0076/0077) ═══════════════════
# 固定问题是「Quick BI 管理端」的概念,统一问题表 scenic_agent_issue_detail 是
# 「APP 前台问题树」的数据源。两边都留着是有意的:管理端继续用固定问题的批量执行、
# 发布成工具等能力,前台只读统一表。所以保存/删除/执行固定问题时必须把统一表带上,
# 否则就会回到「后台建了、APP 里看不到」的老问题上 —— 这正是 0076 要解决的事。


async def _resolve_label(db, data: dict) -> None:
    """按编码三元组查标签表,把中文名回填到 data 上(就地修改)。

    只认编码不认名字:名字是给人看的、可以被改,编码才是稳定的挂载点。
    组合不存在就直接报错 —— 存进去的话前台按编码分组时会分出一个空壳节点。
    """
    from cogni_agent.gateway_service.issue_models import PublicProblemDimensionLabel

    codes = [str(data.get(k) or "").strip() for k in ("topic_id", "dimension_id", "metric_id")]
    if not all(codes):
        return
    row = (await db.execute(select(PublicProblemDimensionLabel).where(
        PublicProblemDimensionLabel.topic_id == codes[0],
        PublicProblemDimensionLabel.dimension_id == codes[1],
        PublicProblemDimensionLabel.metric_id == codes[2],
    ))).scalars().first()
    if row is None:
        raise ValidationError(
            f"主题/维度/指标 组合不存在:{codes[0]} / {codes[1]} / {codes[2]},"
            "请先到「知识中心 - 维度标签」维护")
    data["topic"], data["dimension"], data["metric"] = row.topic, row.dimension, row.metric


async def _target_display(db, target: dict | None) -> tuple[str | None, str | None]:
    """把 target JSON 摊成「数据编码 + 数据名称」两列,供前台直接显示。

    target 里可能只有 asset_id(登记过的资产),那就顺带把资产的 ref_id/name 取出来;
    取不到也不报错 —— 显示名缺一个字段,不值当拦住保存。
    """
    t = target or {}
    data_id = t.get("page_id") or t.get("cube_id")
    # 直接粘贴的看板 / 门户地址:名称用粘贴时解析出来的看板名(asset_name),而不是一长串 URL;
    # 编码从地址里取 —— 看板取 pageId,门户取 productId(门户的 pageId 要运行时按菜单解析)
    data_name = t.get("cube_name") or t.get("asset_name") or t.get("name") or t.get("dashboard")
    if not data_id and t.get("dashboard"):
        from cogni_agent.tool_center_service.quickbi import dashboard as dash

        url = str(t["dashboard"])
        try:
            data_id = (dash.extract_dataportal_ids(url)["productId"] if dash.is_dataportal_url(url)
                       else dash.extract_page_id(url))
        except ValueError:
            data_id = None
    if t.get("asset_id"):
        a = await db.get(QbiAsset, str(t["asset_id"]))
        if a is not None:
            data_id = data_id or a.ref_id
            data_name = data_name or a.name
    return (str(data_id) if data_id else None, str(data_name)[:255] if data_name else None)


async def _load_fixed_row(db, fq_id: str, actor: Actor | None = None):
    """按 ID 取一条固定问题。**新旧两种 ID 都认**:

    issue_xxx 是统一表的业务主键;历史的工具配置、运行记录里存的是老表的 UUID,
    那些配置不该因为换了张表就集体失效,所以 source_question_id 也参与匹配。
    """
    D = _issue_model()
    cond = or_(D.issue_id == fq_id, D.source_question_id == fq_id)
    stmt = select(D).where(cond, _fixed_scope(actor) if actor is not None else _fixed_base())
    return (await db.execute(stmt)).scalars().first()


async def get_fixed(actor: Actor, fq_id: str):
    async with get_sessionmaker()() as db:
        q = await _load_fixed_row(db, fq_id, actor)
        if q is None:
            raise NotFound(f"固定问题不存在:{fq_id}")
        return q


async def save_fixed(actor: Actor, data: dict, fq_id: str | None = None) -> dict:
    """新建/修改固定问题 —— 直接写统一问题表(business_class=quick_bi)。

    前台读的就是这张表,所以保存完立刻就能在 APP 的问题树上看到(前提是挂了三级标签)。
    """
    from cogni_agent.common import permissions as perm

    perm.assert_can_write(actor.role, "固定问题")
    _validate_fixed(data)
    if data.get("connection_id"):
        await get_connection(actor, data["connection_id"])
    D = _issue_model()
    async with get_sessionmaker()() as db:
        # 三级标签:只认编码,中文名由标签表回填(名字可能被改过,以表为准)。
        # **必须在 db.add(q) 之前查**:新建时 q 的必填列还没赋值,此时再发一条 SELECT
        # 会触发 SQLAlchemy 的 autoflush,把半成品行刷进库 → title 非空约束直接报错。
        await _resolve_label(db, data)
        if fq_id:
            q = await _load_fixed_row(db, fq_id)
            if q is None:
                raise NotFound("固定问题不存在")
            # 按角色 + 景区判,不按创建者:景区管理员/操作员管得了本景区的全部问题,
            # 但动不了集团级那批(所有景区共用,改一次全场跟着变)
            perm.assert_in_scope(actor.role, actor.scenic_id, q.scenic_id, "固定问题")
        else:
            for k in ("question", "capability"):
                if not data.get(k):
                    raise ValidationError(f"缺少必填字段 {k}")
            conn = await get_connection(actor, data.get("connection_id"))
            q = D(issue_id=f"{_FIXED_ID_PREFIX}{uuid.uuid4().hex[:16]}",
                  business_class="quick_bi", owner_user_id=actor.user_id,
                  scenic_id=actor.scenic_id, connection_id=conn.id,
                  visibility="group" if actor.is_group else "private", target={},
                  title="", issue="")
            db.add(q)

        # 老接口字段名 → 表字段名:question→issue(表里 issue 才是问题原文)
        if data.get("question") is not None:
            q.issue = data["question"]
        for k in ("connection_id", "capability", "target", "locale", "category", "domain",
                  "ask_type", "sort_order", "active", "visibility", "model_instruction",
                  "topic", "topic_id", "dimension", "dimension_id", "metric", "metric_id",
                  # 加速(预热):开关 + 刷新间隔 + 存储时长。两个分钟数留空表示用全局默认,
                  # 所以这里不做兜底填值 —— 填死了之后改全局默认就影响不到这条问题了
                  "prewarm_enabled", "prewarm_interval_minutes", "prewarm_ttl_minutes"):
            if data.get(k) is not None:
                setattr(q, k, data[k])
        # 归属景区:集团角色可以把问题挂到指定景区(只有该景区的账号能看到/发起)。
        # 景区角色不允许改 —— 否则它能把自己的问题挂到别的景区名下。空串 = 集团级(全景区通用)。
        if data.get("scenic_id") is not None:
            if not actor.is_group:
                raise Forbidden("只有集团角色可以变更问题的归属景区")
            q.scenic_id = str(data["scenic_id"]).strip() or None
        if data.get("domain") == "":
            q.domain = None            # 显式清空:前端把下拉选回「未分类」就是这个意思
        if str(data.get("topic_id") or "") == "" and "topic_id" in data:
            # 前端把三级选择器清空 = 这条问题不再挂在问题树上,六列一起置空。
            # 只清编码不清名字的话,列表上还会显示一个已经不生效的标签路径。
            q.topic = q.topic_id = q.dimension = q.dimension_id = q.metric = q.metric_id = None
        if not q.ask_type:
            q.ask_type = default_ask_type(q.capability)
        _auto_route_theme(q)
        q.title = (data.get("title") or q.title or str(q.issue)[:40])[:255]
        # 目标数据的编码/名称随 target 一起刷新:前台列表直接显示「问的是哪份数据」
        q.target_data_id, q.target_data_name = await _target_display(db, q.target)
        await db.commit()
        await db.refresh(q)
        return fixed_dict(q)


def _auto_route_theme(q) -> None:
    """解读 / 报告类问题没挂标签时,自动挂到「主题解读 / <主题> / 解读报告」下。

    为什么要自动:前台的问题树只认三级标签,没挂的一律不展示(未分类分支已经去掉了)。
    而运营建一条「解读」或「报告」时,心里想的就是「它应该出现在对应的板块里」——
    让他每次再手工选一遍三级,少选一次就是「建了但前台没有」,而且不报错。

    主题按数据域认(客群 / 票务 / 舆情 / OTA直销),认不出就按问题文本里出现的主题名认;
    都认不出就不猜 —— 挂错板块比不挂更糟,至少不挂时管理端列表会标「未挂标签」。
    已经挂过标签的不动。
    """
    from cogni_agent.gateway_service.scenic_binding import THEME_PREFIX, THEMES

    if (q.ask_type or "") not in ("insight", "report") and (q.capability or "") not in ("insight", "report"):
        return
    if q.topic_id and q.dimension_id and q.metric_id:
        return                                   # 运营自己选过了,以他为准
    hay = f"{q.domain or ''} {q.title or ''} {q.issue or ''}"
    hit = next((t for t in THEMES if t["domain"] and t["domain"] in hay), None)
    if hit is None:
        hit = next((t for t in THEMES if t["name"] in hay), None)
    if hit is None:
        return
    q.topic, q.topic_id = "主题解读", "themeReport"
    q.dimension, q.dimension_id = hit["name"], f"theme_{hit['code']}"
    q.metric, q.metric_id = "解读报告", f"themeReport_{hit['code']}"
    log.info(f"问题「{q.title or q.issue}」自动挂到 主题解读/{hit['name']}/解读报告")
    _ = THEME_PREFIX      # 口径常量保持在同一处,避免两边各写一份前缀


# ── 「查看思考」该取哪一条 ─────────────────────────────────────────────
# 思考过程存在**当次运行的 result.thinking.reasoning** 里,问题本身只存配置不存过程 ——
# 只能回溯运行记录。列表出身的 run_dict(full=False)不带 result,拿它去取必然是空框。
# 只认解读/报告类:同一条问题下会混着问数类的运行,它们的 thinking 装的是 NL2SQL 的
# 取数日志(选中哪个数据集、几步 SQL),甚至有「该问题与数据集无关,请重新提问」这种
# 拒绝文案 —— 当成思考过程展示比取到空更糟,所以按能力排掉,而不是事后猜哪段像思考。
_THINKING_CAPABILITIES = ("insight", "report")


async def latest_thinking(actor: Actor, fq_id: str) -> dict:
    """这条固定问题**最近一次成功且真有思考**的解读/报告类运行的思考过程(只读)。"""
    q = await get_fixed(actor, fq_id)
    async with get_sessionmaker()() as db:
        stmt = select(QbiRun).where(
            _run_scope(actor),
            QbiRun.fixed_question_id.in_(_fixed_ids(q)),
            QbiRun.status == "success",
            QbiRun.capability.in_(_THINKING_CAPABILITIES),
            # 「有思考」写成查询条件:空思考的运行才是多数,取到它等于白查
            func.coalesce(QbiRun.result["thinking"]["reasoning"].astext, "") != "",
        ).order_by(QbiRun.created_at.desc()).limit(1)
        r = (await db.execute(stmt)).scalars().first()
    if r is None:
        return {"found": False,
                "reason": "这条问题还没有成功的「解读 / 报告」类运行记录 —— 先在列表点「运行」。"
                          "问数类运行不算:它的 thinking 是 NL2SQL 取数日志,不是解读的思考。"}
    thinking = (r.result or {}).get("thinking") or {}
    return {"found": True, "run_id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "capability": r.capability,
            "capability_label": cap.CAPABILITIES.get(r.capability, {}).get("label", r.capability),
            "duration_ms": r.duration_ms,
            # 当次真正发出去的问句:改过口径之后,它是「这份思考在答哪一版」的凭据
            "question": r.question,
            "text": thinking.get("reasoning") or ""}


async def queue_stats() -> dict:
    """任务队列水位:排队 / 运行中,按来源分(预热 vs 其余),供问题列表右上角常驻展示。

    给的是**全局**数,不按景区过滤:预热与实时链路(APP / 开放平台 / 操练台)共用一张
    任务表、同一条队(claim_one 不看 source),要判断的正是「这条队现在堵不堵」。
    Quick BI 加速本身不走任务队列(quickbi_prewarm 直接串行跑),这里的「预热」是
    APP 问题库那条预热链路(cronjobs/prewarm.py)派的单。
    """
    from datetime import datetime, timezone

    from cogni_agent.control_plane_service.models import SOURCE_PREWARM, Task

    async with get_sessionmaker()() as db:
        rows = (await db.execute(
            select(Task.status, Task.source, func.count())
            .where(Task.status.in_(("QUEUED", "RUNNING")))
            .group_by(Task.status, Task.source))).all()
        oldest = (await db.execute(select(func.min(Task.created_at)).where(Task.status == "QUEUED"))).scalar()
    pre = {"queued": 0, "running": 0}
    queued = running = 0
    for st, source, n in rows:
        key = "queued" if st == "QUEUED" else "running"
        if key == "queued":
            queued += n
        else:
            running += n
        if source == SOURCE_PREWARM:
            pre[key] += n
    # 队首那条等了多久:总数大但走得快不算堵,这个数才对应「排到了什么时候」的体感
    now = datetime.now(timezone.utc)
    waited = 0
    if oldest is not None:
        o = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        waited = max(0, int((now - o).total_seconds()))
    return {"queued": queued, "running": running, "prewarm": pre,
            "others": {"queued": queued - pre["queued"], "running": running - pre["running"]},
            "oldest_queued_seconds": waited, "updated_at": now.isoformat()}


# ═══════════════════════════ 固定问题批量运维 ═══════════════════════════
# 批量改一批问题要的是「一次动作、逐条可见」:改 30 条里漏了一条,汇总成「成功 29」
# 是看不出漏了哪条的。所以按条 yield 事件,由前端滚成日志。

BATCH_OPS = ("set_interval", "copy", "insight", "delete")
BATCH_MAX_IDS = 50

# 主题(三级标签的第一级,APP 主题卡按它分组)→ 数据集名称关键字。数据集命名没有统一规范
# (同一主题下并存「九龙潭-票务-…」与「票务-景区当日客流」两种写法),跨连接还能对齐的只有
# 名称里的主题词,所以按包含匹配。主题名本身也算一个关键字(排最前),表里没列到的主题照样能配。
# 匹配不到就跳过并在日志里说清楚,**不猜一个数据集挂上去** —— 挂错的数据集会给出一份
# 像模像样但答非所问的解读。
TOPIC_DATASET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "客流": ("客流", "入园", "在园", "游客"),
    "画像": ("客流", "画像", "客群", "游客"),
    "客群": ("客群", "画像", "游客"),
    "票务": ("票务", "售票", "退票", "票种", "收入"),
    "舆情": ("舆情", "评论", "评价", "口碑", "情感"),
    "OTA直销": ("ota", "直销", "渠道", "分销"),
    "渠道": ("ota", "直销", "渠道", "分销"),
}
# 概览类主题不按名称匹配:要的是跨主题的全貌,按名称挑要么挑不到、要么只挑到一块。
# 所以命中的是**候选里的全部数据集**。
TOPIC_ALL_DATASETS = ("概览", "数据概览", "总览", "全域")


async def _dataset_candidates(actor: Actor, connection_id: str | None, scenic_id: str | None) -> list[dict]:
    """候选数据集:先已登记(且可用)的数据资产,没有再实时拉该连接下可问数的数据集。"""
    out: list[dict] = []
    if connection_id:
        async with get_sessionmaker()() as db:
            rows = (await db.execute(select(QbiAsset).where(
                QbiAsset.kind == "dataset", QbiAsset.connection_id == connection_id,
                QbiAsset.active.is_(True)))).scalars().all()
            out = [{"cube_id": a.ref_id, "cube_name": a.name, "scenic_id": a.scenic_id} for a in rows]
    if not out:
        try:
            out = [{"cube_id": c.get("cube_id"), "cube_name": c.get("cube_name"), "scenic_id": None}
                   for c in await list_cubes(actor, connection_id)]
        except Exception as exc:  # noqa: BLE001  拉不动就当没有候选,由调用方报「未匹配」
            log.warning(f"拉连接 {connection_id} 的数据集失败:{exc}")
    # 同景区(与集团级)的排前面:一个连接下可能登记了多个景区的数据集,名称相似时按景区挑才不会串
    if scenic_id:
        out = [c for c in out if c.get("scenic_id") in (scenic_id, None)] or out
        out.sort(key=lambda c: 0 if c.get("scenic_id") == scenic_id else 1)
    seen: set[str] = set()
    uniq = []
    for c in out:          # 同一份数据集可能被登记多次:不去重会被取数两遍
        if c.get("cube_id") and c["cube_id"] not in seen:
            seen.add(c["cube_id"])
            uniq.append(c)
    return uniq


async def _match_topic_datasets(actor: Actor, q) -> tuple[list[dict], str]:
    """按问题的主题在候选数据集里挑**所有**命中该主题的数据集,返回 (命中, 主题名)。

    为什么挑全部而不是一个:解读要看的是这一主题的全貌 —— 舆情下「评论」「评分」
    「平台分布」都得一起看;能力层支持多份(run_dataset_insight 一次多表取数后再解读)。
    """
    topic = (q.topic or q.domain or "").strip()
    cands = await _dataset_candidates(actor, q.connection_id, q.scenic_id)
    if topic in TOPIC_ALL_DATASETS:
        return cands, topic
    keys = [topic.lower()] + [k.lower() for k in TOPIC_DATASET_KEYWORDS.get(topic, ())]
    keys = [k for k in dict.fromkeys(keys) if k]
    ranked = []
    for i, c in enumerate(cands):
        name = (c.get("cube_name") or "").lower()
        rank = next((j for j, k in enumerate(keys) if k in name), None)
        if rank is not None:
            ranked.append((rank, i, c))
    ranked.sort(key=lambda z: (z[0], z[1]))
    return [c for _, _, c in ranked], topic


_COPY_FIELDS = ("title", "capability", "target", "locale", "category", "domain", "ask_type",
                "model_instruction", "topic_id", "dimension_id", "metric_id", "sort_order", "active",
                "visibility", "prewarm_enabled", "prewarm_interval_minutes", "prewarm_ttl_minutes")


async def _batch_one(actor: Actor, op: str, fq_id: str, *, interval_minutes: int | None = None,
                     ttl_minutes: int | None = None, prewarm_enabled: bool | None = None,
                     connection_id: str | None = None) -> dict:
    """对一条固定问题执行一个批量动作,返回 {ok, title, message[, new_id]}。"""
    q = await get_fixed(actor, fq_id)
    title = q.title or (q.issue or "")[:24] or q.issue_id

    if op == "set_interval":
        # 开关三态:True = 一并开启 / False = 关闭 / None = 不动开关只改时间。
        if prewarm_enabled is False:
            await save_fixed(actor, {"prewarm_enabled": False}, fq_id)
            return {"ok": True, "title": title, "message": "已关闭加速"}
        if prewarm_enabled is None and not q.prewarm_enabled:
            # 没开加速的行改了时间也不生效,留一个看不见的值只会更难排查
            return {"ok": False, "title": title, "message": "未开启加速,跳过(要一并开启请选「开启加速」)"}
        if not interval_minutes:
            return {"ok": False, "title": title, "message": "未填写刷新时间"}
        payload: dict = {"prewarm_interval_minutes": interval_minutes}
        # 存储时间必须 >= 刷新时间(见 _validate_fixed):只改刷新时间时,原存储时间比它短就一起抬上去,
        # 否则保存直接被挡,运营还得一条条回头改
        ttl = ttl_minutes or max(q.prewarm_ttl_minutes or 0, interval_minutes)
        payload["prewarm_ttl_minutes"] = ttl
        if prewarm_enabled:
            payload["prewarm_enabled"] = True
        await save_fixed(actor, payload, fq_id)
        return {"ok": True, "title": title,
                "message": ("开启加速," if prewarm_enabled else "") + f"刷新 {interval_minutes} 分钟 / 存储 {ttl} 分钟"}

    if op == "copy":
        if not connection_id:
            return {"ok": False, "title": title, "message": "未指定目标连接"}
        # 走 get_connection 而不是直接查表:环境变量连接(不在库里)也是合法目标
        conn = await get_connection(actor, connection_id)
        scenic = conn.row.scenic_id if conn.row is not None else None
        # 只换「连接 + 景区」,其余照抄:复制的目的是把同一套问法铺到另一个景区的账号下,
        # 问句/口径/能力必须一致,否则两份数据没法横向对比
        payload = {k: getattr(q, k) for k in _COPY_FIELDS}
        payload.update({"question": q.issue, "connection_id": conn.id})
        if actor.is_group:            # 只有集团角色能指定归属景区;景区角色复制的本来就归自己
            payload["scenic_id"] = scenic or ""
        new = await save_fixed(actor, payload)
        note = ""
        if (q.target or {}).get("asset_id") and conn.id != q.connection_id:
            # 资产(看板/数据集登记)挂在原连接上,换了连接就引用不到 —— 说清楚,而不是等运行时报错
            note = ";目标数据里的资产属于原连接,需在新连接下重新选择"
        return {"ok": True, "title": title, "new_id": new["id"],
                "message": f"已复制到连接「{conn.name}」(景区 {scenic or '集团级'})→ {new['id']}{note}"}

    if op == "insight":
        if not (q.topic or q.domain):
            return {"ok": False, "title": title, "message": "未挂主题标签也没有数据域,无从匹配数据集"}
        hits, topic = await _match_topic_datasets(actor, q)
        if not hits:
            why = ("连接下没有可用数据集" if topic in TOPIC_ALL_DATASETS
                   else f"连接下没有名称匹配「{topic}」的数据集")
            return {"ok": False, "title": title, "message": why + "(可先到数据资产登记)"}
        cids = [h["cube_id"] for h in hits]
        names = [h.get("cube_name") or h["cube_id"] for h in hits]
        target: dict = {"cube_ids": cids, "cube_name": names[0]} if len(cids) > 1 else \
            {"cube_id": cids[0], "cube_name": names[0]}
        # 只改能力与目标数据:问句与口径不动,改的是「这条由谁、按哪几份数据来答」
        await save_fixed(actor, {"capability": "insight", "ask_type": "insight", "target": target}, fq_id)
        shown = "、".join(names[:3]) + (f" 等 {len(names)} 份" if len(names) > 3 else "")
        return {"ok": True, "title": title, "message": f"能力 → 解读,数据集 {len(cids)} 份 → {shown}"}

    if op == "delete":
        await delete_fixed(actor, fq_id)       # 权限 / 不存在由它自己抛,外层按条记失败
        return {"ok": True, "title": title, "message": "已删除"}

    raise ValidationError(f"未知的批量操作 {op}")


async def batch_op(actor: Actor, op: str, ids: list[str], *, interval_minutes: int | None = None,
                   ttl_minutes: int | None = None, prewarm_enabled: bool | None = None,
                   connection_id: str | None = None) -> AsyncIterator[dict]:
    """批量运维固定问题(逐条 yield 事件:start / item / done)。单条失败不中断其余。

    op: set_interval 改加速的刷新/存储时间(prewarm_enabled 三态:True 一并开启 / False 关闭 /
        None 不动开关)/ copy 复制到指定连接(景区跟着连接走)/ insight 切成解读并按主题挂上
        匹配的数据集 / delete 删除。批量执行仍走 /fixed-questions/batch-run。
    """
    if op not in BATCH_OPS:
        raise ValidationError(f"未知的批量操作 {op}(可选:{', '.join(BATCH_OPS)})")
    if not ids:
        raise ValidationError("ids 不能为空")
    todo = ids[:BATCH_MAX_IDS]
    total = len(todo)
    yield {"type": "start", "op": op, "total": total, "ignored": max(0, len(ids) - total),
           "message": f"开始处理 {total} 条" + (f"(超过 {BATCH_MAX_IDS} 条的已忽略)" if len(ids) > total else "")}
    ok = fail = 0
    for i, fq_id in enumerate(todo, 1):
        try:
            r = await _batch_one(actor, op, fq_id, interval_minutes=interval_minutes, ttl_minutes=ttl_minutes,
                                 prewarm_enabled=prewarm_enabled, connection_id=connection_id)
        except Exception as exc:  # noqa: BLE001  单条炸了也要让后面几条继续
            log.warning(f"批量 {op} 第 {i} 条 {fq_id} 失败:{exc}")
            r = {"ok": False, "message": str(exc)}
        ok, fail = ok + (1 if r.get("ok") else 0), fail + (0 if r.get("ok") else 1)
        yield {"type": "item", "index": i, "total": total, "id": fq_id, "title": r.get("title"),
               "ok": bool(r.get("ok")), "message": r.get("message") or "", "new_id": r.get("new_id")}
    yield {"type": "done", "op": op, "total": total, "succeeded": ok, "failed": fail}


async def delete_fixed(actor: Actor, fq_id: str) -> None:
    from cogni_agent.common import permissions as perm

    async with get_sessionmaker()() as db:
        q = await _load_fixed_row(db, fq_id)
        if q is None:
            raise NotFound("固定问题不存在")
        # 删除比修改更严:景区操作员不能删(不可逆),集团级问题景区角色也动不了
        perm.assert_can_delete_obj(actor.role, actor.scenic_id, q.scenic_id, "固定问题")
        await db.delete(q)
        await db.commit()


async def _touch_fixed(fq_id: str, run_id: str, status_: str) -> None:
    """执行留痕。失败不抛 —— 留痕不该挡住业务。"""
    try:
        async with get_sessionmaker()() as db:
            q = await _load_fixed_row(db, fq_id)
            if q is not None:
                q.last_run_id, q.last_run_status, q.last_run_time = run_id, status_, utcnow()
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"固定问题 {fq_id} 执行留痕写入失败(不影响本次执行):{exc}")


async def run_fixed(actor: Actor, fq_id: str, *, params: dict | None = None,
                    target_override: dict | None = None, report_async: bool | None = None,
                    scenic_id: str | None = None, use_cache: bool = True) -> AsyncIterator[dict]:
    """执行固定问题。问题文本支持 {{变量}} 占位,由 params 填充(如 {{month}})。

    scenic_id:同一条固定问题可以换景区问 —— 传了就用传的,没传沿用这条问题绑定的景区。

    use_cache:False = 强制现跑(列表上的「加速」按钮、定时预热任务),
    跑出来的这一份就是下一段时间里大家读到的缓存。
    """
    q = await get_fixed(actor, fq_id)
    if not q.active:
        raise ValidationError(f"固定问题「{q.title}」已停用")
    question = q.issue
    for k, v in (params or {}).items():
        question = question.replace("{{" + str(k) + "}}", str(v))
    # {{截止时间}} 这类由运行时刻决定,调用方本来就给不了 —— 先替换,不算缺参数
    question = render_runtime_vars(question)
    missing = re.findall(r"\{\{\s*(\w+)\s*\}\}", question)
    if missing:
        raise ValidationError(f"固定问题缺少参数:{', '.join(sorted(set(missing)))}")
    target = {**(q.target or {}), **(target_override or {})}
    async for e in run_stream(actor, connection_id=q.connection_id, capability=q.capability, question=question,
                              target=target, mode="fixed", locale=q.locale, fixed_question_id=q.issue_id,
                              report_async=report_async, scenic_id=scenic_id or q.scenic_id,
                              use_cache=use_cache):
        yield e


async def prewarm_now(actor: Actor, fq_id: str, *, scenic_id: str | None = None) -> dict:
    """立刻重新跑一遍这条问题,把结果刷成新的缓存 —— 列表上的「加速」按钮。

    强制现跑(use_cache=False):点「加速」的人要的就是「现在去取最新的数」,
    如果这里还吃缓存,按钮就变成了一个什么都不做的按钮 —— 而且它会成功,
    人看到「已加速」就走了,拿到的还是旧数。

    跑完不另外存:run_stream 本来就把这次运行整份写进了 tc_qbi_runs,
    下一个来问的人按 (问题, 景区, 存储时间) 就能找到它,这就是缓存本身。
    """
    q = await get_fixed(actor, fq_id)
    if not q.active:
        raise ValidationError(f"固定问题「{q.title}」已停用,停用的问题不预热")
    enabled, interval, ttl = prewarm_settings(q)
    if not enabled:
        raise ValidationError(f"固定问题「{q.title}」没有开启加速;请先在编辑里打开加速开关")
    target_scenic = scenic_id or q.scenic_id or actor.scenic_id
    started = time.perf_counter()
    last: dict = {}
    # 报告类一条要跑几分钟,不能让「加速」按钮把 HTTP 请求挂在那里 ——
    # 报告有它自己的收尾轮询(report_watcher),这里创建完就返回
    async for ev in run_fixed(actor, fq_id, scenic_id=target_scenic,
                              report_async=True, use_cache=False):
        if ev.get("type") == "done":
            last = ev
    ok = last.get("status") == "success"
    log.info("加速:问题 %s 景区 %s 现跑完成 status=%s", q.issue_id, target_scenic, last.get("status"))
    return {"ok": ok, "run_id": last.get("run_id"), "status": last.get("status") or "unknown",
            "error": last.get("error"), "error_code": last.get("error_code"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "scenic_id": target_scenic,
            # 让前端能直接写出「已加速,有效期到 xx:xx」,而不是自己再算一遍口径
            "prewarm_interval_minutes": interval, "prewarm_ttl_minutes": ttl}


async def batch_run(actor: Actor, ids: list[str], *, params: dict | None = None) -> list[dict]:
    """批量执行(串行,Quick BI 同一用户的报告任务不允许并发);报告类只创建不等待。"""
    out = []
    for fq_id in ids[:50]:
        done: dict = {"fixed_question_id": fq_id}
        try:
            async for e in run_fixed(actor, fq_id, params=params, report_async=True):
                if e["type"] == "done":
                    done.update({k: e.get(k) for k in ("run_id", "status", "answer", "error", "chat_id",
                                                        "report_url", "duration_ms", "chart_count")})
        except Exception as exc:  # noqa: BLE001
            done.update({"status": "failed", "error": str(exc)})
        out.append(done)
    return out


async def adopt_suggestions(actor: Actor, asset_id: str, questions: list[dict] | None = None) -> list[dict]:
    """把仪表板解析出的候选问题批量存成固定问题。

    生成的问题跟资产**同一个景区**:资产登记在哪个景区(没绑就看它那条连接的景区),问题
    就属于哪个景区。否则集团账号一键生成的是一批「集团级、全景区可见」的问题,而别的景区
    根本看不到这块看板(get_asset 对它们是 NotFound)—— 问题出现在它们的列表里,一点就坏。
    """
    a = await get_asset(actor, asset_id)
    items = questions if questions is not None else (a.meta or {}).get("suggested_questions") or []
    scenic = a.scenic_id
    if not scenic:
        try:
            _v = await get_connection(actor, a.connection_id)
            scenic = _v.row.scenic_id if _v.row is not None else None
        except (NotFound, Forbidden):   # 连接读不到就留空,不强卡「一键生成」
            scenic = None
    created = []
    for i, s in enumerate(items):
        if not isinstance(s, dict) or not str(s.get("question") or "").strip():
            continue
        capability = s.get("capability") or "dataset_qa"
        target = {"asset_id": a.id}
        if s.get("cube_id") and capability == "dataset_qa":
            target = {"cube_id": s["cube_id"], "asset_id": a.id}
        created.append(await save_fixed(actor, {
            "connection_id": a.connection_id, "title": (s.get("title") or s["question"])[:128],
            "question": s["question"], "capability": capability, "target": target,
            "category": a.name[:64], "sort_order": i,
            # 只有集团角色能指定归属景区(save_fixed 的规则);景区角色建的本来就归自己景区
            **({"scenic_id": scenic or ""} if actor.is_group else {}),
        }))
    return created


# ═══════════════════════════ 场景二:对话 ═══════════════════════════

def session_dict(s: QbiSession) -> dict:
    return {"id": s.id, "connection_id": s.connection_id, "title": s.title, "capability": s.capability,
            "target": s.target or {}, "turn_count": s.turn_count, "archived": s.archived, "source": s.source,
            "scenic_id": s.scenic_id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None}


def _session_scope(actor: Actor):
    if actor.source == "api":
        return QbiSession.api_key_id == actor.api_key_id
    return QbiSession.user_id == actor.user_id


async def create_session(actor: Actor, *, connection_id: str | None, capability: str = "auto",
                         target: dict | None = None, title: str | None = None,
                         scenic_id: str | None = None) -> dict:
    if capability not in CAPABILITY_KEYS | {"auto"}:
        raise ValidationError(f"未知能力:{capability}")
    scenic_id = _bind_scenic(actor, scenic_id)
    target = dict(target or {})
    # 会话把目标数据粘住,那么连接也该跟着这份目标数据 —— 否则第一轮就撞「属于另一个连接」
    connection_id = await connection_for_target(actor, target, connection_id)
    view = await get_connection(actor, connection_id)
    if target.get("asset_id"):
        a = await get_asset(actor, target["asset_id"])
        target["asset_kind"] = a.kind
        target.setdefault("asset_name", a.name)
    async with get_sessionmaker()() as db:
        s = QbiSession(connection_id=view.id, capability=capability, target=target, title=(title or "")[:128],
                       user_id=actor.user_id, scenic_id=scenic_id, source=actor.source,
                       api_key_id=actor.api_key_id)
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return session_dict(s)


async def get_session(actor: Actor, session_id: str) -> QbiSession:
    async with get_sessionmaker()() as db:
        s = (await db.execute(select(QbiSession).where(QbiSession.id == session_id, _session_scope(actor)))
             ).scalar_one_or_none()
        if s is None:
            raise NotFound(f"会话不存在:{session_id}")
        return s


async def list_sessions(actor: Actor, limit: int = 50) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(QbiSession).where(_session_scope(actor), QbiSession.archived.is_(False))
        rows = await db.execute(stmt.order_by(QbiSession.updated_at.desc()).limit(limit))
        return [session_dict(s) for s in rows.scalars()]


async def session_detail(actor: Actor, session_id: str) -> dict:
    s = await get_session(actor, session_id)
    async with get_sessionmaker()() as db:
        rows = await db.execute(select(QbiRun).where(QbiRun.session_id == s.id).order_by(QbiRun.created_at))
        return {**session_dict(s), "messages": [run_dict(r) for r in rows.scalars()]}


async def archive_session(actor: Actor, session_id: str) -> None:
    await get_session(actor, session_id)
    async with get_sessionmaker()() as db:
        s = await db.get(QbiSession, session_id)
        s.archived = True
        await db.commit()


async def chat(actor: Actor, session_id: str, question: str, *, capability: str | None = None,
               target: dict | None = None, report_async: bool | None = None) -> AsyncIterator[dict]:
    """对话一轮。目标数据粘在会话上;本轮传入的 target 会合并进会话(后续轮次沿用)。"""
    s = await get_session(actor, session_id)
    if s.archived:
        raise ValidationError("会话已归档")
    merged = {**(s.target or {}), **{k: v for k, v in (target or {}).items() if v not in (None, "")}}
    prev_q = None
    async with get_sessionmaker()() as db:
        last = (await db.execute(select(QbiRun).where(QbiRun.session_id == s.id, QbiRun.status == "success")
                                 .order_by(QbiRun.created_at.desc()).limit(1))).scalar_one_or_none()
        if last is not None and merged.get("carry_context", True):
            prev_q = last.question
    cap_ = capability or (s.capability if s.capability != "auto" else None)
    # 会话上粘着的连接也可能已被删除/停用/改绑别的景区 —— 接着聊**就是一次新发起**,
    # 不是「历史留痕」。现场就是打开一条 9/18 的老会话继续问,撞上
    # 「Quick BI 连接不存在:290987f7-…」;而同样的问题新开一个会话反而正常。
    # 解析出能用的那条就顺手把会话也更正过来,免得每轮都要重算一遍。
    conn_id = await usable_connection_id(actor, s.connection_id, s.scenic_id or actor.scenic_id)
    if not conn_id:
        raise ValidationError(
            f"景区 {s.scenic_id or '(集团级)'} 还没有可用的 Quick BI 连接 —— "
            "这条会话原先用的那条已被删除或停用。请到「工具中心 · Quick BI · 连接配置」"
            "新建一条并绑到这个景区,或换一条会话继续")
    if conn_id != s.connection_id:
        async with get_sessionmaker()() as db:
            row = await db.get(QbiSession, s.id)
            if row is not None:
                row.connection_id = conn_id
                await db.commit()
        log.info("会话 %s 的连接由 %s 更正为 %s", s.id, s.connection_id, conn_id)
    last_cube = None
    started = False
    try:
        async for e in run_stream(actor, connection_id=conn_id, capability=cap_, question=question,
                                  target=merged, mode="chat", session_id=s.id, context_question=prev_q,
                                  report_async=report_async, scenic_id=s.scenic_id):
            started = True
            if e["type"] == "meta" and e.get("cube_id"):
                last_cube = e["cube_id"]
            yield e
    except ZhilvError as exc:
        # 起跑前就失败(连接/资产/参数不对):运行记录是在这些校验**之后**才建的,
        # 所以这一轮压根没落库 —— 界面上看得到红字,一刷新问题和报错一起消失,
        # 像是从来没问过。而这恰恰是最需要留痕的一轮:人要拿着它去查为什么。
        # 这里补一条 failed 记录,让历史与刚才看到的对得上。
        await _record_failed_turn(actor, s, question, merged, conn_id, cap_, str(exc))
        started = True
        yield {"type": "error", "message": str(exc), "code": getattr(exc, "code", None), "kind": "input"}
        yield {"type": "done", "run_id": None, "status": "failed", "error": str(exc)}
    finally:
        if started:
            with contextlib.suppress(BaseException):
                await asyncio.shield(_after_chat_turn(s.id, question, merged, last_cube))


async def _record_failed_turn(actor: Actor, s, question: str, target: dict,
                              conn_id: str | None, capability: str | None, error: str) -> None:
    """起跑前就失败的那一轮也要留一条运行记录。

    不留的话,刷新之后这一轮凭空消失 —— 用户看到的是「我刚才明明问过、还报了错」,
    历史里却一片空白,没法拿着它来查。
    """
    with contextlib.suppress(BaseException):
        async with get_sessionmaker()() as db:
            db.add(QbiRun(connection_id=conn_id or s.connection_id, capability=capability or "auto",
                          mode="chat", question=question,
                          target={k: v for k, v in (target or {}).items() if k != "data"},
                          status="failed", error=error[:2000], error_code="PREFLIGHT_FAILED",
                          session_id=s.id, source=actor.source, user_id=actor.user_id,
                          scenic_id=s.scenic_id, api_key_id=actor.api_key_id,
                          finished_at=utcnow()))
            await db.commit()


async def _after_chat_turn(session_id: str, question: str, merged: dict, last_cube: str | None) -> None:
    async with get_sessionmaker()() as db:
        row = await db.get(QbiSession, session_id)
        if row is None:
            return
        row.turn_count = (row.turn_count or 0) + 1
        if not row.title:
            row.title = question[:128]
        new_target = dict(merged)
        # 智能选表选中的数据集粘在会话上:追问时不必每轮重新选表,也避免上下轮落到不同数据集
        if last_cube and not new_target.get("cube_id") and not new_target.get("file_id") \
                and not new_target.get("asset_id") and new_target.get("sticky_cube", True):
            new_target["cube_id"] = last_cube
        row.target = new_target
        await db.commit()


# ═══════════════════════════ 场景三:报告 ═══════════════════════════

async def start_report(actor: Actor, *, connection_id: str | None, question: str, target: dict | None = None,
                       locale: str | None = None, mode: str = "report", scenic_id: str | None = None) -> dict:
    """创建报告任务并立即返回(不等待生成完成)。"""
    t = {**(target or {}), "wait": False}
    done: dict = {}
    run_id = None
    async for e in run_stream(actor, connection_id=connection_id, capability="report", question=question,
                              target=t, mode=mode, locale=locale, scenic_id=scenic_id):
        if e["type"] == "run":
            run_id = e["run_id"]
        if e["type"] == "done":
            done = e
    return {**(await get_run(actor, run_id)), "done": done} if run_id else done


async def _run_connection(actor: Actor, run: dict) -> ConnView | None:
    """取这条运行记录当时用的连接;连接已被删除/停用则返回 None。

    为什么不直接抛:运行记录是**历史**,而连接是会被删的。主题解读卡片一进去就会把
    「我上次跑过的那次」拉出来刷新一下 —— 连接删了之后,这一刷就是
    「Quick BI 连接不存在:<一串 id>」,而那份报告明明早就跑完、结果就存在库里。
    一条历史记录拉不到连接,最多是「刷不出更新的进度」,不该让整张卡片打不开。
    """
    from cogni_agent.common.errors import ZhilvError

    try:
        return await get_connection(actor.with_trust(run["connection_id"]), run["connection_id"])
    except ZhilvError as exc:
        log.warning("运行记录 %s 的连接 %s 已不可用(%s),回显已存结果",
                    run.get("id"), run.get("connection_id"), exc)
        return None


async def refresh_report(actor: Actor, run_id: str) -> dict:
    """拉取一次报告最新进度;完成则落库。无状态,多实例安全。"""
    run = await get_run(actor, run_id)
    if run["capability"] != "report" or not run.get("chat_id"):
        return run
    if run["status"] != "running":
        return run
    view = await _run_connection(actor, run)
    if view is None:
        # 连接没了,这条记录**永远**不可能再有进展 —— 必须落库收尾,不能只在返回值里说。
        # 只返回不落库的话,它会一直留在「生成中」里,被后台 watcher 每 30 秒捞出来重试一次,
        # 安安静静地循环到天荒地老(原先抛异常也一样,只是多打几行日志)。
        msg = ("这次报告用的 Quick BI 连接已被删除,拉不到最新进度了;"
               "请重新发起一次(会用当前景区可用的连接)")
        await _update_run(run_id, status="failed", finished_at=utcnow(),
                          error=msg, error_code="CONNECTION_DELETED")
        return {**run, "status": "failed", "error": msg}
    async with view.client() as qbi:
        raw = await cap.fetch_report_raw(qbi, run["chat_id"])
    s = cap.summarize_report(raw, base=view.cfg.base, chat_id=run["chat_id"])
    acc = ResultAccumulator()
    acc.extend(s["events"])
    result = _compact(acc.to_result())
    result["progress"] = {"event_count": s["event_count"], "finished": s["finished"]}
    values: dict = {"result": result, "event_count": s["event_count"], "answer": acc.answer or None}
    if s["finished"]:
        values.update(status="success" if s["success"] else "failed", report_url=s["report_url"],
                      error=s["error"], finished_at=utcnow())
    await _update_run(run_id, **values)
    if s["finished"] and run.get("fixed_question_id"):
        await _touch_fixed(run["fixed_question_id"], run_id, values["status"])
    return await get_run(actor, run_id)


async def stream_report(actor: Actor, run_id: str) -> AsyncIterator[dict]:
    """跟踪一个已创建的报告直到结束(断线后可重复调用,从头回放进度)。"""
    run = await get_run(actor, run_id)
    if run["capability"] != "report" or not run.get("chat_id"):
        raise ValidationError("该运行记录不是报告任务或尚未创建成功")
    yield {"type": "run", "run_id": run_id, "capability": "report", "chat_id": run["chat_id"]}
    if run["status"] != "running":
        yield {"type": "done", "run_id": run_id, "status": run["status"], "report_url": run.get("report_url"),
               "error": run.get("error"), "answer": run.get("answer"), "chat_id": run["chat_id"]}
        return
    view = await _run_connection(actor, run)
    if view is None:
        yield {"type": "done", "run_id": run_id, "status": "failed", "report_url": run.get("report_url"),
               "answer": run.get("answer"), "chat_id": run["chat_id"],
               "error": "这次报告用的 Quick BI 连接已被删除,拉不到最新进度了;请重新发起一次"}
        return
    acc = ResultAccumulator()
    final: dict = {}
    try:
        async with view.client() as qbi:
            async for e in cap.poll_report(qbi, run["chat_id"], poll_interval=float(_setting("report_poll_seconds", 10)),
                                           max_wait=float(_setting("report_max_wait_seconds", 1800))):
                acc.add(e)
                if e["type"] == "report" and e.get("finished"):
                    final = e
                if e["type"] != "report_progress":
                    yield e
    except QuickBIError as exc:
        yield {"type": "error", "message": str(exc), "code": exc.code, "kind": exc.kind}
    if final:
        st = "success" if final.get("success") else "failed"
        await _update_run(run_id, status=st, report_url=final.get("report_url"), error=final.get("error"),
                          answer=acc.answer or None, result=_compact(acc.to_result()), finished_at=utcnow())
        if run.get("fixed_question_id"):
            await _touch_fixed(run["fixed_question_id"], run_id, st)
        yield {"type": "done", "run_id": run_id, "status": st, "report_url": final.get("report_url"),
               "error": final.get("error"), "answer": acc.answer, "chat_id": run["chat_id"]}
    else:
        yield {"type": "done", "run_id": run_id, "status": "running", "chat_id": run["chat_id"]}


def overview() -> dict:
    env = env_connection()
    ak, sk = _trial_pair()
    return {
        "capabilities": [{"key": k, **v} for k, v in cap.CAPABILITIES.items()],
        "env_connection": connection_dict(env) if env else None,
        "trial_configured": bool(ak and sk),
        "limits": {"max_file_bytes": fs.MAX_FILE_BYTES, "max_insight_chars": fs.MAX_INSIGHT_CHARS,
                   "qa_suffixes": sorted(fs.QA_SUFFIXES), "report_suffixes": sorted(fs.REPORT_SUFFIXES)},
        "scenarios": [
            {"key": "fixed", "label": "固定问题", "desc": "预置问题 × 能力 × 目标数据,一键/批量执行"},
            {"key": "chat", "label": "对话场景", "desc": "会话粘住目标数据,多轮追问,自动按意图路由能力"},
            {"key": "report", "label": "报告生成", "desc": "异步生成报告,可引用上传文件或仪表板数据"},
        ],
    }


async def validate_tool_refs(actor: Actor, cfg: dict) -> tuple[str, set[str]]:
    """校验 quickbi 工具配置里的引用对 actor 可访问,返回 (连接 id, 可信 id 集合)。

    创建/编辑工具时用编辑者身份校验;执行时用工具所有者身份再校验一次(配置可能被他人改过、
    所有者权限可能已变化)。模型运行时传入的 file_id / dashboard 等不在此列。
    """
    cfg = cfg or {}
    refs: set[str] = set()
    view = await get_connection(actor, cfg.get("connection_id") or None)
    refs.add(view.id)
    target = cfg.get("target") or {}
    if not isinstance(target, dict):
        raise ValidationError("target 必须是对象")
    if target.get("asset_id"):
        a = await get_asset(actor, str(target["asset_id"]))
        await _same_connection(view, a.connection_id, f"资产「{a.name}」")
        refs.add(a.id)
    for fid in list(target.get("file_ids") or []) + ([target["file_id"]] if target.get("file_id") else []):
        f = await _load_file(actor, str(fid))
        await _same_connection(view, f.connection_id, f"文件「{f.file_name}」")
        refs.add(f.id)
    if cfg.get("fixed_question_id"):
        q = await get_fixed(actor, str(cfg["fixed_question_id"]))
        # 新旧两个 ID 都加进授权引用:工具配置里存的可能是老表 UUID(0079 之前建的),
        # 而运行时按 issue_id 校验 —— 只加一个,老工具会在鉴权那一步被判成越权
        refs.add(q.issue_id)
        if q.source_question_id:
            refs.add(q.source_question_id)
    cap_ = cfg.get("capability") or "dataset_qa"
    if cap_ not in CAPABILITY_KEYS | {"auto"}:
        raise ValidationError(f"未知能力:{cap_}")
    return view.id, refs
