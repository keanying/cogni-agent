"""19 号文档 S4:APP 固定问题命中判定(question bank matching)。

question_id 是唯一契约(2026-09-12 评审定稿,26 号评审落地):
  - 卡片/选择器/关注问题等固定入口必带 question_id,服务端反查校验
    (存在 + active + 景区匹配),不信任前端冗余字段;
  - 原「message 归一化文本匹配」兜底(老版本 APP / 用户手打)已删除——
    自由输入的文本走 Triager 动态链路本来就是正确语义,省一次全量比对,
    question_id 成为固定问题路线的唯一入口契约。

23 号:命中且问题行 skill_id 非空 → 调用方把任务转为 executor_type=skill +
executor_id=skill_id 直跳该技能(L1,跳过 Triager 意图识别)。原「composition_id →
tc_skill_compositions → skill_ids_ordered[0]」恒等转发层已删除。
"""
from __future__ import annotations

import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.time import now_cst
from cogni_agent.gateway_service.models import AppQuestionBank


async def match_question(
    db: AsyncSession,
    *,
    question_id: str | None,
    scenic_id: str | None = None,
) -> AppQuestionBank | None:
    """固定问题命中判定:question_id 唯一契约。

    校验存在 + active + 景区匹配(通用模板 scenic_id 为 NULL,景区专属行
    须与当前会话景区一致);id 无效即未命中(调用方走 Triager 动态链路),
    绝不降级文本匹配——防止脏 id 静默改道。
    """
    if not question_id:
        return None
    q = await db.get(AppQuestionBank, question_id)
    if not q or not q.active:
        return None
    if q.scenic_id is not None and q.scenic_id != scenic_id:
        return None
    return q


def workflow_target(q: AppQuestionBank) -> bool:
    """命中问题是否要走工作流直跳(23 号:问题行绑定了执行技能)。

    原判定 route_mode=workflow 且 composition_id 非空;组合层删除后,route_mode 一并
    删除,由 skill_id 是否为空派生——非空 = 直跳该技能,空 = 走 Triager 动态链路。
    """
    return bool(q.skill_id)


# ── 参数模板展开(19 号 S7.5) ────────────────────────────────────────────────
# 定位:基准日期与对比窗口(travelDate/startTime/endTime/compareXxx)都从
# time_period 语义在派单前生成确定值(27 号评审定稿:参数在派单前全部生成,
# 无运行期默认补全——缺=由 L2 拦截暴露,不隐藏)。
# 派生参数(supplierId 等)由工具 schema 的 x-param-source 在执行期解析注入。
from datetime import date, datetime, timedelta

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _week_start(d: date) -> date:
    """本周一(问题库 week_to_date 语义:周一至今日)。"""
    return d - timedelta(days=d.weekday())


def _last_month_same_day(today: date) -> date:
    """上月同日(本月累计 vs 上月同期的环比上界)。

    今天 31 号而上月没有 31 号时取上月最后一天(不臆造、不回退到月初)。
    """
    last_day = today.replace(day=1) - timedelta(days=1)
    try:
        return last_day.replace(day=today.day)
    except ValueError:
        return last_day


