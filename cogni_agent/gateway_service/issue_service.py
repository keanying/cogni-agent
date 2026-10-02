"""景区智能体问题体系的读写(前后端改造 §1~§3)。

两件事:
1. **三级标签**(主题→维度→指标)的维护 —— 知识中心里增删改,前台选择器用它分组;
2. **问题**的维护与查询 —— 一行问题挂在某个指标下,`business_class` 决定怎么发起。

放在 service 层而不是直接写在路由里,是因为同一套逻辑有三个调用方:
APP 的 businessIssueList、管理端的问题维护、以及发起提问时按 issue_id 取配置。
"""
from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import NotFound, ValidationError
from cogni_agent.common.logging import get_logger
from cogni_agent.gateway_service.issue_models import (
    BUSINESS_CLASSES, BUSINESS_CLASS_LOCAL, BUSINESS_CLASS_QUICK_BI,
    PublicProblemDimensionLabel, ScenicAgentIssueDetail,
)

log = get_logger("gateway.issue")

# 「主题解读」那一档的主题编码。前台问题树刻意不收它 —— 它由首页主题卡片承载。
# 与 scenic_binding.ensure_theme_issues 写进问题行的值必须一致。
THEME_REPORT_TOPIC_ID = "themeReport"


# ── 三级标签 ────────────────────────────────────────────────────────────

def label_dict(row: PublicProblemDimensionLabel) -> dict:
    return {
        "id": row.id,
        "topic": row.topic, "topic_id": row.topic_id,
        "dimension": row.dimension, "dimension_id": row.dimension_id,
        "metric": row.metric, "metric_id": row.metric_id,
        "sort_order": row.sort_order, "active": row.active,
        "create_time": row.create_time, "update_time": row.update_time,
    }


async def list_labels(db: AsyncSession, *, active: bool | None = True,
                      topic_id: str | None = None, with_usage: bool = False) -> list[dict]:
    """全部三级路径,按 主题 → 维度 → 指标 的展示顺序。

    with_usage=True 时每行带上 `issue_count`(挂在这个标签下的问题数)。管理页靠它:
    删除按钮能直接说清「下面还有 N 个问题,删不了」,改名前能提示「会同步改掉 N 个问题」——
    不给这个数的话,运营只能点了删除才知道删不了,改了名才发现改到了别人的问题。
    """
    stmt = sa.select(PublicProblemDimensionLabel)
    if active is not None:
        stmt = stmt.where(PublicProblemDimensionLabel.active.is_(active))
    if topic_id:
        stmt = stmt.where(PublicProblemDimensionLabel.topic_id == topic_id)
    stmt = stmt.order_by(PublicProblemDimensionLabel.sort_order, PublicProblemDimensionLabel.id)
    rows = [label_dict(r) for r in (await db.execute(stmt)).scalars()]
    if with_usage and rows:
        # 一次分组计数,不按行逐个查(标签几十行,问题可能上千)
        counts = {
            (t, d, m): n for t, d, m, n in (await db.execute(
                sa.select(ScenicAgentIssueDetail.topic_id, ScenicAgentIssueDetail.dimension_id,
                          ScenicAgentIssueDetail.metric_id, sa.func.count())
                .group_by(ScenicAgentIssueDetail.topic_id, ScenicAgentIssueDetail.dimension_id,
                          ScenicAgentIssueDetail.metric_id))).all()}
        for r in rows:
            r["issue_count"] = counts.get((r["topic_id"], r["dimension_id"], r["metric_id"]), 0)
    return rows


async def label_tree(db: AsyncSession, *, active: bool | None = True) -> list[dict]:
    """把平铺的三级路径折成树:主题 → 维度 → 指标。

    前台的选择器就是这个形状。在服务端折好而不是让前端自己分组,是因为顺序
    (sort_order)只有服务端知道,前端按 Object.keys 分组会丢掉顺序。
    """
    rows = await list_labels(db, active=active)
    topics: list[dict] = []
    t_index: dict[str, dict] = {}
    d_index: dict[tuple[str, str], dict] = {}
    for r in rows:
        t = t_index.get(r["topic_id"])
        if t is None:
            t = {"topic": r["topic"], "topic_id": r["topic_id"], "dimensions": []}
            t_index[r["topic_id"]] = t
            topics.append(t)
        key = (r["topic_id"], r["dimension_id"])
        d = d_index.get(key)
        if d is None:
            d = {"dimension": r["dimension"], "dimension_id": r["dimension_id"], "metrics": []}
            d_index[key] = d
            t["dimensions"].append(d)
        d["metrics"].append({"metric": r["metric"], "metric_id": r["metric_id"], "label_id": r["id"]})
    return topics


