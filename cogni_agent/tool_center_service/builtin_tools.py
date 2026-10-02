# -*- coding: utf-8 -*-
"""系统内置工具:不需要运维配置、开箱就有的那几个。

**为什么要有内置工具**:有些能力每个景区都要、配法完全一样,却因为"没人配"而缺席 ——
最典型的是时间。模型自己推日期推错了很难发现:它照样给你一份结构完整、
数字却对不上的回答。与其在每条问题的口径里叮嘱"今天是几号",不如给它一个能问的工具。

这里的工具与 tc_tools 里那些**不同**:它们不落库、不走沙箱、没有凭证,
就是一段本地函数。注册在 BUILTIN 里,解析 Skill 时按名字挂进去。

目前三类:
  时间类   now / date_shift / date_range / align_period —— 让代码算日期,不让模型推
  数据类   table_query —— 对上一步的大结果做筛选/分组/统计(pandas),而不是让模型肉眼找
  降级类   quickbi_link —— Quick BI 跑不通时,至少把看板地址给出来
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

from cogni_agent.common.logging import get_logger
from cogni_agent.common.time import now_cst

log = get_logger("tool_center_service.builtin")

# 周期名 → 起止(相对今天)。中文口语与英文都认,模型两种都可能说
_PERIODS = {
    "today": 0, "今日": 0, "今天": 0, "当日": 0, "本日": 0,
    "yesterday": 1, "昨日": 1, "昨天": 1, "前一天": 1,
    "前天": 2, "前日": 2, "day_before_yesterday": 2,
}


def _today() -> dt.date:
    return now_cst().date()


# ── 法定节假日 ────────────────────────────────────────────────────────────
#
# **这是一张表,不是一套算法 —— 这一点是刻意的。**
# 春节、端午、中秋按农历走,清明按天文节气定,调休安排每年由国务院单独发文,
# 三者都**推不出来**。而「猜一个日期」在这里是最坏的结果:模型会拿着这个日期去取数,
# 取回来的是一段风平浪静的普通日子,然后一本正经地写「今年五一客流同比下滑」——
# 报告结构完整、数字也真实,只是那几天根本不是五一。没人会回头去核对日期。
#
# 所以表里没有的年份一律明说「不知道」,让它回去问人,而不是替它编一个。
# 每年末需要补一行(国务院办公厅发文之后),补的时候连**调休后的实际放假区间**一起写,
# 而不是只写节日当天 —— 运营口中的「五一」指的是那段假期,不是 5 月 1 日一天。
_HOLIDAYS: dict[int, dict[str, tuple[str, str]]] = {
    2025: {
        "元旦": ("2025-01-01", "2025-01-01"),
        "春节": ("2025-01-28", "2025-02-04"),
        "清明": ("2025-04-04", "2025-04-06"),
        "劳动节": ("2025-05-01", "2025-05-05"),
        "端午": ("2025-05-31", "2025-06-02"),
        "国庆": ("2025-10-01", "2025-10-08"),   # 与中秋连休
        "中秋": ("2025-10-06", "2025-10-08"),
    },
    2026: {
        "元旦": ("2026-01-01", "2026-01-03"),
        "春节": ("2026-02-15", "2026-02-23"),
        "清明": ("2026-04-04", "2026-04-06"),
        "劳动节": ("2026-05-01", "2026-05-05"),
        "端午": ("2026-06-19", "2026-06-21"),
        "中秋": ("2026-09-25", "2026-09-27"),
        "国庆": ("2026-10-01", "2026-10-08"),
    },
}

# 口语叫法 → 表里的键。运营嘴里说的是「五一」「十一」「过年」,不是「劳动节」。
_HOLIDAY_ALIASES = {
    "五一": "劳动节", "51": "劳动节", "五一假期": "劳动节", "劳动节": "劳动节", "五一节": "劳动节",
    "十一": "国庆", "国庆": "国庆", "国庆节": "国庆", "黄金周": "国庆",
    "春节": "春节", "过年": "春节", "年节": "春节", "新春": "春节",
    "元旦": "元旦", "新年": "元旦",
    "清明": "清明", "清明节": "清明",
    "端午": "端午", "端午节": "端午", "龙舟节": "端午",
    "中秋": "中秋", "中秋节": "中秋",
}


def _holiday_key(text: str) -> str | None:
    """从一句周期名里认出节日。认不出返回 None —— 认不出就是认不出,不猜。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in _HOLIDAY_ALIASES:
        return _HOLIDAY_ALIASES[t]
    # 「今年五一」「去年国庆」这类带前缀的说法
    for alias, key in _HOLIDAY_ALIASES.items():
        if alias in t:
            return key
    return None


def _holiday_year(text: str, base: dt.date) -> int:
    """「去年春节」里的「去年」。说不清就按今年算,但今年那个已经过完很久时取下一年没有意义,
    所以只认明确的前缀,不做「离得最近」这类推断 —— 推断出来的年份同样没人会核对。"""
    t = (text or "")
    if "去年" in t or "上年" in t:
        return base.year - 1
    if "明年" in t or "下一年" in t:
        return base.year + 1
    if "前年" in t:
        return base.year - 2
    m = re.search(r"(19|20)\d{2}", t)
    if m:
        # 原先这里是「在 base.year±几年里找一个能对上的字符串」,范围外的年份(比如 2030)
        # 一个都对不上,于是**静默退回今年** —— 问「2030 年春节」拿到的是今年春节的日期,
        # 而返回里写着的年份也是今年,谁也看不出被换掉了。写清楚的年份就按它算,
        # 算不出来由上面那层明说「不知道这一年」。
        return int(m.group(0))
    return base.year


def _quarter_range(y: int, q: int) -> tuple[dt.date, dt.date]:
    m = (q - 1) * 3 + 1
    start = dt.date(y, m, 1)
    end_m = m + 2
    return start, dt.date(y, end_m, _days_in_month(y, end_m))


def builtin_now(**_: Any) -> dict:
    """现在几点、今天几号、周几、本月第几天。

    模型不知道"今天"是哪天 —— 它只知道训练时的世界。不给它这个,
    它要么问用户,要么按训练时的日期算,后者是错的且不报错。
    """
    n = now_cst()
    d = n.date()
    return {
        "datetime": n.isoformat(), "date": d.isoformat(),
        "year": d.year, "month": d.month, "day": d.day,
        "weekday": d.isoweekday(), "weekday_cn": "一二三四五六日"[d.isoweekday() - 1],
        "month_start": d.replace(day=1).isoformat(),
        "timezone": "Asia/Shanghai(CST,+08:00)",
        "note": "本系统所有业务日期都按这个时区算,不要用 UTC 推算",
    }


def builtin_date_shift(date: str = "", days: int = 0, months: int = 0, **_: Any) -> dict:
    """把一个日期前后挪几天/几个月。挪月时自动处理月末(1月31日 + 1月 = 2月28/29日)。"""
    base = _parse_date(date)
    out = base + dt.timedelta(days=int(days or 0))
    m = int(months or 0)
    if m:
        y, mo = out.year, out.month + m
        y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
        # 月末对齐:3月31日往前一个月不是「2月31日」
        last = _days_in_month(y, mo)
        out = dt.date(y, mo, min(out.day, last))
    return {"date": out.isoformat(), "from": base.isoformat(), "days": days, "months": months}


def builtin_date_range(period: str = "today", end: str = "", **_: Any) -> dict:
    """把「今天 / 昨天 / 近7天 / 本周 / 本月 / 上月 / 今年」这类说法换成起止日期。

    这是最该由代码算的东西:「近 7 天」到底含不含今天、「本周」从周一还是周日起,
    各人理解不同,模型也不稳定。这里定死一套并把口径写在返回里,让它照着用。
    """
    end_d = _parse_date(end)
    p = (period or "today").strip().lower()

    def r(s: dt.date, e: dt.date, note: str) -> dict:
        return {"start": s.isoformat(), "end": e.isoformat(),
                "days": (e - s).days + 1, "period": period, "note": note}

    if p in _PERIODS:
        d = end_d - dt.timedelta(days=_PERIODS[p])
        return r(d, d, "单日")
    for key, n in (("近7天", 7), ("last_7_days", 7), ("近30天", 30), ("last_30_days", 30),
                   ("近90天", 90), ("last_90_days", 90)):
        if p == key.lower():
            return r(end_d - dt.timedelta(days=n - 1), end_d, f"含今天在内的 {n} 天")
    if p in ("本周", "this_week", "week_to_date", "这周", "本星期"):
        return r(end_d - dt.timedelta(days=end_d.isoweekday() - 1), end_d, "周一为一周之始,至今天")
    if p in ("上周", "last_week", "上个星期", "上一周"):
        this_mon = end_d - dt.timedelta(days=end_d.isoweekday() - 1)
        return r(this_mon - dt.timedelta(days=7), this_mon - dt.timedelta(days=1), "上一个完整周(周一至周日)")
    if p in ("本月", "this_month", "month_to_date", "这个月", "本月份", "当月"):
        return r(end_d.replace(day=1), end_d, "本月 1 号至今天")
    if p in ("上月", "last_month", "上个月", "上一个月", "上一月"):
        first = end_d.replace(day=1)
        prev_end = first - dt.timedelta(days=1)
        return r(prev_end.replace(day=1), prev_end, "上一个完整自然月")
    if p in ("今年", "this_year", "year_to_date", "本年", "本年度"):
        return r(end_d.replace(month=1, day=1), end_d, "1 月 1 日至今天")
    if p in ("去年", "last_year", "上年"):
        y = end_d.year - 1
        return r(dt.date(y, 1, 1), dt.date(y, 12, 31), f"{y} 全年")

    # ── 季度 ──
    # 运营嘴里说的是「三季度」不是「3季度」,中文数字先换成阿拉伯数字再匹配
    for _cn, _ar in (("一", "1"), ("二", "2"), ("三", "3"), ("四", "4")):
        if p.startswith(_cn) or p.startswith("第" + _cn):
            p = p.replace(_cn, _ar, 1)
            break
    cur_q = (end_d.month - 1) // 3 + 1
    if p in ("本季度", "this_quarter", "本季", "quarter_to_date"):
        qs, _ = _quarter_range(end_d.year, cur_q)
        return r(qs, end_d, f"{end_d.year} 年 Q{cur_q} 季初至今天")
    if p in ("上季度", "last_quarter", "上一季度", "上季"):
        y, q = (end_d.year - 1, 4) if cur_q == 1 else (end_d.year, cur_q - 1)
        qs, qe = _quarter_range(y, q)
        return r(qs, qe, f"上一个完整季度({y} 年 Q{q})")
    if p in ("去年同季度", "去年同期季度", "last_year_quarter"):
        qs, qe = _quarter_range(end_d.year - 1, cur_q)
        return r(qs, qe, f"去年同一个季度({end_d.year - 1} 年 Q{cur_q})")
    # 「2026Q1」「2026年第1季度」「Q3」「三季度」都认。两种写法分开写:
    # 带 q/第 前缀时后缀可省(Q1),不带前缀时后缀必须有(否则「3」会被当成季度)。
    _q = (re.match(r"^(?:(\d{4})\s*年?)?\s*(?:q|第)\s*([1-4])\s*(?:季度|季)?$", p)
          or re.match(r"^(?:(\d{4})\s*年?)?\s*([1-4])\s*(?:季度|季)$", p))
    if _q:
        y = int(_q.group(1)) if _q.group(1) else end_d.year
        qs, qe = _quarter_range(y, int(_q.group(2)))
        return r(qs, qe, f"{y} 年 Q{_q.group(2)} 整季")

    # ── 「近 N 天」的通用写法(上面只列了 7/30/90) ──
    _n = re.match(r"^(?:近|最近|过去|last)\s*(\d{1,4})\s*(?:天|日|days?)$", p)
    if _n:
        n = max(1, min(int(_n.group(1)), 3650))
        return r(end_d - dt.timedelta(days=n - 1), end_d, f"含今天在内的 {n} 天")

    # ── 节假日 ──
    #
    # 表里没有的年份**明确说不知道**,不按单日兜底。兜底成单日是这里最危险的做法:
    # 模型会拿着「今天」去取数,取回一段普通日子的数,然后写「今年五一客流同比下滑」——
    # 报告结构完整、数字也真实,只是那几天根本不是五一,而没人会回头核对日期。
    hk = _holiday_key(period)
    if hk:
        hy = _holiday_year(period, end_d)
        table = _HOLIDAYS.get(hy) or {}
        span = table.get(hk)
        if span:
            return {"start": span[0], "end": span[1],
                    "days": (_parse_date(span[1]) - _parse_date(span[0])).days + 1,
                    "period": period, "holiday": hk, "year": hy,
                    "note": f"{hy} 年{hk}法定假期(含调休,{span[0]} 至 {span[1]})"}
        return {"error": f"系统里没有 {hy} 年「{hk}」的放假安排,无法换算成日期。"
                         f"这类节日按农历/节气走、调休每年单独发文,**推不出来也不能猜**——"
                         f"请直接给出具体起止日期,或改问已知年份({sorted(_HOLIDAYS)})。",
                "period": period, "holiday": hk, "year": hy,
                "known_years": sorted(_HOLIDAYS)}

    return r(end_d, end_d, f"认不出周期「{period}」,按单日处理;"
                           f"可用:今日/昨日/近N天/本周/上周/本月/上月/本季度/上季度/"
                           f"今年/去年,以及节假日(五一/国庆/春节/元旦/清明/端午/中秋)")


