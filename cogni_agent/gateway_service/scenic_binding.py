# -*- coding: utf-8 -*-
"""景区绑定:一切按景区对齐的唯一解析处。

**为什么需要这个模块**:系统里「景区」是所有东西的轴 ——
人绑景区、Quick BI 连接绑景区、问题绑景区、前台能看到什么也按景区。
但这条轴原先散在四五个地方各判一遍:APP 端一套、Quick BI 一套、问题一套,
于是只要有人改了归属(把账号换个景区、把连接换个景区、把问题换个景区),
就会有某一处对不上,表现成「发起请求失败」或者「答非所问」——
而报错信息指向的往往是最后那一步(连接不存在 / 没有可用 Agent),跟真正的原因隔着三层。

这里把四个判断收到一处:

  effective_scenic()         这次请求算哪个景区(景区账号不能越界)
  quickbi_connection_for()   这个景区能用哪条 Quick BI 连接
  theme_issue()              这个景区的「主题解读」问题(概览/客群/票务/舆情/OTA)
  ensure_theme_issues()      给这个景区补齐那 5 条问题(绑连接、绑数据、挂标签)

下游只调这几个函数,不再各自拼可见性条件。
"""
from __future__ import annotations

import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import ValidationError
from cogni_agent.common.logging import get_logger
from cogni_agent.gateway_service.issue_models import (
    BUSINESS_CLASS_QUICK_BI, ScenicAgentIssueDetail,
)

log = get_logger("gateway_service.scenic_binding")

# 「主题解读」的五个主题。key 与 guide_data.GUIDE_CARDS 的 theme_code 一致;
# 前台点卡片发的就是 "【主题解读】<name>",所以这里同时记下展示名用于反查。
# 所有主题报告共用的开头。单独抽出来是因为它规定的是**怎么读数据**,
# 而不是读哪一块 —— 五个主题在这一点上应当完全一致,各写一份迟早会写歪。
_REPORT_BASE = (
    "你面对的是本景区的一块 Quick BI 可视化看板,下面给到的就是这块看板上各个图表的数据。"
    "请**严格基于这些图表的数据**写一份可视化数据解读报告:\n"
    "① 只用看板上真实出现的数字,没有的指标就说「看板未覆盖」,不要推测、不要补充常识性数据;\n"
    "② 每个结论后面跟上它的来源图表名与关键数值,让人能对回看板上去核对;\n"
    "③ 有同比/环比就必须写出来,并说明是变好还是变差;\n"
    "④ 用中文、分段小标题、结论先行;不要罗列原始表格,也不要复述图表标题当正文;\n"
    "⑤ 全文 600-900 字。\n"
)