def _placeholders(now: datetime) -> dict[str, str]:
    today = now.date()
    yd = today - timedelta(days=1)
    dby = today - timedelta(days=2)   # 前日(环比基准)
    return {
        "today": today.isoformat(),
        "yesterday": yd.isoformat(),
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today_start": f"{today.isoformat()} 00:00:00",
        "today_end": f"{today.isoformat()} 23:59:59",
        "yesterday_start": f"{yd.isoformat()} 00:00:00",
        "yesterday_end": f"{yd.isoformat()} 23:59:59",
        # 对比窗口占位符(32 号补:比对类工具的 compareXxx 全部装配期生成,
        # 不再留给模型执行期自填——填错日期/格式是隐性事故源)
        "day_before_yesterday_start": f"{dby.isoformat()} 00:00:00",
        "day_before_yesterday_end": f"{dby.isoformat()} 23:59:59",
        "last_week_start": _week_start(today - timedelta(days=7)).isoformat(),
        "last_week_end": (_week_start(today - timedelta(days=7)) + timedelta(days=6)).isoformat(),
        # 上一个 7 天窗口(近7天环比:本期 days_7_ago~今天,上期 prev_7_* )
        "prev_days_7_start": (today - timedelta(days=13)).isoformat(),
        "prev_days_7_end": (today - timedelta(days=7)).isoformat(),
        "week_start": _week_start(today).isoformat(),
        "month_start": today.replace(day=1).isoformat(),
        # 月份占位符(YYYY-MM,evaluationMonth 类工具参数用)
        "month": f"{today.year}-{today.month:02d}",
        "last_month_start": (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat(),
        "days_7_ago": (today - timedelta(days=6)).isoformat(),
        "plus_3d_end": (today + timedelta(days=3)).isoformat(),
        # 月末/年末/上月同日(33 号补):PERIOD_CMP 的 month_to_date / last_month
        # 环比口径引用这些占位符,而它们原本不存在——模板写上只会拿到无法展开的
        # 原样值,再被 expand_param_template **静默丢弃**(比缺参数更隐蔽:看着配了,
        # 实际没值)。补齐后环比窗口才是真值。
        "month_end": ((today.replace(day=28) + timedelta(days=7)).replace(day=1)
                      - timedelta(days=1)).isoformat(),
        "last_month_end": (today.replace(day=1) - timedelta(days=1)).isoformat(),
        "last_month_to_date_end": _last_month_same_day(today).isoformat(),
        "year_start": today.replace(month=1, day=1).isoformat(),
        "year_end": today.replace(month=12, day=31).isoformat(),
    }


def expand_param_template(template: dict | None, now: datetime | None = None) -> dict[str, str]:
    """展开参数模板:{参数名: "{{today}} 00:00:00" 等} → {参数名: 实际值}。

    未知占位符保留原样(不臆造);值非字符串原样返回(支持静态量)。
    无法展开(整值都是未知占位符)的参数丢弃——宁缺勿臆造,留给 L3/L2 处理。
    """
    if not template:
        return {}
    # 业务日期占位符统一北京时间(容器时区 UTC,datetime.now() 在 00:00~08:00 偏一天)
    ph = _placeholders(now or now_cst())
    out: dict[str, str] = {}
    for key, raw in template.items():
        if not isinstance(raw, str):
            out[key] = raw
            continue
        expanded = _PLACEHOLDER.sub(lambda m: ph.get(m.group(1), m.group(0)), raw)
        # 整值仍是未解析占位符 = 模板配置错误,丢弃防脏值进 slots
        if _PLACEHOLDER.fullmatch(expanded.strip()) and expanded.strip() not in ph.values():
            continue
        out[key] = expanded
    return out


# ── 22 号:return_format 标识自检(问题标注的图表形态 ↔ 绑定技能提示词引导) ──────────
# return_format 只是问题行的「运营标注」,运行时(graphs/网关/预热)一律不消费、不做
# 任何提示词注入——图表引导必须内置于绑定技能自身的提示词。标注唯一用途 = 确认
# 「该问题要求的图表展示,绑定技能的提示词是否教了怎么出」:引导锚点对齐
# scripts/create_p0_group_skills.PROMPT_TMPL 的【结构化图表输出(PRD 3.2.2.4)】段
# (card 形态须教 cards 单值数据卡片;line/bar/pie/radar 须教 charts 图)。
_CHART_FMT_FEATURES = {
    "card": ("cards",),
    "line": ("charts",),
    "bar": ("charts",),
    "pie": ("charts",),
    "radar": ("charts",),
}


def has_chart_format_guidance(prompt: str | None, return_format: str) -> bool:
    """技能提示词是否含该问题 return_format 标注的图表输出引导(自检判定,纯文本)。

    - auto / 空 / 未知形态:问题不强制图表 → 恒 True(不存在引导缺失);
    - 固定形态:提示词须出现对应特征(card→cards;折线/条形/饼/雷达→charts)。
      提示词为空或未出现特征 → False,即「问题要图、技能没教」,需补技能提示词。
    """
    features = _CHART_FMT_FEATURES.get(return_format or "")
    if features is None:
        return True
    if not prompt:
        return False
    return all(f in prompt for f in features)


def _skill_chart_status(skill_id: str, skill_name: str | None,
                        snapshot: dict | None, return_format: str) -> dict:
    """由绑定技能最新 published 快照算「图表引导状态」(单条/批量共用的纯计算)。

    ok = 技能提示词含该形态引导;技能缺失 / 无 published 版本时 prompt 视为空 →
    guidance_ok=False,按「未就绪」暴露给运营,而非静默通过。
    """
    prompt = (snapshot.get("prompt") or "") if snapshot else ""
    ok = has_chart_format_guidance(prompt or None, return_format)
    return {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "published": bool(snapshot),
        "guidance_ok": ok,
        "ok": ok,
    }


async def question_chart_guidance(db: AsyncSession, q: AppQuestionBank) -> dict | None:
    """单问题(运营接口 create/update 回显用):绑定技能的图表引导状态。

    仅对绑定了技能 + return_format 固定形态的问题计算,其余返回 None(不展示)。
    技能缺失 / 无 published 版本都按 ok=False 暴露(见 _skill_chart_status)。
    """
    if not workflow_target(q) or (q.return_format or "auto") == "auto":
        return None
    from cogni_agent.tool_center_service.skill.models import Skill

    skill = await db.get(Skill, q.skill_id)
    from cogni_agent.tool_center_service import versioning as ver

    snaps = await ver.latest_published_snapshots(db, "skill", [q.skill_id])
    return _skill_chart_status(q.skill_id, skill.name if skill else None,
                               snaps.get(q.skill_id), q.return_format)


async def enrich_chart_guidance(db: AsyncSession, questions: list[AppQuestionBank]) -> dict[str, dict]:
    """批量(列表页):question_id → 图表引导状态。只处理绑定技能 + 固定形态行,
    技能/版本查询跨行去重,避免列表页 N+1。"""
    targets = [q for q in questions
               if workflow_target(q) and (q.return_format or "auto") != "auto"]
    if not targets:
        return {}
    skill_ids = {q.skill_id for q in targets}
    from cogni_agent.tool_center_service.skill.models import Skill

    skill_name = {s.id: s.name for s in (await db.execute(
        select(Skill).where(Skill.id.in_(skill_ids)))).scalars()}
    from cogni_agent.tool_center_service import versioning as ver

    snaps = await ver.latest_published_snapshots(db, "skill", list(skill_ids))
    return {
        q.id: _skill_chart_status(q.skill_id, skill_name.get(q.skill_id),
                                  snaps.get(q.skill_id), q.return_format)
        for q in targets
    }


# ── 23 号:固定问题引用守卫(供框架层删技能时调用) ──────────────────────────────
# 分层:框架层(tool_center_service)不直接摸 APP 业务表;本模块(业务层)暴露
# 「一查一改」两个函数,框架层只调用它们。
async def skills_referenced_by_questions(
    db: AsyncSession, skill_ids: set[str]
) -> dict[str, list[tuple[str, str, bool]]]:
    """返回 {skill_id: [(question_id, question_text, active), ...]}(只含被引用的技能)。

    删技能前的引用守卫用:框架层据此判断能否删除、是否需要强制解绑。
    """
    if not skill_ids:
        return {}
    rows = (await db.execute(
        select(AppQuestionBank.id, AppQuestionBank.skill_id,
               AppQuestionBank.question_text, AppQuestionBank.active)
        .where(AppQuestionBank.skill_id.in_(skill_ids))
    )).all()
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for qid, sid, text, active in rows:
        out.setdefault(sid, []).append((qid, text, bool(active)))
    return out


async def unbind_questions_from_skill(db: AsyncSession, skill_id: str) -> int:
    """把引用该技能的问题行 skill_id 置空(问题行保留),返回受影响行数。

    force 删除技能时调用:问题行不级联删除(承载 theme_code/param_template/预热档位
    等运营配置,是业务资产),解绑后退化为走 Triager 动态链路,功能仍可用。
    """
    res = await db.execute(
        update(AppQuestionBank)
        .where(AppQuestionBank.skill_id == skill_id)
        .values(skill_id=None))
    return res.rowcount or 0
