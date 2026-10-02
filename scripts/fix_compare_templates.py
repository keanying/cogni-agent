"""32 号补:对比类问题的 param_template 补齐 compareXxx 对比窗口。

背景:全库 57 个配模板的问题 0 个配了对比窗口——比对汇总工具的
compareStartTime/compareEndTime(required)全部留给模型执行期自填,
违背 27 号「参数装配期全量生成」定稿(填错日期/格式是隐性事故源)。
本脚本按问题语义补齐 5 个对比类问题的模板(幂等:只补缺失键,不动已有)。

用法: ZHILV_ENV=dev .venv/bin/python scripts/fix_compare_templates.py [--dry-run]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402
from cogni_agent.common.db import get_engine  # noqa: E402

# 问题(模糊定位) → 需要补的对比窗口键值
FIXES = {
    "昨日售票收入及与前日对比情况": {
        "compareStartTime": "{{day_before_yesterday_start}}",
        "compareEndTime": "{{day_before_yesterday_end}}",
    },
    "本周各渠道订单与上周对比": {
        "compareStartTime": "{{last_week_start}}",
        "compareEndTime": "{{last_week_end}}",
    },
    "近7天客流与上周环比变化": {
        "compareStartTime": "{{prev_days_7_start}}",
        "compareEndTime": "{{prev_days_7_end}}",
    },
    "当前各OTA评分环比变化情况": {
        "compareStartTime": "{{yesterday_start}}",
        "compareEndTime": "{{yesterday_end}}",
    },
    "负面舆情较前日是否有激增": {
        "compareStartTime": "{{day_before_yesterday_start}}",
        "compareEndTime": "{{day_before_yesterday_end}}",
    },
}


async def main() -> None:
    dry = "--dry-run" in sys.argv
    eng = get_engine()
    async with eng.begin() as c:
        for kw, add in FIXES.items():
            rows = (await c.execute(text(
                "SELECT id, question_text, param_template FROM numeri_core_app_question_bank "
                "WHERE question_text LIKE :kw"), {"kw": f"%{kw}%"})).all()
            for rid, qtext, tpl in rows:
                tpl = tpl or {}
                missing = {k: v for k, v in add.items() if not tpl.get(k)}
                if not missing:
                    print(f"  = {qtext[:24]} 已齐,跳过")
                    continue
                new_tpl = {**tpl, **missing}
                print(f"  {'[dry] ' if dry else ''}{qtext[:24]} +{sorted(missing)} → {json.dumps(new_tpl, ensure_ascii=False)}")
                if not dry:
                    await c.execute(text(
                        "UPDATE numeri_core_app_question_bank SET param_template = :t "
                        "WHERE id = :id"),
                        {"t": json.dumps(new_tpl, ensure_ascii=False), "id": rid})
    await eng.dispose()
    print("完成" if not dry else "[dry] 未写库")


if __name__ == "__main__":
    asyncio.run(main())