THEMES: list[dict[str, Any]] = [
    # theme_code        展示名     数据域(找资产/问题时用)   给 Quick BI 的报告口径
    #
    # instruction 是**默认**口径,只在第一次生成时写进问题行;之后运营在
    # 「Quick BI · 固定问题」里改的那份才是准的(重绑不覆盖,见 ensure_theme_issues)。
    #
    # 口径怎么写:报告打在**绑定的那块仪表板**上,所以要让模型「按图说话」——
    # 先说明数据来源就是这块看板上的图表,再规定读图的顺序与产出结构。
    # 不写这一句的话,模型会把看板当背景板,自己编一套通用结论出来 ——
    # 看着很完整,但和图上的数对不上,这种错运营一眼看不出来。
    {"code": "overview", "name": "概览", "domain": "全域",
     "instruction": _REPORT_BASE + (
         "本报告是本景区的**经营概览**。按客流、票务、舆情、渠道四块各出一段,"
         "每段先给一句结论,再用看板上的数支撑(至少 2 个指标,带同比/环比)。"
         "最后单列「需要关注」3 条,按影响面排序,每条写清是哪张图看出来的。")},
    {"code": "passenger_flow", "name": "客群", "domain": "客群",
     "instruction": _REPORT_BASE + (
         "本报告解读**客群结构**。逐项读看板上的客群图表:年龄、性别、客源地、"
         "出游距离、消费偏好、旅游偏好,每项写出占比最高的两档与其变化方向。"
         "然后指出结构性变化与风险(如某一客群占比大幅下滑),"
         "最后给 2-3 条针对该客群的可执行建议(产品/渠道/时段)。")},
    {"code": "ticket", "name": "票务", "domain": "票务",
     "instruction": _REPORT_BASE + (
         "本报告解读**票务经营**。读看板上的票务图表:销量、收入、票种结构、"
         "渠道占比、退票情况,每项给出当期值与环比。挑出异常(环比波动超过 ±10%、"
         "或某票种/渠道占比突变)并说明可能原因,最后给 2-3 条建议。")},
    {"code": "public_opinion", "name": "舆情", "domain": "舆情",
     "instruction": _REPORT_BASE + (
         "本报告解读**口碑舆情**。读看板上的舆情图表:总评分与评分变化、情感倾向占比、"
         "负面关键词、平台分布、评论量趋势。把负面点按出现频次列出来(至少 3 条,"
         "带上原词),指出最该先处置的那条,最后给 2-3 条改善建议。")},
    {"code": "ota_direct_sales", "name": "OTA直销", "domain": "OTA直销",
     "instruction": _REPORT_BASE + (
         "本报告解读**OTA 与直销渠道**。读看板上的渠道图表:各渠道订单量、出票量、"
         "核销率、客单价,以及与上期的对比。说明渠道结构的变化(谁在涨、谁在掉),"
         "指出核销率或客单价异常的渠道,最后给 2-3 条渠道运营建议。")},
]
THEME_BY_CODE = {t["code"]: t for t in THEMES}
THEME_BY_NAME = {t["name"]: t for t in THEMES}

THEME_PREFIX = "【主题解读】"


def theme_of_message(message: str) -> dict | None:
    """把「【主题解读】客群」这样的消息还原成主题定义。

    老前端点主题卡片时发的就是这句原文(没有 issue_id),后端得认得出来 ——
    认不出来就会当成一句自由提问丢给默认 Agent,回一段「请告诉我您需要查询什么」,
    而用户明明点的是「客群解读」。
    """
    text = (message or "").strip()
    if not text.startswith(THEME_PREFIX):
        return None
    return THEME_BY_NAME.get(text[len(THEME_PREFIX):].strip())


def effective_scenic(user, requested: str | None = None) -> str | None:
    """这次请求算哪个景区。

    景区账号只能用自己绑的那个 —— 传别的直接拒,否则一个景区的人能把请求打到
    另一个景区的数据上。集团账号(scenic_id 为空)可以指定任意景区,不指定就是集团级。
    """
    want = (requested or "").strip() or None
    mine = getattr(user, "scenic_id", None) or None
    if want and mine and want != mine:
        raise ValidationError(f"当前账号绑定的是景区 {mine},无权指定景区 {want}")
    return want or mine


# ── 本轮用哪个景区 ────────────────────────────────────────────────────
#
# 规则(产品口径):**请求里指定了就用指定的 → 没指定用用户当前归属 → 都没有就问用户**。
#
# 落到两类账号上:
#   - 景区账号:只能是自己**当前**归属的那个景区。不用会话上记的旧景区 ——
#     用户被改了归属之后,旧会话还在用旧景区取数,取回来的是别人家的数据,
#     而回答里的景区名、数字都对得上号,没人会发现。(当前归属由 deps 每次请求从库里重读。)
#   - 集团账号:没有「归属」可用。消息里点名了哪个景区就用哪个,并记到会话上;
#     这段会话之前点名过就沿用;都没有就交给执行层,真用到景区时发起澄清。

