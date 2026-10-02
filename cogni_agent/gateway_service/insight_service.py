"""
概览页核心指标解读 + 看板结论卡彩色指标条服务（v1.1 阶段二）。

支撑:
  agent/home 的 overview_insights     → 概览页各主题核心指标解读（异常分色）
  GET /api/app/panel/{theme}/insight  → 命中预热缓存时的 metrics 四维彩色指标条

设计要点:
1. 异常分色规则来自需求文档 3.2.1.2「概览页各主题核心指标解读概要」的阈值表：
     客流  在园人数/最大承载数   ≤75% 正常(蓝) / ≤90% 警戒(橙) / >90% 严重(红)
     舆情  负面评价比例          ≤10% 正常(蓝) / ≤30% 警戒(橙) / >30% 严重(红)
     票务  退票率               ≤5%  正常(蓝) / ≤10% 警戒(橙) / >10% 严重(红)
     OTA  核销率               越高越好，需求未给异常阈值（默认 normal）
2. 指标数据源(24 号接入真实管道):预热【主题解读】概览行 overview_metrics.items
   ——链路:预热报告任务(工具真数据)→ 概览解读报告 → 摘要技能二次加工输出
   metrics 数组 → cronjobs/prewarm 摘要收割写入行 → 本模块按景区读当日行。
   当日行缺失(未预热/摘要未出)或某维度缺失 → display "--"、level normal,
   纯读缓存不回退实时生成(与 insight 接口 20 号 P5 口径一致)。
"""
from __future__ import annotations

# ── 主题指标定义与异常阈值（需求文档 3.2.1.2） ─────────────────────────────
# level_rule: ratio_high_danger = 比值越高越危险（在园占比/负面比例/退票率）
#             ratio_high_good  = 比值越高越好（核销率），仅展示不判级
THEME_DEFS: dict[str, dict] = {
    "passenger_flow": {
        "name": "客流",
        "metric": "在园人数/最大承载数",
        "level_rule": "ratio_high_danger",
        "warning": 0.75,
        "danger": 0.90,
        "unit": "%",
    },
    "ticket": {
        "name": "票务",
        "metric": "退票率",
        "level_rule": "ratio_high_danger",
        "warning": 0.05,
        "danger": 0.10,
        "unit": "%",
    },
    "public_opinion": {
        "name": "舆情",
        "metric": "负面评价比例",
        "level_rule": "ratio_high_danger",
        "warning": 0.10,
        "danger": 0.30,
        "unit": "%",
    },
    "ota_direct_sales": {
        "name": "OTA直销",
        "metric": "核销率",
        "level_rule": "ratio_high_good",
        "unit": "%",
    },
}


# ── 指标快照(24号:预热概览行二次加工产物,替换原硬编码样例) ──────────────────
_OVERVIEW_THEMES = ("passenger_flow", "ticket", "public_opinion", "ota_direct_sales")


async def _load_overview_items(db, scenic_id: str | None,
                               ota_account: str | None = None, *,
                               user_id: str | None = None,
                               agent_hint: str | None = None) -> dict:
    """读取该景区当日【主题解读】概览行的四维指标 items。

    25 号:概览按「景区×账号」拆行,优先当前用户账号匹配行,无则回退账号无关
    基础行(NULL,未绑账号用户的三域概览)。
    27 号:版本严格匹配(全部严格)——行 skill_version 必须等于该概览问题技能的
    指派 pin(用户对锚 agent 的指派快照);无指派/未绑 → 无行(维度缺失),
    不回退任意版本行。
    {theme_code: {"value": 0~1 或 None, "display": "12,600/20,000"}};行缺失或
    老行无 items 键 → 空 dict(调用方按维度缺失处理)。
    """
    from datetime import datetime as _dt

    from sqlalchemy import select as _sa_select

    from cogni_agent.common.time import today_cst
    from cogni_agent.gateway_service.models import AppPrewarmCache, AppQuestionBank

    q = (await db.execute(
        _sa_select(AppQuestionBank).where(
            AppQuestionBank.theme_code == "overview",
            AppQuestionBank.question_text.like("【主题解读】%"),
        ).limit(1)
    )).scalars().first()
    if q is None:
        return {}

    from cogni_agent.tool_center_service.versioning import assigned_skill_version

    pin = (await assigned_skill_version(
        db, q.agent_id or agent_hint, user_id, q.skill_id)
        if q.skill_id else None)

    def _row_where(ota):
        return [
            AppPrewarmCache.question_id == q.id,
            AppPrewarmCache.scenic_id == (scenic_id or ""),
            AppPrewarmCache.skill_version == pin,
            AppPrewarmCache.biz_date == today_cst(),
            AppPrewarmCache.answer.isnot(None),
            AppPrewarmCache.ota_account == ota,
        ]

    row = (await db.execute(
        _sa_select(AppPrewarmCache).where(*_row_where(ota_account)).limit(1)
    )).scalars().first()
    if row is None and ota_account:
        row = (await db.execute(
            _sa_select(AppPrewarmCache).where(
                *_row_where(None)).limit(1)
        )).scalars().first()
    om = (row.overview_metrics or {}) if row is not None else {}
    items = om.get("items") if isinstance(om, dict) else None
    return items if isinstance(items, dict) else {}


def _calc_level(theme: str, value: float | None) -> str:
    """按需求文档阈值表计算异常等级: danger / warning / normal。"""
    rule = THEME_DEFS.get(theme, {}).get("level_rule")
    if rule == "ratio_high_good" or value is None:
        return "normal"
    if value > THEME_DEFS[theme]["danger"]:
        return "danger"
    if value > THEME_DEFS[theme]["warning"]:
        return "warning"
    return "normal"


async def get_overview_metric_items(db, scenic_id: str | None,
                                    ota_account: str | None = None, *,
                                    user_id: str | None = None,
                                    agent_hint: str | None = None) -> list[dict]:
    """概览四维彩色指标条(panel/insight.metrics 消费,与 agent/home overview_insights 同源)。

    item: {theme, name=主题中文名, metric, value=display 串, level}。
    27 号:user_id/agent_hint 用于版本严格匹配(见 _load_overview_items)。
    """
    items = await _load_overview_items(db, scenic_id, ota_account,
                                       user_id=user_id, agent_hint=agent_hint)
    out: list[dict] = []
    for theme in _OVERVIEW_THEMES:
        meta = THEME_DEFS[theme]
        m = items.get(theme) or {}
        out.append({
            "theme": theme,
            "name": meta["name"],
            "metric": meta["metric"],
            "value": m.get("display", "--"),
            "level": _calc_level(theme, m.get("value")),
        })
    return out


async def get_overview_insights(db, scenic_id: str | None,
                                ota_account: str | None = None, *,
                                user_id: str | None = None,
                                agent_hint: str | None = None) -> list[dict]:
    """概览页各主题核心指标解读概要（异常分色），供 agent/home 返回。"""
    items = await _load_overview_items(db, scenic_id, ota_account,
                                       user_id=user_id, agent_hint=agent_hint)
    out: list[dict] = []
    for theme in _OVERVIEW_THEMES:
        meta = THEME_DEFS[theme]
        m = items.get(theme) or {}
        out.append({
            "theme": theme,
            "metric": meta["metric"],
            "value": m.get("display", "--"),
            "level": _calc_level(theme, m.get("value")),
        })
    return out