def builtin_align_period(start: str, end: str, mode: str = "同比", **_: Any) -> dict:
    """给一个时间段,算出它的对比区间(同比 / 环比)。

    同比 = 去年同期(天数一致);环比 = 紧挨着的上一段(等长)。
    这两个词现场天天说,但算错了极难发现 —— 报告照样成篇,只是对比的基准是错的。
    """
    s, e = _parse_date(start), _parse_date(end)
    if s > e:
        s, e = e, s
    span = (e - s).days
    if str(mode).strip() in ("同比", "yoy", "year_over_year"):
        ps, pe = _shift_year(s), _shift_year(e)
        note = "去年同期(同一段日期往前挪一年)"
    else:
        pe = s - dt.timedelta(days=1)
        ps = pe - dt.timedelta(days=span)
        note = "上一个等长区间(紧挨着当前区间之前)"
    return {"current": {"start": s.isoformat(), "end": e.isoformat(), "days": span + 1},
            "compare": {"start": ps.isoformat(), "end": pe.isoformat(), "days": span + 1},
            "mode": mode, "note": note}


def builtin_table_query(rows: list | None = None, filter: str = "", group_by: str = "",
                        agg: str = "", sort_by: str = "", top: int = 0, dataset: str = "",
                        _task_id: str | None = None, **_: Any) -> dict:
    """对一批行做筛选 / 分组 / 统计(pandas),再把**结果**给模型。

    为什么要有这个:上一步的工具可能返回上千行。让模型在对话里肉眼找最大值、
    算占比、挑 TOP10,既慢又容易错,而且那上千行本身就装不进上下文。
    把这件事交给 pandas —— 它算得对,而且只把答案回给模型。

    dataset   服务端留存的数据句柄(ds_1 这种,取数工具的摘要末尾会给)。
              **上千行的数据走这个** —— 那种数据压根没进过上下文,模型手里只有摘要,
              传不出 rows 来。给了句柄就从服务端取整份数据算。
    rows      模型直接传进来的行(数据本来就不多时用这个)
    filter    pandas query 语法,如 `amount > 1000 and channel == "携程"`
    group_by  逗号分隔的列名
    agg       如 `amount:sum,orders:sum` 或 `amount:mean`
    sort_by   列名,前面加 `-` 表示倒序
    top       只要前 N 行(0 = 不限,但最多回 200 行)
    """
    import pandas as pd

    from cogni_agent.runtime_service.engine import databuf

    got, err = databuf.resolve_rows(_task_id, dataset or "", rows)
    if err:
        return {"error": err}
    if not got:
        return {"error": "数据为空(0 行),没有可统计的内容"}
    rows = got
    df = pd.DataFrame(rows)
    steps: list[str] = [f"原始 {len(df)} 行"]
    try:
        if filter:
            df = df.query(filter)
            steps.append(f"筛选 `{filter}` 后 {len(df)} 行")
        if group_by:
            keys = [k.strip() for k in group_by.split(",") if k.strip()]
            spec: dict[str, Any] = {}
            for item in (agg or "").split(","):
                if ":" in item:
                    col, how = item.split(":", 1)
                    spec[col.strip()] = how.strip()
            df = (df.groupby(keys).agg(spec).reset_index() if spec
                  else df.groupby(keys).size().reset_index(name="count"))
            steps.append(f"按 {keys} 分组后 {len(df)} 组")
        if sort_by:
            col, asc = (sort_by[1:], False) if sort_by.startswith("-") else (sort_by, True)
            df = df.sort_values(col, ascending=asc)
            steps.append(f"按 {col} {'降序' if not asc else '升序'}")
        n = int(top or 0)
        limit = min(n, 200) if n > 0 else 200
        truncated = len(df) > limit
        df = df.head(limit)
    except Exception as exc:                    # noqa: BLE001 - 语法错误要说清楚,别当成"没数据"
        return {"error": f"处理失败:{type(exc).__name__}: {exc}",
                "hint": "filter 用 pandas query 语法(列名不加引号,字符串值加引号);"
                        "agg 形如 amount:sum,orders:sum",
                "columns": list(pd.DataFrame(rows).columns)}
    import json as _json
    return {"steps": steps, "row_count": len(df),
            "truncated": truncated and "结果超过 200 行,只回了前 200 行" or "",
            "rows": _json.loads(df.to_json(orient="records", force_ascii=False))}


# ── 数据处理:pandas / numpy ─────────────────────────────────────────
#
# 这一组共用两条约定:
#
# ① **取数口统一**:每个工具都既收 dataset(服务端留存的数据句柄)又收 rows
#    (模型直接传的行)。大结果压缩时明细会留在 databuf 里,摘要末尾告诉模型句柄号 ——
#    所以真正上千行的那种数据,模型是**拿句柄**来算的,一行都不进上下文。
#
# ② **错误要说清是哪种错**。最怕的不是报错,是把「列名写错了」「数据集过期了」
#    含糊成一句「没有数据」—— 模型收到这句会直接回「本期无数据」,
#    而真实情况是数据好好的,只是它写错了一个列名。所以失败时一律带上可用列名 / 可用句柄。


def _fmt(v: Any) -> Any:
    """数值保留两位,其余原样。

    不做这一步的话,pandas 算出来的是 1203559.9999999998 这种 —— 模型会把它
    原样写进结论,读的人只会觉得这系统不太对劲。
    """
    if isinstance(v, float):
        return round(v, 2)
    return v


def _load(task_id, dataset, rows):
    """统一取数 + 转 DataFrame。返回 (df, 错误说明)。"""
    try:
        import pandas as pd
    except ImportError:                     # pragma: no cover - 取决于环境装没装
        # 裸 ImportError 抛给模型的话,它看到的是一句 Python 报错,多半会当成
        # 「这个工具坏了」然后改用心算 —— 而心算的结果没人会去验算。说清楚。
        return None, ("数据统计工具不可用:当前环境没有安装 pandas。"
                      "请联系运维执行安装后重试;**不要自己心算代替**")

    from cogni_agent.runtime_service.engine import databuf

    got, err = databuf.resolve_rows(task_id, dataset or "", rows)
    if err:
        return None, err
    if not got:
        return None, "数据为空(0 行),没有可统计的内容"
    return pd.DataFrame(got), ""


def _cols_hint(df) -> str:
    return "可用的列:" + "、".join(str(c) for c in df.columns)


def _rows_out(df, limit: int = 200) -> dict:
    """DataFrame → 回给模型的行。统一封顶,免得一个工具把上下文吃光。"""
    import json as _json

    total = len(df)
    cut = df.head(limit)
    out = {"row_count": int(total), "rows": _json.loads(cut.to_json(orient="records", force_ascii=False))}
    if total > limit:
        out["truncated"] = f"共 {total} 行,只回了前 {limit} 行;要看别的段请加筛选或排序条件"
    return out


def builtin_table_stats(dataset: str = "", rows: list | None = None, columns: str = "",
                        _task_id: str | None = None, **_: Any) -> dict:
    """一份数据的整体画像:每列多少行、多少空值、数值列的合计/均值/中位数/分位数/极值。

    为什么要单独有它:拿到一份陌生数据时,人会先看"这数据长什么样"。
    模型没有这一步就直接下结论 —— 它会抓着样本行里的几个数字当全局特征,
    比如样本里恰好都是携程,就说"渠道以携程为主"。
    """
    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    import pandas as pd

    want = [c.strip() for c in (columns or "").split(",") if c.strip()]
    if want:
        missing = [c for c in want if c not in df.columns]
        if missing:
            return {"error": f"这些列不存在:{'、'.join(missing)}。{_cols_hint(df)}"}
        df = df[want]
    out: list[dict] = []
    for col in df.columns:
        s = df[col]
        info: dict[str, Any] = {"column": str(col), "count": int(s.notna().sum()),
                                "null_count": int(s.isna().sum())}
        num = pd.to_numeric(s.dropna(), errors="coerce")
        if len(num.dropna()) and num.notna().mean() > 0.9:
            n = num.dropna()
            info.update(kind="number", sum=_fmt(float(n.sum())), mean=_fmt(float(n.mean())),
                        median=_fmt(float(n.median())), std=_fmt(float(n.std())) if len(n) > 1 else 0,
                        min=_fmt(float(n.min())), max=_fmt(float(n.max())),
                        p25=_fmt(float(n.quantile(.25))), p75=_fmt(float(n.quantile(.75))))
        else:
            as_str = s.dropna().astype(str)
            vc = as_str.value_counts().head(5)
            info.update(kind="category", unique=int(as_str.nunique()),
                        top=[{"value": _fmt(k), "count": int(v)} for k, v in vc.items()])
        out.append(info)
    return {"row_count": int(len(df)), "columns": out}


