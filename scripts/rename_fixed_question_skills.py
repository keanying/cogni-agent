#!/usr/bin/env python
"""固定问题技能改名:「数字大脑-X」→「固定问题-X」(幂等,可重复执行)。

    uv run python scripts/rename_fixed_question_skills.py --env dev [--apply]

背景(26 号):
- 「数字大脑-」前缀是早期产品名,这批技能实际是 APP 固定问题(问题库直绑 skill_id)
  的执行体,名字应体现用途而非品牌 → 统一改「固定问题-」前缀。
- 不改名清单:「数字大脑-预热摘要」——由 cronjobs/prewarm.py 按名字查技能,
  且它不绑任何固定问题(预热调度直达),改名会连带改运行时代码与重新部署。

连带必须同步的三处(不同步 = 线上事故):
  ① 技能本身 name(PUT 只落草稿,必须发版才生效)
  ② Agent 的 intent_config.prompt 路由映射(13 行「关键词 → 技能名」)
  ③ Agent 版本 pin + 用户指派(不切则用户仍走旧版本,路由映射还是旧名)

默认只打印计划(--apply 才落库),遵循「写操作先列清单」铁律。

⚠️ 已知坑(2026-09-15 dev 实测):本脚本的 ① 改名**不生效**。PUT /skills/{id}
只把内容写进版本快照,而 skill adapter 的字段白名单不含 name → 快照存不下名字;
运行时与管理端列表读的是主表 tc_skills.name。改名必须直接改主表(26 号已用
UPDATE numeri_core_tc_skills SET name=... 完成 13 个)。本脚本 ① 仍会跑并发版,
但产出的是「内容无变化的空版本」;真正有用的只有 ② Agent 路由映射同步与 ③ 切指派。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys

OLD_PREFIX = "数字大脑-"
NEW_PREFIX = "固定问题-"
# 不参与改名的技能(按完整名精确排除)
KEEP_AS_IS = {"数字大脑-预热摘要"}
AGENT_NAME = "景区数字大脑"


def _load_p0():
    """复用 create_p0_group_skills 的登录/请求封装,避免重复实现。"""
    spec = importlib.util.spec_from_file_location(
        "p0", "scripts/create_p0_group_skills.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _latest(vers: list[dict], status: str):
    return next((v for v in sorted(vers, key=lambda v: v.get("version") or 0, reverse=True)
                 if v.get("status") == status), None)


def rename_skill(m, base: str, token: str, skill: dict, new_name: str) -> str:
    """技能改名 + 发版。返回新版本 id。"""
    sid = skill["id"]
    vers = m.call(base, token, "GET", f"/resources/skill/{sid}/versions").get("items", [])
    snap = (_latest(vers, "published") or {}).get("snapshot") or {}
    upd = {
        "name": new_name,
        "description": snap.get("description"),
        "suggested_mode": snap.get("suggested_mode"),
        "knowledge_sources": snap.get("knowledge_sources"),
        "constraints": snap.get("constraints"),
        "tool_usage": snap.get("tool_usage"),
        "prompt": snap.get("prompt"),
        "tool_ids": [t.get("tool_id") for t in (snap.get("tools") or []) if t.get("tool_id")],
    }
    upd = {k: v for k, v in upd.items() if v is not None}
    m.call(base, token, "PUT", f"/skills/{sid}", upd)
    vers = m.call(base, token, "GET", f"/resources/skill/{sid}/versions").get("items", [])
    draft = _latest(vers, "draft")
    vid = draft["id"] if draft else m.call(
        base, token, "POST", f"/resources/skill/{sid}/versions",
        {"note": f"固定问题技能改名: {skill['name']} → {new_name}"}).get("id")
    m.call(base, token, "POST", f"/versions/{vid}/refresh-pins")
    m.call(base, token, "POST", f"/versions/{vid}/publish")
    return vid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev", choices=["local", "dev"])
    ap.add_argument("--account", default="pftroot")
    ap.add_argument("--password", default=None)
    ap.add_argument("--apply", action="store_true", help="默认只打印计划,加此参数才落库")
    args = ap.parse_args()

    m = _load_p0()
    base = m.ENVS[args.env]
    token = None
    for pwd in ([args.password] if args.password else ["0000@123", "pftroot123"]):
        try:
            token = m.login(base, args.account, pwd)
            print(f"✓ 登录成功({args.account}) @ {args.env}")
            break
        except SystemExit:
            continue
    if not token:
        raise SystemExit("登录失败:账号密码均不匹配")

    skills = m.call(base, token, "GET", "/skills").get("items", [])
    renames = []
    for s in skills:
        name = s["name"]
        if not name.startswith(OLD_PREFIX) or name in KEEP_AS_IS:
            continue
        renames.append((s, NEW_PREFIX + name[len(OLD_PREFIX):]))

    print(f"\n① 技能改名计划({len(renames)} 个,保留 {len(KEEP_AS_IS)} 个不动):")
    for s, new in renames:
        print(f"  - {s['name']}  →  {new}")
    if not renames:
        print("  (无需要改名的技能)")
    if not args.apply:
        print("\n[DRY-RUN] 未落库。确认无误后加 --apply 执行。")
        return

    for s, new in renames:
        vid = rename_skill(m, base, token, s, new)
        print(f"  ✓ 已改名并发布: {new} (vid={vid[:8]})")

    # ── ② Agent 路由映射同步 + 发版 + 切指派 ──────────────────────────
    print("\n② Agent 路由映射同步")
    agent = next((a for a in m.call(base, token, "GET", "/agents").get("items", [])
                  if a["name"] == AGENT_NAME), None)
    if not agent:
        raise SystemExit(f"Agent[{AGENT_NAME}] 不存在")
    aid = agent["id"]
    avers = m.call(base, token, "GET", f"/resources/agent/{aid}/versions").get("items", [])
    pub = _latest(avers, "published") or {}
    snap = pub.get("snapshot") or {}
    intent = dict(snap.get("intent_config") or {})
    old_prompt = intent.get("prompt") or ""
    new_prompt = old_prompt.replace(OLD_PREFIX, NEW_PREFIX)
    sids = [b.get("skill_id") for b in (snap.get("bound_skills") or []) if b.get("skill_id")]

    upd = {k: snap.get(k) for k in ["persona_desc", "tone_style", "opening_line",
                                    "composition_mode", "rate_limit_qps", "rate_limit_daily"]}
    upd["bound_skill_ids"] = sids
    upd["intent_config"] = {**intent, "prompt": new_prompt}
    upd = {k: v for k, v in upd.items() if v is not None}
    m.call(base, token, "PUT", f"/agents/{aid}", upd)
    print(f"  ✓ intent 路由映射已替换({len(renames)} 处技能名),绑定 {len(sids)} 技能")

    avers = m.call(base, token, "GET", f"/resources/agent/{aid}/versions").get("items", [])
    draft = _latest(avers, "draft")
    vid = draft["id"] if draft else m.call(
        base, token, "POST", f"/resources/agent/{aid}/versions",
        {"note": "固定问题技能改名:同步路由映射并刷新 pin"}).get("id")
    m.call(base, token, "POST", f"/versions/{vid}/refresh-pins")
    m.call(base, token, "POST", f"/versions/{vid}/publish")
    print(f"  ✓ Agent 已发布新版本 (vid={vid[:8]})")

    # ③ 指派切换(不切 = 用户仍走旧版本,路由还是旧技能名 → 必切)
    assigns = m.call(base, token, "GET", f"/agents/{aid}/assignments").get("items", [])
    if not assigns:
        print("③ 无指派记录,跳过")
    else:
        print(f"③ 切换指派({len(assigns)} 条)")
        for a in assigns:
            uid = a.get("scope_id") or a.get("user_id")
            if a.get("version_id") == vid:
                print(f"  - {str(uid)[:10]}: 已是新版,跳过")
                continue
            m.call(base, token, "POST", f"/agents/{aid}/assignments",
                   {"user_id": uid, "version_id": vid, "note": "固定问题技能改名:切到同步后的新版本"})
            print(f"  ✓ {str(uid)[:10]} → v{vid[:8]}")

    print("\n完成。后续同步项(代码常量,需人工改后提交):")
    print("  · scripts/create_p0_group_skills.py GROUPS[*].name / SUMMARY_SKILL 保留")
    print("  · scripts/merge_agent_skills.py INTENT_PROMPT / OVERVIEW_SKILL")
    print("  · scripts/ensure_overview_guide_card.py SKILL_NAME")


if __name__ == "__main__":
    sys.exit(main())
