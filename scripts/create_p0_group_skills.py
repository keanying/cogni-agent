#!/usr/bin/env python
"""
19 号文档 S5 + 23 号:P0 分组技能创建(两阶段)+ 发布 + 问题库直绑 skill_id。

    uv run python scripts/create_p0_group_skills.py [--env local|dev]

设计(19 号 D5/D6/D7;23 号去组合层):
- 5 个分组技能不挂 agent、不进任何 pool 组合(Triager 候选集零污染);
- 两阶段约束写在 prompt:先并行取数、数据齐后一次生成(概览技能已验证模式);
- 问题行直接绑 skill_id(23 号:原「每组一条单技能组合」恒等转发层已删,0055 删表);
- visibility=public(APP 用户可见,吸取「Skill 对当前调用方不可见」事故教训)。

幂等:同名技能已存在则跳过;问题库按文本精确回填 skill_id。
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

ENVS = {
    "local": "http://localhost:8000",
    "dev": "https://cogni-agent.12301dev.com:9443",
}

# ── 分组定义:名称/描述(路由用)/两阶段 prompt/工具名/覆盖的问题文本 ─────────
# 所有取数工具的 supplierId 前置来源(必须绑进技能,x-param-source 执行期自动调用)
SUPPLIER_TOOL = "通过景区ID获取供应商ID列表"

# 二次加工技能(20 号 P3):对主题报告做摘要,输出 {"summary","metric"} JSON。
# 无工具 → L0 单次 LLM 调用;cronjobs/prewarm 派单 executor_type=skill 直达本技能。
SUMMARY_SKILL = {
    "name": "数字大脑-预热摘要",
    "description": "对预热产出的景区主题报告做二次加工:生成 60 字卡片摘要与该主题的关键彩色指标。"
                   "仅由预热调度系统调用,不要在用户对话中路由到本技能。",
    "tools": [],
    "prompt": (
        "你负责对景区数据报告做二次加工(卡片摘要)。用户消息是一份完整主题报告,"
        "你只需输出一个 ```json 代码块(不要输出其他任何文字)。分两种:\n"
        "```json\n"
        "单主题报告(客流/票务/舆情/OTA直销)只输出 summary:\n"
        '{"summary": "60字以内的一句话结论,保留关键数字与同比/环比"}\n'
        "概览报告(覆盖客流/票务/舆情/OTA直销四域)额外输出 metric 与 metrics:\n"
        '{"summary": "...",\n'
        ' "metric": {"theme": "概览", "metric": "整体最关键指标名", "value": "当前值",\n'
        '            "level": "normal|warning|danger"},\n'
        ' "metrics": [{"theme": "passenger_flow|ticket|public_opinion|ota_direct_sales",\n'
        '              "value": 0.63,\n'
        '              "display": "12,600/20,000"}]}\n'
        "```\n"
        "metrics 规则:四域逐条列出报告中有数据的维度;"
        "value 为用于分色的 0~1 比值(在园承载比/退票率/负面评价占比/核销率,"
        "报告只给绝对数且有分母时换算,无分母填 null),"
        "display 为报告中的原始展示串(数字原样保留)。\n"
        "分色规则(从报告数据判断):在园承载比≥75%警告/≥90%严重;售票收入同比下降>10%警告/>30%严重;"
        "负面占比>10%警告/>30%严重;渠道出票下降>30%警告/>50%严重;数据正常或向好=normal。"
        "报告中找不到该主题指标数据时 level=normal,metric 用报告中实际出现的核心指标。"
    ),
    "constraints": {
        "max_steps": 1,
        "缺参时": "不反问,直接按报告中可得内容生成摘要",
        "禁止编造": "数字一律来自用户消息中的报告,缺失则不带数字",
    },
    }

GROUPS = [
    {
        "name": "固定问题-客流今日实时",
        "description": "回答今日实时客流类问题:在园人数、承载率、是否触达预警阈值、与昨日同期对比。"
                      "仅当问题明确针对今日实时客流时使用;历史趋势(近7天)、昨日票务、舆情、OTA 类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "今日客流统计", "今日报表汇总", "分时报表汇总", "客流比对汇总"],
        "dims": [
            "当前在园人数 = 值(较昨日同期 ±人数 / ±%)",
            "承载率 = 在园人数 ÷ 最大承载",
            "今日累计入园 = 值",
            "预警状态 = 未触发 / 黄色(承载比≥75%) / 红色(承载比≥90%)",
        ],
        "questions": [
            "今日实时在园人数是多少？",
            "今日实时在园人数与昨日同期相比如何？",
            "今日客流是否触达预警阈值？",
            "今日客流当前承载率多少？",
            "【主题解读】客流",
        ],
    },
    {
        "name": "固定问题-票务昨日汇总",
        "description": "回答昨日票务类问题:售票收入及同比、售票总量、退票量、退票率、各票种销售。"
                      "仅当问题针对昨日票务时使用;本月累计、预售、客流、舆情类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "门票报表汇总", "产品报表汇总", "报表比对汇总"],
        "dims": [
            "售票收入 = 值(同比 ±%)",
            "售票总量 = 值(退票前)",
            "退票量 = 值",
            "退票率 = 退票量 ÷ 售票总量",
            "票种结构 = 销量 TOP3 票种及销量",
            "预警状态 = 未触发 / 黄色(退票率>5%) / 红色(退票率>10%)",
        ],
        "questions": [
            "昨日售票收入及与前日对比情况？",
            "昨日售票总量？",
            "昨日退票量有多少？",
            "昨日退票率是否异常？",
            "昨日各票种销售量是多少？",
            "【主题解读】票务",
        ],
    },
    {
        "name": "固定问题-票务月度进度",
        "description": "回答本月票务经营进度类问题:本月累计收入、目标达成进度。"
                       "仅当问题针对本月月度维度时使用;昨日票务、未来预售、客流类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "长跨度报表比对汇总", "每日报表趋势", "门票报表汇总"],
        "dims": [
            "本月累计收入 = 值",
            "目标达成率 = 累计收入 ÷ 月目标",
            "剩余缺口 = 月目标 - 累计收入",
            "剩余天数日均需完成 = 剩余缺口 ÷ 剩余天数",
            "与上月同期对比 = ±%",
            "进度预警 = 时间进度与达成率的差值(达成率落后时间进度需提示)",
        ],
        "questions": [
            "本月累计收入及目标达成进度如何？",
        ],
    },
    {
        "name": "固定问题-舆情当前态势",
        "description": "回答当前舆情类问题:情感倾向、正负面占比、负面激增、突发热点事件。"
                       "仅当问题针对当前/今日舆情时使用;近7天趋势、票务、客流类问题不要用本技能。",
        "tools": ["舆情-维度分数和平台占比", "舆情综合评价得分"],
        "dims": [
            "情感倾向 = 正面 / 中性 / 负面 各占比",
            "舆情综合评分 = 值",
            "负面环比 = 较前日 ±pp",
            "突发热点事件 = 无 / 逐条列出事件",
            "预警状态 = 未触发 / 黄色(负面占比>10%) / 红色(负面占比>30%)",
        ],
        "questions": [
            "当前有没有突发热点事件需要关注？",
            "当前舆情情感倾向如何？",
            "当前舆情情感正面/负面占比？",
            "负面舆情较前日是否有激增？增幅多少？",
            "【主题解读】舆情",
        ],
    },
    {
        "name": "固定问题-OTA当前汇总",
        "description": "回答 OTA 渠道当前经营类问题:各渠道核销率、本周订单、渠道出票异常。"
                       "仅当问题针对 OTA 渠道经营时使用;客流、舆情、昨日票务类问题不要用本技能。",
        "tools": [SUPPLIER_TOOL, "OTA指标", "OTA-雷达图", "雷达图指标", "销售渠道报表汇总"],
        "dims": [
            "各渠道核销率 = 渠道: 值",
            "本周各渠道订单量 = 渠道: 值",
            "异常渠道 = 无 / 渠道及出票下降幅度",
            "预警状态 = 未触发 / 黄色(出票下降>30%) / 红色(出票下降>50%)",
        ],
        "questions": [
            "当前各渠道核销率如何？",
            "本周各渠道订单情况？",
            "本周渠道出票是否异常？",
            "【主题解读】OTA直销",
        ],
    },
]

PROMPT_TMPL = """你是景区数字大脑的「{title}」分析师,服务对象是景区管理者。

