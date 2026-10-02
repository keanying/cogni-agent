#!/usr/bin/env python
"""
存量问题行 metric_name 回填(0052 方案 A 增量通道,不重灌)。

dev 等已 seed 过的环境,0052 迁移只加空列;全量重灌 seed 会清掉 skill_id
(S5 技能回填,重灌后需重跑 create_p0_group_skills.py 才能恢复)。
本脚本按 question_text 精确匹配种子 QUESTIONS 常量(通用模板 scenic_id IS NULL),
仅 UPDATE metric_name,不动路由/预热/技能绑定等其余字段。幂等:可重复执行。

用法:ZHILV_ENV=dev uv run python scripts/backfill_question_metric_name.py
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import select

from cogni_agent.common.db import get_engine
from cogni_agent.gateway_service.models import AppQuestionBank

_ROOT = Path(__file__).resolve().parent.parent


def _load_seed_questions() -> list[tuple]:
    """从 seed 模块加载 QUESTIONS(避免 scripts 非包的 import 问题,直接按路径加载)。"""
    spec = importlib.util.spec_from_file_location(
        "seed_app_question_bank", _ROOT / "scripts" / "seed_app_question_bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.QUESTIONS


async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = get_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    # (theme, question, metric_code, metric_name, ...) → metric_name 映射(仅非空)
    mapping = {q: metric_name for (_t, q, _m, metric_name, *_r) in _load_seed_questions() if metric_name}

    async with Session() as db:
        rows = (await db.execute(
            select(AppQuestionBank).where(
                AppQuestionBank.scenic_id.is_(None),
                AppQuestionBank.dimension_name.is_not(None),
            )
        )).scalars().all()
        hit = miss = 0
        for r in rows:
            want = mapping.get(r.question_text)
            if want is None:
                miss += 1  # 种子已无此选择器行 → 保留原值不动
                continue
            if r.metric_name != want:
                r.metric_name = want
                hit += 1
        await db.commit()
        print(f"回填完成: 更新 {hit} 行 | 无种子映射保留原值 {miss} 行")


if __name__ == "__main__":
    asyncio.run(main())