# 候选景区 ID 的形状(PFTSCA01018088 / PFTSCX00012325 这类)。**只用来挑候选,
# 不作为判定依据** —— 真正的判定是「库里有没有这个 ID」,凭形状认会把订单号当景区。
_SCENIC_ID_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{7,31}\b")


# 景区名里的通用后缀。算「核心名」时去掉 —— 不去的话,一句「这个风景区怎么样」
# 会因为「风景区」三个字和每个景区都对得上。
_SCENIC_SUFFIX = re.compile(r"(国家级)?(名胜)?(风景名胜区|风景区|旅游区|景区|度假区|国家公园|森林公园|公园|景点)$")


def _longest_common_run(a: str, b: str) -> int:
    """a 与 b 最长公共连续片段的长度(景区名很短,朴素 DP 足够)。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


async def scenic_from_text(db: AsyncSession, text: str) -> tuple[str | None, str]:
    """从一句话里认出用户点名的景区。返回 (scenic_id, 认出的依据);认不出返回 (None, 原因)。

    **只接受唯一结果**:「泰宁」同时对得上两个景区时不替用户挑一个 ——
    挑错了取回来的是另一个景区的数,而回答看起来完全正常。宁可再问一次。
    """
    from cogni_agent.user_center_service.models import SysScenic

    t = (text or "").strip()
    if not t:
        return None, "空消息"
    ids = set(_SCENIC_ID_TOKEN.findall(t.upper()))
    if ids:
        rows = (await db.execute(sa.select(SysScenic.scenic_id).where(
            SysScenic.scenic_id.in_(sorted(ids))))).scalars().all()
        if len(rows) == 1:
            return rows[0], f"景区 ID {rows[0]}"
        if len(rows) > 1:
            return None, f"一句话里出现了多个景区 ID({'、'.join(rows)}),无法确定是哪一个"
    names = (await db.execute(sa.select(SysScenic.scenic_id, SysScenic.scenic_name))).all()
    # 正向:景区全名出现在句子里(「九龙潭名胜风景区的客流」)。取最长命中,
    # 这样「九龙潭名胜风景区」不会被一个叫「九龙潭」的短名抢走。
    hits = [(sid, nm) for sid, nm in names if nm and len(nm) >= 2 and nm in t]
    if hits:
        longest = max(len(nm) for _, nm in hits)
        top = [(sid, nm) for sid, nm in hits if len(nm) == longest]
        if len(top) == 1:
            return top[0][0], f"景区名「{top[0][1]}」"
        return None, f"「{'」「'.join(nm for _, nm in top)}」都对得上,无法确定是哪一个"
    # 句中简称:「大金湖近7天客流」—— 用户第一句话就这么说,而景区全名是「泰宁大金湖风景区」。
    # 不认这种说法的话,点名了景区还被反问「查哪个景区」,只会让人觉得系统没在听。
    # 做法:把景区名去掉通用后缀得到「核心名」,找它和这句话最长的公共片段(≥3 字),
    # 唯一最长才算。3 字下限是为了不让「泰宁」这种两个景区共有的地名单独定案。
    best_len, best = 0, []
    for sid, nm in names:
        core_nm = _SCENIC_SUFFIX.sub("", nm or "")
        n = _longest_common_run(core_nm, t)
        if n >= 3:
            if n > best_len:
                best_len, best = n, [(sid, nm)]
            elif n == best_len:
                best.append((sid, nm))
    if len(best) == 1:
        return best[0][0], f"景区简称 → {best[0][1]}"
    if len(best) > 1:
        return None, f"「{'」「'.join(nm for _, nm in best)}」都对得上,请说全称或景区 ID"
    # 反向:用户只说了简称(回答澄清时最常见:「九龙潭」)。只在整句很短时这么认 ——
    # 长句里随便一个两字词都可能是某个景区名的一部分,那样会误认。
    core = re.sub(r"[\s,。,.!！?？的吧呀啊]+", "", t)
    if 2 <= len(core) <= 12:
        part = [(sid, nm) for sid, nm in names if nm and core in nm]
        if len(part) == 1:
            return part[0][0], f"景区简称「{core}」→ {part[0][1]}"
        if len(part) > 1:
            return None, f"「{core}」对得上 {len(part)} 个景区,请说全称或景区 ID"
    return None, "消息里没有点名景区"


async def resolve_turn_scenic(db: AsyncSession, *, user, conv=None,
                              message: str = "") -> tuple[str | None, str]:
    """本轮对话用哪个景区。返回 (scenic_id | None, 来源说明)。None = 需要向用户澄清。

    会话对象上的 scenic_id 会被就地更新(集团账号点名了新景区时),由调用方提交。
    """
    mine = getattr(user, "scenic_id", None) or None
    if mine:
        # 景区账号:永远是**当前**归属。会话上记的是旧的就跟着改过来。
        if conv is not None and getattr(conv, "scenic_id", None) != mine:
            log.info(f"会话 {getattr(conv, 'id', '?')} 的景区 {getattr(conv, 'scenic_id', None)} "
                     f"与用户当前归属 {mine} 不一致,按当前归属")
            conv.scenic_id = mine
        return mine, "用户当前归属景区"
    # 集团账号:先看这句话有没有点名
    sid, why = await scenic_from_text(db, message)
    if sid:
        if conv is not None and getattr(conv, "scenic_id", None) != sid:
            conv.scenic_id = sid
        return sid, f"消息中指定({why})"
    prior = getattr(conv, "scenic_id", None) if conv is not None else None
    if prior:
        return prior, "本会话此前指定的景区"
    return None, why


async def scenic_choices(db: AsyncSession, limit: int = 8) -> list[str]:
    """澄清时给用户看几个可选的景区名 —— 直接问「哪个景区」而不给选项,
    集团账号的人未必记得住那串 ID,只能再问一轮「有哪些」。"""
    from cogni_agent.user_center_service.models import SysScenic

    rows = (await db.execute(
        sa.select(SysScenic.scenic_name).where(SysScenic.is_enabled.is_(True))
        .order_by(SysScenic.scenic_name).limit(limit))).scalars().all()
    return [r for r in rows if r]


async def quickbi_connection_for(db: AsyncSession, scenic_id: str | None) -> str | None:
    """这个景区能用哪条 Quick BI 连接。

    顺序:⓪ 景区数据集映射里配的连接(tc_qbi_scenic_datasets,显式配置,启用中才算)
    ① 绑定到该景区的启用连接(默认连接优先)② 集团级(未绑景区)的启用连接。
    这正是「景区绑定」的落点 —— 管理员把连接绑到景区之后,该景区的人发起提问就该用它,
    不必再在每条问题上重复指定;而问题上指定的连接一旦失效(改了归属/停用),
    也能落到这里兜底,而不是直接 500。
    """
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    async def _pick(cond) -> str | None:
        row = (await db.execute(
            sa.select(QbiConnection.id)
            .where(QbiConnection.active.is_(True), cond)
            .order_by(QbiConnection.is_default.desc(), QbiConnection.created_at)
            .limit(1))).scalar_one_or_none()
        return row

    if scenic_id:
        from cogni_agent.tool_center_service.quickbi.models import QbiScenicDataset

        mapped = await _pick(QbiConnection.id.in_(
            sa.select(QbiScenicDataset.connection_id).where(QbiScenicDataset.scenic_id == scenic_id)))
        if mapped:
            return mapped
        hit = await _pick(QbiConnection.scenic_id == scenic_id)
        if hit:
            return hit
    return await _pick(QbiConnection.scenic_id.is_(None))


async def connection_usable(db: AsyncSession, connection_id: str | None,
                            scenic_id: str | None) -> str | None:
    """问题上写的连接现在还能用吗?不能用就按景区换一条。

    「改了归属之后发起失败」就是这里:问题还指着一条已经改绑别的景区(或已停用)的连接。
    与其报「连接不存在」让人去猜,不如按景区解析出一条能用的。
    """
    from cogni_agent.tool_center_service.quickbi.models import QbiConnection

    cid = (connection_id or "").strip() or None
    if cid:
        row = (await db.execute(sa.select(QbiConnection).where(
            QbiConnection.id == cid, QbiConnection.active.is_(True)))).scalars().first()
        # 连接还在、且没绑到别的景区(绑到别的景区 = 这条问题被挪走了,按当前景区重挑)
        if row is not None and (not row.scenic_id or not scenic_id or row.scenic_id == scenic_id):
            return row.id
        log.info(f"问题绑定的 Quick BI 连接 {cid} 对景区 {scenic_id} 不可用,按景区重新解析")
    return await quickbi_connection_for(db, scenic_id)


async def theme_issue(db: AsyncSession, scenic_id: str | None, theme_code: str):
    """取这个景区的某个主题解读问题(没有就返回 None)。

    先找本景区专属的,再找集团级通用的 —— 与问题列表同一口径。
    """
    stmt = sa.select(ScenicAgentIssueDetail).where(
        ScenicAgentIssueDetail.metric_id == f"themeReport_{theme_code}",
        ScenicAgentIssueDetail.active.is_(True),
    )
    if scenic_id:
        stmt = stmt.where(sa.or_(ScenicAgentIssueDetail.scenic_id == scenic_id,
                                 ScenicAgentIssueDetail.scenic_id.is_(None)))
    else:
        stmt = stmt.where(ScenicAgentIssueDetail.scenic_id.is_(None))
    # 本景区的排在集团级前面:景区自己配过就用自己的
    stmt = stmt.order_by(ScenicAgentIssueDetail.scenic_id.isnot(None).desc(),
                         ScenicAgentIssueDetail.id)
    return (await db.execute(stmt)).scalars().first()


# 主题 → 资产名里可能出现的叫法。运营给仪表板起名不会照着我们的 domain 字段起:
# 「票务」那块看板常叫「门票销售」「售票分析」,「概览」常叫「综合看板」「驾驶舱」。
# 只拿 domain 一个词去匹配,命中率低得可怜 —— 而一旦不命中,旧代码就退回「任一仪表板」,
# 于是票务主题被绑到舆情看板上,报告老老实实按舆情看板写,标题却写着「票务」。
_DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "全域": ("全域", "概览", "总览", "综合", "驾驶舱", "大屏", "总体"),
    "客群": ("客群", "客流", "画像", "游客"),
    "票务": ("票务", "门票", "售票", "票房", "销售", "营收"),
    "舆情": ("舆情", "口碑", "评价", "评论", "满意度"),
    "OTA直销": ("OTA", "直销", "渠道", "分销"),
}


_GENERIC_ALIASES = {"游客"}


async def _theme_target(db: AsyncSession, connection_id: str, scenic_id: str | None,
                        domain: str | None) -> tuple[dict, str | None, str | None]:
    """给主题挑一份目标数据:只挑**名字对得上这个主题**的已登记仪表板。

    挑不到就返回空 target —— Quick BI 会自己智能选表。
    **宁可让它自己选,也绝不硬塞一份不相关的数据**:塞错了它照样会答,而且答得很像回事。
    现场那次就是这么来的:票务主题绑到了舆情看板上,于是「票务」卡片里是一份舆情报告,
    结构完整、数字也对(对的是舆情的数),运营只会觉得是模型答跑题了。

    (这段话原本就写在这里,但代码写的是 `a = hit or rows[0]` —— 注释说不塞,
    代码照塞不误。现在两者一致了。)
    """
    from cogni_agent.tool_center_service.quickbi.models import QbiAsset

    conds = [QbiAsset.connection_id == connection_id, QbiAsset.kind == "dashboard"]
    if scenic_id:
        conds.append(sa.or_(QbiAsset.scenic_id == scenic_id, QbiAsset.scenic_id.is_(None)))
    rows = (await db.execute(sa.select(QbiAsset).where(*conds)
                             .order_by(QbiAsset.scenic_id.isnot(None).desc(),
                                       QbiAsset.created_at))).scalars().all()
    if not rows or not domain:
        return {}, None, None
    keys = _DOMAIN_ALIASES.get(domain) or (domain.replace("直销", ""),)
    # 「游客」是各主题都会用的泛称(游客画像 / 游客评价 / 游客量),只算加分、不算冲突
    others = [k for d, ks in _DOMAIN_ALIASES.items() if d != domain for k in ks if k not in _GENERIC_ALIASES]

    def _score(a) -> tuple[int, int]:
        name = a.name or ""
        mine = sum(1 for k in keys if k and k in name)
        # 同时命中别的主题的词 = 名字有歧义。例如「游客评价分析」里的「游客」
        # 命中客群、「评价」命中舆情 —— 以前按顺序取第一个命中的,客群主题就会
        # 绑到这块舆情看板上,客群卡片里写出来的是评论量。
        clash = sum(1 for k in others if k and k in name and k not in keys)
        return mine, clash

    scored = [(a, *_score(a)) for a in rows]
    clean = [a for a, mine, clash in scored if mine and not clash]
    fuzzy = sorted([x for x in scored if x[1] and x[2]], key=lambda x: (x[2] - x[1]))
    # 先要「只像本主题」的;没有才退而求其次,取本主题词多于别的主题词的那块
    hit = clean[0] if clean else next((a for a, mine, clash in fuzzy if mine > clash), None)
    if hit is None:
        if len(rows) == 1:
            # 只登记了一块看板:没有「挑错」这回事 —— 它是这个景区仅有的那份数据,
            # 名字叫什么(往往就是景区名)说明不了它不覆盖这个主题。
            # 真正危险的是**多块看板里挑错一块**,那才会出现「票务卡片里是舆情报告」。
            return {"asset_id": rows[0].id, "page_id": rows[0].ref_id}, rows[0].ref_id, rows[0].name
        log.info(f"主题「{domain}」在连接 {connection_id} 下没找到名字对得上的仪表板"
                 f"(候选 {[a.name for a in rows][:8]}),不绑目标数据,交给 Quick BI 自己选表 ——"
                 f"硬塞一块别的主题的看板,报告会按那块看板写,而卡片标题仍写着本主题")
        return {}, None, None
    return {"asset_id": hit.id, "page_id": hit.ref_id}, hit.ref_id, hit.name


# 重绑时**保留**运营自己写过的那几列。
# 判断标准是「这一列有没有被人动过」,所以只要非空就保留 ——
# 默认值是生成时写进去的,运营改过之后就不该再被代码里的常量覆盖。
_AUTHORED_FIELDS = ("model_instruction", "title", "issue", "category")


async def ensure_theme_issues(db: AsyncSession, scenic_id: str | None,
                              connection_id: str | None = None,
                              overwrite: bool = False) -> dict:
    """给这个景区补齐 5 条「主题解读」问题(概览/客群/票务/舆情/OTA直销)。

    这些问题是**报告类**:前台点主题卡片就出一份该主题的完整解读。它们必须绑到
    Quick BI —— 不绑的话点下去只会走通用对话,回一句「请告诉我您需要查询什么」,
    而这正是现场反馈的那个问题。

    幂等:已存在的默认跳过(overwrite=True 才按当前连接/数据重新绑一遍)。

    **重绑只重绑连接与数据**:标题、问题原文、口径说明(model_instruction)这几列
    一旦运营改过就保留 —— 否则每换一次看板,五条口径都得重写一遍,而且是悄悄没的。
    """
    conn_id = connection_id or await quickbi_connection_for(db, scenic_id)
    if not conn_id:
        raise ValidationError(
            "该景区还没有可用的 Quick BI 连接:请先在「工具中心 · Quick BI · 连接配置」"
            "新建连接并把归属景区绑到这个景区(或建一条集团级连接供所有景区共用)")

    created, updated, skipped = [], [], []
    for t in THEMES:
        row = await theme_issue(db, scenic_id, t["code"])
        # 集团级那条不算「本景区已有」:本景区要配自己的就得新建一条
        own = row if (row is not None and row.scenic_id == scenic_id) else None
        if own is not None and not overwrite:
            skipped.append(t["code"])
            continue

        target, data_id, data_name = await _theme_target(db, conn_id, scenic_id, t["domain"])
        values = dict(
            scenic_id=scenic_id, business_class=BUSINESS_CLASS_QUICK_BI,
            connection_id=conn_id, ask_type="report", capability="report",
            target=target, target_data_id=data_id, target_data_name=data_name,
            # 三级标签:主题解读自成一档,挂在「主题解读 / <主题> / 解读报告」下,
            # 这样它既能出现在问题树里,又不会和运营自己配的指标问题混在一起
            topic="主题解读", topic_id="themeReport",
            dimension=t["name"], dimension_id=f"theme_{t['code']}",
            metric="解读报告", metric_id=f"themeReport_{t['code']}",
            title=f"{THEME_PREFIX}{t['name']}",
            issue=f"{THEME_PREFIX}{t['name']}",
            model_instruction=t["instruction"],
            time_period="today", sort_order=THEMES.index(t), active=True,
            visibility="group", domain=t["domain"], category="主题解读",
        )
        if own is None:
            db.add(ScenicAgentIssueDetail(
                issue_id=f"issue_theme_{t['code']}_{uuid.uuid4().hex[:8]}", **values))
            created.append(t["code"])
        else:
            # 「全部重绑」重绑的是**连接与数据**,不该把运营改过的文案冲掉 ——
            # 冲掉的话,每次换个看板就得把五条口径重写一遍,而且是悄悄没的,
            # 运营下次看到的报告变了味,也不会想到是点了「全部重绑」。
            keep = {k: getattr(own, k) for k in _AUTHORED_FIELDS
                    if (getattr(own, k) or "").strip()}
            for k, v in values.items():
                setattr(own, k, v)
            for k, v in keep.items():
                setattr(own, k, v)
            updated.append(t["code"])
    await db.commit()
    log.info(f"景区 {scenic_id} 主题解读问题:新增 {len(created)} / 更新 {len(updated)} / 跳过 {len(skipped)}")
    return {"connection_id": conn_id, "created": created, "updated": updated, "skipped": skipped}


async def recent_success_run(db: AsyncSession, issue_id: str, scenic_id: str | None,
                             within_hours: int = 12,
                             expect_target_data_id: str | None = None,
                             strict_target: bool = False) -> dict | None:
    """这条问题**在这个景区**最近一次成功的运行结果(用于「预热跑一次、后续直出」)。

    主题解读是重活(报告类可能跑几分钟到几十分钟),每次点都重跑一遍既慢又费额度。
    预热任务跑完之后,窗口期内再点就直接回放那一次的结果。
    窗口默认 12 小时:当天的解读当天有效,隔夜的数据已经变了,该重跑。

    **scenic_id 是必传的,没有默认值** —— 这一点是故意的。一条集团级的主题解读对所有景区
    生效(theme_issue 找不到本景区专属的就回退到它),所以「这条问题最近一次成功的运行」
    在不限定景区时,取到的完全可能是**别的景区**那一次:A 景区的客群报告原样出现在
    B 景区的卡片上,而卡片正常渲染、正文结构完整、数字看着都对。没人会去核对。
    给它一个默认值,就等于给这个错误留了一条默认路径。

    **strict_target**:这条问题绑的目标数据改过之后,改之前跑出来的结果就作废了 ——
    它回答的是另一块看板。主题解读被绑错看板的那次事故(票务主题绑到舆情看板)修完,
    如果不管这一条,前台还会继续回放旧的那份错报告,最长 30 天 —— 代码改对了、
    现象一点没变,而没人猜得到是缓存。开启后只认 target_data_id 与当前绑定一致的运行;
    问题本身没绑目标数据(交给 Quick BI 自己选表)时不做这个判断,因为那时没有可比的基准。
    """
    from datetime import datetime, timedelta, timezone

    from cogni_agent.tool_center_service.quickbi.models import QbiRun

    since = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    stmt = sa.select(QbiRun).where(
        QbiRun.fixed_question_id == issue_id,
        QbiRun.status == "success",
        QbiRun.created_at >= since,
    )
    # 景区为空(集团级)时只认同样为空的那些,别把某个景区的结果当成集团级的
    stmt = stmt.where(QbiRun.scenic_id == scenic_id) if scenic_id \
        else stmt.where(QbiRun.scenic_id.is_(None))
    if strict_target and expect_target_data_id:
        # target 是 JSONB,不同来源写进去的键不完全一致(asset_id/page_id),
        # 按列比更稳:运行记录里 target 的 page_id 就是问题的 target_data_id。
        stmt = stmt.where(QbiRun.target["page_id"].astext == expect_target_data_id)
    row = (await db.execute(
        stmt.order_by(QbiRun.created_at.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    return {"run_id": row.id, "answer": row.answer or "", "result": row.result or {},
            "report_url": row.report_url, "created_at": row.created_at}


def expand_params(issue, message: str) -> str:
    """把问题里的 {{变量}} 按参数模板展开。

    模板里常见的是日期占位({{today}}/{{yesterday}}/{{month}}),让代码算比让模型推靠谱 ——
    模型推错日期很难发现:它照样会给你一份结构完整、数字却对不上的回答。
    没有模板就原样返回,不做任何猜测。
    """
    from datetime import date, timedelta

    text = (message or "").strip() or (issue.issue or "")
    tpl = dict(issue.param_template or {})
    today = date.today()
    builtin = {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "month": today.strftime("%Y-%m"),
        "last_month": (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
        "year": str(today.year),
    }
    for k, v in {**builtin, **tpl}.items():
        val = str(v)
        for name, repl in builtin.items():          # 模板值本身也可能带占位符
            val = val.replace("{{" + name + "}}", repl)
        text = text.replace("{{" + str(k) + "}}", val)
    return text


async def match_issue_by_text(db: AsyncSession, scenic_id: str | None, text: str):
    """按问题原文/标题反查问题行(关注问题这类只存了文本的入口用)。

    关注问题表里存的是当时那句话,没有问题 ID。点回去时若只发文本,
    一条配在 Quick BI 上的问题就会被本地 Agent 接走 —— 用户看到的是「回答失败」
    或者一段没查过数的客套话。这里按文本对回问题配置,对不上就算了(照常走自由提问)。
    """
    t = (text or "").strip()
    if not t:
        return None
    stmt = sa.select(ScenicAgentIssueDetail).where(
        ScenicAgentIssueDetail.active.is_(True),
        sa.or_(ScenicAgentIssueDetail.issue == t, ScenicAgentIssueDetail.title == t),
    )
    if scenic_id:
        stmt = stmt.where(sa.or_(ScenicAgentIssueDetail.scenic_id == scenic_id,
                                 ScenicAgentIssueDetail.scenic_id.is_(None)))
    # 本景区的优先于集团级的:同一句话两个景区都配过时,用自己那条(连接/数据都不一样)
    stmt = stmt.order_by(ScenicAgentIssueDetail.scenic_id.isnot(None).desc(),
                         ScenicAgentIssueDetail.id)
    return (await db.execute(stmt)).scalars().first()