def builtin_table_compare(dataset: str = "", rows: list | None = None,
                          dataset_b: str = "", rows_b: list | None = None,
                          key: str = "", value: str = "",
                          _task_id: str | None = None, **_: Any) -> dict:
    """两期数据按 key 对齐,算差值与增长率。同比环比就用它。

    为什么不让模型自己减:两份数据的行序往往不一样,还会各自缺几个类别。
    模型按位置两两相减就会**错位** —— 把携程的本期减去美团的上期,
    算出来的增长率照样是个像模像样的百分数,没人看得出错。
    这里按 key 对齐,并把只在一期出现的类别单独列出来(新增 / 消失,那恰恰是该说的事)。
    """
    if not key or not value:
        return {"error": "必须指定 key(对齐用的类别列,如 渠道/日期)和 value(要比较的数值列,如 销售额)"}
    a, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": f"本期数据取不到:{err}"}
    b, err = _load(_task_id, dataset_b, rows_b)
    if err:
        return {"error": f"对比期数据取不到:{err}"}
    import pandas as pd

    for name, df in (("本期", a), ("对比期", b)):
        for c in (key, value):
            if c not in df.columns:
                return {"error": f"{name}数据里没有列「{c}」。{_cols_hint(df)}"}
    ga = a.groupby(key)[value].apply(lambda s: pd.to_numeric(s, errors="coerce").sum())
    gb = b.groupby(key)[value].apply(lambda s: pd.to_numeric(s, errors="coerce").sum())
    items: list[dict] = []
    for k in sorted(set(ga.index) | set(gb.index), key=str):
        cur, prev = ga.get(k), gb.get(k)
        row: dict[str, Any] = {key: _fmt(k)}
        if cur is None:
            row.update(status="已消失", current=0, previous=_fmt(float(prev)), diff=_fmt(-float(prev)),
                       change_rate="-100.0%")
        elif prev is None:
            row.update(status="新增", current=_fmt(float(cur)), previous=0, diff=_fmt(float(cur)),
                       change_rate="(上期无此项,无法计算增长率)")
        else:
            cur, prev = float(cur), float(prev)
            row.update(status="持平" if cur == prev else ("上升" if cur > prev else "下降"),
                       current=_fmt(cur), previous=_fmt(prev), diff=_fmt(cur - prev),
                       change_rate=(f"{(cur - prev) / prev * 100:+.1f}%" if prev
                                    else "(上期为 0,无法计算增长率)"))
        items.append(row)
    items.sort(key=lambda r: abs(float(r["diff"]) if isinstance(r["diff"], (int, float)) else 0),
               reverse=True)
    tot_a, tot_b = float(ga.sum()), float(gb.sum())
    return {
        "key": key, "value": value,
        "total": {"current": _fmt(tot_a), "previous": _fmt(tot_b),
                  "diff": _fmt(tot_a - tot_b),
                  "change_rate": f"{(tot_a - tot_b) / tot_b * 100:+.1f}%" if tot_b else "(上期为 0)"},
        "note": "items 已按变化绝对值从大到小排序,前几项就是最该说的事",
        "items": items[:100],
    }


def builtin_table_pivot(dataset: str = "", rows: list | None = None, index: str = "",
                        columns: str = "", value: str = "", agg: str = "sum",
                        _task_id: str | None = None, **_: Any) -> dict:
    """透视表:行 × 列 × 值。如「渠道 × 月份 的销售额」。

    交叉维度的问题(哪个渠道在哪个月掉了)用一维分组答不了,
    而让模型把一维结果在脑子里拼成二维,拼错了也看不出来。
    """
    if not index or not value:
        return {"error": "必须指定 index(行维度)和 value(数值列);columns(列维度)可选"}
    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    import pandas as pd

    for c in [index, value] + ([columns] if columns else []):
        if c not in df.columns:
            return {"error": f"列「{c}」不存在。{_cols_hint(df)}"}
    df[value] = pd.to_numeric(df[value], errors="coerce")
    how = (agg or "sum").strip().lower()
    if how not in ("sum", "mean", "count", "max", "min", "median"):
        return {"error": f"不支持的聚合方式「{agg}」,可用:sum / mean / count / max / min / median"}
    try:
        pt = pd.pivot_table(df, index=index, columns=(columns or None), values=value,
                            aggfunc=how, fill_value=0)
    except Exception as exc:                    # noqa: BLE001
        return {"error": f"透视失败:{type(exc).__name__}: {exc}", "hint": _cols_hint(df)}
    pt = pt.reset_index()
    pt.columns = [str(c) for c in pt.columns]
    out = _rows_out(pt, limit=100)
    out["note"] = f"{index} × {columns or '(无列维度)'} 的 {value} {how}"
    return out


def builtin_table_outliers(dataset: str = "", rows: list | None = None, column: str = "",
                           method: str = "iqr", label: str = "",
                           _task_id: str | None = None, **_: Any) -> dict:
    """挑出数值明显偏离的行(IQR 四分位法 或 z-score 标准差法)。

    「指出异常」是解读类报告里反复出现的要求,而模型靠眼睛在几十上百行里挑异常
    很不可靠 —— 它往往挑的是**最大的那几行**,但最大不等于异常
    (旺季最大是正常的,淡季突然冒高才是异常)。这里按分布来判。
    """
    if not column:
        return {"error": "必须指定 column(要检测的数值列)"}
    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    import pandas as pd

    if column not in df.columns:
        return {"error": f"列「{column}」不存在。{_cols_hint(df)}"}
    s = pd.to_numeric(df[column], errors="coerce")
    valid = s.dropna()
    if len(valid) < 4:
        return {"error": f"有效数值只有 {len(valid)} 个,太少了,判不出异常(至少要 4 个)"}
    how = (method or "iqr").strip().lower()
    if how == "zscore":
        mu, sd = float(valid.mean()), float(valid.std())
        if not sd:
            return {"row_count": 0, "rows": [], "note": f"{column} 全部相同({_fmt(mu)}),没有异常点"}
        score = (s - mu) / sd
        mask = score.abs() > 3
        rule = f"z-score 法:偏离均值 {_fmt(mu)} 超过 3 倍标准差({_fmt(sd)})"
    else:
        q1, q3 = float(valid.quantile(.25)), float(valid.quantile(.75))
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mask = (s < lo) | (s > hi)
        rule = f"IQR 法:正常区间 {_fmt(lo)} ~ {_fmt(hi)}(Q1={_fmt(q1)}, Q3={_fmt(q3)})"
    hit = df[mask.fillna(False)].copy()
    if label and label in df.columns:
        hit = hit[[label, column]] if column in hit.columns else hit
    out = _rows_out(hit, limit=50)
    out["rule"] = rule
    out["note"] = (f"在 {len(df)} 行里挑出 {len(hit)} 个异常点。"
                   "异常≠错误,要结合业务说明为什么偏离" if len(hit)
                   else f"没有异常点 —— {column} 的分布是平稳的,这本身也是一个结论")
    return out


def builtin_table_trend(dataset: str = "", rows: list | None = None, date_column: str = "",
                        value: str = "", freq: str = "day", _task_id: str | None = None,
                        **_: Any) -> dict:
    """时间序列:按日/周/月汇总,并给出环比与整体趋势方向。

    明细常常是一天好几条。模型直接读明细看不出趋势,而按「最后一行比第一行」
    下结论又会被单日波动带偏 —— 这里按周期汇总后逐期环比,并用线性拟合的斜率
    判一次整体方向,两个口径一起给。
    """
    if not date_column or not value:
        return {"error": "必须指定 date_column(日期列)和 value(数值列)"}
    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    import numpy as np
    import pandas as pd

    for c in (date_column, value):
        if c not in df.columns:
            return {"error": f"列「{c}」不存在。{_cols_hint(df)}"}
    df["_d"] = pd.to_datetime(df[date_column], errors="coerce")
    df["_v"] = pd.to_numeric(df[value], errors="coerce")
    df = df.dropna(subset=["_d"])
    if df.empty:
        return {"error": f"列「{date_column}」里没有能识别成日期的值。"
                         "日期请用 2026-09-19 这样的格式"}
    rule = {"day": "D", "日": "D", "week": "W", "周": "W", "month": "ME", "月": "ME"}.get(
        (freq or "day").strip().lower(), "D")
    g = df.set_index("_d")["_v"].resample(rule).sum().reset_index()
    g.columns = ["period", value]
    g["period"] = g["period"].dt.strftime("%Y-%m-%d")
    vals = g[value].astype(float)
    g["环比"] = [""] + [f"{(vals.iloc[i] - vals.iloc[i - 1]) / vals.iloc[i - 1] * 100:+.1f}%"
                        if vals.iloc[i - 1] else "(上期为 0)" for i in range(1, len(vals))]
    direction = "数据点太少,判不出方向"
    if len(vals) >= 3:
        slope = float(np.polyfit(np.arange(len(vals)), vals.to_numpy(), 1)[0])
        avg = float(vals.mean()) or 1.0
        pct = slope / abs(avg) * 100
        direction = ("整体上升" if pct > 2 else "整体下降" if pct < -2 else "基本持平") + \
                    f"(每{ {'D':'天','W':'周','ME':'月'}[rule] }平均变化 {_fmt(slope)},约 {pct:+.1f}%)"
    out = _rows_out(g, limit=120)
    out["trend"] = direction
    out["note"] = "环比是与紧挨着的上一期比;trend 是整体方向(线性拟合),两者可能不一致——短期波动不等于趋势"
    return out


def builtin_table_search(dataset: str = "", rows: list | None = None, keyword: str = "",
                         columns: str = "", top: int = 50,
                         _task_id: str | None = None, **_: Any) -> dict:
    """在一份数据里按关键词找行(不区分大小写,任意列包含即命中)。

    和 sys_table_query 的区别:query 要写准列名和取值(pandas 语法),
    而很多时候模型只知道「用户提到了携程」,不知道它在哪一列、叫不叫这个名。
    找不到时会明确说「没找到」并给出该列的实际取值,而不是回一个空表让模型以为没数据。
    """
    if not keyword:
        return {"error": "必须指定 keyword(要找的关键词)"}
    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    want = [c.strip() for c in (columns or "").split(",") if c.strip()]
    if want:
        missing = [c for c in want if c not in df.columns]
        if missing:
            return {"error": f"这些列不存在:{'、'.join(missing)}。{_cols_hint(df)}"}
        scope = df[want]
    else:
        scope = df
    mask = scope.astype(str).apply(
        lambda col: col.str.contains(str(keyword), case=False, na=False, regex=False)).any(axis=1)
    hit = df[mask]
    if hit.empty:
        # 给出候选取值,让模型知道该换哪个词 —— 只回空表的话它会当成「本期没有携程的订单」
        hints = {}
        for c in (want or list(df.columns))[:5]:
            vals = df[c].dropna().astype(str).unique()[:10].tolist()
            if vals:
                hints[str(c)] = vals
        return {"row_count": 0, "rows": [],
                "note": f"没有任何一行包含「{keyword}」。**这不代表业务上没有发生**,"
                        "也可能是叫法不同;下面是各列的实际取值,可换个词再找",
                "candidates": hints}
    out = _rows_out(hit, limit=max(1, min(int(top or 50), 200)))
    out["note"] = f"在 {len(df)} 行里找到 {len(hit)} 行包含「{keyword}」"
    return out


