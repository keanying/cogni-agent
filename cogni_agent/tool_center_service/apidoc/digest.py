# -*- coding: utf-8 -*-
"""工具结果压缩:上千行的返回,先算再给,而不是原样塞给模型。

**要解决的问题**:一次数据查询返回几百上千行是常态。把它原样序列化进对话,
有两种死法 —— ① 超出上下文被截断,模型看到半截 JSON,然后一本正经地基于半截数据作答;
② 没超但占满,后面的追问全被挤掉。而且这两种都不报错。

所以这里在「工具返回」和「给模型看」之间加一层:

    原始返回 → 找到数据体 → DataFrame → 摘要(行数/列/数值统计/TopN/时间范围)
                                      → 采样若干行(首尾各几行,而不是只取前 N 行)
                                      → 一段结构化的中文说明

给模型的是**摘要 + 样本**,不是全量。要看某一列的明细,模型可以带筛选条件再调一次 ——
这比让它在几千行里自己找强得多,也是人做这件事的方式。

为什么用 pandas/numpy 而不是手写循环:分组、求和、占比、排序、时间解析这些,
手写一遍就是重新实现一遍 pandas,而且必然在边界上写错(空值、混合类型、时区)。
"""
from __future__ import annotations

import json
from typing import Any

from cogni_agent.common.logging import get_logger

log = get_logger("tool_center_service.apidoc.digest")

# 摘要里最多列几行样本。首尾各一半 —— 只取前 N 行的话,按时间倒序的数据永远看不到最早那几条
MAX_SAMPLE_ROWS = 12
# 类别列最多列几个取值
MAX_CATEGORIES = 8
# 最多详细统计几列。超出的只报列名 —— 宽表逐列统计会让摘要本身撑爆上下文
MAX_COLUMNS = 25
# 触发压缩的行数门槛:少于这个数就原样给,压缩反而丢信息
COMPRESS_MIN_ROWS = 20


def _dig(obj: Any, path: list[str]) -> Any:
    """按 ["data","list"] 之类的路径取数据体;路径不对就原样返回。"""
    cur = obj
    for seg in path or []:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and cur and isinstance(cur[0], dict) and seg in cur[0]:
            # data[].profile 这种:一层列表里再取同名字段,拍平
            out = []
            for item in cur:
                v = item.get(seg) if isinstance(item, dict) else None
                out.extend(v if isinstance(v, list) else ([v] if v is not None else []))
            cur = out
        else:
            return cur
    return cur


def _find_rows(payload: Any, data_path: list[str] | None) -> tuple[list[dict], list[str]]:
    """找出「那一堆行」。返回 (行, 实际用的路径)。

    先按文档给的 data_path 找;找不到就在整个返回体里挑**最长的那个字典列表** ——
    绝大多数接口的数据体就是它,而猜错的代价只是摘要不够贴切,不会出错。
    """
    if data_path:
        got = _dig(payload, data_path)
        rows = _as_rows(got)
        if rows:
            return rows, list(data_path)
    best: list[dict] = []
    best_path: list[str] = []

    def walk(node: Any, path: list[str], depth: int = 0) -> None:
        nonlocal best, best_path
        if depth > 4:
            return
        rows = _as_rows(node)
        if len(rows) > len(best):
            best, best_path = rows, list(path)
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [k], depth + 1)
        elif isinstance(node, list) and node and isinstance(node[0], (dict, list)):
            walk(node[0], path + ["[]"], depth + 1)

    walk(payload, [])
    return best, best_path


def _as_rows(node: Any) -> list[dict]:
    if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
        return node
    return []


def _fmt(v: Any) -> Any:
    """数值保留两位;其它原样(但截断超长字符串,URL 这类没必要整条进上下文)。"""
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, str) and len(v) > 120:
        return v[:120] + "…"
    return v


