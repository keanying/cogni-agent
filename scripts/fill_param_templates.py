#!/usr/bin/env python
"""33 号:按 time_period 语义补齐问题行 param_template 的工具必填参数。

定位(gateway_service/question_bank 模块头):基准日期与对比窗口都从 time_period
在装配期生成确定值,**无运行期默认补全**——模板没配的键,模型要么反问
(任务 f400994c)要么编个值发出去拿空数据再空转(249488c8:4 轮 273s、零输出)。
所以缺口只能靠补模板消除,不能指望执行层兜底。

补全口径(参数名语义 × period):
    compareStart*/compareEnd*   上期窗口(PERIOD_CMP,常驻模块单一真相源)
    startDate/endDate           日期窗口   YYYY-MM-DD
    startTime/endTime           时间窗口   YYYY-MM-DD HH:mm:ss
    qTime/travelDate 等         基准日(单日)
    evaluationMonth             YYYY-MM({{month}})

**为什么按「技能绑定工具 required 并集」全补,不做语义甄别**:槽位只进提示词
(graphs.py 把 slots 渲染成「已知上下文参数,工具参数优先使用,不要再追问」),
多配 = 多一行提示词文本,无害;少配 = 模型缺值要反问或编造,已实证会出事故。
典型例子:「今日实时在园人数」绑的技能同时挂了比对工具,于是报缺 compareXxx
(语义上确实用不到),但补上只是多两行提示词,不补则模型一旦真调比对工具就
没有值可用——两害相权取其轻。

用法:
    ZHILV_ENV=dev .venv/bin/python scripts/fill_param_templates.py [--apply]

缺省 dry-run(只打印);--apply 才写库。已配的键一律不覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from cogni_agent.common.db import get_sessionmaker  # noqa: E402
from cogni_agent.gateway_service.param_audit import (  # noqa: E402
    PERIOD_CMP, _skill_tool_requirements)

# 日期窗口(YYYY-MM-DD):period → (起, 止)
_DATE_WIN = {
    "today": ("{{today}}", "{{today}}"),
    "yesterday": ("{{yesterday}}", "{{yesterday}}"),
    "week_to_date": ("{{week_start}}", "{{today}}"),
    "last_7_days": ("{{days_7_ago}}", "{{today}}"),
    "month_to_date": ("{{month_start}}", "{{today}}"),
    "next_3_days": ("{{tomorrow}}", "{{plus_3d_end}}"),
}
# 时间窗口(YYYY-MM-DD HH:mm:ss):period → (起, 止)
_DT_WIN = {
    "today": ("{{today_start}}", "{{now}}"),
    "yesterday": ("{{yesterday_start}}", "{{yesterday_end}}"),
    "week_to_date": ("{{week_start}} 00:00:00", "{{now}}"),
    "last_7_days": ("{{days_7_ago}} 00:00:00", "{{now}}"),
    "month_to_date": ("{{month_start}} 00:00:00", "{{now}}"),
    "next_3_days": ("{{tomorrow}} 00:00:00", "{{plus_3d_end}} 23:59:59"),
}
# 单日基准(舆情 qTime / 客流 travelDate 等)
_POINT = {
    "today": "{{today}}",
    "yesterday": "{{yesterday}}",
    "week_to_date": "{{today}}",
    "last_7_days": "{{today}}",
    "month_to_date": "{{today}}",
    "next_3_days": "{{tomorrow}}",
}
_POINT_KEYS = {"qTime", "travelDate", "statDate", "bizDate", "queryDate"}


def suggest(param: str, period: str) -> str | None:
    """按参数名语义 + period 给出模板值(占位符);无口径时返回 None(留给人工)。"""
    if "compareStart" in param or "compareEnd" in param:
        pair = PERIOD_CMP.get(period)
        if not pair:
            return None
        return pair[0] if "compareStart" in param else pair[1]
    if param == "evaluationMonth":
        return "{{month}}"
    if param in _POINT_KEYS:
        return _POINT.get(period)
    if param.startswith("startDate"):
        return (_DATE_WIN.get(period) or (None, None))[0]
    if param.startswith("endDate"):
        return (_DATE_WIN.get(period) or (None, None))[1]
    if param.startswith("startTime"):
        return (_DT_WIN.get(period) or (None, None))[0]
    if param.startswith("endTime"):
        return (_DT_WIN.get(period) or (None, None))[1]
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库(缺省只打印)")
    args = ap.parse_args()
    async with get_sessionmaker()() as db:
        qs = (await db.execute(text(
            "SELECT id, question_text, skill_id, time_period, param_template, active "
            "FROM numeri_core_app_question_bank WHERE skill_id IS NOT NULL"))).all()
        plan: list[tuple[str, str, str, dict, dict]] = []
        unknown: dict[str, set] = {}
        for qid, qtext, sid, period, tpl, active in qs:
            tpl = dict(tpl or {})
            req, _unwired = await _skill_tool_requirements(db, sid)
            need: set = set()
            for keys in req.values():
                need.update(keys)
            missing = sorted(need - set(tpl.keys()))
            if not missing:
                continue
            add = {}
            for p in missing:
                v = suggest(p, period or "")
                if v:
                    add[p] = v
                else:
                    unknown.setdefault(p, set()).add(period or "")
            if add:
                plan.append((qid, qtext, period, add, tpl))
        print(f"待补齐问题 {len(plan)} 个(共 {len(qs)} 个有技能绑定):\n")
        for qid, qtext, period, add, _tpl in plan:
            print(f"  [{str(qid)[:8]}] {qtext[:26]:28} period={period}")
            for k, v in sorted(add.items()):
                print(f"      + {k} = {v}")
        if unknown:
            print("\n✗ 无启发式口径(需人工定值):")
            for k, pers in unknown.items():
                print(f"    - {k}: period={sorted(pers)}")
        if not args.apply:
            print("\n(dry-run:未写库;加 --apply 执行)")
            return
        for qid, _qtext, _period, add, tpl in plan:
            await db.execute(text(
                "UPDATE numeri_core_app_question_bank SET param_template=:t WHERE id=:id"),
                {"t": json.dumps({**tpl, **add}, ensure_ascii=False), "id": qid})
        await db.commit()
        print(f"\n✓ 已写入 {len(plan)} 个问题行")


if __name__ == "__main__":
    asyncio.run(main())
