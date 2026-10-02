"""
M7-08 / 架构图 ⑦:Casbin RBAC 策略引擎——角色到权限的单一事实源。

**现状与定位(要说清楚,避免误解)**:平台里绝大多数鉴权目前仍由各 service 里的
显式角色判断完成(如 `_require_admin`、`test_permission`)。这些判断是有效且经过
验证的,把它们一次性换成策略引擎是一次风险不小的重构。所以本模块采取渐进路线:

1. **先成为"能力清单"的权威来源**:`permissions_for(role)` 给出某个角色能做什么,
   由 `/api/auth/permissions` 暴露给前端。前端据此决定菜单/按钮的可见性,而不是像
   以前那样在 Sidebar 里硬编码一份角色白名单——硬编码的那份和后端的判断迟早会漂移,
   而且改权限要同时改两处。
2. **再逐步接管服务端判定**:`can()` 已可用,新增的判定点直接走它;既有的显式判断
   保留为纵深防御,后续按模块迁移。

**前端可见性 ≠ 服务端鉴权**:前端隐藏按钮只是体验优化,真正的拦截永远在服务端。
两者都读同一份策略,是为了不再出现"菜单能看见但一点就 403"这种不一致。
"""
from __future__ import annotations

import asyncio
import functools
import threading
from pathlib import Path

import casbin
import casbin_sqlalchemy_adapter

from cogni_agent.config import get_settings
from cogni_agent.common.logging import get_logger

log = get_logger("governance_event_bus_service.policy")

_MODEL_PATH = Path(__file__).resolve().parents[2] / "config" / "casbin_model.conf"
_enforcer: casbin.Enforcer | None = None
_enforcer_lock = threading.Lock()

# ── 权限域常量 ───────────────────────────────────────────────────────────
DOMAIN_ADMIN = "admin"   # 管理端(Web 后台)
DOMAIN_APP = "app"       # APP 端

# ── 角色继承(高权限继承低权限)────────────────────────────────────────────
# 写成继承而不是给每个角色重复罗列权限:加一条新权限时只要挂在最低的那个角色上,
# 上面的角色自动获得,不会出现"给 group_admin 加了忘了给 super_admin 加"。
ROLE_INHERITANCE = [
    ("group_developer", "group_viewer"),   # 开发继承只读
    ("group_admin", "group_developer"),    # 集团管理员继承开发
    ("super_admin", "group_admin"),        # 超管继承集团管理员
    ("scenic_admin", "scenic_operator"),   # 景区管理员继承景区操作员
]

# ── 基线策略 ─────────────────────────────────────────────────────────────
# obj 命名约定:<模块>:<资源>,act 取 read / write / delete / execute / approve。
# 只写"该角色新增的权限",继承来的不重复写。
# 四元组:(sub, obj, act, domain) —— domain 区分管理端/APP 端。
BASE_POLICIES = [
    # 景区操作员:能用操练台、看自己景区的任务,不能改任何定义
    ("scenic_operator", "console", "execute", DOMAIN_ADMIN),
    ("scenic_operator", "dashboard", "read", DOMAIN_ADMIN),
    ("scenic_operator", "audit:task", "read", DOMAIN_ADMIN),
    ("scenic_operator", "tool:*", "read", DOMAIN_ADMIN),
    ("scenic_operator", "skill:*", "read", DOMAIN_ADMIN),
    ("scenic_operator", "agent:*", "read", DOMAIN_ADMIN),
    ("scenic_operator", "knowledge:*", "read", DOMAIN_ADMIN),
    # 景区管理员:多了审批权和本景区的知识维护
    ("scenic_admin", "audit:approval", "approve", DOMAIN_ADMIN),
    ("scenic_admin", "knowledge:*", "write", DOMAIN_ADMIN),
    # 集团只读:能看全部模块,但不能写
    ("group_viewer", "console", "execute", DOMAIN_ADMIN),
    ("group_viewer", "dashboard", "read", DOMAIN_ADMIN),
    ("group_viewer", "tool:*", "read", DOMAIN_ADMIN),
    ("group_viewer", "skill:*", "read", DOMAIN_ADMIN),
    ("group_viewer", "agent:*", "read", DOMAIN_ADMIN),
    ("group_viewer", "knowledge:*", "read", DOMAIN_ADMIN),
    ("group_viewer", "audit:*", "read", DOMAIN_ADMIN),
    # 集团开发:1~5 模块全权限,但系统中心不可见(README 第三章明确)
    ("group_developer", "tool:*", "write", DOMAIN_ADMIN),
    ("group_developer", "tool:*", "delete", DOMAIN_ADMIN),
    ("group_developer", "skill:*", "write", DOMAIN_ADMIN),
    ("group_developer", "agent:*", "write", DOMAIN_ADMIN),
    ("group_developer", "agent:*", "publish", DOMAIN_ADMIN),
    ("group_developer", "knowledge:*", "write", DOMAIN_ADMIN),
    ("group_developer", "audit:eval", "write", DOMAIN_ADMIN),
    # 集团管理员:加上系统中心与审批
    ("group_admin", "system:*", "read", DOMAIN_ADMIN),
    ("group_admin", "system:*", "write", DOMAIN_ADMIN),
    ("group_admin", "user:*", "read", DOMAIN_ADMIN),
    ("group_admin", "audit:approval", "approve", DOMAIN_ADMIN),
    ("group_admin", "audit:log", "read", DOMAIN_ADMIN),
    # 超管:全量兜底
    ("super_admin", "*", "*", DOMAIN_ADMIN),
]