【执行纪律:三阶段,参数校验先于取数】
第一阶段(参数校验):检查取数工具的必填参数是否都能从会话上下文取到
  · 齐全 → 进入第二阶段
  · 缺失且无法从上下文推断 → 立即向用户澄清缺失项,本轮即结束;
    禁止带着空值或猜测值调用工具——空结果不是"取数完成",只会让你反复重试
第二阶段(取数):参数齐全后,把相关取数工具全部调用一遍拿到完整数据。数据齐全之前,禁止输出任何结论或分析
第三阶段(生成):数据齐全后,一次性生成结构化回答

【回答骨架(固定小节,标题原样输出、顺序不可变)】
### 结论先行
一句话结论,必须含本轮最关键的数字与判断(向好 / 需关注 / 需干预)
### 核心数据
按下列维度逐条列出,一行一条,格式「维度 = 值(同比/环比)」:
{dims}
### 异常与预警
逐条说明触发了哪条阈值口径;无预警时整节只写「当前无预警」
### 行动建议
1~2 条可执行动作,面向管理者决策;无异常时写「暂无需要干预的事项」
### 数据说明
仅在数据不全时出现:缺失维度 + 原因,一句话交代

【骨架纪律(与骨架同等重要)】
- 禁止「数据获取情况说明」「参数确认」「我先查一下」之类自述开场:取数过程、缺失原因一律不进正文开头,
  只在末尾「数据说明」用一句话交代;回答正文只讲业务结论与数据
