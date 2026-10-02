# -*- coding: utf-8 -*-
"""角色 × 景区 的统一权限判定。

**一句话规则**(与组织管理里的角色一一对应):

  super_admin / group_admin / group_developer   集团三角色,不受限:增 / 改 / 删,跨景区
  group_viewer                                  集团只读:只能看
  scenic_admin                                  本景区全部内容:增 / 改 / 删(仅限本景区)
  scenic_operator                               本景区:增 / 改,**不能删**

两条容易被忽略、但每次出问题都在它们身上的细则:

1. **景区角色动不了集团级内容**。scenic_id 为空的资源(集团配的默认连接、通用问题模板)
   对景区用户是「能用不能改」—— 允许改的话,一个景区的人改一次,所有景区跟着变。
2. **判定按景区,不按创建者**。原先是「谁建的谁能改」,于是同景区的另一个管理员接手时
   动不了前任建的东西,只能找超管 —— 而景区管理员本来就该管得了本景区的全部内容。

判定集中在这里,是因为它散在各处时必然会长歪:同一条规则在 Quick BI 写一遍、
在 APP 运营写一遍、在知识中心再写一遍,过几个月就会有一处漏掉 delete 或漏掉景区。
"""
from __future__ import annotations

from cogni_agent.common.errors import Forbidden, NotFound

# 集团三角色:不受景区限制,增删改全开
GROUP_MANAGE_ROLES = frozenset({"super_admin", "group_admin", "group_developer"})
# 集团只读
GROUP_READONLY_ROLES = frozenset({"group_viewer"})
# 景区管理员:本景区内增删改
SCENIC_MANAGE_ROLES = frozenset({"scenic_admin"})
# 景区操作员:本景区内增改,不能删
SCENIC_WRITE_ROLES = frozenset({"scenic_operator"})

GROUP_ROLES = GROUP_MANAGE_ROLES | GROUP_READONLY_ROLES
SCENIC_ROLES = SCENIC_MANAGE_ROLES | SCENIC_WRITE_ROLES

ROLE_LABELS = {
    "super_admin": "超级管理员", "group_admin": "集团管理员",
    "group_developer": "集团开发者", "group_viewer": "集团只读",
    "scenic_admin": "景区管理员", "scenic_operator": "景区操作员",
}


def is_group_role(role: str | None) -> bool:
    """集团级身份(含只读)。集团账号不绑景区,看到的是全部景区的内容。"""
    return (role or "") in GROUP_ROLES


def can_manage_all(role: str | None) -> bool:
    """不受景区限制、可增删改 —— 只有集团三角色。"""
    return (role or "") in GROUP_MANAGE_ROLES


def can_create(role: str | None) -> bool:
    """能不能新增/修改。集团只读不能,其余都能(景区角色受景区范围约束)。"""
    return (role or "") in (GROUP_MANAGE_ROLES | SCENIC_MANAGE_ROLES | SCENIC_WRITE_ROLES)


def can_delete(role: str | None) -> bool:
    """能不能删。景区操作员不能删 —— 删除不可逆,留给景区管理员。"""
    return (role or "") in (GROUP_MANAGE_ROLES | SCENIC_MANAGE_ROLES)


def assert_can_write(role: str | None, what: str = "内容") -> None:
    if not can_create(role):
        raise Forbidden(f"{ROLE_LABELS.get(role or '', role or '当前角色')}为只读角色,不能新增或修改{what}")


def assert_can_delete(role: str | None, what: str = "内容") -> None:
    if not can_delete(role):
        if (role or "") in SCENIC_WRITE_ROLES:
            raise Forbidden(f"景区操作员不能删除{what};删除请联系本景区的景区管理员")
        raise Forbidden(f"{ROLE_LABELS.get(role or '', role or '当前角色')}不能删除{what}")


def assert_in_scope(role: str | None, user_scenic: str | None,
                    obj_scenic: str | None, what: str = "内容") -> None:
    """景区角色只能动本景区的东西;集团角色不受限。

    obj_scenic 为空 = 集团级内容:景区角色**能用但不能改** ——
    那是所有景区共用的一份,改一次全场跟着变。
    """
    if can_manage_all(role):
        return
    if is_group_role(role):          # group_viewer:前面的 write/delete 判定已经拦下
        return
    mine = user_scenic or None
    if not mine:
        raise Forbidden(f"当前账号未绑定景区,无法操作{what};请联系管理员在「系统设置 · 组织管理」绑定")
    if obj_scenic is None:
        raise Forbidden(f"这是集团级{what}(所有景区共用),景区角色只能使用、不能修改")
    if obj_scenic != mine:
        raise Forbidden(f"该{what}属于景区 {obj_scenic},当前账号绑定的是 {mine},无权操作")