def builtin_metric_delta(current: float | str | None = None,
                         previous: float | str | None = None,
                         kind: str = "环比", target: float | str | None = None,
                         total: float | str | None = None,
                         digits: int = 2, **_: Any) -> dict:
    """同比 / 环比 / 占比 / 达成率 / 差额 —— 由代码定公式和分母。

    **为什么不让模型自己写表达式**:这几个数最容易错的地方根本不是算术,而是
    **分母取谁、符号朝哪边**。`(今-去)/去` 写成 `(今-去)/今`,照样得到一个像样的
    百分比,量级也合理 —— 报告里写着「同比增长 8.3%」,实际是 9.1%,没人会去验算。
    公式写在这里,模型只需要把两个数交出来。

    current   本期值
    previous  对比期值(同比=去年同期,环比=上一期)
    target    目标值(算达成率时用)
    total     总量(算占比时用)
    kind      同比 / 环比 / 占比 / 达成率 / 差额
    """
    def _num(v, name):
        if isinstance(v, (int, float)):
            return float(v)
        t = str(v or "").strip().replace(",", "").replace("%", "")
        if not t:
            return None
        try:
            return float(t)
        except ValueError:
            raise ValueError(f"{name} 不是一个数字:{v!r}")

    k = str(kind or "环比").strip()
    try:
        cur = _num(current, "current")
        prev = _num(previous, "previous")
        tgt = _num(target, "target")
        tot = _num(total, "total")
    except ValueError as e:
        return {"error": str(e)}
    if cur is None:
        return {"error": "没有给 current(本期值)"}
    d = max(0, min(int(digits or 0), 6))

    def _pct(num: float, den: float | None, den_name: str) -> dict:
        if den is None:
            return {"error": f"算{k}需要{den_name},但没给"}
        if den == 0:
            # 分母为 0 时**不返回数字**。返回 0 或 100% 都会被原样写进报告,
            # 而「从 0 涨到 500」在业务上根本没有百分比可言 —— 它是「从无到有」。
            how = (f"请按「从 0 增加到 {cur}」这样的说法描述"
                   if k in ("同比", "环比", "差额") else
                   f"{den_name}为 0 时没有可分的基数,请直接说明本期值是 {cur}")
            return {"kind": k, "current": cur, "base": den, "percent": None,
                    "note": f"{den_name}为 0,{k}没有意义(不是 0%,也不是 100%)。"
                            f"{how},不要给百分比。"}
        val = round((num / den) * 100, d)
        return {"kind": k, "current": cur, "base": den,
                "percent": val, "text": f"{val}%"}

    if k in ("同比", "yoy", "year_over_year", "环比", "mom", "month_over_month", "qoq", "chain"):
        if prev is None:
            return {"error": f"算{k}需要 previous(对比期值),但没给"}
        out = _pct(cur - prev, prev, "对比期值")
        if out.get("percent") is not None:
            out["diff"] = round(cur - prev, d)
            out["direction"] = "上升" if cur > prev else ("下降" if cur < prev else "持平")
            out["formula"] = "(本期 - 对比期) / 对比期 × 100%"
            out["note"] = ("分母是**对比期**,不是本期 —— 这是这个数最常见的错法,"
                           "错了之后得到的仍是一个量级合理的百分比,看不出来")
        out["kind"] = k
        return out

    if k in ("占比", "share", "proportion", "比重"):
        out = _pct(cur, tot, "total(总量)")
        if out.get("percent") is not None:
            out["formula"] = "部分 / 总量 × 100%"
        return out

    if k in ("达成率", "completion", "achievement", "完成率"):
        out = _pct(cur, tgt, "target(目标值)")
        if out.get("percent") is not None:
            out["formula"] = "实际 / 目标 × 100%"
            out["gap"] = round(cur - tgt, d)
            out["reached"] = cur >= tgt
        return out

    if k in ("差额", "diff", "delta", "差值"):
        if prev is None:
            return {"error": "算差额需要 previous(对比期值),但没给"}
        return {"kind": k, "current": cur, "base": prev,
                "diff": round(cur - prev, d),
                "direction": "上升" if cur > prev else ("下降" if cur < prev else "持平"),
                "formula": "本期 - 对比期"}

    return {"error": f"认不出口径「{kind}」;可用:同比 / 环比 / 占比 / 达成率 / 差额"}


