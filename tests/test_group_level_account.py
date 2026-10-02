"""
集团级账号(scenic_id 为空)全链路可用性测试。

这条 bug 的杀伤力:超管等集团级账号**发起任何对话都直接 500**,而错误发生在
写事件发件箱时,报的是 `evt_outbox.scenic_id NOT NULL violation` —— 从现象
(聊天报错)完全联想不到原因(事件表约束)。

全库有 17 处 scenic_id 已经统一成"可空 = 集团级",只有 evt_outbox / gov_audit /
gov_llm_usage 三张漏了。这里逐张钉死。
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect


def _nullable(model, col: str) -> bool:
    return model.__table__.columns[col].nullable


def test_outbox_scenic_id_nullable():
    """集团级任务没有所属景区。这里 NOT NULL 会让超管发起对话直接 500。"""
    from cogni_agent.governance_event_bus_service.events.outbox import OutboxEvent

    assert _nullable(OutboxEvent, "scenic_id"), "evt_outbox.scenic_id 必须可空"


def test_audit_scenic_id_nullable():
    from cogni_agent.governance_event_bus_service.audit import AuditLog as AuditEvent

    assert _nullable(AuditEvent, "scenic_id"), "gov_audit.scenic_id 必须可空"


def test_llm_usage_scenic_id_nullable():
    from cogni_agent.governance_event_bus_service.cost import LlmUsage

    assert _nullable(LlmUsage, "scenic_id"), "gov_llm_usage.scenic_id 必须可空"


def test_scenic_master_pk_stays_not_null():
    """反向确认:SysScenic.scenic_id 是主键,它本来就该 NOT NULL,不要跟着一起改。

    (0078 之前这条测的是 gov_scenic_orgs,那张表已并入 sys_scenics 并删除。)
    """
    from cogni_agent.user_center_service.models import SysScenic

    assert not _nullable(SysScenic, "scenic_id")


def test_no_other_table_forgets_nullable_scenic_id():
    """全库扫一遍:承载「集团级资源」语义的表,scenic_id 必须可空。

    集团级资源(超管创建的工具/技能/Agent)没有所属景区,scenic_id 为空。
    漏掉一张表就会让集团级账号在那条路径上直接 500,而报错指向的是
    「NOT NULL violation」,完全联想不到「这是集团级资源」。

    **两类表要排除**:
    - scenic_id 是主键的(SysScenic:它就是景区本身)
    - scenic_id 是业务唯一键的(AppScenicConfig:一景区一行配置,
      没有景区的配置本身就不成立)
    这两类里 scenic_id 是「这一行属于谁」的标识,而不是「可选的归属」。
    """
    from cogni_agent.common import schema  # noqa: F401 - 触发全部模型注册
    from cogni_agent.common.db import Base

    offenders = []
    for table in Base.metadata.tables.values():
        col = table.columns.get("scenic_id")
        if col is None or col.nullable or col.primary_key:
            continue
        # 唯一键上的 scenic_id = 这张表按景区分行,必填是对的。
        # **两种声明方式都要认**:UniqueConstraint 和 Index(unique=True) ——
        # 项目里两种写法都在用,只认一种会漏判。
        in_unique = any(
            "scenic_id" in {c.name for c in uc.columns}
            for uc in table.constraints
            if uc.__class__.__name__ == "UniqueConstraint"
        ) or any(
            idx.unique and "scenic_id" in {c.name for c in idx.columns}
            for idx in table.indexes
        )
        if in_unique:
            continue
        offenders.append(table.name)

    assert not offenders, (
        f"这些表的 scenic_id 仍是 NOT NULL,集团级账号(scenic_id 为空)"
        f"会在写入时直接 500:{offenders}"
    )


# ── 长文本字段 ──
@pytest.mark.parametrize("module,model,col", [
    ("cogni_agent.knowledge_service.multi2vec_clip_knowledge.store", "KbChunk", "content_ref"),
    ("cogni_agent.governance_event_bus_service.evals", "EvalCase", "test_input"),
    ("cogni_agent.governance_event_bus_service.evals", "EvalRun", "error"),
    ("cogni_agent.governance_event_bus_service.approvals", "Approval", "opinion"),
])
def test_long_content_columns_are_text(module, model, col):
    """这些字段存的是长内容。VARCHAR(N) 会**静默截断** ——
    检索出来的资料缺一半,模型基于残缺内容作答,很难发现。"""
    import importlib

    m = getattr(importlib.import_module(module), model)
    coltype = m.__table__.columns[col].type
    assert coltype.__class__.__name__ == "Text", \
        f"{model}.{col} 应为 Text,实际 {coltype}"
