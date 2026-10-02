"""Casbin 策略初始化(幂等)。

首次启动或手动执行时,将默认角色权限策略写入 casbin_rule 表。
重复执行为幂等操作——已存在的策略不会重复插入。
"""
from __future__ import annotations

import casbin
import casbin_sqlalchemy_adapter

from cogni_agent.common.db import _build_dsn  # noqa: SLF001
from cogni_agent.common.logging import get_logger

log = get_logger("user_center_service.seed")

# RBAC 模型定义(与 governance_event_bus_service/policy.py 保持一致)
_MODEL_TEXT = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub) && r.obj == p.obj && r.act == p.act
"""

# 默认权限策略
_POLICIES: list[tuple[str, str, str, str]] = [
    ("p", "super_admin", "*", "*"),         # 超级管理员:全部资源全部操作
    ("p", "group_admin", "system_center", "*"),  # 集团管理员:系统中心全权限
    ("p", "group_admin", "members", "*"),        # 集团管理员:成员管理全权限
    ("p", "group_developer", "tools", "write"),   # 集团开发者:工具写权限
    ("p", "group_developer", "skills", "write"),  # 集团开发者:技能写权限
    ("p", "group_developer", "agents", "write"),  # 集团开发者:智能体写权限
    ("p", "group_viewer", "*", "read"),           # 集团查看者:全模块只读
    ("p", "scenic_user", "scenic_data", "*"),     # 景区用户:本景区数据
]

# 角色继承关系
_ROLE_LINKS: list[tuple[str, str, str]] = [
    ("g", "group_admin", "group_developer"),  # group_admin 继承 group_developer 的权限
    ("g", "group_admin", "group_viewer"),     # group_admin 继承 group_viewer 的权限
    ("g", "super_admin", "group_admin"),      # super_admin 继承 group_admin 的权限
]


def seed_casbin_policies() -> int:
    """初始化 Casbin 策略(幂等)。

    Returns:
        新增策略条数(首次执行返回策略总数,重复执行返回0)
    """
    dsn = _build_dsn().replace("+asyncpg", "")
    adapter = casbin_sqlalchemy_adapter.Adapter(dsn)

    # 写入 model 配置到临时文件
    with open("/tmp/zhilv_casbin_model.conf", "w") as f:
        f.write(_MODEL_TEXT)

    enforcer = casbin.Enforcer("/tmp/zhilv_casbin_model.conf", adapter)

    # 获取已有策略
    existing_policies = set()
    for p in enforcer.get_policy():
        existing_policies.add(tuple(p))
    for g in enforcer.get_grouping_policy():
        existing_policies.add(tuple(g))

    added = 0
    # 写入 p 策略
    for policy in _POLICIES:
        rule = tuple(policy[1:])  # 去掉前缀 p/g
        if rule in existing_policies:
            continue
        enforcer.add_policy(policy[1], policy[2], policy[3])
        existing_policies.add(rule)
        added += 1
        log.info(f"已添加策略: {policy[1]} → {policy[2]}:{policy[3]}")

    # 写入 g 策略(角色继承)
    for link in _ROLE_LINKS:
        rule = tuple(link[1:])  # 去掉前缀
        if rule in existing_policies:
            continue
        enforcer.add_grouping_policy(link[1], link[2])
        existing_policies.add(rule)
        added += 1
        log.info(f"已添加角色继承: {link[1]} → {link[2]}")

    enforcer.save_policy()
    log.info(f"Casbin 策略初始化完成, 新增 {added} 条")
    return added