def builtin_calc(expression: str = "", **_: Any) -> dict:
    """算一个数学表达式(支持 + - * / % ** () 与 round/abs/min/max/sum/sqrt)。

    模型做算术是会错的,尤其是多位数乘除、百分比和占比 —— 而且错得很自然:
    它给出的是一个位数正确、量级合理的数,没人会去验算。凡是最终要写进结论的数字,
    都该过一遍这里。

    只做纯数学:不认变量、不认函数定义、不碰任何系统能力。
    """
    import ast as _ast
    import math as _math
    import operator as _op

    expr = (expression or "").strip()
    if not expr:
        return {"error": "没有给表达式"}
    if len(expr) > 500:
        return {"error": "表达式太长(超过 500 字),请拆开算"}
    _BIN = {_ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul, _ast.Div: _op.truediv,
            _ast.FloorDiv: _op.floordiv, _ast.Mod: _op.mod, _ast.Pow: _op.pow}
    _FN = {"round": round, "abs": abs, "min": min, "max": max, "sum": sum,
           "sqrt": _math.sqrt, "floor": _math.floor, "ceil": _math.ceil}

    def ev(node):
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("只能算数字")
        if isinstance(node, _ast.BinOp) and type(node.op) in _BIN:
            l, r = ev(node.left), ev(node.right)
            if type(node.op) in (_ast.Div, _ast.FloorDiv, _ast.Mod) and r == 0:
                raise ValueError("除数为 0")
            # 幂运算要同时卡底数和指数。只卡底数是不够的:2**99999999 底数才 2,
            # 却能把这个进程算到卡死 —— 而卡死的不是"算错一个数",是整个任务没了响应。
            if type(node.op) is _ast.Pow and (abs(l) > 1e6 or abs(r) > 1000):
                raise ValueError("幂运算超出范围(底数上限 1e6,指数上限 1000),拒绝计算")
            return _BIN[type(node.op)](l, r)
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, (_ast.UAdd, _ast.USub)):
            return ev(node.operand) * (-1 if isinstance(node.op, _ast.USub) else 1)
        if isinstance(node, (_ast.List, _ast.Tuple)):
            return [ev(x) for x in node.elts]
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id in _FN:
            return _FN[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("表达式里有不支持的写法(只支持数字、+ - * / % ** () 与 "
                         "round/abs/min/max/sum/sqrt/floor/ceil)")

    try:
        val = ev(_ast.parse(expr, mode="eval").body)
    except ZeroDivisionError:
        return {"error": "除数为 0"}
    except Exception as exc:                    # noqa: BLE001
        return {"error": f"算不了:{exc}", "expression": expr}
    return {"expression": expr, "result": _fmt(val) if isinstance(val, float) else val}


def builtin_datasets(_task_id: str | None = None, **_: Any) -> dict:
    """列出本次任务里已经留存的数据集(句柄 / 来自哪个工具 / 多少行 / 有哪些列)。

    取数工具返回很大时,明细会留在服务端、只把摘要和句柄给模型。隔了几轮之后
    模型未必还记得句柄号 —— 记错一个号,数据工具就会报「数据集不存在」,
    而它很可能就此改口说「没有数据」。有这个工具,它可以先看一眼有什么。
    """
    from cogni_agent.runtime_service.engine import databuf

    items = databuf.catalog(_task_id)
    return {"count": len(items), "datasets": items,
            "note": "用 dataset=\"<句柄>\" 传给 sys_table_query / sys_table_stats 等工具"
                    if items else "当前没有已留存的数据集 —— 先调用取数工具拿数据"}


def builtin_json_peek(data: Any = None, path: str = "", max_keys: int = 40, **_: Any) -> dict:
    """看一坨 JSON 的结构(有哪些键、各是什么类型、长什么样),也可按路径取其中一段。

    接口返回嵌套很深时,模型常常猜错数据在哪一层,然后取到 None 又当成"没有数据"。
    先看结构、再按 path(形如 data.list)取需要的那一段,比猜可靠。
    """
    import json as _json

    if data is None:
        return {"error": "没有给 data"}
    if isinstance(data, str):
        try:
            data = _json.loads(data)
        except (ValueError, TypeError):
            return {"error": "data 不是合法的 JSON 文本", "preview": data[:200]}
    cur = data
    for seg in [s for s in (path or "").split(".") if s]:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            avail = list(cur)[:max_keys] if isinstance(cur, dict) else f"(这是个 {type(cur).__name__})"
            return {"error": f"路径 {path} 里的「{seg}」找不到", "available": avail}

    def shape(node, depth=0):
        if depth > 3:
            return "…"
        if isinstance(node, dict):
            return {k: shape(v, depth + 1) for k, v in list(node.items())[:max_keys]}
        if isinstance(node, list):
            return [f"共 {len(node)} 项"] + ([shape(node[0], depth + 1)] if node else [])
        if isinstance(node, str):
            return f"字符串:{node[:60]}" + ("…" if len(node) > 60 else "")
        return f"{type(node).__name__}:{node}"

    return {"path": path or "(根)", "type": type(cur).__name__,
            "length": len(cur) if isinstance(cur, (list, dict, str)) else None,
            "shape": shape(cur),
            "note": "shape 是结构速览,不是完整数据;确认数据体在哪一层后,"
                    "用 sys_table_query 之类的工具去算,不要把整坨 JSON 搬进回答"}


def _mixed_group_column(df, label_column: str, value_cols: list[str]):
    """找出「把几个维度混在一张图里」的那一列,没有返回 None。

    客群画像这类接口一次返回多个维度的标签:
        标签名=收入 标签值=低/中/高 … 占比 … TGI
        标签名=消费水平 标签值=低/中/高 …
    模型拿「标签值」做横轴直接画,收入的「低」、消费的「低」、手机价位的「1001-2000」
    就排在同一根轴上、切进同一张饼(用户截图:40 块的饼、横轴上「低 / 1001-2000 / 中」)。
    图画得出来、数也对,但比的东西没有意义。

    判定(任一成立,且该列是文本、2~30 个取值、每个取值至少 2 行):
      ① 标签列有重复值,而「分组列 + 标签列」不重复 —— 同一个标签值在不同维度下各出现一次;
      ② 第一列数值按该列分组求和,每组都≈100(或≈1)—— 每组各自是一份完整的占比。
    """
    import pandas as pd

    n = len(df)
    if n < 4 or not value_cols:
        return None
    labels = df[label_column].astype(str)
    dup = labels.duplicated().any()
    first = pd.to_numeric(df[value_cols[0]], errors="coerce")
    for c in df.columns:
        if c == label_column or c in value_cols:
            continue
        col = df[c]
        if pd.to_numeric(col, errors="coerce").notna().mean() > 0.8:
            continue                                  # 数值列不是分组
        g = col.astype(str)
        k = g.nunique()
        if not 2 <= k <= 30 or g.value_counts().min() < 2:
            continue
        if dup and not pd.DataFrame({"g": g, "l": labels}).duplicated().any():
            return c
        sums = first.groupby(g).sum(min_count=1).dropna()
        if len(sums) == k and all(abs(v - 100) <= 3 for v in sums) or \
                len(sums) == k and all(abs(v - 1) <= 0.03 for v in sums):
            return c
    return None


def builtin_chart(dataset: str = "", rows: list | None = None, chart_type: str = "",
                  title: str = "", label_column: str = "", value_columns: str = "",
                  line_columns: str = "", marks: str = "", baseline: Any = None,
                  baseline_label: str = "", filter: str = "",
                  _task_id: str | None = None, **_: Any) -> dict:
    """画一张图(曲线 / 柱状 / 饼图 / 雷达图),直接渲染给用户看。

    **数字不经过你的手**:你只说「用哪一列做标签、哪几列做数值」,
    取数、对齐、配图都在服务端做完。让模型把几十个数字重抄一遍是行不通的 ——
    它会把 21085 写成 21085.0、把 3.6234 四舍五入成 3.6,图的形状对、数却和
    取回来的那份对不上,而这种错没人会去核对。

    chart_type:
      line   曲线图 —— 看随时间的走势
      bar    柱状图 —— 比大小(分布图用它)
      pie    饼图   —— 看占比,只取 value_columns 的第一列
      radar  雷达图 —— 多个指标放在一起看形状,value_columns 给几列就是几条

    label_column   每个点/每一块的名字(日期、渠道、指标名…)
    value_columns  数值列,逗号分隔;radar / line / bar 可以给多列(多条线/多组柱)
    """
    from cogni_agent.runtime_service.engine import databuf

    df, err = _load(_task_id, dataset, rows)
    if err:
        return {"error": err}
    from cogni_agent.runtime_service.engine import chart_templates as _ct

    kind = (chart_type or "").strip().lower()
    # 中文叫法一律认:模型经常直接写「饼图」「漏斗」,拒掉它只会换来一次重试。
    alias = {
        "曲线": "line", "折线": "line", "线图": "line",
        "面积": "area", "面积图": "area", "堆叠面积": "stacked_area",
        "柱状": "bar", "柱形": "bar", "分布": "bar", "柱状图": "bar",
        "堆叠柱": "stacked_bar", "堆叠柱状": "stacked_bar", "堆积柱": "stacked_bar",
        "横向柱": "horizontal_bar", "条形": "horizontal_bar", "条形图": "horizontal_bar",
        "排名": "horizontal_bar", "排行": "horizontal_bar", "ranking": "horizontal_bar",
        "饼": "pie", "饼图": "pie", "环": "donut", "环图": "donut", "圆环": "donut", "doughnut": "donut",
        "雷达": "radar", "雷达图": "radar",
        "散点": "scatter", "散点图": "scatter", "相关": "scatter",
        "漏斗": "funnel", "漏斗图": "funnel", "转化": "funnel",
        "仪表": "gauge", "仪表盘": "gauge", "达成率": "gauge", "完成率": "gauge",
        "矩形树": "treemap", "树图": "treemap", "矩形树图": "treemap",
        "桑基": "sankey", "桑基图": "sankey", "流向": "sankey", "流向图": "sankey",
        "热力": "heatmap", "热力图": "heatmap", "热图": "heatmap",
        "日历": "calendar", "日历图": "calendar", "日历热力": "calendar",
        "箱线": "boxplot", "箱线图": "boxplot", "箱型": "boxplot", "盒须": "boxplot",
        "旭日": "sunburst", "旭日图": "sunburst",
        "河流": "themeriver", "主题河流": "themeriver", "themeRiver": "themeriver",
        "双轴": "combo", "组合": "combo", "组合图": "combo", "柱线": "combo", "量率": "combo",
    }
    kind = alias.get(kind, kind)
    if kind not in _ct.CHART_TYPES:
        return {"error": f"不支持的图表类型「{chart_type}」。可用:{_CHART_MENU}"}
    if not label_column or label_column not in df.columns:
        return {"error": f"label_column「{label_column}」不存在。{_cols_hint(df)}"}
    cols = [c.strip() for c in (value_columns or "").split(",") if c.strip()]
    if not cols:
        return {"error": "必须指定 value_columns(要画的数值列,逗号分隔)"}
    missing = [c for c in cols if c not in df.columns]
    if missing:
        return {"error": f"这些数值列不存在:{'、'.join(missing)}。{_cols_hint(df)}"}
    line_cols = [c.strip() for c in (line_columns or "").split(",") if c.strip()]
    if kind == "combo":
        if not line_cols:
            return {"error": "combo(双轴)要同时给 value_columns(量,画柱,左轴)和 "
                             "line_columns(率,画线,右轴)。只有一种量级的话用 bar / line 就够了"}
        miss2 = [c for c in line_cols if c not in df.columns]
        if miss2:
            return {"error": f"这些 line_columns 不存在:{'、'.join(miss2)}。{_cols_hint(df)}"}

    import pandas as pd

    # ── 只画其中一组(filter="标签名=人生阶段")──
    _filter_note = None
    if filter and str(filter).strip():
        fk, _, fv = str(filter).replace("＝", "=").partition("=")
        fk, fv = fk.strip(), fv.strip()
        if not fk or fk not in df.columns or not fv:
            return {"error": f"filter 要写成「列名=值」,如 标签名=人生阶段。{_cols_hint(df)}"}
        sub = df[df[fk].astype(str) == fv]
        if sub.empty:
            vals = "、".join(map(str, df[fk].astype(str).unique()[:30]))
            return {"error": f"列「{fk}」里没有「{fv}」。可选:{vals}"}
        df = sub.reset_index(drop=True)
    else:
        # ── 多个维度混在一起:能从标题认出是哪一组就只画那一组,认不出就让模型选 ──
        gcol = _mixed_group_column(df, label_column, cols + line_cols)
        if gcol is not None:
            groups = [str(v) for v in dict.fromkeys(df[gcol].astype(str).tolist())]
            hit = [g_ for g_ in groups if g_ and g_ in str(title or "")]
            if len(hit) == 1:
                df = df[df[gcol].astype(str) == hit[0]].reset_index(drop=True)
                _filter_note = (f"数据里有 {len(groups)} 个「{gcol}」,已按标题只画「{hit[0]}」;"
                                f"其余({'、'.join(g_ for g_ in groups if g_ != hit[0])[:120]})要看请分别画")
            else:
                return {"error": (f"这份数据按「{gcol}」分成 {len(groups)} 组"
                                  f"({'、'.join(groups)[:200]}),每组是各自独立的一份分布。"
                                  "混在一张图里,不同组的类别会排在同一根轴上、切进同一张饼,比较没有意义。"
                                  f"请加 filter=\"{gcol}=<其中一组>\" 只画用户问的那一组;"
                                  "用户要看全部时,挑最相关的 1~3 组各画一张,不要一组一张画满")}

    labels = [str(v) for v in df[label_column].tolist()]
    series = []
    for c in cols:
        nums = pd.to_numeric(df[c], errors="coerce")
        if nums.isna().all():
            return {"error": f"列「{c}」里没有可用的数值,画不了图。"
                             "请确认选的是数值列,而不是文本列"}
        series.append({"name": str(c), "data": [None if pd.isna(v) else _fmt(float(v))
                                                for v in nums.tolist()]})
    # 一张图最多多少个点:再多前端挤成一团,读图的人什么也看不出来。
    # 超了就明说,让模型先聚合(sys_table_query 分组)再来画,而不是默默截断。
    if len(labels) > 60:
        return {"error": f"有 {len(labels)} 个数据点,画出来会挤成一团看不清。"
                         "请先用 sys_table_query 按维度分组聚合(或取 TOP N)再画,"
                         "或者换一个粒度(比如按月而不是按天)"}

    spec: dict[str, Any] = {"title": title or f"{label_column} 的 {'、'.join(cols + line_cols)}",
                            "type": kind}
    # 极值点 / 均值线 / 基准线(只对直角坐标系的图生效,其余类型模板里直接忽略)
    _marks = [m.strip().lower() for m in str(marks or "").replace("，", ",").split(",") if m.strip()]
    _mark_alias = {"最大": "max", "峰值": "max", "最高": "max", "最小": "min", "最低": "min",
                   "谷值": "min", "平均": "average", "均值": "average", "avg": "average", "mean": "average"}
    spec["marks"] = [_mark_alias.get(m, m) for m in _marks]
    if baseline not in (None, ""):
        try:
            spec["baseline"] = float(baseline)
            spec["baseline_label"] = baseline_label or "基准"
        except (TypeError, ValueError):
            return {"error": f"baseline 要是一个数(如 TGI 的 100、目标值 5000),而不是「{baseline}」"}
    # ── 只吃「一个标签列 + 一个数值列」的那几种 ──
    # 它们讲的都是「每一项占多少 / 是多少」,给多列没有意义:
    # 饼图画两列等于把两件事摞在一张饼上,读的人无从分辨。多给了就只用第一列并说明。
    _single = {"pie", "donut", "funnel", "treemap", "gauge", "sankey", "calendar", "sunburst"}
    _extra_note = None
    if kind in _single and len(cols) > 1:
        _extra_note = (f"「{kind}」只能表达一个数值,已只用第一列「{cols[0]}」;"
                       f"要同时看 {'、'.join(cols[1:])},请分开画或改用 bar / line")

    if kind in ("pie", "donut"):
        spec["data"] = [{"name": n, "value": v}
                        for n, v in zip(labels, series[0]["data"]) if v is not None]
        folded, why = _ct.pie_fold(spec["data"])
        if folded is None:
            # 长尾分布:饼图读不出东西,换成排名条形图(模板里同样的规则)
            kind = "horizontal_bar"
            spec["type"] = kind
            spec.pop("data")
            spec["x"] = labels
            spec["series"] = series[:1]
            _fold = f"{why},饼图切不开,已改画排名条形图"
        else:
            _fold = why + "(正文里提一句「其它」包含哪些)" if why else ""
        if _fold:
            _extra_note = f"{_extra_note};{_fold}" if _extra_note else _fold
    elif kind in ("funnel", "treemap"):
        spec["data"] = [{"name": n, "value": v}
                        for n, v in zip(labels, series[0]["data"]) if v is not None]
    elif kind == "gauge":
        # 仪表盘只画一个数。给了多行就取第一行 —— 一块表盘上放不下两个指针。
        first = next(((n, v) for n, v in zip(labels, series[0]["data"]) if v is not None), None)
        if first is None:
            return {"error": "没有可用的数值,画不了仪表盘"}
        spec["data"] = [{"name": str(first[0]), "value": first[1]}]
        # 变化率(环比/同比/增长…)或负数不该上表盘:0~100 的盘上 +2.99% 只是一根贴底的指针,
        # 负数干脆画不出来。说清楚让模型换成指标卡(结构化输出的 cards)或柱图
        from cogni_agent.runtime_service.engine.chart_templates import gauge_as_card
        if gauge_as_card({**spec, "title": spec.get("title") or cols[0]}):
            return {"error": "仪表盘只适合完成率 / 达成率这类「离满格多远」的指标。"
                             f"「{first[0]}」是变化率(或负数),请改用指标卡(结构化输出 cards 里给出数值与涨跌)"
                             ",或用 bar 把本期与上期并排画"}
        spec["max"] = max(100.0, float(first[1]))
        if len(labels) > 1:
            _extra_note = (f"仪表盘只画一个数,已用第一行「{first[0]}」;"
                           f"要横向比较多个对象请改用 bar 或 horizontal_bar")
    elif kind == "sankey":
        # 桑基要「从 → 到 → 多少」。标签列里写成 "A→B" / "A->B" / "A-B" 都认。
        links = []
        for n, v in zip(labels, series[0]["data"]):
            if v is None:
                continue
            parts = None
            for sep in ("→", "->", "=>", ">", "-", "—", "至"):
                if sep in n:
                    parts = [x.strip() for x in n.split(sep, 1)]
                    break
            if not parts or not parts[0] or not parts[1]:
                return {"error": f"桑基图要的是「从哪到哪」,而 label_column 里是「{n}」。"
                                 f"请把标签列做成 `携程→成人票` 这样的形式(用 → 或 -> 分隔),"
                                 f"或者改用 bar / treemap 看各自的量"}
            links.append({"source": parts[0], "target": parts[1], "value": v})
        if not links:
            return {"error": "没有可用的流向数据"}
        spec["links"] = links
    elif kind == "scatter":
        # 散点:x 轴也必须是数值。label_column 是文本时说清楚,不画一张假图。
        xs = pd.to_numeric(df[label_column], errors="coerce")
        if xs.isna().all():
            return {"error": f"散点图的横轴要是**数值**,而「{label_column}」是文本列。"
                             f"想看不同类别的大小请用 bar;想看两个指标的相关性,"
                             f"把其中一个数值列填进 label_column"}
        spec["x"] = [None if pd.isna(v) else _fmt(float(v)) for v in xs.tolist()]
        spec["series"] = series
        spec["categories"] = str(label_column)
    elif kind == "heatmap":
        # 热力:标签列做 y(行),数值列名做 x(列),每格一个数。
        spec["x"] = [str(c) for c in cols]
        spec["y"] = labels
        spec["data"] = [[ci, ri, v]
                        for ci, srs in enumerate(series)
                        for ri, v in enumerate(srs["data"]) if v is not None]
    elif kind == "calendar":
        spec["data"] = [[str(n)[:10], v] for n, v in zip(labels, series[0]["data"])
                        if v is not None and len(str(n)) >= 10]
        if not spec["data"]:
            return {"error": f"日历图要的是日期,而「{label_column}」里不是 YYYY-MM-DD 形式的日期"}
    elif kind == "boxplot":
        # 箱线要五个数(min/Q1/中位/Q3/max),且**由服务端算**——
        # 让模型自己报分位数,报出来的看着合理却和真实分布对不上,
        # 而箱线图恰恰就是用来看分布的。
        if len(cols) < 5:
            grouped = []
            cats = []
            for lab in dict.fromkeys(labels):
                vals = pd.to_numeric(df.loc[df[label_column].astype(str) == lab, cols[0]],
                                     errors="coerce").dropna()
                if len(vals) < 5:
                    continue
                cats.append(lab)
                grouped.append([_fmt(float(vals.min())), _fmt(float(vals.quantile(.25))),
                                _fmt(float(vals.median())), _fmt(float(vals.quantile(.75))),
                                _fmt(float(vals.max()))])
            if not grouped:
                return {"error": "箱线图要看分布,每个类别至少要有 5 个数据点。"
                                 "当前数据每类点太少 —— 点少时直接看具体数值比画箱线图更清楚"}
            spec["x"] = cats
            spec["data"] = grouped
        else:
            spec["x"] = labels
            spec["data"] = [[s_["data"][i] for s_ in series[:5]] for i in range(len(labels))]
    elif kind == "sunburst":
        # 旭日:标签列用「一级/二级」的路径写法,按 / 拆层。
        tree: dict = {}
        for n, v in zip(labels, series[0]["data"]):
            if v is None:
                continue
            parts = [x.strip() for x in str(n).replace("\\", "/").split("/") if x.strip()]
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1] if parts else str(n)] = v

        def _to_nodes(d):
            out = []
            for k, v in d.items():
                out.append({"name": k, "value": v} if not isinstance(v, dict)
                           else {"name": k, "children": _to_nodes(v)})
            return out

        spec["data"] = _to_nodes(tree)
    elif kind == "themeriver":
        spec["data"] = [[str(n), v, s_["name"]]
                        for s_ in series
                        for n, v in zip(labels, s_["data"]) if v is not None]
    elif kind == "radar":
        # 雷达图**每根轴要有自己的量程**。共用一个量程的话,这张图就没法看了:
        # 「票房 21085 元」和「口碑 3.79 分」画在一起,后者会直接贴在圆心上,
        # 看图的人会以为口碑是 0。所以按每根轴自己的最大值定量程,留 15% 余量
        # (贴边的点看不出是「刚好到顶」还是「超出了」)。
        maxes = []
        for i in range(len(labels)):
            vals = [s["data"][i] for s in series
                    if i < len(s["data"]) and isinstance(s["data"][i], (int, float))]
            top = max(vals) if vals else 1.0
            maxes.append(_fmt(top * 1.15) if top > 0 else 1.0)
        spec["indicators"] = [{"name": n, "max": m} for n, m in zip(labels, maxes)]
        spec["series"] = series
    else:
        spec["x"] = labels
        spec["series"] = series
        if kind == "combo":
            lser = []
            for c in line_cols:
                nums = pd.to_numeric(df[c], errors="coerce")
                if nums.isna().all():
                    return {"error": f"列「{c}」里没有可用的数值,画不了线"}
                lser.append({"name": str(c), "data": [None if pd.isna(v) else _fmt(float(v))
                                                      for v in nums.tolist()]})
            spec["line_series"] = lser

    from cogni_agent.runtime_service.engine import chart_templates

    option = chart_templates.build_chart(spec)
    if option is None:
        return {"error": "图表配置没通过校验,换一组列试试"}
    # 除了 ECharts option,再带一份**中性的行列形态**。
    # 两个前端用的不是同一个图表库:APP(uni)装的是 echarts,直接吃 option;
    # 管理端操练台装的是 recharts,吃不了 option —— 现场就是这么「图跑出来了但页面上没有」。
    # 让服务端一次算好、两种形态一起发,两边同源,不会出现「APP 上是一个数、后台是另一个数」;
    # 也不必为了一张图再往管理端塞一个 1MB 的图表库。
    # 中性行列表**由 option 现推**,不再另起一套手搓的。
    # 手搓那份只覆盖「一个标签列 + N 个数值列」这一种形状,桑基(从→到)、箱线(五数)、
    # 热力(二维格)根本不是这个形状 —— 手搓出来的表和图上画的不是同一回事,
    # 而两边都能正常显示,没人会发现它们对不上。由 option 推则天然同源。
    from cogni_agent.runtime_service.engine import chart_view as _cv

    card = _cv.ensure_view({"title": spec["title"], "type": kind, "option": option})
    # 由 option 推出来的表,维度列叫通用的「名称」(option 里没有列名这个概念)。
    # 这里把它换回用户数据里真正的列名 —— 表头写「指标」「渠道」比写「名称」有用得多,
    # 而这张表在后台是**唯一**能看的东西(ECharts 独有的那几种根本画不出来)。
    _t = card.get("table") or {}
    if _t.get("fields") and _t["fields"][0].get("name") == _cv._DIM and str(label_column) != _cv._DIM:
        _old = _cv._DIM
        _t["fields"][0]["name"] = str(label_column)
        _t["rows"] = [{(str(label_column) if k == _old else k): v for k, v in r.items()}
                      for r in _t.get("rows") or []]
    if not (card.get("table") or {}).get("rows"):
        # 推不出来时退回最朴素的那份 —— 宁可表的形状粗糙,也不能一行数据都没有:
        # 管理端对 ECharts 独有的类型只能显示表格,表没了那边就是彻底空白。
        card["table"] = {
            "chart_type": kind, "title": spec["title"],
            "fields": ([{"name": str(label_column), "type": "string", "role": "dimension"}]
                       + [{"name": str(c), "type": "number", "role": "metric"} for c in cols]),
            "rows": [{str(label_column): labels[i],
                      **{str(c): series[j]["data"][i] for j, c in enumerate(cols)}}
                     for i in range(len(labels))],
            "row_count": len(labels)}
    n = databuf.stash_chart(_task_id, card)
    if not n:
        return {"error": "当前没有任务上下文,画不了图(操练台单跑时不支持)"}
    out = {"ok": True, "chart": spec["title"], "type": kind,
           "points": len(labels),
           "series": [s["name"] for s in series] + [str(c) for c in line_cols if kind == "combo"],
           "note": f"图已生成,会直接显示给用户(本次第 {n} 张)。"
                   "**不要再把这些数字抄进回答里画 ASCII 图** —— 用户已经看到图了,"
                   "你只需要用文字讲清这张图说明了什么。"
                   # 管理后台现在和 APP 用同一个 ECharts 画(见 web EChartsView),
                   # 不再有「后台只能看表」的类型。但图可能在消息推送、导出文档里看不到,
                   # 正文里把结论写清楚是任何时候都成立的要求。
                   "关键结论要用文字写进正文,不能只说「见图」。"}
    if kind == "horizontal_bar" and len(labels) > _ct.HBAR_MAX_ROWS:
        _cut = f"共 {len(labels)} 项,排名图只画了最大的 {_ct.HBAR_MAX_ROWS} 项"
        _extra_note = f"{_extra_note};{_cut}" if _extra_note else _cut
    for _n in (_filter_note, _extra_note):
        if _n:
            out["note"] += f"\n注意:{_n}"
    return out