# APP 端基线策略
# 五大数据导航 tab 菜单的可见性权限(均为 read):
#   数据概览(data_overview)  票务(ticket_manage)
#   客流(passenger_flow)      舆情(public_opinion)
#   OTA直销(ota_direct_sales)
#
# 角色分配:
#   - 景区操作员 → 5 个 tab 可见
#   - 景区管理员 → 继承操作员(5 个 tab 均可见)
APP_BASE_POLICIES: list[tuple[str, str, str, str]] = [
    # ── 景区操作员:5 个 tab 菜单(景区管理员通过 ROLE_INHERITANCE 继承) ──
    ("scenic_operator", "app:data_overview", "read", DOMAIN_APP),
    ("scenic_operator", "app:ticket_manage", "read", DOMAIN_APP),
    ("scenic_operator", "app:passenger_flow", "read", DOMAIN_APP),
    ("scenic_operator", "app:public_opinion", "read", DOMAIN_APP),
    ("scenic_operator", "app:ota_direct_sales", "read", DOMAIN_APP),
]

# 前端菜单 -> 所需权限。放在后端是为了让"菜单可见性"和"服务端鉴权"共用同一份策略。
MENU_PERMISSIONS = {
    "dashboard": ("dashboard", "read"),
    "console": ("console", "execute"),
    "tools": ("tool:list", "read"),
    "knowledge": ("knowledge:list", "read"),
    "audit": ("audit:task", "read"),
    "user_center": ("user:*", "read"),
    "system": ("system:settings", "read"),
    # 链路可视化是排查工具,与系统中心同级:只有集团管理员及以上可见。
    # 不登记的话 permissions.menus 里没有这个键,前端 `!== false` 的兜底会让所有
    # 角色都看见菜单——"前端兜底可见"和"后端策略决定"是两回事,前者迟早漂移。
    "graph_viz": ("system:settings", "read"),
}

# APP 端 tab 菜单 -> 所需权限（domain=app 域）
APP_MENU_PERMISSIONS = {
    "data_overview": ("app:data_overview", "read"),
    "ticket_manage": ("app:ticket_manage", "read"),
    "passenger_flow": ("app:passenger_flow", "read"),
    "public_opinion": ("app:public_opinion", "read"),
    "ota_direct_sales": ("app:ota_direct_sales", "read"),
}


def get_enforcer() -> casbin.Enforcer:
    global _enforcer
    if _enforcer is None:
        if not _MODEL_PATH.exists():
            raise RuntimeError(f"Casbin 模型文件不存在:{_MODEL_PATH}")
        # Casbin 的 SQLAlchemy adapter 是同步的,不能用 asyncpg 驱动。
        # 显式切到 psycopg 同步驱动。用 URL.create 自动处理密码特殊字符转义。
        from sqlalchemy.engine import URL

        db = get_settings().get("database", default={})
        sync_dsn = URL.create(
            drivername="postgresql+psycopg",
            username=db["user"],
            password=db["password"],
            host=db["host"],
            port=int(db["port"]),
            database=db["db"],
        ).render_as_string(hide_password=False)
        adapter = casbin_sqlalchemy_adapter.Adapter(sync_dsn)
        _enforcer = casbin.Enforcer(str(_MODEL_PATH), adapter)
    return _enforcer


def _sync_can(role: str, resource: str, action: str, domain: str = DOMAIN_ADMIN) -> bool:
    try:
        with _enforcer_lock:
            return bool(get_enforcer().enforce(role, resource, action, domain))
    except Exception:  # noqa: BLE001 - 策略引擎不可用时不能把整个请求打挂
        log.exception(f"策略判定失败(role={role} obj={resource} act={action} domain={domain}),按拒绝处理")
        return False


