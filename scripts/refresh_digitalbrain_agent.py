"""景区数字大脑 agent 重发布(刷新技能 pin)+ 指派切新版。

为什么必须走这一趟(版本不是「发了就生效」):
    运行时解析是**钉版本**的,且没有「取最新 published」兜底——
      · 指派钉 agent 的 version_id;
      · agent 快照 bound_skills 里每个技能钉 version(技能 pin);
      · 技能快照 tools 里每个工具钉 version。
    任一层不刷新,新发布的技能/工具就**不会被用上**(resolver 无 pin 直接判不可用)。
    所以「改技能」的完整动作是:发技能新版 → 刷 agent pin → 发 agent 新版 → 切指派。

历史:
    27 号:26 号重发布 13 个 P0 技能(工具 schema 参数注入声明调整)后,
    agent v8 的 pin 仍指旧版本(旧工具链,前置工具已删,不可用),本脚本首次使用。
    33 号:补 tool_usage(工具选择前置)重发布 12 个技能后再次使用;此时 13 个技能
    pin 全部落后(含 26/27 号之后的累积欠账),一并刷到最新。

本脚本:派生 → refresh_pins(pin 升到各技能最新 published)→ 发布 →
N 条用户指派切到新 agent 版本(内容=同一 agent 的 pin 刷新,指派语义不变)。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogni_agent.common.db import get_sessionmaker  # noqa: E402
from cogni_agent.common.logging import get_logger  # noqa: E402
from cogni_agent.tool_center_service import versioning as ver  # noqa: E402

log = get_logger("refresh_digitalbrain")


async def main():
    from sqlalchemy import select, text
    from cogni_agent.tool_center_service.agent.models import Agent
    from cogni_agent.tool_center_service.versioning import ResourceAssignment

    async with get_sessionmaker()() as db:
        agent = (await db.execute(select(Agent).where(
            Agent.name == "景区数字大脑"))).scalars().first()
        if agent is None:
            print("未找到景区数字大脑")
            return
        pub = await ver.latest_published_version(db, "agent", agent.id)
        print(f"当前 published: v{pub.version} ({pub.id[:8]})")
        # 残留草稿(09-10 脚本批量残留,与技能侧同批):无 description 的空壳,
        # 丢弃后从 published 派生
        stale = await ver.latest_draft(db, "agent", agent.id)
        if stale is not None:
            await ver.discard_draft(db, stale.id)
            print(f"丢弃残留草稿 {stale.id[:8]}")
        draft = await ver.derive_version(
            db, "agent", agent.id, base_version_id=pub.id, snapshot={},
            created_by="33号pin刷新", note="技能 pin 刷新(33号 tool_usage 补齐配套)")
        r = await ver.refresh_pins(db, draft.id)
        for ch in (r.get("changed") or [])[:5]:
            print("  pin:", ch)
        out = await ver.publish_version(db, draft.id)
        new_ver = out["version"]
        print(f"新版本 v{new_ver} 已发布")
        # 指派切新版(同一 agent 的 pin 刷新,指派语义不变)
        assigns = (await db.execute(select(ResourceAssignment).where(
            ResourceAssignment.agent_id == agent.id))).scalars().all()
        for a in assigns:
            await ver.upsert_assignment(
                db, agent.id, a.scope_id, draft.id,
                note="33号:技能 pin 刷新后切换(agent 内容同源)")
            print(f"  指派 {str(a.scope_id)[:12]} → v{new_ver}({draft.id[:8]})")
        print("完成")


asyncio.run(main())
