"""
工具返回结果的结构化摘要:`返回 4,821 条记录 · 6 个产品线 · 15 个销售区域`。

**与 structured_output.py 的分工**(两者都叫「结构化」,容易混):

| | structured_output | 本模块 |
|---|---|---|
| 处理对象 | **最终答复**(模型生成的文本) | **工具返回结果**(接口/SQL 的原始数据) |
| 产出 | 卡片 / 图表 / 洞察 / 建议 | 一句话摘要 + 发现项 |
| 用途 | 前端渲染答复 | 执行过程可见性 + 给模型的数据规模提示 |

**为什么值得单独做**:
1. **给人看**:工具返回一坨 JSON,执行过程里要么全展开(刷屏)、要么截断
   120 字符(看不出到底取到了什么)。一句摘要能让人一眼判断「这步取对了没有」。
2. **给模型看**:摘要作为前缀附在结果上,让它一眼知道数据规模,
   据此决定全量分析还是先聚合。

**刻意不调模型来总结** —— 那会给每次工具调用再加一次 LLM 往返,延迟和成本都翻倍,
而这里要的信息(多少条、有哪些维度)从数据结构本身就能算出来。
"""
from __future__ import annotations

from typing import Any

from cogni_agent.common.logging import get_logger

log = get_logger("runtime_service.engine.summarize")

# 一条记录里,取值数量落在这个区间的字符串字段才当作「维度」。
# 太少(全表一个值)说明是常量,没有信息量;太多(几乎每条都不同)是 id 一类,
# 列出来也没意义。
_DIM_MIN, _DIM_MAX = 2, 30
_MAX_DIMS = 3
_SAMPLE = 500          # 只扫前 N 条推断维度:几万条全扫没必要


def summarize(result: Any, *, kind: str | None = None) -> tuple[str, list[str]]:
    """返回 (一句话摘要, 发现项列表)。

    `kind` 影响措辞:脚本步骤说「处理完成」比「返回 3 个字段」更贴近它在做的事 ——
    数据清洗脚本返回的是加工结果,用户关心的是「洗完了没有」。

    发现项来自工具**自己声明**的 `_findings`。**引擎不去猜什么算异常** ——
    那是业务逻辑,自动猜的结果要么全是噪音,要么漏掉真正重要的。
    """
    findings: list[str] = []
    if isinstance(result, dict):
        raw = result.get("_findings") or result.get("findings")
        if isinstance(raw, list):
            findings = [str(x)[:120] for x in raw[:5]]
        # 工具自己给了摘要就用它的 —— 它比通用推断更懂自己的数据
        explicit = result.get("_summary") or result.get("summary")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()[:200], findings

    return _auto_summary(result, kind), findings


def _auto_summary(result: Any, kind: str | None) -> str:
    rows = _extract_rows(result)
    if rows is None:
        return _scalar_summary(result, kind)
    if not rows:
        # 空结果要说清楚。只说「0 条」的话,用户分不清是正常的空还是查错了,
        # 「条件未命中」这个措辞更接近事实
        return "返回 0 条记录(查询条件未命中任何数据)"

    parts = [f"返回 {len(rows):,} 条记录"]
    for name, count in _dimensions(rows):
        parts.append(f"{count} 个{name}")
    return " · ".join(parts)


def _extract_rows(result: Any) -> list[dict] | None:
    """从各种返回形状里找出「记录数组」。认不出来返回 None,走标量摘要。"""
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)] if result else []
    if isinstance(result, dict):
        for key in ("items", "data", "rows", "records", "list", "result"):
            v = result.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return None


def _dimensions(rows: list[dict]) -> list[tuple[str, int]]:
    """推断维度字段。按不重复数从少到多排 —— 少的通常是更高层级的分类
    (产品线 < 区域 < 城市),更有概括价值。"""
    sample = rows[:_SAMPLE]
    if not sample:
        return []
    stats: list[tuple[str, int]] = []
    for field in list(sample[0].keys())[:30]:
        values = set()
        for r in sample:
            v = r.get(field)
            # 只看字符串:数值字段的不重复数没有分类含义
            # (金额有几百个不同值,不说明任何事)
            if isinstance(v, str) and v:
                values.add(v)
                if len(values) > _DIM_MAX:
                    break
        if _DIM_MIN <= len(values) <= _DIM_MAX:
            stats.append((_label(field), len(values)))
    stats.sort(key=lambda x: x[1])
    return stats[:_MAX_DIMS]


# 常见字段名 -> 中文标签。认不出来就用原字段名,不硬猜。
_LABELS = {
    "product_line": "产品线", "product": "产品", "category": "分类",
    "region": "销售区域", "area": "区域", "province": "省份", "city": "城市",
    "channel": "渠道", "platform": "平台", "scenic_name": "景区", "scenic_id": "景区",
    "status": "状态", "type": "类型", "dept": "部门", "department": "部门",
    "date": "日期", "month": "月份", "source": "来源", "ticket_type": "票种",
}


def _label(field: str) -> str:
    return _LABELS.get(field.lower(), field)


def _scalar_summary(result: Any, kind: str | None) -> str:
    if isinstance(result, dict):
        keys = [k for k in result.keys() if not str(k).startswith("_")]
        if not keys:
            return "返回空对象"
        if kind == "script":
            # 脚本常见返回 {"total_count":..., "items":[...]};
            # 有数量信息就优先说数量,那是最有信息量的一项
            for key in ("total_count", "count", "processed", "rows"):
                v = result.get(key)
                if isinstance(v, int):
                    return f"处理完成,共 {v:,} 条"
            return f"处理完成,返回 {'、'.join(str(k) for k in keys[:4])}"
        return f"返回 {len(keys)} 个字段:{'、'.join(str(k) for k in keys[:6])}" + (
            " 等" if len(keys) > 6 else "")
    if isinstance(result, str):
        n = len(result)
        return f"返回文本 {n:,} 字" if n > 80 else f"返回:{result}"
    if isinstance(result, bool):
        return f"返回:{result}"
    if isinstance(result, int):
        return f"返回数值:{result:,}"
    if isinstance(result, float):
        return f"返回数值:{result}"
    if result is None:
        return "返回空"
    return f"返回 {type(result).__name__}"
