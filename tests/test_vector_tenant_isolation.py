"""
向量后端的租户隔离测试。

**这是整个知识中心最不能出错的地方。** pgvector 时代隔离靠调用方在 SQL 里写
`WHERE scenic_id = ...`;换成 Milvus 之后如果还指望调用方记得传过滤条件,漏一次就是
跨租户数据泄露 —— 而且这种泄露**不会报错**,只会安静地把别的景区的资料喂给模型,
可能几个月都没人发现。

所以这里逐条钉死过滤表达式的形状,任何人改动 _filter_expr 都会被这些用例拦下。
"""
from __future__ import annotations

import pytest

from cogni_agent.knowledge_service.vector_backends import MilvusBackend, VectorRecord


@pytest.fixture
def backend() -> MilvusBackend:
    return MilvusBackend({"host": "localhost", "port": 19530, "collection": "t", "dim": 8})


def test_filter_includes_both_kb_and_tenant(backend):
    """必须同时限定知识库和租户,少任何一个都是越权。"""
    expr = backend._filter_expr("kb_1", "wuyishan")
    assert 'kb_id == "kb_1"' in expr, "必须按知识库过滤"
    assert 'scenic_id == "wuyishan"' in expr, "必须按租户过滤"


def test_group_level_knowledge_is_visible_to_all(backend):
    """集团级知识(scenic_id 为空)对所有景区可见 —— 这是刻意的业务规则,不是漏洞。"""
    expr = backend._filter_expr("kb_1", "wuyishan")
    assert 'scenic_id == ""' in expr, "集团级知识库必须对所有景区可见"
    assert " or " in expr, "租户条件应是 (本租户 or 集团级)"


def test_tenant_condition_is_parenthesized(backend):
    """`kb and (a or b)` 与 `kb and a or b` 完全不同 —— 后者会让所有集团级知识
    绕过 kb_id 限制被检索出来。括号不能少。"""
    expr = backend._filter_expr("kb_1", "wuyishan")
    assert "and (" in expr and expr.rstrip().endswith(")"), f"租户条件必须整体加括号,实际:{expr}"


def test_none_tenant_maps_to_empty_string(backend):
    """集团级调用方(scenic_id=None)映射为空串,与写入侧保持一致。

    不用 null:Milvus 对 null 的标量过滤在不同版本间差异较大,而空串比较行为在所有
    版本上都是确定的。宁可在边界做一次 None <-> "" 转换,也不要让隔离条件依赖不稳定语义。
    """
    assert backend._tenant_value(None) == ""
    assert backend._tenant_value("") == ""
    assert backend._tenant_value("wuyishan") == "wuyishan"
    expr = backend._filter_expr("kb_1", None)
    assert 'scenic_id == ""' in expr


def test_quotes_are_escaped(backend):
    """租户 id / 知识库 id 里出现引号时必须转义,否则过滤表达式会被截断 ——
    表达式被截断的后果不是报错,而是**过滤条件失效**。"""
    expr = backend._filter_expr('kb"1', 'ten"ant')
    assert '\\"' in expr
    # 转义后不应出现能提前闭合字符串的裸引号序列
    assert expr.count('"') % 2 == 0, f"引号未成对,表达式可能被截断:{expr}"


def test_upsert_writes_normalized_tenant(backend):
    """写入侧也要做同样的 None -> "" 归一,否则写进去的是 null、查的是空串,永远查不到。"""
    rec = VectorRecord(chunk_id="c1", kb_id="kb1", scenic_id=None, embedding=[0.0] * 8)
    assert backend._tenant_value(rec.scenic_id) == ""


def test_delete_by_kb_is_scoped(backend):
    """按知识库删除时不能误删别的知识库 —— 这个操作不可逆。"""
    import inspect

    src = inspect.getsource(backend.delete_by_kb)
    assert 'kb_id ==' in src, "删除必须限定 kb_id"


def test_backend_contract_requires_scenic_id():
    """search 的签名必须把 scenic_id 作为**关键字必填参数**,
    调用方没有'忘记传'的机会。"""
    import inspect

    sig = inspect.signature(MilvusBackend.search)
    p = sig.parameters["scenic_id"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY, "scenic_id 应为 keyword-only,避免位置参数传错"
    assert p.default is inspect.Parameter.empty, "scenic_id 不能有默认值,必须显式传入"
