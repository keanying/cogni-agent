#!/usr/bin/env python
"""灌入 Casbin 基线策略。用法: uv run python scripts/seed_policies.py [--reset]"""
import sys

from cogni_agent.governance_event_bus_service.policy import permissions_for, seed_policies

if __name__ == "__main__":
    stats = seed_policies(reset="--reset" in sys.argv)
    print(f"✓ 策略已就绪:{stats}")
    import asyncio
    for role in ("scenic_operator", "group_developer", "group_admin", "super_admin"):
        p = asyncio.run(permissions_for(role))
        visible = [k for k, v in p["menus"].items() if v]
        print(f"  {role:18s} 可见菜单: {visible}")
