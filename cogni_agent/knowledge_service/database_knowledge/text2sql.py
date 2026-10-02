"""M6-03:自然语言转 SQL,生成后必须经沙箱只读执行,不直连业务库。"""
from __future__ import annotations

import re

from cogni_agent.common.logging import get_logger
from cogni_agent.runtime_service.llm.client import complete
from cogni_agent.tool_center_service.sandbox.local import LocalSandbox

log = get_logger("knowledge_service.text2sql")

_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


async def text_to_sql(question: str, catalog_context: str, *, model: str | None = None) -> str:
    """调 LLM,把 catalog_context(库表释义,通常是若干条 DDL + 字段释义拼接)+ question
    转成只读 SELECT 语句。返回的 SQL 会在 run_readonly_query() 里再过一次只读强校验,
    这里生成阶段本身也在 Prompt 里明确约束,双保险。"""
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个只读数据分析助手。根据给出的数据库表结构(DDL 和字段释义),把用户的自然语言"
                "问题转换成一条 PostgreSQL 只读 SELECT 语句。严格要求:只输出 SQL,不要任何解释文字;"
                "只能用 SELECT,绝不允许 INSERT/UPDATE/DELETE/DROP/ALTER 等任何写操作;"
                "只能使用给出的表结构里存在的表和字段。"
            ),
        },
        {"role": "user", "content": f"表结构:\n{catalog_context}\n\n问题:{question}"},
    ]
    resp = await complete(model, messages, temperature=0, max_tokens=500)
    raw = (resp.choices[0].message.content or "").strip()
    match = _SQL_FENCE.search(raw)
    sql = match.group(1).strip() if match else raw
    if not re.match(r"(?is)^\s*select\b", sql):
        raise ValueError(f"模型未生成合法的只读 SELECT 语句:{sql[:200]}")
    return sql


async def run_readonly_query(datasource_id: str, sql: str) -> dict:
    """强制走沙箱只读执行(与 M5-11 数据库类型 Tool 共用同一套只读校验/参数化逻辑,
    这里 SQL 本身已经是完整语句、没有 {{var}} 占位符,直接透传给沙箱的 DB 执行器)。"""
    sandbox = LocalSandbox()
    return await sandbox._execute_db({"datasource_id": datasource_id, "sql_template": sql}, {})
