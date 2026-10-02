"""32 号补:激活问题的参数键名缺口批量补齐(qTime/evaluationMonth/客流比对窗口)。

审计甄别后真实缺口(语义会调到该工具的 required,非技能工具并集误报):
- qTime(舆情工具,YYYY-MM-DD):舆情/概览类 today 问题
- evaluationMonth(OTA雷达图,YYYY-MM):OTA直销卡/概览卡
- 客流比对窗口:今日在园与昨日同期相比(今日基准+昨日对比)

用法: ZHILV_ENV=dev .venv/bin/python scripts/fix_param_coverage_batch.py [--dry-run]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402
from cogni_agent.common.db import get_engine  # noqa: E402

# 问题(模糊定位,只动 active 的) → 补充键
FIXES = {
    # qTime 类(舆情 today)
    "当前有没有突发热点事件需要关注": {"qTime": "{{today}}"},
    "当前舆情情感倾向如何": {"qTime": "{{today}}"},
    "当前舆情情感正面/负面占比": {"qTime": "{{today}}"},
    "负面舆情较前日是否有激增": {"qTime": "{{today}}"},
    "【主题解读】舆情": {"qTime": "{{today}}"},
    "【主题解读】概览": {"qTime": "{{today}}", "evaluationMonth": "{{month}}"},
    # evaluationMonth 类(OTA 雷达图,卡片解读调用)
    "【主题解读】OTA直销": {"evaluationMonth": "{{month}}"},
    # 客流比对窗口(今日 vs 昨日同期)
    "今日实时在园人数与昨日同期相比": {
        "startTime": "{{today_start}}", "endTime": "{{now}}",
        "compareStartTime": "{{yesterday_start}}", "compareEndTime": "{{yesterday_end}}",
    },
}


async def main() -> None:
    dry = "--dry-run" in sys.argv
    eng = get_engine()
    async with eng.begin() as c:
        for kw, add in FIXES.items():
            rows = (await c.execute(text(
                "SELECT id, question_text, param_template, active "
                "FROM numeri_core_app_question_bank WHERE question_text LIKE :kw"),
                {"kw": f"%{kw}%"})).all()
            for rid, qtext, tpl, active in rows:
                if not active:
                    print(f"  ⊘ {qtext[:24]}(停用,跳过——上线前按审计补)")
                    continue
                tpl = tpl or {}
                missing = {k: v for k, v in add.items() if not tpl.get(k)}
                if not missing:
                    print(f"  = {qtext[:24]} 已齐")
                    continue
                new_tpl = {**tpl, **missing}
                print(f"  {'[dry] ' if dry else ''}{qtext[:24]} +{sorted(missing)}")
                if not dry:
                    await c.execute(text(
                        "UPDATE numeri_core_app_question_bank SET param_template=:t WHERE id=:id"),
                        {"t": json.dumps(new_tpl, ensure_ascii=False), "id": rid})
    await eng.dispose()
    print("完成" if not dry else "[dry] 未写库")


if __name__ == "__main__":
    asyncio.run(main())
