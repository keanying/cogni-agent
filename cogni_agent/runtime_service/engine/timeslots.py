# -*- coding: utf-8 -*-
"""从一句话里把**时间参数**算出来 —— 确定性的,不走 LLM。

为什么单独做这一层:现场那次提问是

    「近一日*(即昨天 2026-09-19)景区 PFTSCA01018088 的数据」

分诊判对了(数据查询 → we),也老老实实列出了缺失参数 `period, start, end, rows`,
然后 —— **没有任何工具被调用**,直接去生成文字,回了一句「本次回答与上一问内容相同,
如需要近一日的数据请告诉我,我将重新为您拉取」。

用户已经把日期说得不能再清楚了。缺的那几个参数本来就是能算出来的:
「昨天」= 2026-09-19,起止日期一减就有。让模型自己推日期是这套系统里最不该做的事 ——
它推错了不报错,照样给一份结构完整、数字对不上的回答(内置的 sys_date_* 工具就是为此存在的)。

所以这一层做的事很窄:**工具声明了哪些时间参数、槽里还缺哪些、能不能从这句话里算出来**。
算得出就填,算不出就不填(留给模型或澄清)。日期口径复用 builtin_tools 里那一套,
不在这里second写一遍 —— 两份日期实现迟早对不上。
"""
from __future__ import annotations

import re

# 工具里表示时间的参数名。只认这些,不做模糊匹配 ——
# 猜错一个参数名,填进去的值会被当成业务参数发出去。
START_KEYS = ("start", "start_date", "startDate", "begin", "begin_date", "from_date", "开始日期")
END_KEYS = ("end", "end_date", "endDate", "to_date", "结束日期")
DATE_KEYS = ("date", "day", "stat_date", "statDate", "日期")
PERIOD_KEYS = ("period", "date_range", "dateRange", "time_range", "timeRange", "周期")

TIME_KEYS = set(START_KEYS) | set(END_KEYS) | set(DATE_KEYS) | set(PERIOD_KEYS)

# 一句话里的时间说法 → builtin sys_date_range 认得的 period。
# 顺序有意义:长的先匹配(「近一日」要先于「近一」这种子串)。
_PHRASES: list[tuple[str, str]] = [
    ("前天", "前天"),
    ("昨日", "昨天"), ("昨天", "昨天"), ("近一日", "昨天"), ("最近一日", "昨天"), ("近1日", "昨天"),
    ("今日", "今天"), ("今天", "今天"), ("当日", "今天"),
    ("本周", "本周"), ("这周", "本周"), ("上周", "上周"),
    ("本月", "本月"), ("这个月", "本月"), ("当月", "本月"), ("上月", "上月"), ("上个月", "上月"),
    ("今年", "今年"), ("本年", "今年"), ("去年", "去年"),
]
# 「近 7 天 / 最近30日 / 过去 90 天」
_RECENT_N = re.compile(r"(?:近|最近|过去)\s*(\d{1,3})\s*[天日]")
# 「2026-09-19」「2026/09/19」「2026年9月19日」
_YMD = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")


def _iso(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def detect_period(text: str) -> str | None:
    """从一句话里认出时间说法,返回 sys_date_range 认得的 period;认不出返回 None。"""
    t = text or ""
    m = _RECENT_N.search(t)
    if m:
        n = int(m.group(1))
        # 只认 sys_date_range 定死口径的那几档;其余交回 None,不自己发明口径
        return {7: "近7天", 30: "近30天", 90: "近90天"}.get(n) or (
            "昨天" if n == 1 else None)
    for phrase, period in _PHRASES:
        if phrase in t:
            return period
    return None


def resolve(goal_text: str, wanted: set[str]) -> dict:
    """把这句话里的时间算成参数值。

    wanted:工具声明了、而槽里还缺的参数名。只算这里面要的,不多填 ——
    往调用里塞一个工具没声明的参数,轻则被忽略,重则接口报「未知字段」。

    **句子里的显式日期优先于相对说法**:用户写「近一日(即昨天 2026-09-19)」时,
    以他写出来的那个日期为准 —— 相对说法算出来的是「运行当天的昨天」,
    跨过零点、或者任务排队了一会儿,两者就不是同一天。
    """
    from cogni_agent.tool_center_service.builtin_tools import builtin_date_range

    text = goal_text or ""
    out: dict = {}
    if not wanted:
        return out

    explicit = [_iso(*m) for m in _YMD.findall(text)]
    period = detect_period(text)
    if not explicit and not period:
        return out

    if explicit:
        start, end = explicit[0], explicit[-1]
    else:
        r = builtin_date_range(period=period)
        start, end = r["start"], r["end"]

    for k in wanted:
        if k in START_KEYS:
            out[k] = start
        elif k in END_KEYS:
            out[k] = end
        elif k in DATE_KEYS:
            # 单日参数:区间退化成一天时给 start,跨天的不猜(猜哪一天都可能是错的)
            out[k] = start if start == end else out.get(k, start)
        elif k in PERIOD_KEYS and period:
            out[k] = period
    return out


def missing_time_slots(tool_schemas: list[dict], slots: dict) -> set[str]:
    """这些工具声明了、而槽里还缺的时间参数。

    只看工具**自己声明**的参数名 —— 不维护一张「业务上可能叫什么」的清单,
    那种清单是工具配置的影子复制,加一个工具忘了同步就静默失效。
    """
    wanted: set[str] = set()
    for schema in tool_schemas:
        for key in ((schema or {}).get("properties") or {}):
            if key in TIME_KEYS and slots.get(key) in (None, ""):
                wanted.add(key)
    return wanted