async def can(role: str, resource: str, action: str, domain: str = DOMAIN_ADMIN) -> bool:
    """异步判定。

    Casbin 的 enforcer 与其 SQLAlchemy adapter 都是同步阻塞的,直接在 async 请求里
    调用会卡住事件循环(策略量大时更明显)。丢到线程池执行。

    domain: 权限域, "admin"=管理端 / "app"=APP 端, 默认 admin。
    """
    return await asyncio.to_thread(_sync_can, role, resource, action, domain)


async def permissions_for(role: str, domain: str = DOMAIN_ADMIN) -> dict:
    """返回该角色在指定域下的能力清单,供前端决定菜单/按钮可见性。

    domain: "admin"=管理端 / "app"=APP 端。
    管理端前端调用 /auth/permissions（默认 admin），APP 端调用 /auth/permissions?domain=app。
    """
    def _compute() -> dict:
        if domain == DOMAIN_APP:
            menus = {name: _sync_can(role, obj, act, domain) for name, (obj, act) in APP_MENU_PERMISSIONS.items()}
            return {"role": role, "menus": menus, "capabilities": {}}
        menus = {name: _sync_can(role, obj, act, domain) for name, (obj, act) in MENU_PERMISSIONS.items()}
        caps = {
            "tool.write": _sync_can(role, "tool:x", "write", domain),
            "tool.delete": _sync_can(role, "tool:x", "delete", domain),
            "skill.write": _sync_can(role, "skill:x", "write", domain),
            "agent.write": _sync_can(role, "agent:x", "write", domain),
            "agent.publish": _sync_can(role, "agent:x", "publish", domain),
            "knowledge.write": _sync_can(role, "knowledge:x", "write", domain),
            "approval.approve": _sync_can(role, "audit:approval", "approve", domain),
            "eval.write": _sync_can(role, "audit:eval", "write", domain),
            "system.write": _sync_can(role, "system:settings", "write", domain),
            "log.read": _sync_can(role, "audit:log", "read", domain),
        }
        return {"role": role, "menus": menus, "capabilities": caps}

    return await asyncio.to_thread(_compute)


def seed_policies(*, reset: bool = False) -> dict:
    """把基线策略写入数据库(幂等)。

    reset=True 时先清空——用于"策略改了要重新灌"的场景。默认不清空,避免把运维在
    生产上手工加的策略洗掉。
    """
    e = get_enforcer()
    if reset:
        e.clear_policy()
        e.save_policy()
    e.load_policy()

    added_p = 0
    for sub, obj, act, domain in BASE_POLICIES:
        if not e.has_policy(sub, obj, act, domain):
            e.add_policy(sub, obj, act, domain)
            added_p += 1
    for sub, obj, act, domain in APP_BASE_POLICIES:
        if not e.has_policy(sub, obj, act, domain):
            e.add_policy(sub, obj, act, domain)
            added_p += 1
    added_g = 0
    for child, parent in ROLE_INHERITANCE:
        if not e.has_grouping_policy(child, parent):
            e.add_grouping_policy(child, parent)
            added_g += 1
    e.save_policy()
    log.info(f"Casbin 策略已就绪:新增 {added_p} 条权限、{added_g} 条角色继承")
    return {"policies_added": added_p, "grouping_added": added_g,
            "total_policies": len(e.get_policy()), "total_grouping": len(e.get_grouping_policy())}


async def ensure_seeded() -> None:
    """启动时确保策略存在且格式正确。

    策略表为空 = 所有判定都会拒绝, 必须在启动阶段兜住。
    同时检测 3元组→4元组升级: 老版策略不含 domain 字段, 前端权限管理页会显示为空。
    """
    def _run() -> None:
        e = get_enforcer()
        e.load_policy()
        policies = e.get_policy()

        if not policies:
            log.warning("Casbin 策略表为空,正在灌入基线策略")
            seed_policies()
            return

        # 3元组→4元组迁移: 模型中新增了 domain 字段后, 旧策略只有 (sub,obj,act),
        # admin_list_permissions 的 len(p)>=4 过滤会把它们全部排除 → 前端显示为空。
        # 这里一次性重置为 4元组格式。
        if all(len(p) < 4 for p in policies):
            log.warning("检测到旧格式策略(3元组,不含domain),正在重置为4元组基线策略")
            seed_policies(reset=True)

    try:
        await asyncio.to_thread(_run)
    except Exception:  # noqa: BLE001 - 策略初始化失败不阻止启动(既有显式鉴权仍在生效)
        log.exception("Casbin 策略初始化失败,菜单可见性接口将退化为全部拒绝")
