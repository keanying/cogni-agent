# -*- coding: utf-8 -*-
"""工具返回 → 给模型看的文本。**先压缩,压不了才截断。**

从 context.py 拆出来的一组纯函数。拆的理由不是好看:这几个函数只跟「给模型多少字」
有关,却和 langchain 的消息裁剪挤在同一个模块里,于是**没装 langchain 就连它们都导入不了** ——
而它们恰恰是最该被单测钉住的一层(压没压住不会报错,只是模型看到半截数据还一本正经地作答)。

context.py 仍然重新导出这些名字,`ctx.compress_or_truncate(...)` 这类既有写法不受影响。
"""
from __future__ import annotations

import json
from typing import Any

from cogni_agent.config import get_settings

# v1.1.x:不再写「完整结果见任务详情」——完整未截断结果从未落库,任务 result 只有
# answer/usage/timeline,那句提示是不实文案。截断后模型应知晓"这只是摘要/部分内容"即可。
_TRUNCATION_NOTE = "\n\n...[内容过长已截断]"


def _cfg(key: str, default):
    return get_settings().get("runtime", "context", key, default=default)


def compress_or_truncate(content, *, response_schema: dict | None = None, max_chars=None,
                         task_id: str | None = None, tool_name: str = "") -> str:
    """工具返回 → 给模型看的文本。**先压缩,压不了才截断。**

    截断是最后一招,而且很糟:模型看到的是半截 JSON,然后一本正经地基于半截数据作答,
    不报错、不自知。所以先试着按「数据摘要 + 样本行」压一遍 ——
    一千多行的返回能压到一千多字,而且保住的是**统计口径**(合计、占比、区间、时间范围),
    正是人拿到这份数据会先看的东西。

    压缩需要知道「数据体在哪、每列是什么」,这来自工具的 response_schema
    (Markdown 接口文档解析出来的返回参数表)。没有这份信息就退回原来的截断。

    **task_id 决定了压缩之后明细还在不在**。以前压完就把原始行丢了,于是摘要虽然
    省了上下文,模型却再也够不着明细 —— 想按渠道分组求和、想看 TOP 20 都做不到,
    它多半会照着摘要里的合计编一个看着合理的答案。传了 task_id 就把整份行留在
    服务端(databuf),摘要末尾带上句柄,模型用内置数据工具按句柄去算:
    上千行一次都没进过上下文,却始终可查。
    """
    from cogni_agent.tool_center_service.apidoc.digest import compress_tool_result, render_digest

    limit = max_chars if max_chars is not None else _cfg("max_tool_result_chars", 4000)
    raw = content
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if raw is not None:
        as_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        if limit <= 0 or len(as_text) > limit:
            digest = compress_tool_result(raw, response_schema=response_schema)
            if digest is not None:
                stashed = _stash_rows(task_id, tool_name, raw, response_schema)
                if stashed:
                    digest["dataset"] = stashed
                return render_digest(digest)
    return truncate_tool_result(content, max_chars=max_chars)


def _stash_rows(task_id: str | None, tool_name: str, raw, response_schema: dict | None) -> dict | None:
    """把压缩掉的那份明细留在服务端。失败不影响返回 —— 最坏就是回到以前的样子。"""
    if not task_id:
        return None
    try:
        from cogni_agent.tool_center_service.apidoc.digest import extract_rows

        from cogni_agent.runtime_service.engine import databuf

        rows = extract_rows(raw, response_schema)
        return databuf.stash(task_id, tool_name or "(未命名工具)", rows)
    except Exception:                       # noqa: BLE001 - 留存是增强,炸了也不能连累结果
        return None


def truncate_tool_result(content: Any, *, max_chars: int | None = None) -> str:
    """工具结果入 ToolMessage 前统一过这里。返回值一定是字符串。

    短内容原样返回;超长内容优先做结构化摘要(字段名 + 前 N 条 + 数值/分类统计,
    见 _summarize_json),无法结构化(非 JSON)时回落头部硬截断。两种路径都
    明确告知已截断——否则模型会以为自己看到的就是全部,基于不完整数据下结论。
    纯确定性变换,不引入 LLM 调用。
    """
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            content = str(content)

    limit = max_chars if max_chars is not None else _cfg("max_tool_result_chars", 4000)
    if limit <= 0 or len(content) <= limit:
        return content
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        value = None
    if value is not None:
        summarized = _summarize_json(value, limit, _TRUNCATION_NOTE)
        if summarized is not None and len(summarized) <= limit:
            return summarized
    return content[:limit] + _TRUNCATION_NOTE


