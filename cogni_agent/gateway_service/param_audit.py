"""参数覆盖审计(32 号):问题 param_template vs 技能工具必填参数的差集。

用途:
1. 上线门禁(routes_app_ops 问题创建/更新):激活/预热开启时计算缺口——
   预热+模板空 → 硬拒绝(全靠模型自填违背「装配期全量生成」);
   激活 → 软警告(响应 param_warnings,运营自行甄别语义);
2. 全量审计(scripts/audit_param_coverage.py,CLI 只读)。

口径:技能绑定工具(最新 published 快照)的 params_schema.required 并集,
剥离系统注入键(x-param-source/身份类);「并集」含语义不会调到的工具
(如非对比问题绑了比对工具),故缺口是**提示**不是错误——语义甄别由人做,
硬规则只挡最严重情形(模板空)。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 身份类键:由 JWT/框架在调用时携带,与工具声明无关,模板不需要配
IDENTITY_KEYS = frozenset({
    "scenicId", "scenic_id", "userId", "user_id",
})

# 本该由系统注入的键(x-param-source 前置工具解析):**仅当工具 schema 真的声明了
# 注入来源**才算已接线。
# 反例(必须警钟长鸣):supplierId 名字一直在这个排除名单里,于是"模板不配
# supplierId"从未报缺口;而实际上门票/产品/报表比对三个工具的 schema 上并没有
# x-param-source —— 注入通道根本没接,supplierId 作为 required 暴露给模型,
# 模型要么反问(任务 f400994c)要么编个值发出去拿到空数据后反复重试
# (任务 249488c8,4 轮 generation 273s、空回答)。按名字排除 = 让这类断线永久
# 隐身,故改为按"是否真的声明了来源"判定。
INJECTABLE_KEYS = frozenset({
    "supplierId", "supplierIds", "account", "otaAccount", "ota_account",
})


# time_period → 对比窗口(环比 compareXxx)占位符。只列真实枚举
# (routes_app_ops.PERIODS)——曾经还留着 week/month/days_7/last_week 等别名,
# 引用的是并不存在的占位符,模板配上后展开为原样占位符再被静默丢弃,
# 看着配了实际没值。占位符定义在 gateway_service/question_bank._placeholders。
PERIOD_CMP: dict[str, tuple[str, str]] = {
    "today": ("{{yesterday_start}}", "{{yesterday_end}}"),
    "yesterday": ("{{day_before_yesterday_start}}", "{{day_before_yesterday_end}}"),
    "week_to_date": ("{{last_week_start}}", "{{last_week_end}}"),
    "last_7_days": ("{{prev_days_7_start}}", "{{prev_days_7_end}}"),
    # 上月同期:原写 {{last_month_end}}(占位符不存在),环比窗口形同未配
    "month_to_date": ("{{last_month_start}}", "{{last_month_to_date_end}}"),
}


def _has_injection_source(prop) -> bool:
    """参数是否声明了系统注入来源(x-param-source)。"""
    if not isinstance(prop, dict):
        return False
    return bool(prop.get("x-param-source"))


async def _skill_tool_requirements(
    db: AsyncSession, skill_id: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """技能最新 published 快照绑定的工具 → (需要模板/模型提供的键, 声明断线的注入键)。

    第二项 unwired 是「本该系统注入、但工具 schema 没声明来源」的键——这类问题
    **补模板治不好**(模型仍会看到这个参数),必须修工具声明,故单列出来。
    """
    # ① 技能最新 published 快照的绑定工具清单
    bound = (await db.execute(text(
        "SELECT snapshot->'tools' FROM numeri_core_tc_resource_versions "
        "WHERE resource_type='skill' AND resource_id=:sid AND status='published' "
        "ORDER BY version DESC LIMIT 1"), {"sid": skill_id})).scalar_one_or_none()
    tool_ids = [b.get("tool_id") for b in (bound or []) if isinstance(b, dict) and b.get("tool_id")]
    if not tool_ids:
        return {}, {}
    # ② 各工具最新 published 快照的 params_schema(要 properties 判定注入声明,
    #    不能只取 required)
    rows = (await db.execute(text(
        "SELECT t.name, v.snapshot->'type_config'->'params_schema' AS sch "
        "FROM numeri_core_tc_resource_versions v "
        "JOIN numeri_core_tc_tools t ON t.id = v.resource_id "
        "WHERE v.resource_type='tool' AND v.status='published' AND v.resource_id = ANY(:ids) "
        "AND v.version = (SELECT MAX(v2.version) FROM numeri_core_tc_resource_versions v2 "
        "  WHERE v2.resource_type='tool' AND v2.resource_id=v.resource_id "
        "  AND v2.status='published')"),
        {"ids": tool_ids})).all()
    out: dict[str, list[str]] = {}
    unwired: dict[str, list[str]] = {}
    for name, sch in rows:
        sch = sch or {}
        props = sch.get("properties") if isinstance(sch.get("properties"), dict) else {}
        need: list[str] = []
        bad: list[str] = []
        for r in (sch.get("required") or []):
            if r in IDENTITY_KEYS:
                continue
            if _has_injection_source(props.get(r)):
                continue  # 真的声明了注入来源 → 值由系统给,模板不需要配
            need.append(r)
            if r in INJECTABLE_KEYS:
                bad.append(r)  # 本该注入却没接线 → 修工具声明,不是补模板
        if need:
            out.setdefault(name, sorted(set(need)))
        if bad:
            unwired.setdefault(name, sorted(set(bad)))
    return out, unwired


async def question_param_gaps(db: AsyncSession, q) -> tuple[list[dict], bool]:
    """单个问题的参数缺口。

    返回 (gaps, template_empty):
    - gaps: [{tool, params:[缺口键], unwired?:[声明断线的键]}] —— 模板未覆盖的
      工具必填参数(并集口径,含语义甄别空间,提示用);其中 unwired 标记的是
      「补模板无效、必须修工具注入声明」的键;
    - template_empty: 模板缺失/为空(最严重缺口,门禁硬规则用)。
    """
    if not q.skill_id:
        return [], False
    tpl = (getattr(q, "param_template", None) or {})
    tool_req, unwired = await _skill_tool_requirements(db, q.skill_id)
    have = set(tpl.keys())
    gaps: list[dict] = []
    for name, req in tool_req.items():
        missing = [p for p in req if p not in have]
        if not missing:
            continue
        item: dict = {"tool": name, "params": missing}
        bad = [p for p in missing if p in (unwired.get(name) or [])]
        if bad:
            item["unwired"] = bad  # 这几项补模板治不好,要修工具 schema 声明
        gaps.append(item)
    template_empty = not tpl and bool(tool_req)
    return gaps, template_empty
