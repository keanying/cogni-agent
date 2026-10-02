#!/usr/bin/env python
"""26 号后续:① 概览解读补票务域工具 ② 每日报表趋势 新版替换旧版 ③ 旧版/副本软删。

## ① 固定问题-概览解读 补票务域工具

原 4 工具只覆盖「今日绝对值」(今日客流统计 / 今日报表汇总 / 舆情 / OTA),
概览要说清「票务变化」和「渠道口径」时缺分母 → 补:
  - 报表比对汇总(票务环比:今日 vs 昨日/上周)
  - 销售渠道报表汇总(渠道口径:各渠道出票/核销占比)
prompt 相应补第 5、6 步取数,并挂 tool_usage 说明;两工具都是「按需调用」,
不是必调,避免概览为了凑数据多打两次接口。

## ② 每日报表趋势 新版替换旧版

旧版(dailyReportTrend, ae50c607)被 3 个技能绑定;新版(api/touristDaily...)
已就绪但零绑定。做法:3 个技能 pin 换新版 → 旧版转零绑定 → 软删旧版与
「-导入」副本(导入残留,被已下线技能引用)→ 新版工具改回规范名「每日报表趋势」
(prompt 里写的就是这个名字,改名后 prompt 无需改动)。

工具名走主表(Tool.name;PUT 接口只写快照、且快照字段白名单不含 name),与
rename_fixed_question_skills.py 的技能改名同理。

用法:
    ZHILV_ENV=dev .venv/bin/python scripts/patch_overview_and_trend.py [--apply] \
        [--delete-tools] [--reassign]
默认 dry-run;--delete-tools 才软删工具;--reassign 才切用户指派。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from cogni_agent.common.db import get_sessionmaker  # noqa: E402
from cogni_agent.tool_center_service import versioning as ver  # noqa: E402
from cogni_agent.tool_center_service.agent.models import Agent  # noqa: E402
from cogni_agent.tool_center_service.versioning import ResourceAssignment  # noqa: E402

CREATED_BY = "概览补票务 + 趋势新版替换"

OVERVIEW_SKILL = "固定问题-概览解读"
# 概览新增工具:名 → 何时用/何时不用(tool_usage 合成进系统提示)
OVERVIEW_ADD_TOOLS = {
    "报表比对汇总": "票务环比口径:当日售票/收入与昨日或上周对比。仅在概览需要说明"
                    "「变化趋势」时调用;用户只要当日绝对值不要调。",
    "销售渠道报表汇总": "渠道口径:各渠道出票量与核销占比。仅在概览需要说明渠道结构"
                        "时调用;只讲全园总量时不要调。",
}
# 插在「硬约束」之前:取数步骤编号接原 1~4
OVERVIEW_PROMPT_STEPS = (
    "5. 调用「报表比对汇总」取当日票务环比(与昨日/上周对比),"
    "仅在需要说明变化趋势时调用,参数由系统注入或按工具必填项提供;\n"
    "6. 调用「销售渠道报表汇总」取各渠道出票/核销占比,"
    "仅在需要说明渠道结构时调用。\n"
)

# ② 每日报表趋势:旧版 → 新版(3 个技能)
TREND_SKILLS = ["固定问题-客流时段与高峰", "固定问题-客流趋势对比", "固定问题-票务月度进度"]
TREND_OLD = "每日报表趋势"
TREND_NEW = "每日报表趋势-新版"
# ③ 软删清单 **按 id 锁定**(删除前必须已零绑定)
# 不能按名字:②b 会把新版改回「每日报表趋势」,按名字删会误杀改完名的新版工具。
# 这里两个 id 都是旧版 dailyReportTrend 及其 -导入 副本(均已于 26 号软删,留作幂等)。
DELETE_TOOL_IDS = (
    "ae50c607-5aea-4135-aa6f-90210fa49f07",  # 每日报表趋势 旧版
)

AGENT_NAME = "景区数字大脑"
from create_p0_group_skills import ENVS as ADMIN_ENVS  # noqa: E402

ADMIN = {k: v.replace("/api/admin", "") for k, v in ADMIN_ENVS.items()}


# ── 通用:技能补/换工具并发布 ─────────────────────────────────────────
async def patch_skill(db, name: str, new_tool_ids: list[str],
                      usage: dict | None, prompt: str | None, note: str,
                      apply: bool) -> None:
    sid = (await db.execute(text(
        "SELECT id FROM numeri_core_tc_skills WHERE name=:n"), {"n": name})).scalar()
    if sid is None:
        print(f"  未找到技能「{name}」")
        return
    pub = await ver.latest_published_version(db, "skill", sid)
    if pub is None:
        print(f"  {name} 无已发布版本")
        return
    base = dict(pub.snapshot or {})
    cur = [str(t.get("tool_id")) for t in (base.get("tools") or []) if t.get("tool_id")]
    if cur == new_tool_ids and (prompt is None or prompt == base.get("prompt")):
        print(f"  - {name}: 工具与 prompt 已是目标状态,跳过(幂等)")
        return
    patch = {"tool_ids": new_tool_ids}
    if usage is not None:
        patch["tool_usage"] = {**(base.get("tool_usage") or {}), **usage}
    if prompt is not None:
        patch["prompt"] = prompt
    if not apply:
        print(f"  [dry] {name}: 工具 {len(cur)} → {len(new_tool_ids)}"
              f"{', prompt 更新' if prompt is not None else ''}")
        return
    stale = await ver.latest_draft(db, "skill", sid)
    if stale is not None:
        await ver.discard_draft(db, stale.id)
    draft = await ver.derive_version(db, "skill", sid, base_version_id=pub.id,
                                     snapshot=patch, created_by=CREATED_BY, note=note)
    await ver.refresh_pins(db, draft.id)
    out = await ver.publish_version(db, draft.id)
    print(f"  ✓ {name} v{out['version']} 已发布(工具 {len(cur)} → {len(new_tool_ids)})")


async def patch_overview(db, tool_id: dict[str, str], apply: bool) -> None:
    print("① 概览解读补票务域工具")
    sid = (await db.execute(text(
        "SELECT id FROM numeri_core_tc_skills WHERE name=:n"),
        {"n": OVERVIEW_SKILL})).scalar()
    pub = await ver.latest_published_version(db, "skill", sid)
    base = dict(pub.snapshot or {})
    prompt = base.get("prompt") or ""
    if "报表比对汇总" in prompt:
        print("  - 概览 prompt 已含新取数步骤,跳过(幂等)")
        return
    idx = prompt.find("\n硬约束")
    if idx == -1:
        print("  ✗ 未找到「硬约束」锚点,人工确认 prompt 结构后再补")
        return
    new_prompt = prompt[:idx] + "\n" + OVERVIEW_PROMPT_STEPS + prompt[idx + 1:]
    cur = [str(t.get("tool_id")) for t in (base.get("tools") or []) if t.get("tool_id")]
    ids = cur + [tool_id[n] for n in OVERVIEW_ADD_TOOLS if tool_id[n] not in cur]
    usage = {tool_id[n]: d for n, d in OVERVIEW_ADD_TOOLS.items()}
    await patch_skill(db, OVERVIEW_SKILL, ids, usage, new_prompt,
                      "概览补票务域工具:环比 + 渠道口径", apply)


async def swap_trend(db, tool_id: dict[str, str], apply: bool) -> None:
    print("\n② 每日报表趋势 新版替换旧版")
    old_id, new_id = tool_id.get(TREND_OLD), tool_id.get(TREND_NEW)
    if not new_id:
        print(f"  ✗ 未找到工具「{TREND_NEW}」,中止")
        return
    for name in TREND_SKILLS:
        sid = (await db.execute(text(
            "SELECT id FROM numeri_core_tc_skills WHERE name=:n"), {"n": name})).scalar()
        if sid is None:
            print(f"  - {name} 不存在,跳过")
            continue
        pub = await ver.latest_published_version(db, "skill", sid)
        base = dict(pub.snapshot or {})
        cur = [str(t.get("tool_id")) for t in (base.get("tools") or []) if t.get("tool_id")]
        if old_id not in cur:
            print(f"  - {name}: 未绑旧版,跳过(幂等)")
            continue
        ids = [new_id if t == old_id else t for t in cur]
        await patch_skill(db, name, ids, None, None,
                          f"每日报表趋势:旧版 → 新版({TREND_NEW})", apply)


async def rename_trend_tool(db, tool_id: dict[str, str], apply: bool) -> None:
    """新版工具改回规范名(旧版删除后名字空出)。"""
    print("\n②b 新版工具改名")
    new_id = tool_id.get(TREND_NEW)
    if not new_id:
        print(f"  - 「{TREND_NEW}」已改名或不存在,跳过(幂等)")
        return
    if not apply:
        print(f"  [dry] {TREND_NEW} → {TREND_OLD}")
        return
    await db.execute(text("UPDATE numeri_core_tc_tools SET name=:n WHERE id=:i"),
                     {"n": TREND_OLD, "i": new_id})
    await db.commit()
    print(f"  ✓ {TREND_NEW} → {TREND_OLD}")


def delete_tools(env: str, tool_rows: list[tuple[str, str]], apply: bool) -> None:
    """③ 软删(force,自动解除残留绑定)。走 API:删除内含引用检查。"""
    print("\n③ 软删旧版/副本工具")
    if not apply:
        for name, _ in tool_rows:
            print(f"  [dry] 将软删 {name}")
        return
    base = ADMIN[env]
    token = None
    for pwd in ("0000@123", "pftroot123"):
        r = httpx.post(f"{base}/api/admin/auth/login",
                       json={"phone": "pftroot", "password": pwd, "rsa_encrypt": False},
                       timeout=20)
        if r.status_code != 200:
            continue
        token = (r.json().get("data") or {}).get("access_token")
        if token:
            break
    if not token:
        raise SystemExit("登录失败")
    h = {"Authorization": f"Bearer {token}"}
    for name, tid in tool_rows:
        r = httpx.delete(f"{base}/api/admin/tools/{tid}?force=true", headers=h, timeout=20)
        print(f"  {'✓' if r.status_code < 300 else '✗'} {name}: {r.status_code} {r.text[:120]}")


async def refresh_agent(db, apply: bool, reassign: bool) -> None:
    """技能发新版后 agent 必须刷 pin 重发,否则用户仍走旧 pin(补丁不生效)。"""
    print("\n④ Agent 联动")
    agent = (await db.execute(select(Agent).where(Agent.name == AGENT_NAME))).scalars().first()
    if agent is None:
        print(f"  未找到 agent「{AGENT_NAME}」")
        return
    pub = await ver.latest_published_version(db, "agent", agent.id)
    if not apply:
        print(f"  [dry] {AGENT_NAME}: 派生草稿 + refresh skill pin + 发布")
    else:
        stale = await ver.latest_draft(db, "agent", agent.id)
        if stale is not None:
            await ver.discard_draft(db, stale.id)
        draft = await ver.derive_version(
            db, "agent", agent.id, base_version_id=pub.id, snapshot={},
            created_by=CREATED_BY, note="概览补工具 + 趋势新版:刷新 skill pin")
        res = await ver.refresh_pins(db, draft.id)
        out = await ver.publish_version(db, draft.id)
        print(f"  ✓ {AGENT_NAME} v{out['version']} 已发布"
              f"(skill pin 变化 {len(res.get('changed') or [])} 个)")
        vid = draft.id
        assigns = (await db.execute(select(ResourceAssignment).where(
            ResourceAssignment.agent_id == agent.id))).scalars().all()
        if not reassign:
            print(f"  指派 {len(assigns)} 条未切(加 --reassign 切到 v{out['version']},"
                  f"不切则用户仍走旧 pin,补丁不生效)")
            return
        for a in assigns:
            if a.version_id == vid:
                continue
            a.version_id = vid
        await db.commit()
        print(f"  ✓ 已切换指派 {len(assigns)} 条 → v{out['version']}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev", choices=["dev", "local"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete-tools", action="store_true", help="执行软删(默认只列清单)")
    ap.add_argument("--reassign", action="store_true", help="切换用户指派到新版本")
    args = ap.parse_args()
    print(f"===== {'APPLY' if args.apply else 'DRY-RUN'}: 概览补票务 + 趋势新版替换 =====")
    async with get_sessionmaker()() as db:
        rows = (await db.execute(text(
            "SELECT id, name FROM numeri_core_tc_tools WHERE deleted_at IS NULL"))).all()
        # 同名工具取第一个(-导入 副本与正式版同名时以正式版为准)
        tool_id = {n: str(i) for i, n in rows}
        await patch_overview(db, tool_id, args.apply)
        await swap_trend(db, tool_id, args.apply)
        del_rows = [(n, str(i)) for i, n in rows if str(i) in DELETE_TOOL_IDS]
        delete_tools(args.env, del_rows, args.apply and args.delete_tools)
        await rename_trend_tool(db, tool_id, args.apply)
        if args.apply:
            await refresh_agent(db, args.apply, args.reassign)
    print("\n完成")


if __name__ == "__main__":
    asyncio.run(main())
