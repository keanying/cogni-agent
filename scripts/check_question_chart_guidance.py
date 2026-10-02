#!/usr/bin/env python
"""
22 号:return_format 标识自检(批量)——问题标注的图表形态 ↔ 绑定技能提示词引导。

    uv run python scripts/check_question_chart_guidance.py
    uv run python scripts/check_question_chart_guidance.py --json    # 机器可读(退出码同源)

背景:return_format(问题要什么图表形态)只是问题行的运营标注,运行时(graphs/网关/
预热)不消费、不做任何提示词注入——图表引导必须内置于绑定技能自身的提示词
(create_p0_group_skills.PROMPT_TMPL 的【结构化图表输出(PRD 3.2.2.4)】段)。
本脚本验证「问题标注 ↔ 绑定技能提示词」的一致性:遍历通用模板中绑定了技能 +
return_format 固定形态的问题,逐个确认其绑定技能的 published 提示词是否含
该形态的引导。存在缺口(技能没教出图 / 技能未绑定或缺失 / 技能无已发布版本)时
列出明细,以退出码 1 结束,供人工或 CI 捕获。连接目标库跟随 ZHILV_ENV(默认 local)。

23 号:原「组合里的技能」判定改为「问题行绑定的 skill_id」单技能判定。
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from cogni_agent.common.db import get_engine
from cogni_agent.gateway_service.models import AppQuestionBank
from cogni_agent.gateway_service.question_bank import question_chart_guidance


async def _scan() -> list[dict]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as db:
        # 不按 skill_id 过滤:seed 重灌会清空绑定,未回填/技能缺失
        # 同样是要暴露的「未就绪」缺口,不能静默滤掉造成假阴性。
        rows = (await db.execute(
            select(AppQuestionBank).where(
                AppQuestionBank.scenic_id.is_(None),          # 通用模板范围(景区专属行由景区侧各自核对)
                AppQuestionBank.return_format != "auto",
            ).order_by(AppQuestionBank.theme_code, AppQuestionBank.sort_order)
        )).scalars().all()
        out = []
        for q in rows:
            if not q.skill_id:
                binding = {"skill_id": None, "skill_name": None,
                           "published": False, "guidance_ok": False, "ok": False,
                           "gap_reason": "skill_not_bound"}
            else:
                binding = await question_chart_guidance(db, q)
            out.append({"id": q.id, "theme": q.theme_code, "question": q.question_text,
                        "return_format": q.return_format, "binding": binding})
        return out


def _detail(binding: dict | None) -> str:
    if binding is None:
        return "无绑定技能信息"
    if binding.get("gap_reason") == "skill_not_bound":
        return "未绑定执行技能(seed 重灌后需跑 scripts/create_p0_group_skills.py)"
    if binding.get("skill_id") is None:
        return "技能不存在(问题行 skill_id 悬空)"
    state = ("已发布,含引导" if binding["guidance_ok"]
             else "缺该形态引导" + (" —— 无已发布版本" if not binding["published"] else ""))
    return f"{binding['skill_name'] or binding['skill_id']}({state})"


def main() -> int:
    ap = argparse.ArgumentParser(description="return_format 标识自检(问题标注 ↔ 绑定技能图表提示词)")
    ap.add_argument("--json", action="store_true", help="输出 JSON(机器可读)")
    args = ap.parse_args()

    items = asyncio.run(_scan())
    if not items:
        print("没有固定形态的问题(return_format 全部为 auto)。")
        return 0

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0 if all(r["binding"] and r["binding"]["ok"] for r in items) else 1

    print(f"共 {len(items)} 条固定形态问题:\n")
    gaps = [r for r in items if not (r["binding"] and r["binding"]["ok"])]
    for r in items:
        ok = bool(r["binding"] and r["binding"]["ok"])
        print(f"  {'✓' if ok else '✗'} [{r['return_format']:>4}] {r['question']}"
              f"  → {_detail(r['binding'])}")
    print()
    if gaps:
        backfill = [g for g in gaps
                    if (g["binding"] or {}).get("gap_reason") == "skill_not_bound"]
        print(f"缺口 {len(gaps)}/{len(items)} 条:问题标注了固定图表形态,但尚未就绪。")
        if backfill:
            print(f"  - {len(backfill)} 条未绑定执行技能:先跑 scripts/create_p0_group_skills.py"
                  " 建技能并回填 skill_id(seed 重灌会清空绑定)。")
        rest = [g for g in gaps if g not in backfill]
        if rest:
            print(f"  - {len(rest)} 条已绑定但技能提示词缺引导/技能未发布/技能悬空:在技能提示词补"
                  "【结构化图表输出】段(参考 scripts/create_p0_group_skills.PROMPT_TMPL),"
                  "或把问题 return_format 改回 auto。")
        return 1
    print("全部通过:标注固定形态的问题,绑定技能提示词均含对应图表输出引导。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