def digest_rows(rows: list[dict], *, field_desc: dict[str, str] | None = None,
                max_sample: int = MAX_SAMPLE_ROWS) -> dict:
    """用 pandas 把一堆行压成「摘要 + 样本」。

    摘要里放什么,是按「人拿到这份数据会先看什么」定的:
      - 多少行、有哪些列(带文档里的中文释义 —— 没有释义的话 amt/cnt 这种列名没人看得懂)
      - 数值列:合计 / 均值 / 最大最小(带出现在哪一行)
      - 类别列:取值分布的前几名与占比
      - 时间列:覆盖的时间范围
    """
    import pandas as pd

    df = pd.DataFrame(rows)
    field_desc = field_desc or {}
    out: dict[str, Any] = {"row_count": int(len(df)), "columns": []}

    # 宽表限流:一份 60 列的数据,逐列统计就是 60 行文字,再配上每行 60 个字段的样本,
    # 摘要本身就上万字 —— 压缩完照样撑爆上下文(实测 5000×60 压出来 13,257 字)。
    # 列太多时只详细统计前 MAX_COLUMNS 列,其余只报列名:模型知道有这些列就够了,
    # 真要看某一列,带着列名调 sys_table_query 就是。
    all_cols = [str(c) for c in df.columns]
    shown_cols = list(df.columns[:MAX_COLUMNS])
    if len(all_cols) > MAX_COLUMNS:
        out["omitted_columns"] = all_cols[MAX_COLUMNS:]

    for col in shown_cols:
        s = df[col]
        info: dict[str, Any] = {"name": str(col)}
        if field_desc.get(str(col)):
            info["desc"] = field_desc[str(col)]
        non_null = s.dropna()
        info["null_count"] = int(len(s) - len(non_null))
        if non_null.empty:
            info["kind"] = "empty"
            out["columns"].append(info)
            continue
        num = pd.to_numeric(non_null, errors="coerce")
        if num.notna().mean() > 0.9:            # 九成能转成数字就当数值列
            info["kind"] = "number"
            info["sum"] = _fmt(float(num.sum()))
            info["mean"] = _fmt(float(num.mean()))
            info["min"] = _fmt(float(num.min()))
            info["max"] = _fmt(float(num.max()))
        else:
            as_str = non_null.astype(str)
            uniq = as_str.nunique()
            info["unique"] = int(uniq)
            if uniq <= max(MAX_CATEGORIES * 4, 40):
                info["kind"] = "category"
                vc = as_str.value_counts().head(MAX_CATEGORIES)
                total = int(len(as_str))
                info["top"] = [{"value": _fmt(k), "count": int(v),
                                "ratio": f"{v / total * 100:.1f}%"} for k, v in vc.items()]
            else:
                info["kind"] = "text"
                info["sample_values"] = [_fmt(v) for v in as_str.head(3).tolist()]
        out["columns"].append(info)

    # 样本行数随列宽收缩。撑爆上下文的从来不是行数或列数单独一个,而是**两者相乘**:
    # 12 行 × 60 列 = 720 个字段值。宽表少给几行,窄表照旧 —— 反正样本只是让模型
    # 看清「这数据长什么样」,不是拿来算数的(算数走句柄)。
    if len(sample_wide := shown_cols) and len(sample_wide) > 8:
        max_sample = max(4, min(max_sample, (MAX_SAMPLE_ROWS * 8) // len(sample_wide)))
    # 样本:首尾各一半。只取前 N 行的话,按时间倒序的数据永远看不到最早那几条
    if len(df) <= max_sample:
        sample = df
    else:
        head = df.head(max_sample // 2)
        tail = df.tail(max_sample - max_sample // 2)
        sample = pd.concat([head, tail])
    # **常量列不进样本行**。真实数据里这类列很多 —— 景区 ID、景区名、口径、ETL 时间
    # 全表只有一个值,却在每一行里重复一遍。实测那份 216 行的客源数据,7 列里有 4 列是常量,
    # 样本行的绝大部分字节都花在重复这 4 个值上。上面的列统计已经写明「1 个取值 —— XXX」,
    # 样本里再抄 12 遍没有任何新信息,只是把上下文挤掉。
    const_cols = [c for c in shown_cols if len(df) > 1 and df[c].astype(str).nunique() == 1]
    sample_cols = [c for c in shown_cols if c not in const_cols]
    if const_cols:
        # 值也要过一遍折叠:常量列本身可能是个嵌套数组(分组接口里很常见),
        # 原样贴出来就是几千字 —— 而它恰恰是「这里还有一层」的信号,一句 [50 项] 就够
        out["constant_columns"] = {str(c): _collapse(df[c].iloc[0]) for c in const_cols}
    sample = sample[sample_cols] if sample_cols else sample[shown_cols]
    rows_out = json.loads(sample.to_json(orient="records", force_ascii=False))
    # **嵌套值折叠**。行里套着数组/对象时(分组接口很常见),整坨嵌套会被原样序列化进样本 ——
    # 100 个分组、每组 50 条明细,样本行本身就上万字。折叠成「[50 项]」这样的占位:
    # 模型知道这里有更深一层、该换个 data_path 再取,而不是对着半截嵌套猜。
    out["sample_rows"] = [{k: _collapse(v) for k, v in r.items()} for r in rows_out]
    note = ("已列出全部行" if len(df) <= max_sample
            else f"共 {len(df)} 行,这里只列首尾各几行;"
                 "要看具体某一段请带上筛选条件再调用一次")
    if const_cols:
        note += f";全表恒定的列({'、'.join(str(c) for c in const_cols)})已从样本里省去,取值见上"
    out["sample_note"] = note
    # 叠加探测:某个低基数类别列把数据切成几组,且每组都够大 —— 这通常是
    # 「一个接口把几份不同口径的数据塞进同一个数组」。不点破的话,上面的合计会被当成总量。
    for col in shown_cols:
        if col in const_cols:
            continue
        vals = df[col].astype(str)
        uniq = vals.nunique()
        if 2 <= uniq <= 5 and len(df) >= 20 and vals.value_counts().min() >= 3:
            out["stacked_by"] = {"column": str(col), "groups": int(uniq),
                                 "values": [str(v) for v in vals.value_counts().index[:5]]}
            break
    return out


def _collapse(v: Any) -> Any:
    """把样本行里的嵌套值折叠成一句话。

    不折叠的话,一行里套 50 条明细就会被整坨序列化进样本 —— 而那 50 条既看不全、
    也没法用(要用得换 data_path 重新取)。折叠后模型至少知道「这里还有一层、有多少项」。
    """
    if isinstance(v, list):
        return f"[{len(v)} 项]" if v else "[]"
    if isinstance(v, dict):
        return "{" + "、".join(list(v)[:5]) + ("…}" if len(v) > 5 else "}")
    return _fmt(v)


def extract_rows(payload: Any, response_schema: dict | None = None) -> list[dict]:
    """从工具返回体里把「那一堆行」挑出来(与压缩用的是同一套定位逻辑)。

    给数据留存用:摘要给模型看,整份行留在服务端等模型按句柄来查。
    两边必须用同一个 _find_rows —— 各写一套的话,摘要说的是 A 表、
    句柄里装的是 B 表,而两者行数往往接近,对不上也看不出来。
    """
    rows, _ = _find_rows(payload, (response_schema or {}).get("data_path"))
    return rows


def compress_tool_result(payload: Any, *, response_schema: dict | None = None,
                         max_sample: int = MAX_SAMPLE_ROWS) -> dict | None:
    """工具返回 → 给模型看的压缩结果。不值得压缩(行数少)时返回 None。

    返回 None 的含义是「原样给模型就好」——由调用方决定,这里不替它做主。
    """
    schema = response_schema or {}
    rows, used_path = _find_rows(payload, schema.get("data_path"))
    if len(rows) < COMPRESS_MIN_ROWS:
        return None
    field_desc = {}
    for f in schema.get("fields") or []:
        name = str(f.get("path") or f.get("name") or "")   # path 是现行结构;name 是早先导入的
        leaf = name.split(".")[-1].replace("[]", "")
        if leaf and f.get("desc"):
            field_desc[leaf] = f["desc"]
    try:
        digest = digest_rows(rows, field_desc=field_desc, max_sample=max_sample)
    except Exception as exc:                    # noqa: BLE001 - 压缩失败就退回原样,不能因此丢结果
        log.warning(f"结果压缩失败,退回原样返回:{type(exc).__name__}: {exc}")
        return None
    digest["data_path"] = ".".join(used_path) or "(根)"
    return digest


def render_digest(digest: dict) -> str:
    """压缩结果 → 给模型看的一段文字。

    写成中文段落而不是再扔一坨 JSON:模型对结构化文字的利用率更高,
    而且这段话人也看得懂 —— 排查「模型为什么这么答」时,看的就是这段。
    """
    lines = [f"【数据摘要】共 {digest['row_count']} 行(数据体位置:{digest.get('data_path', '-')})", ""]
    # 全表恒定的列单独说一次,不占逐列统计的篇幅,也不进样本行
    const = digest.get("constant_columns") or {}
    if const:
        lines.append("- 全表恒定:" + "、".join(f"{k}={v}" for k, v in const.items()))
    for col in digest.get("columns", []):
        if col["name"] in const:
            continue          # 上面「全表恒定」那行已经说完了,不必再占一行说「1 个取值」
        name = col["name"] + (f"({col['desc']})" if col.get("desc") else "")
        kind = col.get("kind")
        if kind == "number":
            lines.append(f"- {name}:合计 {col['sum']},均值 {col['mean']},"
                         f"区间 {col['min']} ~ {col['max']}")
        elif kind == "category":
            top = "、".join(f"{t['value']} {t['count']}({t['ratio']})" for t in col.get("top", []))
            lines.append(f"- {name}:{col['unique']} 个取值,占比前几位 —— {top}")
        elif kind == "text":
            lines.append(f"- {name}:{col['unique']} 个不同取值,例如 {col.get('sample_values')}")
        else:
            lines.append(f"- {name}:全为空")
        if col.get("null_count"):
            lines[-1] += f";空值 {col['null_count']} 个"
    lines += ["", f"【样本行】{digest.get('sample_note', '')}",
              json.dumps(digest.get("sample_rows", []), ensure_ascii=False)]
    # 数据留存句柄:**这一段是摘要能用起来的关键**。没有它,模型手里只有上面那些
    # 统计口径和几行样本 —— 想按渠道分组求和、想看 TOP 20,它够不着明细,
    # 于是往往照着摘要里的数字编一个看起来合理的答案,不报错也不自知。
    # 有了句柄,它可以把这件事交给内置的数据工具去算(算得准,而且明细一行都不进上下文)。
    omitted = digest.get("omitted_columns") or []
    if omitted:
        lines += ["", f"(另有 {len(omitted)} 列未逐列统计:{'、'.join(str(c) for c in omitted)};"
                      "需要哪一列就带上列名调 sys_table_query)"]
    # 叠在一起的多份数据:同一个数组里塞了几个口径的行(靠某一列区分)。
    # 这时候上面那些「合计/均值」是**跨口径算的**,多半没有业务含义 ——
    # 现场那份客源数据就是三档(客源城市/客源省份/省内客源市)叠在一起,
    # popularity 的「合计 260.48」是三组百分比加在一块儿,读出来就是个错数,
    # 而它看着完全像个正常的合计。必须说破。
    split = digest.get("stacked_by")
    if split:
        lines += ["", f"⚠ 这份数据按「{split['column']}」叠了 {split['groups']} 组"
                      f"({'、'.join(split['values'])})。上面的合计/均值是**跨组算的**,"
                      "多半没有业务含义;要按组统计请用 "
                      f"sys_table_query(group_by=\"{split['column']}\", ...)"]
    ds = digest.get("dataset") or {}
    if ds.get("handle"):
        lines += ["", f"【完整数据已留存】句柄 {ds['handle']},共 {ds['row_count']} 行"
                      + (";原始数据更多,只留了前这些行" if ds.get("truncated") else "")
                      + f"。列:{'、'.join(ds.get('columns') or [])}",
                  f"要筛选 / 分组统计 / 排序取 TOP N,调 sys_table_query(dataset=\"{ds['handle']}\", ...);"
                  f"要看整体分布调 sys_table_stats,要做两期对比调 sys_table_compare。"
                  f"**用户想看图就调 sys_chart(dataset=\"{ds['handle']}\", ...)**,会真的渲染出图 ——"
                  f"占比用 pie、比大小/看分布用 bar、看走势用 line、多指标看形状用 radar;"
                  f"类别很多时先用 sys_table_query 取 TOP N 再画。"
                  f"**不要凭上面的摘要估算明细** —— 摘要只有合计与样本。"]
    return "\n".join(lines)
