"""业务时间基准(北京时间 / 东八区)。

全系统业务时间统一北京时间:业务日 biz_date、参数模板日期占位符({{today}}
/{{now}}/{{yesterday}}…)、预热调度窗口,都按东八区取「今天」。

服务器与容器系统时区恒为 UTC,直接 date.today() / datetime.now() 取到的是 UTC
日期 —— 东八区 00:00~08:00 这一段时间会被算成前一天,结果是:
- 预热按「那天」建行、APP 按「这天」查行,两边日期对不上 → 当日行永远命中不了;
- 参数模板给模型灌的是昨天的日期 → 取数窗口整体错一天。

业务侧的「今天 / 现在」一律走本模块,不再各写各的。DB 存储层仍按 UTC 存
(SQLAlchemy DateTime(timezone=True) 落库即 UTC),只是换算口径收口到这里。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

CST = timezone(timedelta(hours=8), name="CST")


def now_cst() -> datetime:
    """当前北京时间(带 +08:00 时区,aware)。"""
    return datetime.now(CST)


def today_cst() -> date:
    """当前北京日期。"""
    return now_cst().date()


def today_start_cst() -> datetime:
    """北京当日 00:00(aware,可直接与 TIMESTAMPTZ 列比较)。"""
    return datetime.combine(today_cst(), datetime.min.time(), tzinfo=CST)


def to_cst(moment: datetime | None) -> datetime | None:
    """换算到北京时间。naive 输入按 UTC 解释(与 utcnow() 写入口径一致)。"""
    if moment is None:
        return None
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment).astimezone(CST)
