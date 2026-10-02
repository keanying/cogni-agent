"""M6-02:跨源检索结果精排(Rerank)。

行业标准 RAG 管线的最后一道工序:bi-encoder 向量检索只保证「召回」不保证「排序最优」,
vector / database / websearch 三路结果直接拼接时,真正相关的片段可能被埋在后面。
精排层(如 cross-encoder reranker)对 top-k 候选做二次相关性打分,把相关片段顶到最前,
直接决定答案质量。

本模块提供可插拔的精排实现(provider 可配):
- heuristic(默认,零成本):query 与候选文本的字符 bigram 相似度 + 原始分数融合。
  对中文有效(不依赖分词),适合暂无独立 rerank 模型服务的场景;
- llm(可选):复用现有 LLM 对 top_n 候选做一次轻量排序,质量更高但多一次模型调用;
- none:关闭精排,原样返回(与未启用时行为一致)。

配置(config.yaml `knowledge.rerank`):
- enabled:  是否启用(默认 true,heuristic 零成本)
- provider: heuristic / llm / none
- top_n:    参与精排的候选数(只取前 top_n 进入精排,控制 llm 模式成本)
- keep_top: 精排后保留返回的条数
- model:    llm 模式使用的模型(为空用默认模型)
"""
from __future__ import annotations

import json
import re

from cogni_agent.common.logging import get_logger

log = get_logger("knowledge_service.rerank")


def _cfg(key: str, default):
    from cogni_agent.config import get_settings

    return get_settings().get("knowledge", "rerank", key, default=default)


def _bigrams(text: str) -> set[str]:
    """字符 2-gram:中文不用分词也能算相似度,对数字/符号拼接也稳定。"""
    compact = "".join(ch for ch in text.lower() if not ch.isspace())
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _result_text(item: dict) -> str:
    """从不同类型的检索结果里提取用于精排的文本。"""
    kt = item.get("knowledge_type")
    if kt == "database":
        rows = item.get("rows") or []
        return " ".join(str(r) for r in rows[:5]) if rows else str(item.get("sql") or "")
    return " ".join(str(item.get(k) or "") for k in ("title", "snippet", "url", "source", "id", "content"))


def _text_similarity(query: str, text: str) -> float:
    qb, tb = _bigrams(query), _bigrams(text)
    if not qb or not tb:
        return 0.0
    return len(qb & tb) / len(qb | tb)


def _heuristic_score(query: str, item: dict) -> float:
    """零成本精排:文本相似度(0.6) + 原始分数归一化(0.4)。"""
    text_sim = _text_similarity(query, _result_text(item))
    raw = item.get("score")
    norm = 0.0
    if isinstance(raw, (int, float)):
        # 余弦相似度范围 [-1,1] 归一化到 [0,1];无分数的类型(database/websearch)全看文本相似度
        norm = max(0.0, min(1.0, (float(raw) + 1.0) / 2.0))
    return 0.6 * text_sim + 0.4 * norm


def _parse_order(text: str) -> list[int] | None:
    """LLM 输出可能带解释/代码块,宽松解析出编号数组。"""
    text = text.strip()
    array = re.search(r"\[([\d\s,，]+)\]", text)
    if not array:
        return None
    try:
        return [int(x) for x in re.findall(r"\d+", array.group(1))]
    except ValueError:
        return None


async def _llm_rerank(query: str, candidates: list[dict]) -> list[int]:
    """LLM 精排:返回按相关度从高到低排列的候选下标。任何异常降级为原序,不阻塞主流程。"""
    from cogni_agent.runtime_service.llm.client import complete_collected  # 延迟导入避免循环依赖

    model = _cfg("model", None)
    numbered = "\n".join(f"[{i}] {_result_text(c)[:200]}" for i, c in enumerate(candidates))
    system = (
        "你是检索结果精排员。给定用户问题和候选片段,按与问题的相关度从高到低排序,"
        "只输出候选编号数组,例如 [2,0,1]。不要输出任何其它内容。"
    )
    user = f"用户问题:{query}\n\n候选片段:\n{numbered}"
    try:
        result = await complete_collected(
            model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=64,
            temperature=0,
            timeout=30,
            max_retries=0,
        )
        order = _parse_order(result.content or "")
        if not order:
            log.warning(f"LLM 精排输出无法解析,退回原序:{(result.content or '')[:120]}")
            return list(range(len(candidates)))
        return order
    except Exception:  # noqa: BLE001 - 精排失败不影响主流程,退回原序
        log.exception("LLM 精排失败,退回原序")
        return list(range(len(candidates)))


async def rerank_results(query: str, results: list[dict]) -> list[dict]:
    """对检索结果做跨源精排(配置见 config.yaml knowledge.rerank)。

    幂等且安全:provider=none / 候选不足 / 精排异常时都原样返回,不会比未启用时更差。
    """
    if not results:
        return results
    enabled = _cfg("enabled", True)
    if not enabled:
        return results
    provider = str(_cfg("provider", "heuristic")).lower()
    if provider == "none":
        return results
    try:
        top_n = int(_cfg("top_n", 8))
        keep_top = int(_cfg("keep_top", 5))
    except (TypeError, ValueError):
        top_n, keep_top = 8, 5
    if top_n <= 1 or keep_top <= 0:
        return results[:keep_top] if keep_top > 0 else results

    candidates = results[:top_n]
    if provider == "llm":
        order = await _llm_rerank(query, candidates)
        ranked: list[dict] = []
        seen: set[int] = set()
        for idx in order:
            if 0 <= idx < len(candidates) and idx not in seen:
                ranked.append(candidates[idx])
                seen.add(idx)
        # LLM 漏报的候选按原序补在后面,保证条数不缩水
        ranked.extend(c for i, c in enumerate(candidates) if i not in seen)
    else:
        ranked = sorted(candidates, key=lambda c: _heuristic_score(query, c), reverse=True)
    # 精排只影响 top_n 内的次序,top_n 之外的结果仍按原序接在后面(避免截断召回)
    return ranked[:keep_top] + results[top_n:]