- 精简档:用户只问单一指标时(如「在园人数多少」),只输出「结论先行 + 核心数据(仅该维度)」两段,
  其余小节省略,不要每次都铺满五段
- 完整档:【主题解读】/ 概览类 / 开放性提问,五段全出
- 某维度工具无数据:该行写「维度 = 暂无数据」,不要整段删掉该小节,也不要臆测填充

【数据纪律】
- 数字全部来自工具返回,禁止编造或估算;给出关键数字的同比/环比变化
- 预警阈值口径:承载比≥75%黄色/≥90%红色;退票率>5%黄色/>10%红色;负面占比>10%黄色/>30%红色;渠道出票下降>30%黄色/>50%红色

【结构化图表输出(PRD 3.2.2.4,必须执行)】
回答末尾追加一个 ```json 代码块,按数据场景选择图表形态:
- 数据占比 → 饼状图:   {{"charts":[{{"title":"...","type":"pie","data":[{{"name":"线上","value":123}},...]}}]}}
- 数据变化/趋势 → 折线图: {{"charts":[{{"title":"近7天趋势","type":"line","x":["日期",...],"series":[{{"name":"客流","data":[1,2,...]}}]}}]}}
- 数量对比(多对象) → 条形图: {{"charts":[{{"title":"各渠道对比","type":"bar","x":["渠道A","渠道B",...],"series":[{{"name":"出票量","data":[10,20,...]}}]}}]}}
- 单一关键数量 → 数据卡片: {{"cards":[{{"label":"今日在园人数","value":"8523","unit":"人","trend":"up"}}]}}
规则:数字必须来自工具返回;一次回答图表合计不超过 3 个(cards+charts),没有合适图表场景时只输出 cards 或省略整个 json 块;json 必须合法,不要嵌套注释。

【参数例外】
- 景区 ID 已由会话上下文注入,直接使用
- 可选参数(如 OTA 商户账号)缺失属预期:照常取数,空结果在结论中说明原因即可,不触发澄清
  (区分口径:必填且无法推断 = 向用户澄清;可选 = 空数据属预期,照常出结论)"""


def build_prompt(g: dict) -> str:
    """技能提示词 = 统一骨架 + 本技能的维度清单(新增工具后只需补 dims 再发一版)。"""
    return PROMPT_TMPL.format(
        title=g["name"].replace("固定问题-", ""),
        dims="\n".join(f"- {d}" for d in g["dims"]),
    )


def login(base: str, account: str, pwd: str) -> str:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="local", choices=list(ENVS))
    ap.add_argument("--account", default="pftroot")
    ap.add_argument("--password", default=None, help="缺省依次尝试 0000@123 / pftroot123")
    args = ap.parse_args()

    base = ENVS[args.env]
    # DB 直连回填(③)定向目标环境:get_sessionmaker 按 ZHILV_ENV 加载 .env.{env}
    os.environ["ZHILV_ENV"] = args.env
    token = None
    for pwd in ([args.password] if args.password else ["0000@123", "pftroot123"]):
        try:
            token = login(base, args.account, pwd)
            print(f"✓ 登录成功({args.account})")
            break
        except SystemExit:
            continue
    if not token:
        raise SystemExit("登录失败:账号密码均不匹配")

    # 工具名→id
    tools = call(base, token, "GET", "/tools").get("items", [])
    tool_id = {t["name"]: t["id"] for t in tools}

    existing_skills = {s["name"]: s for s in call(base, token, "GET", "/skills").get("items", [])}

    # local 环境的问题库回填任务(循环结束后一次 event loop 内完成——
    # 多次 asyncio.run 会让 SQLAlchemy 连接池跨 loop 复用报 Event loop is closed)
    backfills: list[tuple[str, list[str]]] = []

    for g in GROUPS:
        missing = [t for t in g["tools"] if t not in tool_id]
        # 目标环境缺少的工具不阻断:已存在的技能沿用其已绑定工具,本轮以「更新提示词」为主;
        # 环境里存在的工具仍会并入(后续在某环境新增工具,再跑一次即自动带上)
        avail_ids = [tool_id[t] for t in g["tools"] if t in tool_id]
        skill = existing_skills.get(g["name"])
        bound_ids: list[str] = []
        if skill:
            _vers = call(base, token, "GET",
                         f"/resources/skill/{skill['id']}/versions").get("items", [])
            _pub = next((v for v in _vers if v.get("status") == "published"), None)
            bound_ids = [p.get("tool_id")
                         for p in (((_pub or {}).get("snapshot") or {}).get("tools") or [])]
        if not avail_ids and not bound_ids:
            print(f"✗ {g['name']}: 缺少工具 {missing} 且无已绑定工具,跳过")
            continue
        if missing:
            print(f"  · {g['name']}: 本环境缺少工具 {missing},沿用已绑定工具"
                  f"{'并合并可用工具' if avail_ids else ''}")

        # ① 技能(已存在则对比最新 published 快照的 tools/prompt,不一致则更新并发布新版本;
        #    注意主表 tool_ids 不随编辑更新——比较必须基于快照,否则每轮重复发版)
        want_ids = sorted(set(bound_ids) | set(avail_ids))
        want_prompt = build_prompt(g)
        if skill:
            skill_id = skill["id"]
            vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
            pub = next((v for v in vers if v.get("status") == "published"), None)
            snap = (pub or {}).get("snapshot") or {}
            snap_ids = sorted(p.get("tool_id") for p in (snap.get("tools") or []))
            prompt_same = (snap.get("prompt") == want_prompt)
            if snap_ids == sorted(want_ids) and prompt_same:
                print(f"• 技能已存在且一致: {g['name']} ({skill_id})")
            else:
                # v1.1.10 内容键纯快照化(0050 删主表内容列)后,PUT 只传局部键会派生
                # 残缺草稿(description/constraints 等不在主表、无 published 可继承),
                # 发布质检直接拦截。更新时必须携带完整内容:未改字段继承 published 快照
                # 现值,快照缺失时回退分组定义常量。
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
                    {"note": "20号P2:图表输出规则/工具绑定更新"}).get("id")
                call(base, token, "POST", f"/versions/{vid}/refresh-pins")
                call(base, token, "POST", f"/versions/{vid}/publish")
                changes = []
                if snap_ids != sorted(want_ids):
                    changes.append(f"工具{snap_ids}→{len(want_ids)}")
                if not prompt_same:
                    changes.append("prompt")
                print(f"✓ 技能已更新+发布新版本: {g['name']} ({','.join(changes)})")
        else:
            created = call(base, token, "POST", "/skills", {
                "name": g["name"], "description": g["description"],
                "tool_ids": [tool_id[t] for t in g["tools"]],
                "suggested_mode": "L1", "visibility": "public",
                "prompt": build_prompt(g),
                "constraints": {
                    "two_phase": "先并行取数,数据齐后才允许一次生成;禁止边取数边回答",
                    "no_fabrication": "所有数字必须来自工具返回,禁止编造;无数据时说明原因",
                },
            })
            skill_id = created["id"]
            print(f"✓ 技能创建: {g['name']} ({skill_id})")

        # ② 发布(有 published 版本则跳过)
        vers = call(base, token, "GET", f"/resources/skill/{skill_id}/versions").get("items", [])
        if any(v.get("status") == "published" for v in vers):
            print("• 已有 published 版本,跳过发布")
        else:
            draft = next((v for v in vers if v.get("status") == "draft"), None)
            if draft:
                vid = draft["id"]
            else:
                ver = call(base, token, "POST", f"/resources/skill/{skill_id}/versions",
                           {"note": "19号文档 P0 分组技能"})
                vid = ver["id"]
            # 15 号(无默认版本):发布预检要求绑定显式 pin。
            # refresh-pins 把 tools 钉到各依赖最新 published 版本。
            call(base, token, "POST", f"/versions/{vid}/refresh-pins")
            call(base, token, "POST", f"/versions/{vid}/publish")
            print(f"✓ 技能已发布 (version_id={vid})")

        # ③ 问题库回填:循环后一次性 DB 直连回填(get_sessionmaker 按 ZHILV_ENV 定向)
        # 23 号:问题行直接绑 skill_id(原「单技能组合」恒等转发层已删,0055 删表);
        # 原「非 local 只打印 SQL」因 S4 组合接口未建,组合层已删,直接 UPDATE 即可。
        backfills.append((skill_id, g["questions"]))

    # 摘要技能(二次加工,不绑组合——cronjobs/prewarm 直达)
    sk = existing_skills.get(SUMMARY_SKILL["name"])
    if sk:
        sid = sk["id"]
        vers = call(base, token, "GET", f"/resources/skill/{sid}/versions").get("items", [])
        pub = next((v for v in vers if v.get("status") == "published"), None)
        snap_ok = pub and (pub.get("snapshot") or {}).get("prompt") == SUMMARY_SKILL["prompt"] \
            and (pub.get("snapshot") or {}).get("constraints") == SUMMARY_SKILL["constraints"]
        if snap_ok:
            print(f"• 摘要技能已存在且一致: {SUMMARY_SKILL['name']} ({sid})")
        else:
            call(base, token, "PUT", f"/skills/{sid}", {
                "description": (pub.get("snapshot") or {}).get("description")
                or SUMMARY_SKILL["description"],
                "prompt": SUMMARY_SKILL["prompt"],
                "constraints": SUMMARY_SKILL["constraints"],
            })
            vers = call(base, token, "GET", f"/resources/skill/{sid}/versions").get("items", [])
            draft = next((v for v in vers if v.get("status") == "draft"), None)
            vid = draft["id"] if draft else call(
                base, token, "POST", f"/resources/skill/{sid}/versions",
                {"note": "20号P3:摘要技能"}).get("id")
            call(base, token, "POST", f"/versions/{vid}/publish")
            print(f"✓ 摘要技能已更新+发布: {SUMMARY_SKILL['name']} ({sid})")
    else:
        created = call(base, token, "POST", "/skills", {
            "name": SUMMARY_SKILL["name"], "description": SUMMARY_SKILL["description"],
            "tool_ids": [], "suggested_mode": "L0", "visibility": "public",
            "prompt": SUMMARY_SKILL["prompt"],
            "constraints": SUMMARY_SKILL["constraints"],
        })
        sid = created["id"]
        ver = call(base, token, "POST", f"/resources/skill/{sid}/versions",
                   {"note": "20号P3:摘要技能"})
        call(base, token, "POST", f"/versions/{ver['id']}/publish")
        print(f"✓ 摘要技能创建+发布: {SUMMARY_SKILL['name']} ({sid})")

    if backfills:
        import asyncio

        async def _backfill():
            from sqlalchemy import update
            from cogni_agent.common.db import get_sessionmaker
            from cogni_agent.gateway_service.models import AppQuestionBank

            total = 0
            async with get_sessionmaker()() as db:
                for skill_id, questions in backfills:
                    res = await db.execute(
                        update(AppQuestionBank)
                        .where(AppQuestionBank.question_text.in_(questions))
                        .values(skill_id=skill_id))
                    total += res.rowcount
                await db.commit()
            return total
        n = asyncio.run(_backfill())
        print(f"\n✓ 问题库回填共 {n} 条")

    print("\n完成。5 组技能就绪、问题行绑定 skill_id 后,S7(命中后 executor 直跳技能)即可联调。")


if __name__ == "__main__":
    sys.exit(main())
