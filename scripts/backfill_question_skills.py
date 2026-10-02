#!/usr/bin/env python
"""
v1.1.11:问题库执行技能全覆盖回填(P1/P2 缺口行绑技能,local/dev 双环境幂等)。

背景(为什么补):v1.1 APP 无自由询问入口,所有可点问题都须命中技能直跳——
问题行 skill_id + param_template 两列齐备(23 号:原 composition_id + route_mode
两列已随编排组合层删除,见 0055 迁移)。seed_app_question_bank.py 只给 P0/主题解读
行回填(22 条,含概览),P1/P2 active 行(28 条)仍 skill_id 为空:点击命中后走
Triager 意图识别,在 v1.1 无自由输入下属浪费路径(意图已由 question_id 锁定,
还要 LLM 重推技能)。

本脚本(幂等,可重复执行):
  ① API 层(管理端):按 NEW_GROUPS 聚类补建分组技能(PROMPT_TMPL 两阶段模板,与
     P0 技能同源)并发布;REUSE_GROUPS 复用既有技能,不重建;
  ② DB 层:对缺口行(通用模板 scenic_id IS NULL 且 skill_id IS NULL)逐行回填
     skill_id + param_template(按行 time_period 展开基准日期,与 seed PARAM_TEMPLATES 同源)。

不动的:
  - tencent_report 下架行(active=false,home 不展示/match_question 不过 active,
    不可达不消费,无需绑定);
  - Triager 意图识别框架/agent 动态池/未绑技能分支(v1.2 自由询问预留,v1.1 数据上
    不可达即可,不做代码守卫)。

用法:
  uv run python scripts/backfill_question_skills.py --env local|dev \
      [--account pftroot] [--password ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 复用 P0 脚本的登录/调用封装、两阶段 prompt 模板(保持技能提示词同源)
from create_p0_group_skills import PROMPT_TMPL, call, login  # noqa: E402

SUPPLIER_TOOL = "通过景区ID获取供应商ID列表"

# ── 新建组(聚类补编:同主题×周期×工具集) ──────────────────────────────────
# 28 条缺口 active 行聚类为 7 组;description 供 Triager/运营展示(何时用/何时不用)。
NEW_GROUPS = [
    {
        "name": "固定问题-客流时段与高峰",
        "description": "回答客流时段与高峰类问题:今日入园高峰时段、今日各时段人流分布、"
                       "本周客流高峰出现在哪几天/哪个时段。仅当问题针对当日分时或本周高峰时使用;"
                       "实时人数/承载率、近7天趋势、票务、舆情类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "分时报表汇总", "每日报表趋势", "客流比对汇总"],
        "questions": [
            "今日入园高峰是哪个时段？",
            "今日各时段人流情况？",
            "本周客流高峰出现在哪几天？",
            "本周客流高峰出现在哪个时段？",
        ],
    },
    {
        "name": "固定问题-客流趋势对比",
        "description": "回答近7天客流趋势类问题:近7天客流走势、与上周环比变化。"
                       "仅当问题针对近7天/多日客流趋势对比时使用;今日实时、分时高峰、票务、"
                       "舆情类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "每日报表趋势", "客流比对汇总"],
        "questions": [
            "近7天客流趋势如何？",
            "近7天客流与上周环比变化？",
        ],
    },
    {
        "name": "固定问题-票务周度经营",
        "description": "回答本周(周度)票务经营结构类问题:各票种销售占比、畅销票种、"
                       "线上线下渠道售票比例、各OTA直销渠道贡献。仅当问题针对本周票务"
                       "票种/渠道构成时使用;昨日票务、月度累计、客流、舆情类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "门票报表汇总", "产品报表汇总", "销售渠道报表汇总", "报表比对汇总"],
        "questions": [
            "本周各票种销售占比是多少？",
            "本周哪种票最畅销？",
            "本周线上渠道和线下窗口售票比例如何？",
            "本周哪个OTA直销渠道贡献最多售票量？",
        ],
    },
    {
        "name": "固定问题-票务未来预售",
        "description": "回答未来3天预售类问题:未来3天预售量及变化情况。"
                       "仅当问题针对未来3天预售量时使用;昨日票务、本月累计、客流、舆情类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "门票报表汇总", "产品报表汇总"],
        "questions": [
            "未来3天预售量如何？",
            "未来3天预售量变化情况？",
        ],
    },
    {
        "name": "固定问题-舆情平台与亮点",
        "description": "回答舆情平台/情感深化类问题:哪个平台负面评价最多、各平台舆情分布、"
                       "OTA评分环比、游客提及的痛点、可用于宣传的正面亮点。"
                       "仅当问题针对舆情平台分布/情感细读/宣传素材时使用;舆情趋势(近7天/月度)、"
                       "客流、票务类问题不要用本技能。",
        "tools": ["舆情-维度分数和平台占比", "舆情综合评价得分", "OTA指标"],
        "questions": [
            "哪个平台负面评价最多？",
            "当前各平台舆情分布情况？",
            "当前各OTA评分环比变化情况？",
            "游客最常提及的痛点是什么？",
            "当前正面舆情有哪些亮点可以用于宣传？",
        ],
    },
    {
        "name": "固定问题-舆情趋势",
        "description": "回答舆情趋势类问题:近7天舆情走势、较上周变化、本月舆情走势。"
                       "仅当问题针对多日/月度舆情趋势对比时使用;当日舆情态势、平台分布、"
                       "客流、票务类问题不要用本技能。",
        "tools": ["舆情-维度分数和平台占比", "舆情综合评价得分"],
        "questions": [
            "近7天舆情趋势如何？",
            "近7天舆情较上周变化情况？",
            "本月舆情走势？",
        ],
    },
    {
        "name": "固定问题-OTA渠道明细",
        "description": "回答OTA渠道明细/对比类问题:本周各渠道订单与上周对比、各渠道出票量、"
                       "客单价、自营与OTA渠道占比。仅当问题针对OTA渠道经营明细/对比时使用;"
                       "核销率、客流、舆情、昨日票务类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "OTA指标", "销售渠道报表汇总", "报表比对汇总"],
        "questions": [
            "本周各渠道订单与上周对比？",
            "当前各渠道出票量分别是多少？",
            "当前各渠道客单价是多少？",
            "当前自营渠道与OTA渠道占比如何？",
        ],
    },
]

# ── 复用现有技能(同域复用,不新建技能;问题行直接回填既有 skill_id) ──────────
# 23 号:原按「组合名」复用,组合层删除后改按技能名(即 P0 分组技能全名)。
REUSE_GROUPS = {
    # 现有 P0 技能名 → 追加的问题文本
    "固定问题-票务月度进度": [
        "本月目标达成进度如何？",
        "本月售票量及收入趋势？",
    ],
    "固定问题-票务昨日汇总": [
        "昨日自营渠道销售量及占比？",
        "昨日退票原因分析？",
    ],
}

# 参数模板(与 seed_app_question_bank.PARAM_TEMPLATES 同源,time_period → 基准日期)
PARAM_TEMPLATES = {
    "today": {"travelDate": "{{today}}"},
    "yesterday": {"startTime": "{{yesterday_start}}", "endTime": "{{yesterday_end}}"},
    "week_to_date": {"startTime": "{{week_start}} 00:00:00", "endTime": "{{now}}"},
    "month_to_date": {"startTime": "{{month_start}} 00:00:00", "endTime": "{{now}}"},
    "last_7_days": {"startTime": "{{days_7_ago}} 00:00:00", "endTime": "{{today_end}}"},
    "next_3_days": {"startTime": "{{tomorrow}} 00:00:00", "endTime": "{{plus_3d_end}} 23:59:59"},
}


async def _db_backfill(env: str, mapping: dict[str, str]) -> int:
    """DB 层回填:缺口行(scenic NULL & skill_id NULL & active)按文本→技能,补两列。"""
    os.environ["ZHILV_ENV"] = env
    from sqlalchemy import text

    from cogni_agent.common.db import get_engine
    from cogni_agent.gateway_service.models import AppQuestionBank

    tq = AppQuestionBank.__tablename__
    async with get_engine().begin() as conn:
        rows = (await conn.execute(text(
            f"SELECT id, question_text, time_period FROM {tq} "
            f"WHERE scenic_id IS NULL AND active = true AND skill_id IS NULL"))).all()
        n = 0
        for rid, qt, period in rows:
            sid = mapping.get(qt)
            if not sid:
                continue
            tpl = PARAM_TEMPLATES.get(period)
            await conn.execute(text(
                f"UPDATE {tq} SET skill_id=:sid, "
                f"param_template=:tpl, updated_at=now() WHERE id=:rid"),
                {"sid": sid, "tpl": json.dumps(tpl) if tpl else None, "rid": rid})
            n += 1
        return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="local", choices=["local", "dev"])
    ap.add_argument("--account", default="pftroot")
    ap.add_argument("--password", default=None, help="缺省依次尝试 0000@123 / pftroot123")
    args = ap.parse_args()

    from create_p0_group_skills import ENVS

    base = ENVS[args.env]
    token = None
    for pwd in ([args.password] if args.password else ["0000@123", "pftroot123"]):
        try:
            token = login(base, args.account, pwd)
            print(f"✓ 登录成功({args.account}) @ {args.env}")
            break
        except SystemExit:
            continue
    if not token:
        raise SystemExit("登录失败:账号密码均不匹配")

    tools = call(base, token, "GET", "/tools").get("items", [])
    tool_id = {t["name"]: t["id"] for t in tools}
    existing_skills = {s["name"]: s for s in call(base, token, "GET", "/skills").get("items", [])}

    mapping: dict[str, str] = {}

    for g in NEW_GROUPS:
        missing = [t for t in g["tools"] if t not in tool_id]
        if missing:
            print(f"✗ {g['name']}: 缺少工具 {missing},跳过(该组问题不绑)")
            continue
        want_ids = [tool_id[t] for t in g["tools"]]
        want_prompt = PROMPT_TMPL.format(title=g["name"].replace("固定问题-", ""))
        skill = existing_skills.get(g["name"])
        if skill:
            skill_id = skill["id"]
            vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
            pub = next((v for v in vers if v.get("status") == "published"), None)
            snap = (pub or {}).get("snapshot") or {}
            snap_ids = sorted(p.get("tool_id") for p in (snap.get("tools") or []))
            prompt_same = snap.get("prompt") == want_prompt
            if snap_ids == sorted(want_ids) and prompt_same:
                print(f"• 技能已存在且一致: {g['name']} ({skill_id[:8]})")
            else:
                upd: dict = {
                    "description": snap.get("description") or g["description"],
                    "suggested_mode": snap.get("suggested_mode"),
                    "knowledge_sources": snap.get("knowledge_sources"),
                    "constraints": snap.get("constraints") or {
                        "two_phase": "先并行取数,数据齐后才允许一次生成;禁止边取数边回答",
                        "no_fabrication": "所有数字必须来自工具返回,禁止编造;无数据时说明原因",
                    },
                    "tool_usage": snap.get("tool_usage"),
                    "tool_ids": want_ids,
                }
                upd = {k: v for k, v in upd.items() if v is not None}
                if not prompt_same:
                    upd["prompt"] = want_prompt
                call(base, token, "PUT", f"/skills/{skill_id}", upd)
                vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
                draft = next((v for v in vers if v.get("status") == "draft"), None)
                vid = draft["id"] if draft else call(
                    base, token, "POST", f"/resources/skill/{skill_id}/versions",
                    {"note": "v1.1.11:技能全覆盖 P1/P2 分组技能"}).get("id")
                call(base, token, "POST", f"/versions/{vid}/refresh-pins")
                call(base, token, "POST", f"/versions/{vid}/publish")
                print(f"✓ 技能已更新+发布: {g['name']} ({skill_id[:8]})")
        else:
            created = call(base, token, "POST", "/skills", {
                "name": g["name"], "description": g["description"],
                "tool_ids": want_ids, "suggested_mode": "L1", "visibility": "public",
                "prompt": want_prompt,
                "constraints": {
                    "two_phase": "先并行取数,数据齐后才允许一次生成;禁止边取数边回答",
                    "no_fabrication": "所有数字必须来自工具返回,禁止编造;无数据时说明原因",
                },
            })
            skill_id = created["id"]
            print(f"✓ 技能创建: {g['name']} ({skill_id[:8]})")

        vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
        if not any(v.get("status") == "published" for v in vers):
            draft = next((v for v in vers if v.get("status") == "draft"), None)
            vid = draft["id"] if draft else call(
                base, token, "POST", f"/resources/skill/{skill_id}/versions",
                {"note": "v1.1.11:技能全覆盖分组技能"}).get("id")
            call(base, token, "POST", f"/versions/{vid}/refresh-pins")
            call(base, token, "POST", f"/versions/{vid}/publish")
            print(f"✓ 技能已发布 (vid={vid})")

        for q in g["questions"]:
            mapping[q] = skill_id
        print(f"  → 组 {g['name']} 覆盖 {len(g['questions'])} 个问题文本")

    # 复用现有技能(只解析 skill_id,不建技能)
    for skill_name, questions in REUSE_GROUPS.items():
        skill = existing_skills.get(skill_name)
        if not skill:
            print(f"✗ 复用组 {skill_name} 不存在于 {args.env},跳过 {len(questions)} 行")
            continue
        print(f"• 复用现有技能: {skill_name} ({skill['id'][:8]}) ← {len(questions)} 行")
        for q in questions:
            mapping[q] = skill["id"]

    n = asyncio.run(_db_backfill(args.env, mapping))
    print(f"\n✓ DB 回填完成: {args.env} 共补 {n} 条(skill_id + param_template)")
    print("复核建议:uv run python /tmp/inventory_questionbank.py %s" % args.env)


if __name__ == "__main__":
    sys.exit(main())