# ── 读用户上传的文件 ────────────────────────────────────────────────

async def builtin_read_file(file_id: str = "", sheet: str = "", _task_id: str | None = None,
                            **_: Any) -> dict:
    """读一个用户上传的文件:Excel / CSV / Word / PDF / 图片(OCR)。

    **表格类文件不会变成文本给你**。一份三千行的 Excel 转成 Markdown 是四十多万字,
    塞不进上下文;截断之后你看到的是半张表,然后会基于半张表下结论。
    所以表格读进来之后变成一个**数据集句柄** —— 和接口取回来的大数据走同一条路,
    sys_table_query / sys_table_stats / sys_chart 立刻就能用在它上面。

    文档(Word/PDF/txt)和图片(OCR)才返回文本,它们本来就是给人读的。
    """
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.runtime_service.engine import databuf
    from cogni_agent.tool_center_service import filereader as fr

    fid = (file_id or "").strip()
    if not fid:
        return {"error": "没有指定 file_id。用户上传文件后会给出一个文件编号,请带上它"}

    # 两张表都认:tc_agent_files 是给智能体读的(走对象存储,10MB),
    # tc_qbi_files 是 Quick BI 那条链路上传的(5MB)。用户不关心区别,
    # 拿着哪个编号都该能读 —— 分不清就会出现「我明明传了,它说文件不存在」。
    from cogni_agent.tool_center_service.quickbi.models import AgentFile, QbiFile

    name = content = None
    try:
        from cogni_agent.tool_center_service import agent_files as af

        row, content = await af.load_content(fid)
        name = row.file_name
    except Exception:                           # noqa: BLE001 - 不在新表就去老表找
        pass
    if content is None:
        async with get_sessionmaker()() as db:
            q = await db.get(QbiFile, fid)
        if q is None:
            return {"error": f"文件「{fid}」不存在或已被删除。"
                             "请让用户重新上传 —— **不要猜文件里有什么**"}
        name, content = q.file_name, q.content
    if not content:
        return {"error": f"文件「{name}」没有内容(可能上传时就失败了),请让用户重新上传"}
    _ = AgentFile
    try:
        got = fr.read(name, content, sheet=sheet or None)
    except fr.FileReadError as exc:
        return {"error": str(exc)}
    except Exception as exc:                    # noqa: BLE001 - 解析炸了要说清是哪一步
        log.warning(f"读文件 {fid} 失败:{type(exc).__name__}: {exc}")
        return {"error": f"这个文件解析失败({type(exc).__name__})。"
                         f"请确认它没有损坏,或换一种格式另存后重传"}

    if got["kind"] == "table":
        rows = got["rows"]
        stashed = databuf.stash(_task_id, f"上传文件:{name}", rows)
        if not stashed:
            # 没有任务上下文就存不了句柄。这时候给前 50 行总比什么都不给强,
            # 但必须说明只有前 50 行 —— 不说的话模型会把它当成整份数据来统计。
            return {"file_name": name, "kind": "table", "row_count": len(rows),
                    "rows": rows[:50],
                    "note": "当前没有任务上下文,只能直接返回前 50 行(不是全部)。"
                            "**不要用这 50 行算合计** —— 那不是整份数据"}
        out = {"file_name": name, "kind": "table",
               "dataset": stashed["handle"], "row_count": stashed["row_count"],
               "columns": stashed["columns"],
               "note": f"表格已读入,句柄 {stashed['handle']}。"
                       f"用 sys_table_stats 先看整体分布,"
                       f"sys_table_query 做筛选/分组统计,sys_chart 画图 —— "
                       f"都填 dataset=\"{stashed['handle']}\"。"
                       f"**不要让我把明细贴出来给你算**,那正是要避免的事"}
        if got.get("sheets") and len(got["sheets"]) > 1:
            # 多 sheet 不提醒的话,模型会把第一个 sheet 当成整个文件
            out["sheets"] = got["sheets"]
            out["note"] += (f"。注意这个文件有 {len(got['sheets'])} 个工作表"
                            f"({'、'.join(got['sheets'])}),现在读的是第一个;"
                            f"要读别的请带 sheet 参数再调一次")
        return out

    text = got["text"]
    return {"file_name": name, "kind": got["kind"], "chars": len(text),
            "text": text,
            "note": ("这是 OCR 识别出来的文字,可能有错字 —— 关键数字请和用户确认一遍"
                     if got["kind"] == "image" else "文档正文(表格已转成 Markdown)")}


# ── 降级:Quick BI 跑不通时至少给出看板地址 ──────────────────────────

async def builtin_quickbi_link(scenic_id: str = "", **_: Any) -> dict:
    """取这个景区的 BI 报表地址(画像 / 舆情看板)。

    定位是**降级**:Quick BI 的问数/解读跑不通时(凭证过期、额度用尽、上游超时),
    用户要的其实是"我去哪儿看这份数据" —— 给一个能点开的看板地址,
    远比一句"调用失败"有用。所以它独立于 Quick BI 连接,走数据平台的开放接口。
    """
    from cogni_agent.config import get_settings
    from cogni_agent.tool_center_service.sandbox.local import LocalSandbox

    cfg = (get_settings().get("tool_center", "builtin", "quickbi_link", default={}) or {})
    url = cfg.get("url") or "https://data.12301dev.com/v1/data/digitalBrain/api/quickBiLink"
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    await ps.refresh()
    key = cfg.get("api_key") or ps.get("data_platform_key")
    if not key:
        return {"error": "未配置数据平台 api key(tool_center.builtin.quickbi_link.api_key),"
                         "无法获取 BI 报表地址;请联系数据平台开发获取 xpftkey 后填入配置"}
    sandbox = LocalSandbox()
    res = await sandbox.execute(
        tool_id="builtin_quickbi_link", tool_type="http",
        type_config={"method": "POST", "url": url,
                     "headers": {"xpftkey": key, "Content-Type": "application/json"}},
        params={"scenicId": scenic_id or "{{scenic_id}}"},
        context={"scenic_id": scenic_id},
    )
    body = res.get("body") if isinstance(res, dict) else res
    if not isinstance(body, dict) or not body.get("status"):
        return {"error": f"数据平台返回异常:{str(body)[:200]}"}
    data = (body.get("data") or [{}])[0] if isinstance(body.get("data"), list) else {}
    return {
        "profile": data.get("profile") or [],      # [{uri, timeType}]
        "opinion": data.get("opinion") or "",
        "note": "这些是可以直接打开的 BI 看板地址;把它们作为链接给用户,不要复述 URL 本身",
    }