def _clean_label(data: dict) -> dict:
    out = {}
    for k in ("topic", "topic_id", "dimension", "dimension_id", "metric", "metric_id"):
        v = str(data.get(k) or "").strip()
        if not v:
            raise ValidationError(f"{k} 不能为空")
        out[k] = v
    if "sort_order" in data and data["sort_order"] is not None:
        out["sort_order"] = int(data["sort_order"])
    if "active" in data and data["active"] is not None:
        out["active"] = bool(data["active"])
    return out


async def _assert_label_unique(db: AsyncSession, data: dict, *, exclude_id: int | None = None) -> None:
    """名称三元组与编码三元组各查一次。

    分开查是为了能说清楚是哪一组撞了 —— 只说「已存在」的话,运营会以为是自己
    刚才点重了,实际可能是编码撞了而名称不同(或者反过来)。
    """
    for cols, label in (
        (("topic", "dimension", "metric"), "主题/维度/指标名称"),
        (("topic_id", "dimension_id", "metric_id"), "主题/维度/指标编码"),
    ):
        stmt = sa.select(PublicProblemDimensionLabel.id).where(
            *[getattr(PublicProblemDimensionLabel, c) == data[c] for c in cols])
        if exclude_id is not None:
            stmt = stmt.where(PublicProblemDimensionLabel.id != exclude_id)
        if (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            vals = " / ".join(data[c] for c in cols)
            raise ValidationError(f"该{label}组合已存在:{vals}")


async def create_label(db: AsyncSession, data: dict) -> dict:
    payload = _clean_label(data)
    await _assert_label_unique(db, payload)
    row = PublicProblemDimensionLabel(**payload)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(f"维度标签新增 {row.topic}/{row.dimension}/{row.metric}")
    return label_dict(row)


async def update_label(db: AsyncSession, label_id: int, data: dict) -> dict:
    row = await db.get(PublicProblemDimensionLabel, label_id)
    if row is None:
        raise NotFound("维度标签不存在")
    merged = {
        "topic": data.get("topic", row.topic), "topic_id": data.get("topic_id", row.topic_id),
        "dimension": data.get("dimension", row.dimension),
        "dimension_id": data.get("dimension_id", row.dimension_id),
        "metric": data.get("metric", row.metric), "metric_id": data.get("metric_id", row.metric_id),
    }
    if "sort_order" in data and data["sort_order"] is not None:
        merged["sort_order"] = data["sort_order"]
    if "active" in data and data["active"] is not None:
        merged["active"] = data["active"]
    payload = _clean_label(merged)
    await _assert_label_unique(db, payload, exclude_id=label_id)
    # 名称改了,已经挂在这个标签下的问题也要跟着改 —— 否则前台按名称分组会分出
    # 一个「旧名字」的空组,而问题看起来凭空消失了。
    rename = (payload["topic"] != row.topic or payload["dimension"] != row.dimension
              or payload["metric"] != row.metric
              or payload["topic_id"] != row.topic_id or payload["dimension_id"] != row.dimension_id
              or payload["metric_id"] != row.metric_id)
    old_codes = (row.topic_id, row.dimension_id, row.metric_id)
    for k, v in payload.items():
        setattr(row, k, v)
    if rename:
        await db.execute(sa.update(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.topic_id == old_codes[0],
            ScenicAgentIssueDetail.dimension_id == old_codes[1],
            ScenicAgentIssueDetail.metric_id == old_codes[2],
        ).values(topic=payload["topic"], topic_id=payload["topic_id"],
                 dimension=payload["dimension"], dimension_id=payload["dimension_id"],
                 metric=payload["metric"], metric_id=payload["metric_id"]))
    await db.commit()
    await db.refresh(row)
    return label_dict(row)


async def delete_label(db: AsyncSession, label_id: int) -> dict:
    """删除标签。下面还挂着问题就不让删 —— 删了那些问题会变成「无处可挂」,
    在前台直接消失,而运营完全不知道发生了什么。"""
    row = await db.get(PublicProblemDimensionLabel, label_id)
    if row is None:
        raise NotFound("维度标签不存在")
    used = (await db.execute(sa.select(sa.func.count()).select_from(ScenicAgentIssueDetail).where(
        ScenicAgentIssueDetail.topic_id == row.topic_id,
        ScenicAgentIssueDetail.dimension_id == row.dimension_id,
        ScenicAgentIssueDetail.metric_id == row.metric_id,
    ))).scalar_one()
    if used:
        raise ValidationError(f"该指标下还有 {used} 个问题,请先移走或删除这些问题再删标签")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "message": f"已删除标签 {row.topic}/{row.dimension}/{row.metric}"}


