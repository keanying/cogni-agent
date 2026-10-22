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

下游只调这几个函数,不再各自拼可见性条件。
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.errors import ValidationError
from cogni_agent.common.logging import get_logger
from cogni_agent.gateway_service.issue_models import (
    ScenicAgentIssueDetail,
)

log = get_logger("gateway_service.scenic_binding")

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


def assert_issue_askable(user, issue_row) -> None:
    """APP 上能不能问这条问题:与 APP 问题列表同一口径(本景区的 + 集团级的;公开的也行)。

    以前只凭 issue_id 就能发起:拿到别的景区的问题 id,就能问到别的景区的数据
    (走缓存时连权限都不过,直接回放别人的结果)。
    """
    from cogni_agent.common.errors import NotFound

    mine = getattr(user, "scenic_id", None) or None
    owner = getattr(issue_row, "scenic_id", None) or None
    if mine and owner and owner != mine and getattr(issue_row, "visibility", None) != "public":
        raise NotFound(f"问题不存在或无权访问:{getattr(issue_row, 'issue_id', '')}")


def ask_scenic_for(user, issue_row, requested: str | None = None) -> str | None:
    """用一条固定问题提问时,这一问算哪个景区。

    - 景区账号:就是它自己的景区。问题上挂的景区只说明「谁建的」,不能拿来当取数景区 ——
      以前取的是 issue.scenic_id:A 景区的公开问题被 B 景区的人点,要么报「无权指定景区 A」,
      要么(走缓存时)直接把 A 景区的结果回放给 B;
    - 集团账号:请求里指定的 > 问题挂的景区 > 集团级。
    """
    if getattr(user, "scenic_id", None):
        return effective_scenic(user, requested)
    return (requested or "").strip() or getattr(issue_row, "scenic_id", None) or None


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
