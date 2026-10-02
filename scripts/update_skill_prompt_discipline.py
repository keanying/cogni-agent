#!/usr/bin/env python
"""33 号:技能 prompt 纪律改写——「两阶段 + 平行参数纪律」→「三阶段:参数校验 → 取数 → 生成」。

为什么要改(同一会话相邻两轮的实证对比):
    旧写法把【执行纪律】(先取数、后回答)与【参数纪律】(缺参先澄清)写成两条
    **平级**纪律。模型会把它们当成两个平级选项来摇骰子——同一个缺 supplierId
    的票务问题:
      · f400994c:选了参数纪律 → 老实反问,44s 收敛;
      · 249488c8:选了执行纪律 → 跳过门禁硬取数,编个值拿回空数据 → 反复重试 →
        工具被摘除 → 步数耗尽 → **零输出**(4m34s、成本照扣、用户看到一片空白)。
    参数校验不是与取数平行的纪律,它是取数之前的**门禁**,必须写进同一条流程。

新写法三条必须写死,少一条就会退化:
    ① 缺参时**禁止调用工具**;② **禁止用空值/猜测值**调用;③ 反问即**本轮结束**。
    另保留可选参数例外(OTA 账号缺失属预期),否则模型会对账号也反问。

用法:
    ZHILV_ENV=dev .venv/bin/python scripts/update_skill_prompt_discipline.py [--dry-run]

幂等:prompt 已含「三阶段,参数校验先于取数」则跳过;旧段落匹配不上则跳过并报告,
交给人工处理(不猜文本)。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from cogni_agent.common.db import get_sessionmaker  # noqa: E402
from cogni_agent.tool_center_service import versioning as ver  # noqa: E402

_NEW_EXEC = """【执行纪律:三阶段,参数校验先于取数】
第一阶段(参数校验):检查取数工具的必填参数是否都能从会话上下文取到
  · 齐全 → 进入第二阶段
  · 缺失且无法从上下文推断 → 立即向用户澄清缺失项,本轮即结束;
    禁止带着空值或猜测值调用工具——空结果不是"取数完成",只会让你反复重试
第二阶段(取数):参数齐全后,把相关取数工具全部调用一遍拿到完整数据。数据齐全之前,禁止输出任何结论或分析
第三阶段(生成):数据齐全后,一次性生成结构化回答"""

_NEW_PARAM = """【参数例外】
- 景区 ID 已由会话上下文注入,直接使用
- 可选参数(如 OTA 商户账号)缺失属预期:照常取数,空结果在结论中说明原因即可,不触发澄清
  (区分口径:必填且无法推断 = 向用户澄清;可选 = 空数据属预期,照常出结论)"""

_OLD_EXEC = re.compile(
    r"【执行纪律:两阶段,禁止边取数边回答】.*?"
    r"第二阶段\(生成\):数据齐全后,一次性生成结构化回答。", re.S)
_OLD_PARAM = re.compile(
    r"【参数纪律】\s*\n- 景区 ID 已由会话上下文注入.*?(?=\n\n|\Z)", re.S)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只观察不写库")
    args = ap.parse_args()
    async with get_sessionmaker()() as db:
        rows = (await db.execute(text(
            "SELECT id, name FROM numeri_core_tc_skills ORDER BY name"))).all()
        for rid, name in rows:
            pub = await ver.latest_published_version(db, "skill", rid)
            if pub is None:
                continue
            snap = dict(pub.snapshot or {})
            prompt = snap.get("prompt") or ""
            if not prompt:
                continue
            if "三阶段,参数校验先于取数" in prompt:
                print(f"- {name}: 已是新写法,跳过")
                continue
            new_prompt, n = _OLD_EXEC.subn(_NEW_EXEC, prompt)
            if n == 0:
                print(f"✗ {name}: 未匹配旧【执行纪律】段落(可能已手工改过),跳过待人工")
                continue
            new_prompt, m = _OLD_PARAM.subn(_NEW_PARAM, new_prompt)
            if m == 0:
                # 没有旧【参数纪律】段,但例外口径不能丢——否则模型会对 OTA 账号反问
                new_prompt = new_prompt.rstrip() + "\n\n" + _NEW_PARAM
                print(f"  · {name}: 未找到【参数纪律】段,已追加【参数例外】")
            if args.dry_run:
                print(f"[dry] {name}: 纪律改写(执行纪律 ✓ 参数例外 ✓)")
                continue
            draft = await ver.latest_draft(db, "skill", rid)
            if draft is not None and not (draft.snapshot or {}).get("description"):
                await ver.discard_draft(db, draft.id)
                draft = None
            if draft is not None:
                base = dict(draft.snapshot or {})
                base["prompt"] = new_prompt
                await ver.update_version(db, draft.id, snapshot=base,
                                         note="prompt 纪律改写:参数校验前置于取数(33号)")
                src = "草稿"
            else:
                snap["prompt"] = new_prompt
                draft = await ver.derive_version(
                    db, "skill", rid, base_version_id=pub.id, snapshot=snap,
                    created_by="33号纪律改写",
                    note="prompt 纪律改写:参数校验前置于取数(33号)")
                src = f"v{pub.version}"
            await ver.publish_version(db, draft.id)
            print(f"✓ {name}: 已发布(基于{src})")


if __name__ == "__main__":
    asyncio.run(main())
