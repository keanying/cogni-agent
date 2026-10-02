#!/usr/bin/env python
"""技能收敛(23 号后续):Agent 侧与问题侧两套技能合并为一套,以问题侧 13 个为主。

背景:dev/local 曾有两套并行技能——问题侧 13 个细粒度(APP 固定问题直跳,两阶段
纪律 prompt + 正式版工具)与 Agent「景区数字大脑」绑定的 5 个粗粒度旧技能
(早期手写 prompt + 「-导入」后缀的工具副本),功能域完全重叠。收敛方向:
细粒度技能的 description 已按「何时用/何时不用」写清,直接作为 Agent 自由
询问的路由候选;粗粒度旧技能与 -导入 工具副本全部下线。

步骤(全部走管理端 API + 发版,幂等可重复):
  ① 概览解读换正式版工具:tool_ids 换正式版,prompt 中「-导入」工具名同步替换,
     发新版本并发布(问题侧唯一还在用 -导入 工具的技能,不先换会连带删工具失败);
  ② Agent 景区数字大脑 改绑 13 个技能 + intent_config 路由提示词重写为 13 技能
     映射,发版并发布(运行时读 published 快照的 bound_skills,PUT 只落草稿);
  ③ 软删 5 个粗粒度旧技能(force,自动解除残留绑定);
  ④ 软删全部「-导入」后缀工具副本(force)。

用法:
  uv run python scripts/merge_agent_skills.py --env local|dev [--account pftroot] \
      [--reassign-assignments]
  # --reassign-assignments:②b 切换分配钉版本——影响用户侧生效版本,须用户
  #   确认后使用(分配可能是有意留在旧版本);缺省只打印钉版现状不动钉。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from create_p0_group_skills import call, login  # noqa: E402

AGENT_NAME = "景区数字大脑"
OVERVIEW_SKILL = "固定问题-概览解读"

# 5 个粗粒度旧技能(Agent 侧,功能域被 13 个细粒度技能覆盖)
OLD_SKILLS = [
    "数字大脑-客流分析",
    "数字大脑-票务经营报表",
    "数字大脑-多维报表分析",
    "数字大脑-雷达图与OTA体检",
    "数字大脑-舆情分析",
]

# 概览解读 prompt/工具名替换表(长名优先,避免前缀吞并)
OVERVIEW_TOOL_RENAME = [
    ("舆情-维度分数和平台占比-导入", "舆情-维度分数和平台占比"),
    ("通过景区ID获取供应商ID列表-导入", "通过景区ID获取供应商ID列表"),
    ("今日客流统计-导入2", "今日客流统计"),
    ("今日客流统计-导入", "今日客流统计"),
    ("今日报表汇总-导入", "今日报表汇总"),
    ("OTA指标-导入", "OTA指标"),
]

# 新 intent_config 路由提示词(13 技能关键词映射;mode/model/recheck 沿用原值)
INTENT_PROMPT = """根据用户请求挂载对应技能，可同时挂载多个：
- 当日在园人数/实时客流/承载率/客流预警 → 固定问题-客流今日实时
- 今日高峰时段/各时段人流/本周客流高峰 → 固定问题-客流时段与高峰
- 近7天客流趋势/与上周环比 → 固定问题-客流趋势对比
- 昨日售票量/收入/退票/各票种销量 → 固定问题-票务昨日汇总
- 本周票种占比/畅销票种/线上线下渠道比例/OTA渠道贡献 → 固定问题-票务周度经营
- 本月收入/目标达成进度/售票趋势 → 固定问题-票务月度进度
- 未来3天预售量及变化 → 固定问题-票务未来预售
- 当前舆情情感/突发热点/负面占比 → 固定问题-舆情当前态势
- 近7天/本月舆情趋势 → 固定问题-舆情趋势
- 平台舆情分布/游客痛点/宣传亮点/OTA评分环比 → 固定问题-舆情平台与亮点
- 当前各渠道出票量/客单价/自营与OTA占比/渠道周对比 → 固定问题-OTA渠道明细
- 渠道核销率/本周渠道订单及异常 → 固定问题-OTA当前汇总
- 整体经营概况/全局态势 → 固定问题-概览解读
跨维度对比（如「客流和收入趋势」）同时挂载对应多个技能；无法判断时优先反问关键参数再执行。"""


def _latest(vers: list[dict], status: str) -> dict | None:
    return next((v for v in vers if v.get("status") == status), None)


def _publish_skill(base, token, skill_id: str) -> None:
    """技能草稿 → 刷新 pin → 发布(与 create_p0_group_skills 同流程)。"""
    vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
    draft = _latest(vers, "draft")
    if draft is None:
        vid = call(base, token, "POST", f"/resources/skill/{skill_id}/versions",
                   {"note": "技能收敛:Agent 与问题侧合并,以问题侧 13 技能为准"}).get("id")
    else:
        vid = draft["id"]
    call(base, token, "POST", f"/versions/{vid}/refresh-pins")
    call(base, token, "POST", f"/versions/{vid}/publish")
    print(f"  ✓ 已发布 skill 版本 (vid={vid[:8]})")


async def _db_reads(env: str, agent_id: str):
    """DB 只读(单次 event loop,避免连接池跨 loop 复用):
    问题库绑定的全部技能 id + Agent 当前 published 快照与版本 id(合并字段防丢内容)
    + 该 Agent 的全部分配[(scope_id, 钉的 version_id)](②b 切版本用)。"""
    os.environ["ZHILV_ENV"] = env
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sqlalchemy import text

    from cogni_agent.common.db import get_engine

    eng = get_engine()
    try:
        async with eng.connect() as c:
            q_sids = [r[0] for r in (await c.execute(text(
                "SELECT DISTINCT skill_id FROM numeri_core_app_question_bank "
                "WHERE skill_id IS NOT NULL"))).all()]
            pub = (await c.execute(text(
                "SELECT id, snapshot FROM numeri_core_tc_resource_versions "
                "WHERE resource_type='agent' AND resource_id=:aid AND status='published' "
                "ORDER BY created_at DESC LIMIT 1"),
                {"aid": agent_id})).first()
            pub_vid, snap = (pub[0], pub[1] or {}) if pub else (None, {})
            assigns = [(r[0], r[1]) for r in (await c.execute(text(
                "SELECT scope_id, version_id FROM numeri_core_tc_agent_assignments "
                "WHERE agent_id=:aid"), {"aid": agent_id})).all()]
            return q_sids, snap, pub_vid, assigns
    finally:
        await eng.dispose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="local", choices=["local", "dev"])
    ap.add_argument("--account", default="pftroot")
    ap.add_argument("--password", default=None, help="缺省依次尝试 0000@123 / pftroot123")
    ap.add_argument("--reassign-assignments", action="store_true",
                    help="②b 切换分配钉版本(影响用户侧生效版本,须用户确认后使用;"
                         "默认只打印现状不动钉)")
    args = ap.parse_args()
    reassign = args.reassign_assignments

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
    skills = {s["name"]: s for s in call(base, token, "GET", "/skills").get("items", [])}

    # ── ① 概览解读换正式版工具 + prompt 工具名替换 ─────────────────────
    print("① 概览解读换正式版工具")
    ov = skills.get(OVERVIEW_SKILL)
    if not ov:
        print(f"  ✗ {OVERVIEW_SKILL} 不存在,跳过")
    else:
        vers = call(base, token, "GET", f"/resources/skill/{ov['id']}/versions").get("items", [])
        pub = _latest(vers, "published") or {}
        snap = pub.get("snapshot") or {}
        # 去重保序(改名表多对一时避免同名工具重复绑定,如 -导入/-导入2 → 同一正式版)
        want_tools = list(dict.fromkeys(n for _, n in OVERVIEW_TOOL_RENAME))
        snap_ids = sorted(t.get("tool_id") for t in (snap.get("tools") or [])
                          if t.get("tool_id"))
        have_ids = sorted(t["id"] for t in tools if t["name"] in want_tools)
        already = (snap_ids == have_ids
                   and "-导入" not in (snap.get("prompt") or ""))
        if already:
            print(f"  - {OVERVIEW_SKILL} 已是正式版工具且 prompt 无 -导入 残留,跳过")
        else:
            new_prompt = snap.get("prompt") or ""
            for old, new in OVERVIEW_TOOL_RENAME:
                new_prompt = new_prompt.replace(old, new)
            missing = [n for n in want_tools if n not in tool_id]
            if missing:
                raise SystemExit(f"正式版工具缺失 {missing},中止")
            upd = {
                "description": snap.get("description"),
                "suggested_mode": snap.get("suggested_mode"),
                "knowledge_sources": snap.get("knowledge_sources"),
                "constraints": snap.get("constraints"),
                "tool_usage": snap.get("tool_usage"),
                "tool_ids": [tool_id[n] for n in want_tools],
                "prompt": new_prompt,
            }
            upd = {k: v for k, v in upd.items() if v is not None}
            call(base, token, "PUT", f"/skills/{ov['id']}", upd)
            _publish_skill(base, token, ov["id"])
            print(f"  ✓ {OVERVIEW_SKILL} 工具换正式版 {len(want_tools)} 个,prompt 同步替换")

    # ── ② Agent 改绑 13 技能 + intent_config 重写,发版发布 ───────────────
    print("② Agent 改绑问题侧技能")
    agent = next((a for a in call(base, token, "GET", "/agents").get("items", [])
                  if a["name"] == AGENT_NAME), None)
    if not agent:
        raise SystemExit(f"Agent[{AGENT_NAME}] 不存在")
    q_sids, pub_snap, pub_vid, assigns = asyncio.run(_db_reads(args.env, agent["id"]))
    if not q_sids:
        raise SystemExit("问题库无技能绑定,中止(应先跑 seed + create_p0 + backfill)")
    # 幂等:published 快照已是新路由提示词且绑定含全部问题侧技能 → 跳过 PUT/发版
    binds = pub_snap.get("bound_skills")
    snap_sids = ({b.get("skill_id") for b in binds if isinstance(b, dict)}
                 if isinstance(binds, list) else set(pub_snap.get("bound_skill_ids") or []))
    if ((pub_snap.get("intent_config") or {}).get("prompt") == INTENT_PROMPT
            and set(q_sids) <= snap_sids and pub_vid):
        vid = pub_vid
        print(f"  - {AGENT_NAME} published 快照已是新路由 + {len(snap_sids)} 技能,跳过发版")
    else:
        old_intent = pub_snap.get("intent_config") or agent.get("intent_config") or {}
        intent = dict(old_intent)
        intent["prompt"] = INTENT_PROMPT
        upd = {
            "bound_skill_ids": q_sids,
            "intent_config": intent,
            "composition_mode": pub_snap.get("composition_mode") or "pool",
            "persona_desc": pub_snap.get("persona_desc"),
            "tone_style": pub_snap.get("tone_style"),
            "opening_line": pub_snap.get("opening_line"),
            "description": pub_snap.get("description"),
            "related_questions_enabled": pub_snap.get("related_questions_enabled"),
            "related_questions": pub_snap.get("related_questions"),
        }
        upd = {k: v for k, v in upd.items() if v is not None}
        call(base, token, "PUT", f"/agents/{agent['id']}", upd)
        # PUT 只落草稿;运行时读 published 快照 → 发版刷新 pin 后发布
        avers = call(base, token, "GET",
                     f"/resources/agent/{agent['id']}/versions").get("items", [])
        draft = _latest(avers, "draft")
        vid = draft["id"] if draft else call(
            base, token, "POST", f"/resources/agent/{agent['id']}/versions",
            {"note": "技能收敛:改绑问题侧 13 技能"}).get("id")
        call(base, token, "POST", f"/versions/{vid}/refresh-pins")
        call(base, token, "POST", f"/versions/{vid}/publish")
        print(f"  ✓ {AGENT_NAME} 绑定 {len(q_sids)} 个技能,intent 路由重写,已发布 (vid={vid[:8]})")

    # ── ②b 分配钉版本切换(默认只报告,--reassign-assignments 才执行) ──────
    # 铁律:分配是「按用户提供不同版本」的机制(15 号 D9-A,付费/定制/新旧客户
    # 分流),某些用户可能有意留在旧版本——切钉 = 替用户做版本选择,必须由
    # 操作者(经用户确认)显式决定,脚本默认不动。运行时按 assignment.version_id
    # 快照解析,不切则用户仍走旧版(旧 intent + 旧技能)。
    if assigns:
        stale = [(s, v) for s, v in assigns if v != vid]
        if not reassign:
            print(f"②b 分配钉版本现状({len(assigns)} 条,未执行切换——需 --reassign-assignments):")
            for scope_id, old_vid in assigns:
                mark = "待切" if old_vid != vid else "已是新版"
                print(f"  - {scope_id[:10]}: {str(old_vid or '')[:14]} [{mark}] → 目标 v{vid[:14]}")
        else:
            print(f"②b 分配钉版本切换({len(assigns)} 条)")
            for scope_id, old_vid in assigns:
                if old_vid == vid:
                    print(f"  - {scope_id[:10]} 已钉当前版本,跳过")
                    continue
                call(base, token, "POST", f"/agents/{agent['id']}/assignments",
                     {"user_id": scope_id, "version_id": vid,
                      "note": "技能收敛:切到问题侧 13 技能新版本(操作者确认)"})
                print(f"  ✓ 分配 {scope_id[:10]}: {str(old_vid or '')[:14]} → v{vid[:14]}")
        if not reassign and stale:
            print(f"  ⚠ {len(stale)} 条钉旧版分配未切换,该部分用户仍走旧版本——"
                  f"如确认全部切新,加 --reassign-assignments 重跑")

    # ── ③ 软删 5 个粗粒度旧技能 ─────────────────────────────────────────
    print("③ 下线旧粗粒度技能")
    for name in OLD_SKILLS:
        s = skills.get(name)
        if not s:
            print(f"  - {name} 不存在(已删?),跳过")
            continue
        call(base, token, "DELETE", f"/skills/{s['id']}?force=true")
        print(f"  ✓ 已下线 {name}")

    # ── ④ 软删全部「-导入」工具副本 ────────────────────────────────────
    print("④ 下线「-导入」工具副本")
    n = 0
    for t in tools:
        if t["name"].endswith("-导入") or t["name"].endswith("-导入2"):
            call(base, token, "DELETE", f"/tools/{t['id']}?force=true")
            n += 1
            print(f"  ✓ 已下线 {t['name']}")
    if n == 0:
        print("  - 无 -导入 工具残留")

    print("\n完成。验证:GET /skills 应只剩问题侧技能;GET /agents 详情绑定 13 技能;GET /tools 无 -导入。")


if __name__ == "__main__":
    sys.exit(main())