def assert_can_write_obj(role: str | None, user_scenic: str | None,
                         obj_scenic: str | None, what: str = "内容") -> None:
    """新增/修改某个具体对象:先看角色,再看景区范围。"""
    assert_can_write(role, what)
    assert_in_scope(role, user_scenic, obj_scenic, what)


def assert_can_delete_obj(role: str | None, user_scenic: str | None,
                          obj_scenic: str | None, what: str = "内容") -> None:
    """删除某个具体对象:先看角色能不能删,再看景区范围。"""
    assert_can_delete(role, what)
    assert_in_scope(role, user_scenic, obj_scenic, what)


# ══════════════════════════════════════════════════════════════════════
# 读:能看见什么
#
# 写门禁管的是「能不能改」,但「后台列表里出现了别的景区的东西」同样是越权 ——
# 而且更隐蔽:没有报错,只是多看见了。下面两个是读侧的统一口径。
#
# 口径与写侧一致,只松一处:**集团级内容(scenic_id 为空)景区角色看得见**
# —— 那本来就是配给所有景区共用的(默认连接、通用问题模板、集团白名单)。
# 写侧禁止他们改,读侧必须让他们看见,否则「能用不能改」的「能用」就无从谈起。
# ══════════════════════════════════════════════════════════════════════

def read_scope(role: str | None, user_scenic: str | None) -> str | None:
    """这个账号的读取范围:返回景区 ID = 只能看这个景区(+集团级);None = 看全部。"""
    if is_group_role(role):
        return None
    return (user_scenic or "").strip() or None


def scenic_read_clause(role: str | None, user_scenic: str | None, column):
    """拼进 select() 的 WHERE:景区角色只看本景区 + 集团级,集团角色不加条件。

    用法:`stmt = stmt.where(perm.scenic_read_clause(user.role, user.scenic_id, Model.scenic_id))`

    没绑景区的景区账号(scenic_id 为空)只看得到集团级内容 —— 不是看全部。
    不这么写的话,一个还没绑景区的新账号反而权限最大。
    """
    import sqlalchemy as _sa

    scope = read_scope(role, user_scenic)
    if scope is None and is_group_role(role):
        return _sa.true()
    if scope is None:                      # 景区角色但没绑景区:只剩集团级
        return column.is_(None)
    return _sa.or_(column == scope, column.is_(None))


def can_read_obj(role: str | None, user_scenic: str | None, obj_scenic: str | None) -> bool:
    """单条记录:这个账号看不看得见。集团级(obj_scenic 为空)对谁都可见。"""
    if is_group_role(role):
        return True
    if obj_scenic is None:
        return True
    return bool(user_scenic) and obj_scenic == user_scenic


def assert_can_read(role: str | None, user_scenic: str | None,
                    obj_scenic: str | None, what: str = "内容") -> None:
    """详情接口的门禁。

    这类接口最容易漏:列表筛得好好的,详情却是一句 db.get(Model, id) ——
    拿到 ID 就能读别的景区的东西,而 ID 往往并不难猜也并不难拿到。
    报 404 而不是 403:连「这条存在」都不该告诉对方。
    """
    if not can_read_obj(role, user_scenic, obj_scenic):
        raise NotFound(f"{what}不存在或无权访问")


def capabilities(role: str | None, scenic_id: str | None) -> dict:
    """给前端的能力清单:按钮显不显示、能不能点,前端照着这个来。

    前端自己按角色硬编码的话,每加一个角色就要改三处页面,而且改漏了是「能点但点了报错」——
    比按钮不显示更糟。
    """
    return {
        "role": role or "",
        "role_label": ROLE_LABELS.get(role or "", role or ""),
        "scenic_id": scenic_id or None,
        "is_group": is_group_role(role),
        "manage_all": can_manage_all(role),      # 跨景区增删改
        "can_create": can_create(role),          # 新增 / 修改
        "can_delete": can_delete(role),          # 删除
        # 景区角色:只能动本景区、动不了集团级内容(前端据此把「集团级」那几行置灰)
        "scope_scenic_id": None if is_group_role(role) else (scenic_id or None),
    }
