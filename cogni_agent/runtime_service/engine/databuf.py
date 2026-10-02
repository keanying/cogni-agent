# -*- coding: utf-8 -*-
"""任务数据留存:把工具返回的大结果留在服务端,只把句柄给模型。

**为什么需要它**。工具返回上千行时,`compress_or_truncate` 会把它压成一段几百字的
数据摘要(合计、占比、区间、样本行)再给模型 —— 这一步是对的,不压就会超上下文,
模型看到半截 JSON 还一本正经地作答。

但压完之后**原始行就丢了**。于是内置的 `sys_table_query` 出现一个很尴尬的处境:
它要求模型把 `rows` 传进来,而模型手里只有那段摘要 —— 真正需要筛选统计的那种大结果,
恰恰是它够不着的。想按渠道分组求和?摘要里没有明细。想看 TOP 20?样本行只有 5 条。
模型这时通常会**照着摘要里的数字编一个看起来合理的答案**,不报错、不自知。

所以把整份结果留在这里,给模型一个句柄(ds_1、ds_2……),摘要末尾写清
「完整数据已留存,句柄 ds_1,共 1842 行」。模型要算什么,就带着句柄调
sys_table_query / sys_table_stats / sys_table_pivot ——
上千行一次都没有进过上下文,却始终可查。

几条刻意的边界:

- **只在内存、只在本进程**:跟 cronjobs 的注册表同一套假设(单副本)。
  多副本时最坏情况是句柄取不到 —— 那时工具会明确说「数据集不存在」并让模型重新取数,
  而不是拿一份空数据算出一个 0 来。**不会算错,只会重来**,这是这里唯一要守住的。
- **随任务结束清掉**:数据是业务明细,不该在内存里留过夜。同时按数量和总行数封顶,
  防止一个任务里连着调十次大接口把内存吃光。
- **不落库**:落库就要考虑清理、权限、租户隔离,而这份数据本来就只在一次任务里有意义。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from cogni_agent.common.logging import get_logger

log = get_logger("runtime_service.databuf")

# 一个任务里最多留几份数据集。超了丢最早的 —— 模型通常在用最近取到的那份。
MAX_DATASETS_PER_TASK = 8
# 单份最多留多少行。再多也没有分析价值,而内存是所有任务共用的。
MAX_ROWS_PER_DATASET = 50_000
# 一个任务最多画几张图。再多前端也滚不完,而每张都要占一份 option
MAX_CHARTS_PER_TASK = 8
# 多久没动过就算过期(秒)。任务正常结束时会主动清,这里是兜底 ——
# 任务异常退出时没人来清,留着就一直占着。
TTL_SECONDS = 30 * 60

_LOCK = threading.Lock()
# task_id → {handle: {"rows", "tool", "columns", "at"}}
_STORE: dict[str, dict[str, dict]] = {}
# task_id → 已经发过几个句柄(句柄号只增不减,不复用 —— 复用会让模型
# 拿着上一份数据的号去查新数据,而两份的列名往往一样,看不出错)
_SEQ: dict[str, int] = {}


def _evict_expired(now: float) -> None:
    """清掉过期任务。调用方必须已持有 _LOCK。"""
    dead = [tid for tid, bag in _STORE.items()
            if not bag or max(d["at"] for d in bag.values()) + TTL_SECONDS < now]
    for tid in dead:
        _STORE.pop(tid, None)
        _SEQ.pop(tid, None)


def stash(task_id: str | None, tool_name: str, rows: list[dict]) -> dict | None:
    """留存一份数据,返回 {"handle", "row_count", "columns", "truncated"}。

    task_id 为空(操练台单跑、没有任务上下文)时返回 None —— 没有归属就没有清理时机,
    留下来就是泄漏。调用方拿到 None 就当没有这个能力,照常给摘要。
    """
    if not task_id or not rows:
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None
    truncated = len(rows) > MAX_ROWS_PER_DATASET
    kept = rows[:MAX_ROWS_PER_DATASET]
    # 列名取前 50 行的并集:有些接口的行不整齐,只看第一行会漏列,
    # 而模型是照着这个列名清单写 filter 的 —— 漏一列它就会用一个不存在的列名
    columns: list[str] = []
    for r in kept[:50]:
        for k in r:
            if k not in columns:
                columns.append(str(k))
    now = time.time()
    with _LOCK:
        _evict_expired(now)
        bag = _STORE.setdefault(task_id, {})
        seq = _SEQ.get(task_id, 0) + 1
        _SEQ[task_id] = seq
        handle = f"ds_{seq}"
        bag[handle] = {"rows": kept, "tool": tool_name, "columns": columns, "at": now}
        while len(bag) > MAX_DATASETS_PER_TASK:
            bag.pop(next(iter(bag)))         # dict 保序,第一个就是最早的
    log.debug(f"任务 {task_id} 留存数据集 {handle}:{len(kept)} 行,来自 {tool_name}")
    return {"handle": handle, "row_count": len(kept), "columns": columns,
            "truncated": truncated}


def fetch(task_id: str | None, handle: str) -> list[dict] | None:
    """按句柄取回整份数据。取不到返回 None(过期、清了、或者根本没这个号)。"""
    if not task_id or not handle:
        return None
    with _LOCK:
        item = (_STORE.get(task_id) or {}).get(str(handle).strip())
        if item is None:
            return None
        item["at"] = time.time()             # 用过就续命,别在分析到一半时过期
        return item["rows"]


def catalog(task_id: str | None) -> list[dict]:
    """这个任务当前留着哪些数据集。模型靠它知道有哪些句柄可用。"""
    if not task_id:
        return []
    with _LOCK:
        bag = _STORE.get(task_id) or {}
        return [{"handle": h, "tool": d["tool"], "row_count": len(d["rows"]),
                 "columns": d["columns"]} for h, d in bag.items()]


def clear(task_id: str | None) -> None:
    """任务结束时清掉。业务明细不该在内存里留过夜。"""
    if not task_id:
        return
    with _LOCK:
        _STORE.pop(task_id, None)
        _SEQ.pop(task_id, None)
        _CHARTS.pop(task_id, None)


def resolve_rows(task_id: str | None, dataset: str = "", rows: Any = None) -> tuple[list[dict], str]:
    """内置数据工具的统一取数口:给了句柄就按句柄取,否则用模型直接传的 rows。

    返回 (行, 错误说明)。错误说明非空时调用方应当原样回给模型 ——
    **话要说清楚**:「数据集 ds_3 不存在」加上现有句柄清单,模型才知道该换哪个号;
    只说一句「没有数据」的话,它会以为这份数据是空的,然后回一个「本期无数据」。
    """
    if dataset:
        got = fetch(task_id, dataset)
        if got is None:
            have = [c["handle"] for c in catalog(task_id)]
            return [], (f"数据集「{dataset}」不存在或已过期。"
                        + (f"当前可用的数据集:{'、'.join(have)}" if have else
                           "当前没有任何已留存的数据集 —— 请先调用取数工具拿到数据。")
                        + "(注意:这不代表数据为空,不要据此回答「无数据」)")
        return got, ""
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)], ""
    return [], ("没有可处理的数据:既没给 dataset 句柄,也没给 rows。"
                "请先调用取数工具;它返回的数据摘要末尾会告诉你句柄号。")

# ── 图表暂存 ────────────────────────────────────────────────────────
#
# 为什么图表也要走服务端暂存,而不是让模型把数据写进回答里:
#
# 系统本来就能画图 —— structured_output 解析回答里的 ```json``` 块,
# chart_templates 把它填成 ECharts option,前端用 recharts 渲染,
# line / bar / pie / radar 四种都支持。但那条路要求**模型把每一个数字重新抄一遍**,
# 而抄几十个数字这件事,模型做不对:它会把 21085 写成 21085.0、把 3.6234 四舍五入成 3.6,
# 甚至凭印象改一两位 —— 图画出来了,形状也对,数却和取回来的那份对不上。没人会去核对。
#
# 所以让 sys_chart 直接从留存的数据集里取数、在服务端把图配好、存在这里,
# 收尾时和模型自己写的结构化图表合并广播。模型只负责说「画哪几列」,不碰数字。
_CHARTS: dict[str, list[dict]] = {}


def stash_chart(task_id: str | None, chart: dict) -> int:
    """存一张已经配好的图。返回这个任务当前存了几张。"""
    if not task_id or not chart:
        return 0
    with _LOCK:
        bag = _CHARTS.setdefault(task_id, [])
        if len(bag) >= MAX_CHARTS_PER_TASK:
            return len(bag)
        bag.append(chart)
        return len(bag)


def charts(task_id: str | None) -> list[dict]:
    """取出这个任务画好的图(收尾广播用)。"""
    if not task_id:
        return []
    with _LOCK:
        return list(_CHARTS.get(task_id) or [])
