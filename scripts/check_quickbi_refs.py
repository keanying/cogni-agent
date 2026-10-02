# -*- coding: utf-8 -*-
"""Quick BI 连接引用体检 / 修复。

**为什么需要它**:连接 id 是裸存在六张表里的字符串(问题、资产、文件、会话、运行记录、分配),
没有外键级联。连接一旦被删,这些行照样存着那串 id,而且不报错、不失效 ——
直到有人去点那条问题,才炸一句「Quick BI 连接不存在:<一串 id>」。
那串 id 这时候已经查不到了(行删了),运营手上只有这一句话,只能来找开发。

**在项目根目录跑**(有 pyproject.toml 的那一层),不是在 web/ 下面。
连哪个库跟着 ZHILV_ENV 走(未设置读 .env,设了读 .env.{那个值})——
和你启动服务时用的**必须是同一个**,否则体检的是另一个库,而且一声不响。
脚本开头会把实际连上的库打出来,对一眼再往下看。

哪些表**影响发起**、哪些只是留痕:

    问题 / 主题解读   影响。点了就报错
    数据资产          影响。**报告/解读的目标数据就是它** —— 资产绑着连接,连接没了,
                      发起时报「资产「x」属于另一个 Quick BI 连接(…)」,而那条连接已经不存在,
                      提示让你「切换到该连接」也切不过去
    上传文件          影响。同资产
    会话              影响。**接着聊就是一次新发起**,用的是会话行上粘着的那条连接 ——
                      打开一条老会话继续问就报错,而同样的问题新开一个会话反而正常
    运行记录          留痕。不影响新发起;但旧报告刷新会炸 ——
                      主题解读卡片会把历史那次拉出来刷新,于是一进去就报错
    分配记录          无用垃圾,删掉即可

用法:
    # 体检
    uv run python scripts/check_quickbi_refs.py

    # 看某一条连接到底怎么了、谁在指着它
    uv run python scripts/check_quickbi_refs.py --id 290987f7-30d8-4e80-aa8d-c93103969571

    # 修(常用):把问题 / 资产 / 文件全部改绑到那条还活着的连接
    uv run python scripts/check_quickbi_refs.py --fix --to <好连接的 id>

    # 只把**问题**的连接清空(之后按景区自动重挑)。资产/文件的连接不可为空,必须 --to
    uv run python scripts/check_quickbi_refs.py --fix --clear
"""
from __future__ import annotations

import argparse
import asyncio

import sqlalchemy as sa


