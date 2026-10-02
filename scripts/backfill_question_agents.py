"""27 号:问题行 agent_id 回填(版本锚)。

所有 active 固定问题的 agent_id 回填为指定 agent(dev=景区数字大脑)。
幂等:只更新 agent_id IS NULL 的行。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogni_agent.common.db import get_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402


async def main():
    eng = get_engine()
    async with eng.begin() as c:
        aid = (await c.execute(text(
            "SELECT id FROM numeri_core_tc_agents WHERE name = '景区数字大脑' LIMIT 1"))).scalar()
        if not aid:
            print("未找到「景区数字大脑」agent")
            return
        n = (await c.execute(text(
            "UPDATE numeri_core_app_question_bank SET agent_id = :a "
            "WHERE agent_id IS NULL AND active"), {"a": aid})).rowcount
        print(f"回填 {n} 行 agent_id = 景区数字大脑({aid[:8]})")
    await eng.dispose()


asyncio.run(main())
