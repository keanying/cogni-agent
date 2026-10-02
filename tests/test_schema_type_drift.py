"""列类型漂移体检(_model_column_type_drift)的查全率回归测试(无库,假连接)。

背景:tc_tools.description 曾在提交 7753315 从 String(255) 放宽为 Text 却没写迁移,
dev 老库列至今 varchar(255),迁移 Agent 时 15 个超长 description 直接 500。自动补列只
补缺列不改类型,此类漂移只有迁移可收敛(见 MIGRATIONS 0040)。

体检函数本身遍历 Base.metadata 全表全列、识别全部 SQLAlchemy 类型,并非只查 Text。
但"识别全部类型"需要被证明,否则下次换一种类型漂移(如 Integer→bigint、numeric 精度
变了)又没人发现。本测试用「模型反推理想库快照」锁定查全率:

  1. 理想快照 = 按 Base.metadata 全表全列翻译出的 information_schema 行,与模型
     完全一致 => 体检必须 0 漂移(自洽);
  2. 逐列破坏 = 把每一列的库类型改成与模型必然不同的表示,体检必须报出该列
     => 每种用到的类型(Text/varchar(n)/Integer/JSON/…/vector)都能被检出;
  3. 映射强制 = 模型里出现任何本测试翻译表没覆盖的新类型类名,测试直接 FAIL 列出,
     => 未来新增类型列时,必须先补翻译/破坏映射,杜绝"新类型进盲区"。

全部用假连接对象,不依赖真实 Postgres,CI 可跑。
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from cogni_agent.common.schema import Base, _model_column_type_drift


# ── 模型列类型 → information_schema 行(后 5 字段) ────────────────────────────
# information_schema 查询列序: data_type, udt_name, character_maximum_length,
# numeric_precision, numeric_scale
def _translate(col_type) -> tuple | None:
    """模型 SQLAlchemy 类型 -> (data_type, udt_name, char_len, num_prec, num_scale)。"""
    from sqlalchemy import (
        BigInteger, Boolean, Date, DateTime, Integer, JSON, LargeBinary, Numeric,
        SmallInteger, String, Text,
    )
    from sqlalchemy.dialects.postgresql import JSONB

    t = col_type
    if isinstance(t, LargeBinary):   # 上传文件原文(tc_qbi_files.content)
        return ("bytea", "bytea", None, None, None)
    # BigInteger/SmallInteger 是 Integer 的子类,必须排在它前面 —— 顺序反了
    # bigint 列会被当成 integer,体检天天报一条假漂移
    if isinstance(t, BigInteger):
        return ("bigint", "int8", None, None, None)
    if isinstance(t, SmallInteger):
        return ("smallint", "int2", None, None, None)
    if isinstance(t, Text):
        return ("text", "text", None, None, None)
    if isinstance(t, String):
        return ("character varying", "varchar", getattr(t, "length", None), None, None)
    if isinstance(t, Boolean):
        return ("boolean", "bool", None, None, None)
    if isinstance(t, Integer):
        return ("integer", "int4", None, None, None)
    if isinstance(t, DateTime):
        return (
            ("timestamp with time zone" if t.timezone else "timestamp without time zone"),
            ("timestamptz" if t.timezone else "timestamp"),
            None, None, None,
        )
    if isinstance(t, Date):
        return ("date", "date", None, None, None)
    if isinstance(t, Numeric):
        return ("numeric", "numeric", None, getattr(t, "precision", None),
                getattr(t, "scale", None))
    if isinstance(t, JSONB):
        return ("jsonb", "jsonb", None, None, None)
    if isinstance(t, JSON):
        return ("json", "json", None, None, None)
    if type(t).__module__.startswith("pgvector.sqlalchemy"):
        return ("USER-DEFINED", "vector", None, None, None)
    return None  # 模型里出现但本测试不认识 => 由 test_1 强制 FAIL


def _break_to(col_type) -> tuple | None:
    """与模型必然不同的库类型表示(每种类型给一个"反例"),用于逐列破坏。"""
    from sqlalchemy import (
        BigInteger, Boolean, Date, DateTime, Integer, JSON, LargeBinary, Numeric,
        SmallInteger, String, Text,
    )
    from sqlalchemy.dialects.postgresql import JSONB

    t = col_type
    if isinstance(t, LargeBinary):   # 库侧被错建为 text(二进制存文本列,取出来就是乱码)
        return ("text", "text", None, None, None)
    if isinstance(t, BigInteger):    # 库侧退回 integer(自增到 21 亿就炸)
        return ("integer", "int4", None, None, None)
    if isinstance(t, SmallInteger):  # 库侧建成了 integer
        return ("integer", "int4", None, None, None)
    if isinstance(t, Text):      # 库侧仍停在旧的 varchar(255) —— 本项目真实事故
        return ("character varying", "varchar", 255, None, None)
    if isinstance(t, String):    # 库侧被错建为 TEXT
        return ("text", "text", None, None, None)
    if isinstance(t, Boolean):   # 库侧建成了 smallint
        return ("smallint", "int2", None, None, None)
    if isinstance(t, Integer):   # 库侧建成了 bigint
        return ("bigint", "int8", None, None, None)
    if isinstance(t, DateTime):  # 库侧丢/加了时区
        if t.timezone:
            return ("timestamp without time zone", "timestamp", None, None, None)
        return ("timestamp with time zone", "timestamptz", None, None, None)
    if isinstance(t, Date):      # 库侧建成了 timestamp
        return ("timestamp without time zone", "timestamp", None, None, None)
    if isinstance(t, Numeric):   # 库侧精度丢了
        return ("double precision", "float8", None, None, None)
    if isinstance(t, JSONB):     # 库侧退化为 json(迁移先走 jsonb 的列若被误建为 json)
        return ("json", "json", None, None, None)
    if isinstance(t, JSON):      # 库侧错建为 text
        return ("text", "text", None, None, None)
    if type(t).__module__.startswith("pgvector.sqlalchemy"):  # 库侧错建为 varchar
        return ("character varying", "varchar", 10, None, None)
    return None


class _Rows:
    def __init__(self, data: list[tuple]):
        self._data = data

    def __iter__(self) -> Iterator[tuple]:
        return iter(self._data)


class _FakeConn:
    """只实现 _model_column_type_drift 用到的 execute:返回可迭代行。"""

    def __init__(self, rows: list[tuple]):
        self._rows = rows

    async def execute(self, stmt):  # noqa: ANN001 - 假对象,忽略语句对象
        return _Rows(self._rows)


def _ideal_rows() -> list[tuple]:
    """按 Base.metadata 全表全列生成"理想库"information_schema 行(0 漂移)。"""
    rows: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            five = _translate(col.type)
            if five is None:
                continue  # 未知类型列(理论上被 test_mapping_covers_all 拦截)
            rows.append((table.name, col.name, *five))
    return rows


def _all_columns():
    """全库 (表, 列, 类型对象) 平铺,供逐列破坏。"""
    return [(t.name, c.name, c.type)
            for t in Base.metadata.sorted_tables for c in t.columns]


def _run_drift(rows: list[tuple]) -> list[str]:
    import asyncio
    return asyncio.run(_model_column_type_drift(_FakeConn(rows)))


def _drifted_names(drift: list[str]) -> set[str]:
    return {line.split(" ")[0] for line in drift}


# ── 用例 1:翻译/破坏映射必须覆盖模型全部类型类名(防新类型进盲区) ────────────
def test_type_mapping_covers_all_model_types():
    uncovered = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if _translate(col.type) is None:
                uncovered.append(f"{table.name}.{col.name}: {type(col.type).__name__}")
            elif _break_to(col.type) is None:
                uncovered.append(f"{table.name}.{col.name}: 缺 break 映射 {type(col.type).__name__}")
    assert not uncovered, (
        "模型里出现了本测试不认识/未配破坏映射的类型,体检可能漏检,请到 "
        "tests/test_schema_type_drift.py 的 _translate/_break_to 补充映射:\n  "
        + "\n  ".join(uncovered)
    )


# ── 用例 2:理想快照(库=模型)必须 0 漂移 ──────────────────────────────────────
def test_ideal_snapshot_zero_drift():
    assert _run_drift(_ideal_rows()) == []


# ── 用例 3:逐列破坏——每种用到的类型被破坏后都必须被体检检出 ─────────────────
def test_every_type_break_is_detected():
    base = _ideal_rows()
    keys = {(r[0], r[1]): r for r in base}
    for tname, cname, ctype in _all_columns():
        if (tname, cname) not in keys:
            continue  # 该列类型不被翻译(理论上被用例 1 拦截)
        broken = list(base)
        broken[broken.index(keys[(tname, cname)])] = (tname, cname, *_break_to(ctype))
        drift = _drifted_names(_run_drift(broken))
        assert f"{tname}.{cname}" in drift, (
            f"破坏 {tname}.{cname}(模型 {type(ctype).__name__})后体检未报出,"
            f"说明该类型的漂移检测失效"
        )


# ── 用例 4:真实事故回归——0051 后 tc_tools 内容列已删(纯版本快照承载) ────────
def test_tc_tools_content_columns_removed_after_0051():
    """v1.2(0051):Tool 与 Skill/Agent 同构 —— description/type/type_config/
    risk_level 主表列已删除,内容只在 tc_resource_versions 快照(adapter extra_keys)。
    历史锚点:tc_tools.description 曾在 dev 老库是 varchar(255)(Text 截断事故,
    0049/类型漂移体检引入)。
    守护:理想快照里不得再出现这些内容列 —— 防有人把内容列加回主表,造成
    「同一内容主表+快照两处来源」,编辑/发布/读端三轨重新分叉。
    漂移检出能力本身由用例 3(逐列破坏)覆盖,此处只管结构不再回退。"""
    tools = next(t for t in Base.metadata.sorted_tables if t.name.endswith("tc_tools"))
    rows = [r for r in _ideal_rows() if r[0] == tools.name]
    cols = {r[1] for r in rows}
    gone = {"description", "type", "type_config", "risk_level"}
    assert not (gone & cols), f"tc_tools 主表不应再有内容列(0051 已删): {sorted(gone & cols)}"