# ── 问题 ────────────────────────────────────────────────────────────────

def issue_dict(row: ScenicAgentIssueDetail) -> dict:
    """管理端用的完整结构(APP 端的精简结构见 business_issue_list)。"""
    return {
        "id": row.id, "issue_id": row.issue_id, "scenic_id": row.scenic_id,
        "business_class": row.business_class,
        "connection_id": row.connection_id,
        # 同一个值的两个字段名:文档里叫 quickbiLinkId,库里叫 connection_id。
        # 都给出去,前端用哪个都行,不用记映射。
        "quickbi_link_id": row.connection_id,
        "ask_type": row.ask_type, "capability": row.capability,
        "target": row.target or {},
        "target_data_id": row.target_data_id, "target_data_name": row.target_data_name,
        "agent_id": row.agent_id, "skill_id": row.skill_id,
        "topic": row.topic, "topic_id": row.topic_id,
        "dimension": row.dimension, "dimension_id": row.dimension_id,
        "metric": row.metric, "metric_id": row.metric_id,
        "title": row.title, "issue": row.issue,
        "model_instruction": row.model_instruction, "param_template": row.param_template,
        "time_period": row.time_period,
        "sort_order": row.sort_order, "active": row.active,
        "last_run_id": row.last_run_id, "last_run_status": row.last_run_status,
        "last_run_time": row.last_run_time,
        "source_question_id": row.source_question_id,
        "create_time": row.create_time, "update_time": row.update_time,
    }


def _scenic_clause(scenic_id: str | None):
    """景区过滤:本景区 + 全景区通用(scenic_id 为空)。

    通用问题也要带出来,否则集团配的模板问题在每个景区都看不到 —— 那就等于没配。
    """
    if not scenic_id:
        return sa.true()
    return sa.or_(ScenicAgentIssueDetail.scenic_id == scenic_id,
                  ScenicAgentIssueDetail.scenic_id.is_(None))


async def list_issues(db: AsyncSession, *, scenic_id: str | None = None,
                      business_class: str | None = None, topic_id: str | None = None,
                      dimension_id: str | None = None, metric_id: str | None = None,
                      keyword: str | None = None, active: bool | None = None) -> list[dict]:
    stmt = sa.select(ScenicAgentIssueDetail).where(_scenic_clause(scenic_id))
    if business_class:
        stmt = stmt.where(ScenicAgentIssueDetail.business_class == business_class)
    if topic_id:
        stmt = stmt.where(ScenicAgentIssueDetail.topic_id == topic_id)
    if dimension_id:
        stmt = stmt.where(ScenicAgentIssueDetail.dimension_id == dimension_id)
    if metric_id:
        stmt = stmt.where(ScenicAgentIssueDetail.metric_id == metric_id)
    if active is not None:
        stmt = stmt.where(ScenicAgentIssueDetail.active.is_(active))
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(sa.or_(ScenicAgentIssueDetail.title.ilike(like),
                                 ScenicAgentIssueDetail.issue.ilike(like)))
    stmt = stmt.order_by(ScenicAgentIssueDetail.sort_order, ScenicAgentIssueDetail.id)
    return [issue_dict(r) for r in (await db.execute(stmt)).scalars()]


