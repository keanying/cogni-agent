#!/usr/bin/env python
"""33 号:给技能补 tool_usage({tool_id: "什么时候用它"})——工具选择前置。

为什么补在这里而不是让模型自己挑(33 号周维度问题的根因):
    模型选工具的依据只有两处——工具 description 与技能的 tool_usage。客流类
    工具的 description 全是「数字大脑数据指标接口:XXX。请求参数:…」,**只列参数
    不写用途**;而 4 个客流技能的 tool_usage 全是空 {}。于是「本周哪几天客流最高」
    这类**多日**问题,模型面前摆着:
      · 分时报表汇总(travelDate 单日)—— 选它就只能查一天,想答多天得按天循环调
      · 每日报表趋势(startDate/endDate 范围)—— 一次拿回按天序列,才是正解
    没有说明,模型只能试错;选错就是多一轮工具调用 + 可能基于单日数据下结论。
    resolver.py 把这层合成进 system_prompt 的【工具使用说明】段,注释也写明:
    「加了工具却忘了写说明,模型不知道什么时候该用它」。

与参数补齐的关系(两者都是「进模型前准备好」):
    param_template 负责把**值**备齐(fill_param_templates.py 已覆盖 startDate/endDate);
    tool_usage 负责让模型**一步选对工具**——值齐了但选错工具,等于白备。

幂等:已有说明的 tool_id 一律不覆盖;工具名不在下表时跳过(留给人工,不猜文案)。

用法:
    ZHILV_ENV=dev .venv/bin/python scripts/fill_tool_usage.py [--apply] [--skill 客流]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from cogni_agent.common.db import get_sessionmaker  # noqa: E402
from cogni_agent.tool_center_service import versioning as ver  # noqa: E402

# 工具名 → 「什么时候用它」(写清何时用 / 何时不用 / 关键参数 / 返回粒度)
USAGE: dict[str, str] = {
    "每日报表趋势": (
        "多日·按天明细(本周哪几天客流最高、近7天每日走势、本月每日趋势)用它:"
        "一次传 startDate/endDate 直接取回按天序列,**不要按天循环调用**;"
        "查单个日期的分时分布不要用它。"
    ),
    "分时报表汇总": (
        "单个日期的各时段分布(今日哪个时段入园最多、今日各时段人流)用它:"
        "travelDate 只传一个日期;多天对比不要用它(改用每日报表趋势)。"
    ),
    "客流比对汇总": (
        "两个时间段的**整段汇总**对比(本周 vs 上周、今日 vs 昨日总量及变化率)用它:"
        "一次传 startTime/endTime/compareStartTime/compareEndTime;"
        "需要按天明细时改用每日报表趋势。"
    ),
    "今日客流统计": (
        "今日实时口径(在园人数、承载率、是否触达预警阈值)用它:"
        "只答今日实时,历史或多日趋势不要用它。"
    ),
    "今日报表汇总": "当日票务/收入汇总:只答当日口径,跨日对比不要用它。",
    # 比对类只有接口名/参数不同的两份(compareReportSummary / longRangeCompareReportSummary):
    # 短周期用前者,跨年/跨季度长周期用后者;两者都只给整段汇总,要按天明细用每日报表趋势。
    "报表比对汇总": (
        "两个时间段的**经营报表整段**对比(昨日 vs 前日、本月 vs 上月:订单数/票数/收入/核销/取消)用它:"
        "一次传 startTime/endTime/compareStartTime/compareEndTime;"
        "要按天趋势明细时改用每日报表趋势。"
    ),
    "长跨度报表比对汇总": (
        "**长周期**经营对比(今年 vs 去年、跨季度/跨年)用它:"
        "一次传 startTime/endTime/compareStartTime/compareEndTime;"
        "近日/本周这类短周期改用报表比对汇总。"
    ),
}


def _norm(name: str) -> str:
    """工具名归一化:「今日客流统计-导入2」「客流比对汇总-导入」这类重复导入的
    工具与原名指向同一接口(url 相同),去掉导入后缀后共用同一份文案。"""
    return re.sub(r"-导入\d*$", "", (name or "").strip())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库并发布(缺省只打印)")
    ap.add_argument("--skill", default="", help="只处理名称含该关键字的技能")
    args = ap.parse_args()
    async with get_sessionmaker()() as db:
        rows = (await db.execute(text(
            "SELECT id, name FROM numeri_core_tc_skills ORDER BY name"))).all()
        tnames = dict((await db.execute(text(
            "SELECT id, name FROM numeri_core_tc_tools"))).all())
        done = 0
        for rid, name in rows:
            if args.skill and args.skill not in name:
                continue
            pub = await ver.latest_published_version(db, "skill", rid)
            if pub is None:
                continue
            snap = dict(pub.snapshot or {})
            usage = dict(snap.get("tool_usage") or {})
            added: dict[str, str] = {}
            for t in (snap.get("tools") or []):
                tid = str(t.get("tool_id") or "")
                sug = USAGE.get(_norm(tnames.get(tid, "")))
                if tid and sug and not usage.get(tid):
                    added[tid] = sug
            if not added:
                continue
            print(f"\n技能: {name}")
            for tid, sug in added.items():
                print(f"    + {tnames.get(tid, tid)}: {sug}")
            if not args.apply:
                done += 1
                continue
            draft = await ver.latest_draft(db, "skill", rid)
            base = dict((draft.snapshot if draft else None) or snap)
            base["tool_usage"] = {**usage, **added}
            if draft is not None:
                await ver.update_version(db, draft.id, snapshot=base,
                                         note="补 tool_usage:工具选择前置(33号)")
                src = "草稿"
            else:
                draft = await ver.derive_version(
                    db, "skill", rid, base_version_id=pub.id, snapshot=base,
                    created_by="33号tool_usage补齐",
                    note="补 tool_usage:工具选择前置(33号)")
                src = f"v{pub.version}"
            await ver.publish_version(db, draft.id)
            print(f"    ✓ 已发布(基于{src})")
            done += 1
        print(f"\n{'待处理' if not args.apply else '已发布'}: {done} 个技能"
              + ("" if args.apply else " (加 --apply 执行)"))


if __name__ == "__main__":
    asyncio.run(main())
