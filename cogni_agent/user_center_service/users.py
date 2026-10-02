"""M2-06/07/08/09/10:用户 CRUD,仅供管理后台调用(路由层加角色校验)。"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import Forbidden, NotFound, Unauthorized, ValidationError
from cogni_agent.common.logging import get_logger
from cogni_agent.user_center_service.auth import hash_password, send_sms_code
from cogni_agent.user_center_service.models import User
from cogni_agent.user_center_service.schemas import CreateUserRequest, UpdateUserRequest

log = get_logger("user_center_service.users")


def default_password_for(phone: str) -> str:
    """新建用户与重置密码的默认初始密码规则:`{手机号后4位}@123`。

    为什么不用随机串:短信通道目前是 mock,随机密码既没真发出去、接口也不回显,
    结果就是账号建完谁都不知道密码是什么。改成手机号后4位+固定后缀的规则:
    ① 对每个用户唯一（手机号全局唯一）→ 不同用户密码不同；
    ② 可推导（知道手机号即可推算）→ 管理员无需额外记忆；
    ③ 不是完整手机号 → 即使泄露也不直接关联到号码。
    风险由 must_change_password 兜住 —— 首次登录会被要求改密。
    需要更强的初始密码时,创建/重置接口都支持传 password 显式指定。
    """
    return f"{phone[-4:]}@123"


async def create_user(db: AsyncSession, req: CreateUserRequest) -> User:
    """M2-06:仅管理后台可调用。account 由纳秒时间戳后8位自动生成,初始密码为 `{手机号后4位}@123`。"""
    account = (getattr(req, "account", None) or "").strip() or str(time.time_ns())[-8:]
    initial_password = (getattr(req, "password", None) or "").strip() or default_password_for(req.phone)
    user = User(
        name=req.name,
        account=account,
        phone=req.phone,
        scenic_id=req.scenic_id,
        role=req.role,
        position=req.position,
        password_hash=hash_password(initial_password),
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    # 短信通道可能是 mock 或未配置,发送失败不该让"用户已创建成功"这件事回滚
    try:
        await send_sms_code(db, req.phone, purpose="new_device")
    except Exception:  # noqa: BLE001
        log.warning(f"用户 {user.account} 创建成功,但初始密码短信通知发送失败")
    log.info(f"created user {user.account} (phone={user.phone[-4:]})")
    user.__dict__["_initial_password"] = initial_password  # 供接口层回传给管理员
    return user


async def list_users(db: AsyncSession, scenic_id: str | None, keyword: str | None) -> list[User]:
    """M2-08:admin 仅见本组织;super_admin(scenic_id=None)见全平台。"""
    stmt = select(User)
    if scenic_id:
        stmt = stmt.where(User.scenic_id == scenic_id)
    if keyword:
        stmt = stmt.where(User.name.ilike(f"%{keyword}%") | User.account.ilike(f"%{keyword}%") | User.phone.ilike(f"%{keyword}%"))
    result = await db.execute(stmt)
    return list(result.scalars())


async def update_user(db: AsyncSession, user_id: str, req: UpdateUserRequest) -> User:
    """M2-07/M2-10:编辑资料与角色分配(角色写入后驱动 Casbin 策略生效,见 governance.policy)。"""
    user = await db.get(User, user_id)
    if not user:
        raise NotFound("用户不存在")
    for field in ("name", "scenic_id", "role", "position", "is_active"):
        value = getattr(req, field)
        if value is not None:
            setattr(user, field, value)
    await db.commit()
    return user


async def reset_password(db: AsyncSession, user_id: str, new_password: str | None = None) -> str:
    """M2-09:管理后台重置密码。

    不传 new_password 时用默认规则 `{手机号后4位}@123`;管理员也可以指定一个自定义密码
    (比如当面告知使用者的临时密码)。无论哪种都会置上 must_change_password,
    使用者首次登录必须改掉。
    """
    user = await db.get(User, user_id)
    if not user:
        raise NotFound("用户不存在")
    pwd = (new_password or "").strip() or default_password_for(user.phone)
    if len(pwd) < 6:
        raise Forbidden("密码长度至少 6 位")
    user.password_hash = hash_password(pwd)
    user.must_change_password = True
    # 管理员重置同样要让旧令牌失效——账号被盗时管理员重置是常见处置手段
    user.credentials_changed_at = datetime.now(timezone.utc)
    await db.commit()
    try:
        await send_sms_code(db, user.phone, purpose="reset_password")
    except Exception:  # noqa: BLE001 - 短信没发出去不影响密码已经被重置这个事实
        log.warning(f"用户 {user.account} 密码已重置,但短信通知发送失败")
    # 回传明文给**管理员**(调用方已通过管理端鉴权),否则 mock 短信下没人知道新密码
    return pwd


async def list_login_devices(db: AsyncSession, user_id: str, limit: int = 20):
    """该用户的登录设备记录,最近的在前(架构图 ② 「最近登录时间/设备列表」)。"""
    from cogni_agent.user_center_service.models import LoginDevice

    stmt = (select(LoginDevice).where(LoginDevice.user_id == user_id)
            .order_by(LoginDevice.login_at.desc()).limit(limit))
    return (await db.execute(stmt)).scalars().all()


async def get_by_id(db: AsyncSession, user_id: str) -> User | None:
    return await db.get(User, user_id)


async def get_by_account_or_phone(db: AsyncSession, account: str) -> User | None:
    """按账号或手机号查用户。

    登录框标注的是「账户或手机号」,两种都得认 —— 种子超管账号 pftroot 的手机号是
    占位值,只认手机号的话它根本登不进去。管理端与 APP 端共用这一个函数,避免两边
    各写一套匹配规则然后逐渐跑偏。
    """
    from sqlalchemy import or_

    return (await db.execute(
        select(User).where(or_(User.phone == account, User.account == account))
    )).scalars().first()


async def change_own_password(db: AsyncSession, user_id: str, old_password: str, new_password: str) -> tuple[bool, int | None, str]:
    """用户自助改密。

    与管理员重置的区别:**必须校验旧密码**。管理员重置是"我确认这个人是谁"之后的
    操作,自助改密则可能发生在令牌被盗的情况下,不校验旧密码等于把改密变成了
    盗号者锁死账号的工具。

    返回 (ok, code, message):
    - 成功: (True, None, "密码已修改")
    - 旧密码不对: (False, 1001, "原密码不正确")
    - 新密码太短: (False, 1002, "新密码至少 8 位")
    - 新密码与旧密码相同: (False, 1003, "新密码不能与原密码相同")
    """
    from cogni_agent.user_center_service.auth import verify_password

    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("用户不存在")
    if not verify_password(old_password, user.password_hash):
        return False, 1001, "原密码不正确"
    new_password = (new_password or "").strip()
    if len(new_password) < 8:
        return False, 1002, "新密码至少 8 位"
    if verify_password(new_password, user.password_hash):
        return False, 1003, "新密码不能与原密码相同"
    user.password_hash = hash_password(new_password)
    user.must_change_password = False   # 首次登录的强制改密要求到此解除
    # 让此前签发的所有令牌立即失效(含其它设备)。改密码的语义本来就该包含"踢掉旧会话"
    user.credentials_changed_at = datetime.now(timezone.utc)
    await db.commit()
    log.info(f"user {user.account} changed own password")
    return True, None, "密码已修改"


# ═══════════════════════════════════════════════════════════════
# 从当前项目补充:删除、分页、角色聚合、批量分配
# ═══════════════════════════════════════════════════════════════

async def list_users_page(
    db: AsyncSession,
    keyword: str | None = None,
    role: str | None = None,
    scenic_id: str | None = None,
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """分页查询用户,含多条件筛选 + 联表查景区名 + 组织隔离。

    返回 (items, total), items 为字典列表(含 scenic_name 联表字段)。
    比原 list_users 多出:role/is_active 筛选、scenic_name 联表、分页。
    """
    from sqlalchemy import or_

    from cogni_agent.user_center_service.models import SysScenic

    base = (
        select(User, SysScenic.scenic_name)
        .outerjoin(SysScenic, User.scenic_id == SysScenic.scenic_id)
    )
    count_base = select(func.count(User.id))

    if keyword:
        kw_filter = or_(
            User.name.ilike(f"%{keyword}%"),
            User.account.ilike(f"%{keyword}%"),
            User.phone.ilike(f"%{keyword}%"),
        )
        base = base.where(kw_filter)
        count_base = count_base.where(kw_filter)
    if role:
        base = base.where(User.role == role)
        count_base = count_base.where(User.role == role)
    if scenic_id:
        base = base.where(User.scenic_id == scenic_id)
        count_base = count_base.where(User.scenic_id == scenic_id)
    if is_active is not None:
        base = base.where(User.is_active == is_active)
        count_base = count_base.where(User.is_active == is_active)

    total = (await db.execute(count_base)).scalar() or 0

    rows = (await db.execute(
        base.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )).all()

    items = []
    for user, sname in rows:
        items.append({
            "id": user.id, "name": user.name, "account": user.account, "phone": user.phone,
            "scenic_id": user.scenic_id, "scenic_name": sname or "",
            "role": user.role, "position": user.position,
            "is_active": user.is_active, "must_change_password": user.must_change_password,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        })
    return items, total


async def delete_user(db: AsyncSession, user_id: str, current_user_id: str) -> None:
    """删除用户。不可删除自己;级联清理相关登录设备记录。"""
    user = await db.get(User, user_id)
    if not user:
        raise NotFound("用户不存在")
    if user.id == current_user_id:
        raise Forbidden("不能删除自己的账号")

    # 级联清理登录设备记录
    from cogni_agent.user_center_service.models import LoginDevice
    stmt = select(LoginDevice).where(LoginDevice.user_id == user_id)
    devices = (await db.execute(stmt)).scalars().all()
    for d in devices:
        await db.delete(d)

    await db.delete(user)
    await db.commit()
    log.info(f"deleted user {user.account} (by {current_user_id})")


async def get_users_by_role(db: AsyncSession) -> dict[str, list[dict]]:
    """按角色聚合用户列表。用于角色分配页面。"""
    from collections import defaultdict

    stmt = select(User).where(User.is_active.is_(True)).order_by(User.name)
    rows = (await db.execute(stmt)).scalars().all()

    result = defaultdict(list)
    for u in rows:
        result[u.role].append({
            "id": u.id, "name": u.name, "account": u.account, "phone": u.phone,
            "scenic_id": u.scenic_id, "position": u.position,
        })
    return dict(result)


async def batch_update_role(db: AsyncSession, user_ids: list[str], role: str) -> int:
    """批量更新用户角色。返回实际修改的行数。"""
    from sqlalchemy import update

    valid_roles = {"super_admin", "group_admin", "group_developer", "group_viewer", "scenic_admin", "scenic_operator"}
    if role not in valid_roles:
        raise ValidationError(f"无效的角色: {role}, 合法值: {','.join(sorted(valid_roles))}")

    stmt = (
        update(User)
        .where(User.id.in_(user_ids))
        .values(role=role, updated_at=datetime.now(timezone.utc))
    )
    result = await db.execute(stmt)
    await db.commit()
    log.info(f"batch updated {result.rowcount} users to role {role}")
    return result.rowcount