async def get_issue(db: AsyncSession, issue_id: str) -> ScenicAgentIssueDetail:
    """按问题 ID 取一行。**新旧两种 ID 都认**。

    issue_xxx 是本表的业务主键;而前端缓存、历史配置、老版本的问题列表里存的可能是
    迁移前的老表 ID(app_question_bank.id 或 tc_qbi_fixed_questions.id)。
    那些入口不该因为后台换了张表就集体报「问题不存在」,所以 source_question_id 也参与匹配。
    """
    row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(sa.or_(
        ScenicAgentIssueDetail.issue_id == issue_id,
        ScenicAgentIssueDetail.source_question_id == issue_id,
    )).order_by(
        # 主键命中优先于溯源 ID 命中(理论上不会同时命中,真撞上了也要有确定的结果)
        (ScenicAgentIssueDetail.issue_id == issue_id).desc(),
    ))).scalars().first()
    if row is None:
        raise NotFound(f"问题不存在:{issue_id}")
    return row


_WRITABLE = (
    "scenic_id", "business_class", "connection_id", "ask_type", "capability", "target",
    "target_data_id", "target_data_name", "agent_id", "skill_id",
    "topic", "topic_id", "dimension", "dimension_id", "metric", "metric_id",
    "title", "issue", "model_instruction", "param_template", "time_period",
    "sort_order", "active",
)


def _validate_issue(data: dict) -> None:
    bc = data.get("business_class")
    if bc is not None and bc not in BUSINESS_CLASSES:
        raise ValidationError(f"business_class 只能是 {' / '.join(BUSINESS_CLASSES)}")
    if bc == BUSINESS_CLASS_QUICK_BI and not (data.get("connection_id") or "").strip():
        # 没有连接就不知道该拿哪套凭证去问,发起时必然失败 —— 与其让用户点了才报错,
        # 不如在保存这一步就说清楚。
        raise ValidationError("Quick BI 问题必须选择一个 Quick BI 连接(connection_id)")
    if not (data.get("issue") or "").strip():
        raise ValidationError("问题内容(issue)不能为空")
    if not (data.get("title") or "").strip():
        raise ValidationError("问题标题(title)不能为空")


async def _fill_label_names(db: AsyncSession, data: dict) -> None:
    """只给了编码时,把中文名补上(前台列表直接用名字分组,不必再查一次标签表)。"""
    codes = (data.get("topic_id"), data.get("dimension_id"), data.get("metric_id"))
    if not all(codes):
        return
    row = (await db.execute(sa.select(PublicProblemDimensionLabel).where(
        PublicProblemDimensionLabel.topic_id == codes[0],
        PublicProblemDimensionLabel.dimension_id == codes[1],
        PublicProblemDimensionLabel.metric_id == codes[2],
    ))).scalars().first()
    if row is None:
        raise ValidationError("指定的 主题/维度/指标 组合不存在,请先到「知识中心-维度标签」维护")
    data.setdefault("topic", row.topic)
    data.setdefault("dimension", row.dimension)
    data.setdefault("metric", row.metric)
    data["topic"], data["dimension"], data["metric"] = row.topic, row.dimension, row.metric


async def create_issue(db: AsyncSession, data: dict) -> dict:
    payload: dict[str, Any] = {k: v for k, v in data.items() if k in _WRITABLE and v is not None}
    payload.setdefault("business_class", BUSINESS_CLASS_LOCAL)
    _validate_issue(payload)
    await _fill_label_names(db, payload)
    row = ScenicAgentIssueDetail(issue_id=f"issue_{uuid.uuid4().hex[:16]}", **payload)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info(f"问题新增 {row.issue_id}({row.business_class}) {row.title}")
    return issue_dict(row)


async def update_issue(db: AsyncSession, issue_id: str, data: dict) -> dict:
    row = await get_issue(db, issue_id)
    payload = {k: v for k, v in data.items() if k in _WRITABLE}
    merged = {**issue_dict(row), **{k: v for k, v in payload.items() if v is not None}}
    _validate_issue(merged)
    if any(payload.get(k) for k in ("topic_id", "dimension_id", "metric_id")):
        await _fill_label_names(db, merged)
        for k in ("topic", "dimension", "metric"):
            payload[k] = merged[k]
    for k, v in payload.items():
        if v is not None:
            setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return issue_dict(row)