# ── 景区 → 供应商 ID(票务类接口的前置) ─────────────────────────────

# 景区 → (取数时间, 供应商 ID 列表)。供应商归属几乎不变,但每个票务问题都要先查一次;
# 一轮并发 4 个票务工具时不缓存就是 4 次同样的请求。10 分钟足够新,也不会让改了归属的人等太久。
_SUPPLIER_CACHE: dict[str, tuple[float, list]] = {}
_SUPPLIER_TTL = 600.0
SUPPLIER_URI = "/v1/data/digitalBrain/getSupplierIdByScenicId"


async def builtin_supplier_ids(scenic_id: str = "", scenicId: str = "", **_: Any) -> dict:
    """按景区取供应商 ID 列表 —— 票务类接口的 supplierId 就是它给的。

    **为什么做成内置**:票务类 10 个接口都要 supplierId,它只能按景区换出来。
    做成工具库里的普通工具时,整条链路系于它:没绑、没发布、对账号不可见、被改了名,
    票务类就全部调不通,而报出来的是「自动获取参数失败」这种谁都看不懂的话。
    内置之后:不用绑、不用发版、对每个技能都在;票务工具的 supplierId 由系统自动调它补齐,
    模型不需要、也不应该自己先查供应商再去调票务(那一步它常常漏掉或拿错景区)。

    景区一律取当前会话的(执行层会把 scenic_id 覆盖成本轮景区),不能拿它查别的景区。
    """
    import time

    from cogni_agent.config import get_settings
    from cogni_agent.tool_center_service.sandbox.local import LocalSandbox

    sid = (scenic_id or scenicId or "").strip()
    if not sid:
        return {"error": "没有景区:请先确定是哪个景区(集团账号需要在问题里说明景区)"}
    hit = _SUPPLIER_CACHE.get(sid)
    if hit and time.monotonic() - hit[0] < _SUPPLIER_TTL:
        return {"scenicId": sid, "supplierId": list(hit[1]), "cached": True}

    cfg = get_settings()
    base = ((cfg.get("tool_center", "data_api_seed", default={}) or {}).get("base_url")
            or "https://data.12301dev.com").rstrip("/")
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    await ps.refresh()
    if not ps.get("data_platform_key"):
        return {"error": "未配置数据平台密钥,取不到供应商 ID,票务类数据暂时查不了。"
                         "请到管理后台「系统设置 → 平台密钥」填写数据平台 xpftkey"
                         "(或设置环境变量 DATA_PLATFORM_XPFTKEY)"}
    res = await LocalSandbox().execute(
        tool_id="builtin_supplier_ids", tool_type="http",
        type_config={"method": "POST", "url": base + SUPPLIER_URI,
                     # 占位由沙箱在发请求时从服务端配置替换(密钥不进任何配置/日志)
                     "headers": {"xpftkey": "{{data_platform_key}}", "Content-Type": "application/json"}},
        params={"scenicId": sid}, context={"scenic_id": sid},
    )
    body = res.get("body") if isinstance(res, dict) else res
    status = res.get("status_code") if isinstance(res, dict) else None
    if not isinstance(body, dict) or not body.get("status"):
        return {"error": f"供应商接口返回异常(HTTP {status}):{str(body)[:200]}"}
    rows = body.get("data") or []
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("scenicId")) == sid),
               rows[0] if rows and isinstance(rows[0], dict) else {})
    ids = row.get("supplierId") or []
    if not isinstance(ids, list):
        ids = [ids]
    if not ids:
        return {"error": f"景区 {sid} 没有查到供应商 ID —— 这个景区在票务系统里可能还没有开通"}
    _SUPPLIER_CACHE[sid] = (time.monotonic(), list(ids))
    return {"scenicId": sid, "supplierId": list(ids)}


# ── 注册表 ──────────────────────────────────────────────────────────

def _parse_date(v: str | None) -> dt.date:
    t = (v or "").strip()
    if not t:
        return _today()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return _today()


def _days_in_month(y: int, m: int) -> int:
    nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


def _shift_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                          # 2 月 29 日
        return d.replace(year=d.year - 1, day=28)


_DATE = {"type": "string", "description": "日期,形如 2026-09-19;留空表示今天"}

# 选图指引。**从 4 种变成 19 种,选错的风险比选不到更大** ——
# 选错的图不会报错,只会把一份对的数据画成一张误导人的图,而图出来了、颜色也对,
# 没人会怀疑它。所以这里按「读图的人要做什么」来分,而不是按图形长什么样。
_CHART_MENU = (
    "看走势 → line 曲线(多条线比较) / area 面积(单条线看体量) / stacked_area 堆叠面积(构成随时间变化);"
    "比大小 → bar 柱状 / horizontal_bar 排名(类别名长或超过 8 项时横着放,会自动按值排序);"
    "量和率一起看 → combo 双轴(柱 = 量在左轴,线 = 率在右轴,如票数 + 变化率、占比 + TGI);"
    "看构成 → stacked_bar 堆叠柱(部分与整体) / pie 饼图 / donut 环图(类别 ≤ 7 个) / "
    "treemap 矩形树图(类别多到饼图切不开时) / sunburst 旭日图(有层级,标签写成「一级/二级」);"
    "多指标看形状 → radar 雷达图;"
    "看相关性 → scatter 散点图(横轴也必须是数值列);"
    "看转化 → funnel 漏斗图(逐级递减的几步);"
    "看单个达成率 → gauge 仪表盘(只画一个数);"
    "看流向 → sankey 桑基图(标签写成「携程→成人票」);"
    "看二维密度 → heatmap 热力图(如时段 × 星期);"
    "看一整年逐日 → calendar 日历图;"
    "看分布与离群 → boxplot 箱线图(每类至少 5 个数据点,五数由服务端算);"
    "看构成的流动 → themeriver 主题河流"
)

# 数据工具共用的两个入参。**dataset 优先** —— 上千行的结果压根没进过上下文,
# 模型手里只有摘要,传不出 rows 来;而摘要末尾会明确给出句柄号。
_DATASET = {"type": "string",
            "description": "服务端留存的数据集句柄,形如 ds_1(取数工具的数据摘要末尾会给出)。"
                           "数据量大时**用它**,不要试图把行贴进来"}
_ROWS = {"type": "array", "items": {"type": "object"},
         "description": "直接传入的数据行(数据本来就不多时用这个;有句柄就优先用 dataset)"}

