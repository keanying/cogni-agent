#!/usr/bin/env python
"""概览引导卡固定路由补齐(首页 overview 卡 question_id + workflow 直跳 + 预热直出)。

背景(为什么补):
  首页 5 张主题引导卡中,客流/票务/舆情/OTA直销 4 主题在问题库有 theme_code 行
  (绑定执行技能 + prewarm_enabled),home 的 theme_first 按 theme_code
  匹配到行 → guide_cards[].question_id 有值,点击锁固定问题 → 技能直跳 + 预热缓存直出;
  而 overview(概览)主题在问题库无行 → 概览卡走静态壳分支 question_id="",点击只发
  文本,归一化匹配 miss → 走 agent 动态链路(无固定路由 / 无预热 / 无来源统计)。

本脚本把概览补齐到与 4 主题同构(幂等,可重复执行):
  ① 技能「固定问题-概览解读」升级:
     - published prompt 无【结构化图表输出(PRD 3.2.2.4)】段 → 追加该段并发布新版本
       (内容全量继承 published 快照,不覆盖运营既有微调;判重只认该段标记);
     - visibility 对齐 public(现为 group——scenic_operator 不在 GROUP_ROLES,
       APP/预热路由会鉴权失败;4 个预热成功的主题解读技能均 public)。
  ② 问题库 theme_code=overview 的【主题解读】概览行
     (字段对齐【主题解读】客流模板:period=today / P0 / return_format=card /
      skill_id 直绑概览技能 / prewarm_enabled;预热调度按业务日判重,无需配时点)。

23 号:原「单技能组合」恒等转发层已删(0055 删表),问题行直接绑 skill_id。

数据层全部走运行时配置(管理 API + DB),不涉及代码/部署。

用法:
  uv run python scripts/ensure_overview_guide_card.py --env dev [--account pftroot] [--password ...]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import uuid

# 结构化输出段标记(判重 + 锚点,与主题解读技能 PROMPT_TMPL 同一引导源)
_STRUCT_MARKER = "结构化图表输出(PRD 3.2.2.4"
_STRUCT_SECTION = """【结构化图表输出(PRD 3.2.2.4,必须执行)】
回答末尾追加一个 ```json 代码块,按四域数据场景选择图表形态:
- 数据占比(舆情平台/情感、OTA 渠道占比) → 饼状图: {"charts":[{"title":"...","type":"pie","data":[{"name":"平台A","value":123},...]}]}
- 数据变化/趋势 → 折线图: {"charts":[{"title":"近7天趋势","type":"line","x":["日期",...],"series":[{"name":"客流","data":[1,2,...]}]}]}
- 数量对比(多对象/渠道/票种) → 条形图: {"charts":[{"title":"各渠道对比","type":"bar","x":["渠道A","渠道B",...],"series":[{"name":"出票量","data":[10,20,...]}]}]}
- 单一关键数量(今日入园数/销售收入/退票率/负面占比) → 数据卡片: {"cards":[{"label":"今日入园人数","value":"8523","unit":"人","trend":"up"}]}
规则:数字必须来自工具返回;一次回答图表合计不超过 3 个(cards+charts);至少把四域最核心的单值放入 cards,没有合适图表场景时只输出 cards;json 必须合法,不要嵌套注释。"""

# 执行纪律段:概览=四域叠加,数据量大的景区(PFTSCA 等)若照常"全量取数+长文复述"会超过
# 任务级 Token 预算(默认 50000),实测 PFTSCA 报告任务因此 FAILED(见 commit/会话记录)。
_DISCIPLINE_MARKER = "【执行纪律(避免任务超时/超预算,必须遵守)】"
_DISCIPLINE_SECTION = """【执行纪律(避免任务超时/超预算,必须遵守)】
1. 工具调用总次数不超过 6 次:同一工具同参数禁止重复调用;某域数据缺失(如 OTA 无账号、某域无数据)时直接在正文标注"暂无",禁止反复尝试补齐。
2. 工具返回的长表格/长列表只提取正文与 json 要用到的关键指标与数字,禁止在回复里复述表格全文。
3. 正文四域解读每域不超过 3 句话、整体 ≤500 字,信息密度优先;正文不要重复 json 里已出现的数字。
4. 最终 json(cards+charts)只放被正文引用的核心数字,合计不超过 6 个(cards≤3、charts≤3)。"""

_EXTRA_SECTIONS = [
    (_STRUCT_MARKER, _STRUCT_SECTION),
    (_DISCIPLINE_MARKER, _DISCIPLINE_SECTION),
]

SKILL_NAME = "固定问题-概览解读"
QUESTION_TEXT = "【主题解读】概览"
THEME_CODE = "overview"

ENVS = {
    "local": "http://localhost:8000",
    "dev": "https://cogni-agent.12301dev.com:9443",
}


def login(base: str, account: str, pwd: str) -> str:
    import httpx

    r = httpx.post(f"{base}/api/admin/auth/login",
                   json={"phone": account, "password": pwd, "rsa_encrypt": False}, timeout=20)
    body = r.json()
    if r.status_code != 200:
        raise SystemExit(f"登录失败: {r.status_code} {str(body)[:300]}")
    data = body.get("data", body) if isinstance(body, dict) else {}
    token = (data or {}).get("access_token")
    if not token:
        raise SystemExit(f"登录响应无 access_token: {str(body)[:300]}")
    return token


def call(base: str, token: str, method: str, path: str, body=None):
    import httpx

    r = httpx.request(method, f"{base}/api/admin{path}",
                      headers={"Authorization": f"Bearer {token}"}, json=body, timeout=60)
    try:
        data = r.json()
    except Exception:
        raise SystemExit(f"{method} {path} 返回非JSON: {r.status_code} {r.text[:300]}")
    if r.status_code >= 400:
        raise SystemExit(f"{method} {path} 失败: {r.status_code} {str(data)[:400]}")
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


# ── DB 层(问题行 upsert + skill visibility 对齐) ──────────────────────────
async def _db_apply(env: str, skill_id: str) -> None:
    os.environ["ZHILV_ENV"] = env
    from sqlalchemy import text

    from cogni_agent.common.db import get_engine
    from cogni_agent.gateway_service.models import AppQuestionBank

    tq = AppQuestionBank.__tablename__
    async with get_engine().begin() as conn:
        # visibility 对齐 public(与 4 个主题解读技能一致,scenic_operator 可见)
        res = await conn.execute(text(
            "UPDATE numeri_core_tc_skills SET visibility='public' "
            "WHERE id=:sid AND visibility <> 'public'"), {"sid": skill_id})
        if res.rowcount:
            print(f"✓ 技能主表 visibility 对齐 public ({skill_id[:8]})")
        # 问题行 upsert
        row = (await conn.execute(text(
            f"SELECT id FROM {tq} WHERE question_text=:qt AND theme_code=:tc LIMIT 1"),
            {"qt": QUESTION_TEXT, "tc": THEME_CODE})).first()
        now = _dt.datetime.now(_dt.timezone.utc)
        if row:
            await conn.execute(text(
                f"UPDATE {tq} SET skill_id=:sid, "
                f"return_format='card', prewarm_enabled=true, active=true, "
                f"param_template=:pt, updated_at=:now WHERE id=:rid"),
                {"sid": skill_id, "pt": json.dumps({"travelDate": "{{today}}"}),
                 "now": now, "rid": row[0]})
            print(f"✓ 问题行已对齐(存在): {QUESTION_TEXT} ({row[0][:8]}) skill={skill_id[:8]}")
        else:
            max_sort = (await conn.execute(text(
                f"SELECT COALESCE(MAX(sort_order),0)+1 FROM {tq} "
                f"WHERE question_text LIKE '【主题解读】%'"))).scalar() or 1
            qid = str(uuid.uuid4())
            await conn.execute(text(
                f"INSERT INTO {tq} (id, scenic_id, theme_code, question_text, metric_code, "
                f"metric_name, scenario, time_period, priority, dimension_name, return_format, "
                f"skill_id, param_template, agent_id, prewarm_enabled, "
                f"prewarm_point, next_run_at, sort_order, active) "
                f"VALUES (:id, NULL, :tc, :qt, NULL, NULL, '日常巡检', 'today', 'P0', NULL, "
                f"'card', :sid, :pt, NULL, true, NULL, NULL, :so, true)"),
                {"id": qid, "tc": THEME_CODE, "qt": QUESTION_TEXT, "sid": skill_id,
                 "pt": json.dumps({"travelDate": "{{today}}"}), "so": max_sort})
            print(f"✓ 问题行已创建: {QUESTION_TEXT} ({qid[:8]}) skill={skill_id[:8]} sort={max_sort}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="local", choices=list(ENVS))
    ap.add_argument("--account", default="pftroot")
    ap.add_argument("--password", default=None, help="缺省依次尝试 0000@123 / pftroot123")
    args = ap.parse_args()

    base = ENVS[args.env]
    token = None
    for pwd in ([args.password] if args.password else ["0000@123", "pftroot123"]):
        try:
            token = login(base, args.account, pwd)
            print(f"✓ 登录成功({args.account} @ {args.env})")
            break
        except SystemExit:
            continue
    if not token:
        raise SystemExit("登录失败:账号密码均不匹配(可用 --password 显式指定)")

    # ── ① 技能:内容升级(补结构化段) + 发布 ──
    skills = {s["name"]: s for s in call(base, token, "GET", "/skills").get("items", [])}
    skill = skills.get(SKILL_NAME)
    if not skill:
        raise SystemExit(f"技能 {SKILL_NAME} 不存在,先在该环境创建(P0 分组技能/doc-driven 生成器),再跑本脚本")
    skill_id = skill["id"]
    vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
    pub = next((v for v in vers if v.get("status") == "published"), None)
    if pub is None:
        raise SystemExit(f"技能 {SKILL_NAME} 无 published 版本,先发布再跑本脚本")
    snap = pub.get("snapshot") or {}
    prompt = snap.get("prompt") or ""
    missing = [sec for marker, sec in _EXTRA_SECTIONS if marker not in prompt]
    if not missing:
        print(f"• 技能 {SKILL_NAME} prompt 已含全部升级段落,跳过内容升级")
    else:
        # 内容键纯快照化:更新必须携带完整内容,未改字段继承 published 快照
        upd = {k: snap.get(k) for k in
               ("description", "suggested_mode", "knowledge_sources", "constraints", "tool_usage")}
        upd["tool_ids"] = [t.get("tool_id") for t in (snap.get("tools") or [])]
        upd = {k: v for k, v in upd.items() if v is not None}
        upd["prompt"] = prompt.rstrip() + "\n\n" + "\n\n".join(missing)
        note = "概览引导卡固定路由:追加" + ",".join(
            "结构化图表输出段" if m == _STRUCT_SECTION else "执行纪律段" for m in missing)
        call(base, token, "PUT", f"/skills/{skill_id}", upd)
        draft = next((v for v in vers if v.get("status") == "draft"), None)
        vid = draft["id"] if draft else call(
            base, token, "POST", f"/resources/skill/{skill_id}/versions",
            {"note": note}).get("id")
        call(base, token, "POST", f"/versions/{vid}/refresh-pins")
        call(base, token, "POST", f"/versions/{vid}/publish")
        print(f"✓ 技能升级+发布新版本: {SKILL_NAME} ({skill_id[:8]}) 缺段={len(missing)}")

    # ── ② DB 层:问题行直绑技能 + visibility ──
    # 23 号:原「单技能组合」恒等转发层已删(0055 删表),问题行直接绑 skill_id。
    asyncio.run(_db_apply(args.env, skill_id))

    print("\n完成。概览卡 question_id 由 home 实时读取问题库,立即生效;"
          "预热缓存(next_run_at 已置 now)由最近一轮预热调度派单,出缓存后点击概览卡直出卡片/图表。")


if __name__ == "__main__":
    main()