async def delete_issue(db: AsyncSession, issue_id: str) -> dict:
    """删除一条问题 —— **删干净**,不留会让它在前台复活或残留的东西。

    原先只删这一行,于是删完之后前台照样看得到 / 点得到:
      ① 来源问题库行还在,下次在「APP 运营 · 固定问题」保存一次,
         sync_from_question_bank 会按 source_question_id 查不到而**原地重建**一条;
      ② 预热缓存按来源问题 ID 存,答案还在,概要卡片与预热直出继续命中;
      ③ 关注列表按**文本**存,列表还列着它,点下去反查不到 issue → 掉回通用对话,
         回一句「请告诉我您需要查询什么」。
    这三样一起删,才是运营点「删除」时以为会发生的事。
    """
    from sqlalchemy import delete as _sa_delete

    from cogni_agent.control_plane_service.models import AppFollowedQuestion
    from cogni_agent.gateway_service.models import AppPrewarmCache, AppQuestionBank

    row = await get_issue(db, issue_id)
    src, texts = row.source_question_id, {t for t in (row.issue, row.title) if t}
    await db.delete(row)
    if src:
        # 预热缓存与问题库行的维度都是来源问题 ID
        await db.execute(_sa_delete(AppPrewarmCache).where(AppPrewarmCache.question_id == src))
        await db.execute(_sa_delete(AppQuestionBank).where(AppQuestionBank.id == src))
    if texts:
        await db.execute(_sa_delete(AppFollowedQuestion).where(
            AppFollowedQuestion.question_text.in_(list(texts))))
    await db.commit()
    log.info(f"删除问题 {issue_id}「{row.title}」(级联清理来源问题/预热缓存/关注记录)")
    return {"ok": True, "message": f"已删除问题「{row.title}」,该问题的预热缓存与关注记录一并清除"}


async def touch_run(db: AsyncSession, issue_id: str, *, run_id: str | None,
                    status: str | None) -> None:
    """记一次执行留痕(最后一次跑成什么样)。失败不抛 —— 留痕不该挡住业务。"""
    try:
        await db.execute(sa.update(ScenicAgentIssueDetail)
                         .where(ScenicAgentIssueDetail.issue_id == issue_id)
                         .values(last_run_id=run_id, last_run_status=status,
                                 last_run_time=sa.func.now()))
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"问题 {issue_id} 执行留痕写入失败(不影响本次提问):{exc}")


# ══════════════ APP 问题库 → 统一问题表 的同步 ══════════════
# 问题库(app_question_bank)有自己的一套机制:预热缓存、指标定义、参数模板门禁,
# 都按它的 id 挂着,所以那张表留着。但前台的问题树只读统一问题表 ——
# 光留着它、不同步,新建的问题就会「后台有、APP 没有」,正是 0076 要解决的老毛病。
# 于是保存/删除问题库时把统一表带上,按 source_question_id 认领同一行。


async def sync_from_question_bank(db: AsyncSession, bank_row) -> None:
    """把一条 APP 问题库的行同步进统一问题表(有则更新、无则插入)。

    同步失败只告警不抛:问题库那一行已经保存成功了,为一条镜像数据把保存整个回滚,
    对运营来说就是「点了保存却说失败,再点一次又重复了」。
    """
    try:
        row = (await db.execute(sa.select(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == bank_row.id))).scalars().first()
        values = dict(
            scenic_id=bank_row.scenic_id, business_class=BUSINESS_CLASS_LOCAL,
            agent_id=getattr(bank_row, "agent_id", None), skill_id=bank_row.skill_id,
            # 问题库的三级是老口径(theme_code / dimension_name / metric_name),
            # 只搬名称、不猜编码 —— 猜错了前台会把问题分到错误的分组里
            topic=bank_row.theme_code, dimension=bank_row.dimension_name,
            metric=bank_row.metric_name,
            title=(bank_row.metric_name or bank_row.question_text[:60]),
            issue=bank_row.question_text,
            model_instruction=getattr(bank_row, "model_instruction", None),
            param_template=bank_row.param_template,
            time_period=bank_row.time_period,
            sort_order=bank_row.sort_order or 0, active=bool(bank_row.active),
            visibility="group",
        )
        if row is None:
            db.add(ScenicAgentIssueDetail(
                issue_id=f"issue_{str(bank_row.id).replace('-', '')}",
                source_question_id=bank_row.id, **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"问题库 {bank_row.id} 同步到统一问题表失败(不影响本次保存):{exc}")


async def drop_issue_of_source(db: AsyncSession, source_id: str) -> None:
    """删问题库那一行时把镜像行一并删掉,免得前台还挂着一个点不动的问题。"""
    try:
        await db.execute(sa.delete(ScenicAgentIssueDetail).where(
            ScenicAgentIssueDetail.source_question_id == source_id))
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"问题 {source_id} 的镜像行删除失败:{exc}")


