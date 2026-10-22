# -*- coding: utf-8 -*-
"""tc_qbi_runs 运行记录保留:每天北京时间过了凌晨 1 点后,删掉 N 天前的行(一天一次)。

**为什么要有它**:这张表只增不减,而撑大它的不是行数,是 `debug`(全量执行留痕,
平均几十 KB/行)和 `result`(正文 + 图表)两列 —— 参考环境实测日均 ~22 MB。
不清理的话,运行记录列表一天比一天慢,磁盘也一路涨上去。

**为什么删整行而不是把 debug 置空**:统计口径(最近运行状态/时间)已经冗余记在问题行上
(last_run_status / last_run_time),只剩统计列的空壳行没有别的读者,留着只是占位。

**保留天数为什么不能随便调小**(config: quickbi.run_retention_days,默认 7,0 = 不清理):
这张表同时是「加速(预热)缓存」本身 —— 缓存 = 存储时间内最近一次成功运行,见
service.cached_run。存储时间上限是 7 天(_validate_fixed),保留天数小于它就会把还在
有效期内的缓存删掉,表现成「开了加速却每次现跑」。所以这里按 7 天封底。

**到点判定为什么用「已过 1 点」而不是「hour == 1」**:心跳是几分钟一拍,
「整点」不是一个能命中的时刻 —— 1:59 触发的那一拍进函数时已经 2:00,相等判定会漏掉一整天;
pod 重启、某一拍失败同理。判的是「该不该跑」,不是「是不是正好这一分钟」。

删掉之后的影响:
- 问题行上的 last_run_id 是软引用(没有外键),删行不报错;只是 N 天前的运行点进去
  查不到详情 —— 列表上的状态走冗余列,不受影响;
- 「查看思考」取的是最近一次成功运行,N 天内都没成功过的问题才会查不到 —— 那种问题
  本来就该重跑;
- quickbi_prewarm 判「最近有人问过的景区」也读这张表,窗口超过 N 天的部分会被截短。

执行语义:
- 今天跑过就跳过(进程内存态;重启后当天可能再跑一次 —— 删除幂等,重跑无害);
- 分批删,每批一个小事务:一次 DELETE 几万行会长时间持锁 + WAL 暴涨;
- 单轮撞到上限**不算当天已完成**,下一拍接着删,直到追平(首次上线历史量大时靠这个);
- 删完不主动 VACUUM:autovacuum 会回收,空间被后续写入复用。想让磁盘真正缩回去,
  挑低峰手工 `VACUUM (VERBOSE) numeri_core_tc_qbi_runs;`(不要 FULL,那是排他锁)。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text

from cogni_agent.common.db import get_sessionmaker, table_name
from cogni_agent.common.logging import get_logger
from cogni_agent.common.time import now_cst
from cogni_agent.config import get_settings

log = get_logger("cronjobs.qbi_runs_retention")

DEFAULT_RETENTION_DAYS = 7
MIN_RETENTION_DAYS = 7      # 不能小于加速存储时间上限(7 天),理由见文件头
RUN_HOUR_CST = 1            # 每天过了这个点(北京时间)就执行
BATCH_SIZE = 500            # 每批删多少行
MAX_ROWS_PER_ROUND = 50000  # 单轮上限:首轮历史量大时不至于一轮跑太久

# 今天是否已执行过(进程内存态;重启丢失只会多跑一次,删除幂等)
_last_run_date: date | None = None

# 分批删:CTE 只挑 id —— 不是 SELECT *。整行里有 debug / result 两个大列,整行取出来
# 等于把 TOAST 全读一遍再扔掉。只取 id 时 CTE 走 created_at 索引顺序扫,DELETE 按主键回删。
_DELETE_SQL = text(
    "WITH doomed AS ("
    f"    SELECT id FROM {table_name('tc_qbi_runs')}"
    "    WHERE created_at < now() - make_interval(days => :days)"
    "    ORDER BY created_at"
    "    LIMIT :lim"
    ")"
    f"DELETE FROM {table_name('tc_qbi_runs')} WHERE id IN (SELECT id FROM doomed)"
)


def retention_days() -> int:
    """配置的保留天数;0 = 不清理。小于封底值时按封底值算(并不报错:配小了不该让任务停摆)。"""
    raw = get_settings().get("quickbi", "run_retention_days", default=DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    if days <= 0:
        return 0
    return max(days, MIN_RETENTION_DAYS)


def due(now: datetime, last_run: date | None) -> bool:
    """这一拍该不该跑:北京时间已过当日 RUN_HOUR_CST 点,且今天还没跑过。"""
    if now.hour < RUN_HOUR_CST:
        return False
    return last_run != now.date()


async def _delete_expired(days: int) -> int:
    total = 0
    while total < MAX_ROWS_PER_ROUND:
        async with get_sessionmaker()() as db:
            deleted = (await db.execute(_DELETE_SQL, {"days": days, "lim": BATCH_SIZE})).rowcount
            await db.commit()
        total += deleted
        if deleted == 0:
            break
    return total


async def run(*, force: bool = False) -> int:
    """删掉保留天数之前的运行记录,返回删除行数。force=True 跳过到点判定(运维立刻清一轮用)。"""
    global _last_run_date

    days = retention_days()
    if not days:
        return 0
    now = now_cst()
    if not force and not due(now, _last_run_date):
        return 0

    total = await _delete_expired(days)
    if total >= MAX_ROWS_PER_ROUND:
        # 撞到上限 = 还没删干净:不记「今天已完成」,下一拍接着追平
        log.warning(f"cronjob[qbi_runs_retention] 本轮删到上限 {MAX_ROWS_PER_ROUND} 行,"
                    f"仍有 {days} 天前的数据,下一拍继续")
        return total
    _last_run_date = now.date()
    log.info(f"cronjob[qbi_runs_retention] 删除 {total} 行 {days} 天前的运行记录")
    return total