async def main() -> int:
    ap = argparse.ArgumentParser(description="Quick BI 连接引用体检 / 修复")
    ap.add_argument("--id", help="只看这一条连接 id")
    ap.add_argument("--fix", action="store_true", help="真的改;不加只看不改")
    ap.add_argument("--to", help="把坏引用改绑到这条连接 id")
    ap.add_argument("--clear", action="store_true", help="把坏引用的连接清空(之后按景区自动重挑)")
    ap.add_argument("--assets", action="store_true",
                    help="只列资产清单:每块板/每个集分别属于哪条连接、哪个景区、是否可用")
    args = ap.parse_args()

    from cogni_agent.common import schema as _models  # noqa: F401  注册全部模型
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.gateway_service.issue_models import ScenicAgentIssueDetail
    from cogni_agent.tool_center_service.quickbi.models import (
        QbiAsset, QbiConnection, QbiFile, QbiGrant, QbiRun, QbiSession,
    )

    # 「问题」放在最前面:只有它会让人点了就报错,其余几张是历史留痕
    tables = [("问题 / 主题解读", ScenicAgentIssueDetail), ("数据资产", QbiAsset),
              ("上传文件", QbiFile), ("会话", QbiSession), ("运行记录", QbiRun),
              ("分配记录", QbiGrant)]

    # 先把「我到底连的是哪个库」打出来。不打的话,ZHILV_ENV 设错时脚本会一本正经地
    # 报告「引用是干净的」—— 因为它体检的根本是另一个库,而这比报错更难发现。
    import os

    from cogni_agent.config import get_settings

    dbcfg = get_settings().get("database", default={})
    print(f"ZHILV_ENV={os.environ.get('ZHILV_ENV') or '(未设置,读 .env)'}  →  "
          f"{dbcfg.get('user')}@{dbcfg.get('host')}:{dbcfg.get('port')}/{dbcfg.get('db')}")
    print("(和启动服务时用的不是同一个库的话,下面的结论都不作数)\n")

    async with get_sessionmaker()() as db:
        conns = {c.id: c for c in (await db.execute(sa.select(QbiConnection))).scalars()}
        print(f"平台上现有 Quick BI 连接 {len(conns)} 条:")
        for c in conns.values():
            print(f"  {c.id}  {c.name:20s} 景区={c.scenic_id or '(集团级)':18s} "
                  f"可见={c.visibility:8s} {'启用' if c.active else '**已停用**'}")
        print()

        if args.assets:
            # 「为什么操练台下拉里少了一块板」几乎都能在这张表上看出来:
            # 要么它属于另一条连接,要么归属景区对不上,要么已被停用
            from cogni_agent.tool_center_service.quickbi.models import QbiAsset as _A

            rows = (await db.execute(sa.select(_A).order_by(_A.kind.desc(), _A.name))).scalars().all()
            print(f"已登记资产 {len(rows)} 个(板在前):\n")
            for a in rows:
                cn = conns.get(a.connection_id)
                print(f"  {'板' if a.kind == 'dashboard' else '集'} {a.name[:30]:32s} "
                      f"连接={(cn.name if cn else '**已删除**'):14s} "
                      f"景区={a.scenic_id or '(集团级)':18s} "
                      f"{'可用' if getattr(a, 'active', True) else '**已停用**'}")
            return 0

        if args.id:
            row = conns.get(args.id)
            print(f"连接 {args.id}:{'**数据库里没有这一行(已被删除)**' if row is None else row.name}")
            print()

        # 逐表找「指着一个不存在 / 已停用连接」的行
        bad: dict[str, list] = {}
        for label, model in tables:
            stmt = sa.select(model).where(model.connection_id.isnot(None))
            if args.id:
                stmt = stmt.where(model.connection_id == args.id)
            rows = (await db.execute(stmt)).scalars().all()
            hits = [r for r in rows
                    if r.connection_id and (r.connection_id not in conns
                                            or not conns[r.connection_id].active)]
            if hits:
                bad[label] = hits

        if not bad:
            print("没有发现指向「已删除 / 已停用」连接的行 —— 引用是干净的。")
            return 0

        for label, hits in bad.items():
            print(f"【{label}】{len(hits)} 条指向已删除/已停用的连接:")
            for r in hits[:30]:
                why = "连接已删除" if r.connection_id not in conns else "连接已停用"
                title = getattr(r, "title", None) or getattr(r, "issue", None) or getattr(r, "name", "") or r.id
                print(f"  {r.id}  {str(title)[:28]:30s} → {r.connection_id}  ({why})")
            if len(hits) > 30:
                print(f"  …… 另有 {len(hits) - 30} 条")
            print()

        # 「影响发起」和「只是留痕」要分开说 —— 混在一起的话,
        # 24 条资产会被当成历史数据忽略掉,而它恰恰是报告/解读的目标数据
        blocking = {k: v for k, v in bad.items()
                    if k in ("问题 / 主题解读", "数据资产", "上传文件", "会话")}
        history = {k: v for k, v in bad.items() if k not in blocking}

        if history:
            print("以下只是历史留痕,不影响**新**发起:"
                  + "、".join(f"{k} {len(v)} 条" for k, v in history.items()))
            if "运行记录" in history:
                print("  注意:主题解读卡片会把历史那次拉出来刷新,所以旧报告点进去仍会报错 ——")
                print("  v3.23.0 起这种情况会直接回显当时存下来的结果,不再报错。")
            print()

        if not blocking:
            print("影响发起的三类(问题/资产/文件)都是干净的。")
            return 0

        print("以下会让发起直接失败:"
              + "、".join(f"{k} {len(v)} 条" for k, v in blocking.items()))
        # 挑一条还能用的连接建议给人,省得他自己去翻 id
        alive = [c for c in conns.values() if c.active]
        suggest = alive[0].id if len(alive) == 1 else None

        if not args.fix:
            tip = f" --to {suggest}" if suggest else " --to <上面那张表里的连接 id>"
            print(f"\n修复:uv run python scripts/check_quickbi_refs.py --fix{tip}")
            return 1

        if args.to:
            if args.to not in conns:
                print(f"--to 指定的连接 {args.to} 不存在,放弃")
                return 2
            target = args.to
        elif args.clear:
            target = None
        else:
            print("--fix 需要配合 --to <连接id>(推荐)或 --clear")
            return 2

        total = 0
        # 资产/文件的 connection_id 不可为空(它就是「这份数据属于哪套凭证」),
        # 所以 --clear 对它们无效 —— 只能改绑
        for label, model in (("问题 / 主题解读", ScenicAgentIssueDetail),
                             ("数据资产", QbiAsset), ("上传文件", QbiFile),
                             ("会话", QbiSession)):
            hits = blocking.get(label)
            if not hits:
                continue
            if target is None and model not in (ScenicAgentIssueDetail, QbiSession):
                print(f"跳过【{label}】{len(hits)} 条:它们的连接不能为空,请改用 --to <连接id>")
                continue
            await db.execute(sa.update(model).where(model.id.in_([r.id for r in hits]))
                             .values(connection_id=target))
            total += len(hits)
            print(f"已把【{label}】{len(hits)} 条"
                  + (f"改绑到 {target}" if target else "的连接清空(发起时按景区自动重挑)"))

        # 指向已删除连接的分配记录是纯垃圾:留着只会让白名单判定认为这条连接「已启用白名单」
        stale_grants = bad.get("分配记录") or []
        if stale_grants:
            await db.execute(sa.delete(QbiGrant).where(
                QbiGrant.id.in_([g.id for g in stale_grants])))
            print(f"已清理 {len(stale_grants)} 条指向已删除连接的分配记录")

        await db.commit()
        print(f"\n共修复 {total} 条。重启服务或直接刷新页面即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