async def business_issue_list(db: AsyncSession, scenic_id: str | None) -> list[dict]:
    """APP 端问题列表:主题 → 维度 → 指标 → 问题 的嵌套结构。

    嵌套里既有中文名也有编码键(如 topic + visitorFlow),是按前后端约定的形状给的:
    前端按中文名展示、按编码键取下一层。空的层级不会出现 —— 没有问题的指标不返回,
    免得前台选到一个点进去是空的指标。

    **三级标签没挂全的问题不出现在这棵树上。** 原先是兜到「未分类 / 未分类 / 未分类」
    一个分支里,于是前台多出一个谁也说不清是什么的入口,而管理端一直按
    「没挂标签 = 前台看不到」提示运营 —— 两边说的不是一回事,运营照着管理端的说法配,
    前台却冒出一堆未分类。挂载点没配就是没配,别替它猜一个。
    """
    rows = (await db.execute(
        sa.select(ScenicAgentIssueDetail)
        .where(_scenic_clause(scenic_id), ScenicAgentIssueDetail.active.is_(True),
               ScenicAgentIssueDetail.topic_id.isnot(None), ScenicAgentIssueDetail.topic_id != "",
               ScenicAgentIssueDetail.dimension_id.isnot(None), ScenicAgentIssueDetail.dimension_id != "",
               ScenicAgentIssueDetail.metric_id.isnot(None), ScenicAgentIssueDetail.metric_id != "",
               # 「主题解读」不进这棵树:它由首页那 5 张主题卡片承载(滑到哪张出哪份,
               # 而且走预热直出)。再放进三级选择器里,等于同一件事两个入口 ——
               # 而选择器里那个点下去要现跑几分钟,体验比卡片差得多。
               # 管理端的选择器不受这条影响(运营仍要能把自己写的解读挂到主题板块下)。
               ScenicAgentIssueDetail.topic_id != THEME_REPORT_TOPIC_ID)
        .order_by(ScenicAgentIssueDetail.sort_order, ScenicAgentIssueDetail.id)
    )).scalars().all()

    topics: list[dict] = []
    t_idx: dict[str, dict] = {}
    d_idx: dict[tuple, dict] = {}
    m_idx: dict[tuple, dict] = {}
    for r in rows:
        tid, did, mid = r.topic_id, r.dimension_id, r.metric_id
        t = t_idx.get(tid)
        if t is None:
            t = {"topic": r.topic or tid, "topicId": tid, tid: []}
            t_idx[tid] = t
            topics.append(t)
        d = d_idx.get((tid, did))
        if d is None:
            d = {"dimension": r.dimension or did, "dimensionId": did, did: []}
            d_idx[(tid, did)] = d
            t[tid].append(d)
        m = m_idx.get((tid, did, mid))
        if m is None:
            m = {"metric": r.metric or mid, "metricId": mid, mid: [{"issues": []}]}
            m_idx[(tid, did, mid)] = m
            d[did].append(m)
        m[mid][0]["issues"].append({
            "issueId": r.issue_id,
            "businessClass": r.business_class,
            "title": r.title,
            "issue": r.issue,
            # 能力:Quick BI 才有(报表/问数/解读/报告),等价于操练台选完问题自动带出的那个
            "capability": r.capability,
            "capabilityLabel": _CAPABILITY_LABEL.get(r.capability or ""),
            "askType": r.ask_type,
            # 就是操练台上选的那个 Quick BI 链接 ID(= connection_id)
            "quickbiLinkId": r.connection_id,
            "connectionId": r.connection_id,
            "targetDataId": r.target_data_id,
            "targetDataName": r.target_data_name,
            # local_system 用:发起时带上,省得前端再查一次
            "agentId": r.agent_id,
            "skillId": r.skill_id,
        })
    return topics


# 能力编码 → 中文名(与 Quick BI 能力表同口径,这里只做展示)
_CAPABILITY_LABEL = {
    "dataset_qa": "问数", "file_qa": "问数", "dashboard_qa": "报表",
    "insight": "解读", "report": "报告",
}