BUILTIN: dict[str, dict] = {
    "sys_now": {
        "fn": builtin_now,
        "description": "获取当前日期时间(景区本地时区)。涉及「今天/本月/最近」等相对时间时**先调它**,不要自己推算日期。",
        "schema": {"type": "object", "properties": {}},
    },
    "sys_date_range": {
        "fn": builtin_date_range,
        "description": "把「今日/昨日/近7天/近30天/本周/上周/本月/上月/今年」换算成确切的起止日期。取数前用它定时间范围。",
        "schema": {"type": "object", "properties": {
            "period": {"type": "string", "description": "周期名,如 近7天 / 本月 / 上月"},
            "end": dict(_DATE, description="以哪天为基准,留空=今天")},
            "required": ["period"]},
    },
    "sys_date_shift": {
        "fn": builtin_date_shift,
        "description": "把一个日期前后挪若干天/月(自动处理月末)。",
        "schema": {"type": "object", "properties": {
            "date": _DATE,
            "days": {"type": "integer", "description": "挪几天,可为负"},
            "months": {"type": "integer", "description": "挪几个月,可为负"}}},
    },
    "sys_align_period": {
        "fn": builtin_align_period,
        "description": "给定时间段,算出它的同比(去年同期)或环比(上一个等长区间)区间。要做对比分析时先调它对齐口径。",
        "schema": {"type": "object", "properties": {
            "start": dict(_DATE, description="当前区间开始日期"),
            "end": dict(_DATE, description="当前区间结束日期"),
            "mode": {"type": "string", "enum": ["同比", "环比"], "description": "同比=去年同期;环比=紧挨着的上一段"}},
            "required": ["start", "end"]},
    },
    "sys_table_query": {
        "fn": builtin_table_query,
        "description": ("对取到的数据做筛选/分组/统计,再看结果。"
                        "数据行数多时**不要自己在脑子里算**:用它来求和、算占比、排序取 TOP N,算得准也省上下文。"
                        "取数工具返回很大时,明细会留在服务端,摘要末尾会给你一个句柄 —— 把句柄填进 dataset 即可。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET,
            "rows": _ROWS,
            "filter": {"type": "string", "description": 'pandas query 语法,如 amount > 1000 and channel == "携程"'},
            "group_by": {"type": "string", "description": "分组列,逗号分隔"},
            "agg": {"type": "string", "description": "聚合,如 amount:sum,orders:sum"},
            "sort_by": {"type": "string", "description": "排序列,前加 - 表示降序"},
            "top": {"type": "integer", "description": "只要前 N 行"}}},
    },
    "sys_table_stats": {
        "fn": builtin_table_stats,
        "description": ("看一份数据的整体画像:每列的行数/空值数,数值列的合计、均值、中位数、"
                        "分位数、极值,类别列的取值分布。**拿到陌生数据先调它** —— "
                        "不然容易拿样本行里的几个数当全局特征。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "columns": {"type": "string", "description": "只看这几列,逗号分隔;留空=全部列"}}},
    },
    "sys_table_compare": {
        "fn": builtin_table_compare,
        "description": ("两期数据按 key 对齐后算差值与增长率(同比/环比用它)。"
                        "**不要自己把两份数据两两相减** —— 行序不同、类别有缺,会错位,"
                        "而错位算出来的增长率看着完全正常。"),
        "schema": {"type": "object", "properties": {
            "dataset": dict(_DATASET, description="本期数据的句柄"),
            "rows": dict(_ROWS, description="本期数据行"),
            "dataset_b": dict(_DATASET, description="对比期(上期/去年同期)数据的句柄"),
            "rows_b": dict(_ROWS, description="对比期数据行"),
            "key": {"type": "string", "description": "对齐用的类别列,如 渠道 / 日期 / 票种"},
            "value": {"type": "string", "description": "要比较的数值列,如 销售额 / 客流量"}},
            "required": ["key", "value"]},
    },
    "sys_table_pivot": {
        "fn": builtin_table_pivot,
        "description": ("透视表:行维度 × 列维度 × 数值。回答「哪个渠道在哪个月掉了」这类交叉问题。"
                        "用一维分组答不了交叉问题,而把一维结果在脑子里拼成二维很容易拼错。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "index": {"type": "string", "description": "行维度列名"},
            "columns": {"type": "string", "description": "列维度列名;留空=只按行维度汇总"},
            "value": {"type": "string", "description": "要聚合的数值列"},
            "agg": {"type": "string", "enum": ["sum", "mean", "count", "max", "min", "median"],
                    "description": "聚合方式,默认 sum"}},
            "required": ["index", "value"]},
    },
    "sys_table_outliers": {
        "fn": builtin_table_outliers,
        "description": ("按分布挑出明显偏离的行(IQR 或 z-score)。要「指出异常」时用它 —— "
                        "最大的几行不等于异常(旺季最大是正常的),靠眼睛挑会挑错。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "column": {"type": "string", "description": "要检测的数值列"},
            "method": {"type": "string", "enum": ["iqr", "zscore"],
                       "description": "iqr=四分位法(默认,对偏态数据更稳);zscore=标准差法"},
            "label": {"type": "string", "description": "结果里带上这一列作为标识,如 日期 / 渠道"}},
            "required": ["column"]},
    },
    "sys_table_trend": {
        "fn": builtin_table_trend,
        "description": ("时间序列:按日/周/月汇总,给出逐期环比与整体趋势方向。"
                        "看走势用它 —— 直接比首尾两行会被单日波动带偏。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "date_column": {"type": "string", "description": "日期列名"},
            "value": {"type": "string", "description": "数值列名"},
            "freq": {"type": "string", "enum": ["day", "week", "month"],
                     "description": "汇总粒度,默认 day"}},
            "required": ["date_column", "value"]},
    },
    "sys_table_search": {
        "fn": builtin_table_search,
        "description": ("在数据里按关键词找行(任意列包含即命中,不区分大小写)。"
                        "只知道用户提了「携程」但不确定在哪一列、叫什么名时用它;"
                        "找不到会给出各列的实际取值,好换个词再找。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "keyword": {"type": "string", "description": "要找的关键词"},
            "columns": {"type": "string", "description": "只在这几列里找,逗号分隔;留空=全部列"},
            "top": {"type": "integer", "description": "最多回几行,默认 50"}},
            "required": ["keyword"]},
    },
    "sys_calc": {
        "fn": builtin_calc,
        "description": ("算一个数学表达式。**凡是要写进最终结论的数字都过一遍它** —— "
                        "自己心算多位数乘除和百分比会错,而且错得很自然:量级和位数都对,没人会去验算。"),
        "schema": {"type": "object", "properties": {
            "expression": {"type": "string",
                           "description": '表达式,如 (386.2-354.1)/354.1*100 或 round(1203560/2456*100, 2)'}},
            "required": ["expression"]},
    },
    "sys_metric_delta": {
        "fn": builtin_metric_delta,
        "description": ("算同比 / 环比 / 占比 / 达成率 / 差额。**这几个口径一律用它,不要自己写表达式** —— "
                        "最容易错的不是算术,是分母取谁:(今-去)/今 和 (今-去)/去 都能得出一个像样的"
                        "百分比,报告里看不出区别。分母为 0 时它会明确告诉你「没有百分比可言」,"
                        "而不是给一个 0% 或 100% 让你照抄。"),
        "schema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["同比", "环比", "占比", "达成率", "差额"],
                     "description": "要算哪个口径"},
            "current": {"type": "number", "description": "本期值(占比时=部分量,达成率时=实际值)"},
            "previous": {"type": "number", "description": "对比期值(同比=去年同期,环比=上一期;算差额也用它)"},
            "total": {"type": "number", "description": "总量(只有算占比时要)"},
            "target": {"type": "number", "description": "目标值(只有算达成率时要)"},
            "digits": {"type": "integer", "description": "小数位,默认 2"}},
            "required": ["kind", "current"]},
    },
    "sys_datasets": {
        "fn": builtin_datasets,
        "description": ("列出本次任务已留存的数据集(句柄/来源工具/行数/列名)。"
                        "忘了句柄号、或者数据工具报「数据集不存在」时,先调它看看有什么。"),
        "schema": {"type": "object", "properties": {}},
    },
    "sys_json_peek": {
        "fn": builtin_json_peek,
        "description": ("看一坨 JSON 的结构(有哪些键、各是什么类型),也可按 path 取其中一段。"
                        "接口返回嵌套很深、不确定数据在哪一层时先调它 —— "
                        "猜错层级会取到空,然后被当成「没有数据」。"),
        "schema": {"type": "object", "properties": {
            "data": {"description": "要查看的 JSON(对象/数组/JSON 文本都行)"},
            "path": {"type": "string", "description": "要取的路径,形如 data.list;留空=看根结构"},
            "max_keys": {"type": "integer", "description": "每层最多列几个键,默认 40"}},
            "required": ["data"]},
    },
    "sys_chart": {
        "fn": builtin_chart,
        "description": ("画一张图直接展示给用户,支持 20 种图形。"
                        "**用户要「图」「可视化」「曲线」「饼图」「雷达图」「漏斗」时调它** —— "
                        "系统会真的渲染出图形,不要用字符拼 ASCII 图糊弄。"
                        "你只指定用哪一列做标签、哪几列做数值,数字由服务端从数据里取,不用你抄。\n"
                        "**按读图的人要做什么来挑**(挑错的图不会报错,只会把一份对的数据"
                        "画成一张误导人的图,而图出来了、颜色也对,没人会怀疑它):\n" + _CHART_MENU + "\n"
                        "几条硬规矩:①「一个数」不要画图,直接在正文里写出来;"
                        "② 类别超过 7~8 个就别用饼图,改 horizontal_bar 或 treemap;"
                        "③ 同一行里的「量」和它的「率」(票数 + 变化率、销售额 + 环比、占比 + TGI)用 combo 双轴;"
                        "两个互不相干、量级差很远的指标(票房 vs 评分)画两张图,不要硬塞进一张;"
                        "④ 问「各自多少」用 bar,问「合起来怎么构成」才用 stacked_bar —— "
                        "堆叠图上除了最底下那条,其余系列都不从 0 起,肉眼比不了大小;"
                        "⑤ **一张图只画一个维度**:数据里有「标签名/维度」这类分组列时(收入、消费、人生阶段各一份占比),"
                        "用 filter 只画用户问的那一组,不要把各组的类别堆在同一根轴上。"),
        "schema": {"type": "object", "properties": {
            "dataset": _DATASET, "rows": _ROWS,
            "chart_type": {"type": "string",
                           "enum": ["line", "area", "stacked_area", "bar", "stacked_bar",
                                    "horizontal_bar", "combo", "pie", "donut", "radar", "scatter",
                                    "funnel", "gauge", "treemap", "sankey",
                                    "heatmap", "calendar", "boxplot", "sunburst", "themeriver"],
                           "description": _CHART_MENU},
            "title": {"type": "string", "description": "图表标题"},
            "label_column": {"type": "string",
                             "description": "做标签的列。一般是日期 / 渠道 / 指标名;"
                                            "sankey 写成「携程→成人票」,sunburst 写成「一级/二级」,"
                                            "scatter 这里要填**数值列**(它是横轴)"},
            "value_columns": {"type": "string",
                              "description": "数值列,逗号分隔。line/bar/radar/heatmap 可给多列;"
                                             "pie/donut/funnel/treemap/gauge/sankey 只用第一列;"
                                             "combo 这里填「量」(画柱,左轴)"},
            "line_columns": {"type": "string",
                             "description": "仅 combo:「率」的列(画线,右轴),逗号分隔。"
                                            "如 value_columns=销售票数、line_columns=票数变化率"},
            "marks": {"type": "string",
                      "description": "可选,走势/比较类图上的标注,逗号分隔:max 标最高点、min 标最低点、"
                                     "average 画均值线。问「峰值/低谷/高于平均吗」时加上"},
            "baseline": {"type": "number",
                         "description": "可选,一条固定参考线的数值:TGI 填 100、目标值、及格线。combo 图上这条线画在右轴(率)上"},
            "baseline_label": {"type": "string", "description": "参考线的名字,如「全国基准」「目标」"},
            "filter": {"type": "string",
                       "description": "可选,只画其中一组,写成「列名=值」,如 标签名=人生阶段。"
                                      "数据里混着多个维度(收入、消费水平、人生阶段…各自一份占比)时必须用它,"
                                      "一张图只画一个维度"}},
            "required": ["chart_type", "label_column", "value_columns"]},
    },
    "sys_read_file": {
        "fn": builtin_read_file,
        "description": ("读用户上传的文件:Excel / CSV / Word / PDF / 图片(OCR)。"
                        "**表格类会变成数据集句柄**(不是文本)—— 之后用 sys_table_query / "
                        "sys_table_stats / sys_chart 按句柄处理,几千行也不会撑爆上下文;"
                        "文档和图片才返回文本。用户提到「我上传的文件/这份表/这个附件」时调它。"),
        "schema": {"type": "object", "properties": {
            "file_id": {"type": "string", "description": "文件编号(用户上传后系统给出的那个)"},
            "sheet": {"type": "string", "description": "只读 Excel 的某个工作表;留空=第一个"}},
            "required": ["file_id"]},
    },
    "sys_supplier_ids": {
        "fn": builtin_supplier_ids,
        "description": ("查当前景区的供应商 ID 列表。票务类工具的 supplierId **由系统自动调它补齐**,"
                        "调票务工具时不需要先调它;只有用户直接问「这个景区的供应商 ID 是多少」时才用。"),
        "schema": {"type": "object", "properties": {
            "scenic_id": {"type": "string", "description": "景区 ID;系统按当前会话自动填"}}},
    },
    "sys_quickbi_link": {
        "fn": builtin_quickbi_link,
        "description": ("获取本景区的 BI 看板地址(客群画像 / 舆情)。"
                        "当 Quick BI 问数或解读调用失败时,用它给出可直接打开的看板链接作为兜底。"),
        "schema": {"type": "object", "properties": {
            "scenic_id": {"type": "string", "description": "景区 ID;留空=当前账号所属景区"}}},
    },
}


# ── 被内置工具取代的历史工具行 ──────────────────────────────────────────
#
# 早期有人在 Tool 中心里按 `type=builtin` 手建过几行(「时间对齐」「数值计算」),
# 想的是「把内置能力也登记一份」。但 type=builtin 的分发是
# `builtin_tools.get(spec.name)` —— **按工具名去这张注册表里找函数**,
# 而注册表的键是 sys_date_range / sys_calc,中文名一个都找不到。
# 所以那几行从建出来的第一天起就调不通:真被技能绑上去,只会在运行时得到
# 「内置工具『时间对齐』不存在」。平时看不出来,是因为真正的内置工具本来就
# 自动注入给每个技能了,模型用的一直是那一份。
#
# 这里把旧名映射到对应的内置工具:已经绑了它们的技能照常能跑(解析时会换成内置的
# 那一份并去重),同时后台列表上会标出「已被系统内置工具取代」,让运维知道可以下线它。
# **只映射,不自动删行** —— 删除是运维的决定,而且那几行上可能写过他们自己的口径说明。
SUPERSEDED: dict[str, str] = {
    "时间对齐": "sys_date_range",
    "时间对其": "sys_date_range",      # 现场确实有写成「对其」的
    "数值计算": "sys_calc",
    "数学计算": "sys_calc",
    # 票务类前置:按景区取供应商 ID(v3.52 起内置)
    "全局-通过景区ID获取供应商ID列表": "sys_supplier_ids",
    "通过景区ID获取供应商ID列表": "sys_supplier_ids",
}


def superseded_by(name: str) -> str | None:
    """这个历史工具名是否已被某个内置工具取代;不是则返回 None。"""
    n = (name or "").strip()
    if n in BUILTIN:
        return None
    return SUPERSEDED.get(n)


def get(name: str) -> Callable | None:
    spec = BUILTIN.get(name)
    if spec is None:
        alias = SUPERSEDED.get((name or "").strip())
        spec = BUILTIN.get(alias) if alias else None
    return spec["fn"] if spec else None


def function_schemas() -> list[dict]:
    """内置工具的 OpenAI function schema 列表。"""
    return [{"type": "function", "function": {
        "name": name, "description": spec["description"], "parameters": spec["schema"]}}
        for name, spec in BUILTIN.items()]