def _summarize_json(value: Any, limit: int, note: str) -> str | None:
    """把超大 JSON 工具结果压成结构化摘要(字段名 + 前 N 条 + 数值统计)。

    返回 None 表示 value 不适合结构化摘要(调用方回落头部硬截断)。
    """
    if isinstance(value, list):
        return _summarize_list(value, limit, note)
    if isinstance(value, dict):
        # 对象里最大的 list 字段(如 {"data": [...], "total": 120})通常是主要载荷,
        # 对它做列表摘要,其余标量字段原样保留。
        list_fields = [(k, v) for k, v in value.items() if isinstance(v, list) and len(v) > 1]
        if list_fields:
            best_key, best = max(list_fields, key=lambda kv: len(kv[1]))
            inner = _summarize_list(best, limit, note)
            if inner is not None:
                head = {k: v for k, v in value.items() if k != best_key}
                head_text = json.dumps(head, ensure_ascii=False, default=str) if head else ""
                return (head_text + "\n" if head_text else "") + f"{best_key}: {inner}"
        # 无大 list 的对象:键很多时保留前 8 个键值对
        items = list(value.items())
        if len(items) > 8:
            kept = [f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False, default=str)}"
                    for k, v in items[:8]]
            return "{ " + ", ".join(kept) + f", ...(共 {len(items)} 个字段,其余省略){note} }}"
    return None


def _summarize_list(rows: list, limit: int, note: str) -> str | None:
    """数组的结构化摘要:共 N 条 + 数值字段 sum/avg/min/max + 前 K 条明细。

    仅对"记录是 dict"的数组生效;字符串/标量数组无法提炼字段,返回 None 回落截断。
    """
    if not rows or not isinstance(rows[0], dict):
        return None
    fields = list(rows[0].keys())
    total = len(rows)
    # 按单条记录大小自适应展示条数(上限 5),保证摘要主体落在 limit 内
    sample = json.dumps(rows[0], ensure_ascii=False, default=str)
    keep = max(1, min(5, int(limit * 0.5) // max(1, len(sample))))
    shown = rows[:keep]
    lines = [f"[结构化摘要] 共 {total} 条记录,展示前 {min(keep, total)} 条:{note}"]
    stats = []
    dists = []
    for f in fields:
        nums = [r[f] for r in rows
                if isinstance(r, dict) and isinstance(r.get(f), (int, float))
                and not isinstance(r.get(f), bool)]
        if len(nums) >= 2:
            stats.append(f"{f}: sum={sum(nums):g}, avg={sum(nums) / len(nums):.2f}, "
                         f"min={min(nums):g}, max={max(nums):g}")
        else:
            # 枚举/短字符串字段(如 sentiment 情感标签)做分类分布统计——防模型只看
            # 前 K 条明细就猜分布(前 5 条恰好全是"正面"≠ 整体 60% 正面)。
            counts: dict[str, int] = {}
            for r in rows:
                v = r.get(f) if isinstance(r, dict) else None
                if isinstance(v, str) and len(v) <= 12:
                    counts[v] = counts.get(v, 0) + 1
            if 2 <= len(counts) <= 10:
                top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
                dist_text = ", ".join(f"{k}:{v}" for k, v in top)
                if len(counts) > 3:
                    dist_text += ", ..."
                dists.append(f"{f} -> {dist_text}")
    if stats:
        lines.append("数值统计: " + "; ".join(stats))
    if dists:
        lines.append("分类分布: " + "; ".join(dists))
    for i, r in enumerate(shown, start=1):
        lines.append(f"[{i}] " + json.dumps(r, ensure_ascii=False, default=str))
    out = "\n".join(lines)
    return out if len(out) <= limit else out[:limit] + note
