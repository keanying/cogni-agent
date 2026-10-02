"""结构体检「库↔模型 双向一致」的回归测试(无库,假连接)。

背景:tc_prompts 早期 create_all 时代建过 version/is_current(Prompt 曾带独立版本化),
后模型改为由 Skill.version 统一管理、Prompt 仅 id/skill_id/content(见 skill/models.py),
但迁移链从未对老库 DROP——dev 残留 NOT NULL 无默认的 version 列,跨环境能力迁移
POST /skills 插入 prompt 时 IntegrityError 500(见 MIGRATIONS 0042)。

教训:体检只查「模型有而库缺」(缺列/缺表)与类型漂移,对「库有而模型无」是盲区——
删了字段/删了整表却不写迁移,残留的 NOT NULL 无默认列只卡写入不卡启动,
部署体检根本扫不到,直到线上出 500 才暴露。修复:冗余列/冗余表纳入只读体检,
sync_schema 任一存在即 raise,部署门禁同步拦截——**模型 = 唯一事实源,库必须是模型
的镜像,删除同样必须写迁移(0042/冗余表检测)**。

豁免约定:
- created_at/updated_at 是 DB server_default 维护列,模型可不声明,不算冗余列;
- {P}schema_migrations 是迁移记录表(_ensure_migration_table 手建、非模型),不算冗余表;
- 非 P 前缀表(LangGraph checkpointer 自建等)不在本项目结构治理范围。

全部用假连接对象,不依赖真实 Postgres,CI 可跑。
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator

from cogni_agent.common.schema import (
    Base,
    P,
    _model_extra_column_drift,
    _model_extra_table_drift,
)


class _Rows:
    def __init__(self, data: list[tuple]):
        self._data = data

    def __iter__(self) -> Iterator[tuple]:
        return iter(self._data)


class _FakeConn:
    """假连接:execute 返回固定行列表((表, 列) 双元素)。"""

    def __init__(self, rows: list[tuple]):
        self._rows = rows

    async def execute(self, stmt):  # noqa: ANN001 - 假对象,忽略语句对象
        return _Rows(self._rows)


class _FakeTableConn(_FakeConn):
    """假连接:execute 返回 ((表名,),) 单元素行,给 _model_extra_table_drift 用。"""

    async def execute(self, stmt):  # noqa: ANN001 - 假对象,忽略语句对象
        return _Rows([(r,) for r in self._rows])


def _prompt_table_name() -> str:
    return next(t.name for t in Base.metadata.sorted_tables
                if t.name.endswith("tc_prompts"))


def _all_model_rows() -> list[tuple]:
    """理想库:库列 == 模型列 => 0 冗余列。"""
    return [(t.name, c.name)
            for t in Base.metadata.sorted_tables for c in t.columns]


def _model_table_names() -> list[str]:
    return [t.name for t in Base.metadata.sorted_tables]


def _run_cols(rows: list[tuple]) -> list[str]:
    return asyncio.run(_model_extra_column_drift(_FakeConn(rows)))


def _run_tables(rows: list[str]) -> list[str]:
    return asyncio.run(_model_extra_table_drift(_FakeTableConn(rows)))


# ═══ 冗余列(_model_extra_column_drift) ═══════════════════════════════════════

# ── 理想快照(库 = 模型)必须 0 冗余 ──────────────────────────────────────────
def test_ideal_snapshot_zero_extra_cols():
    assert _run_cols(_all_model_rows()) == []


# ── 真实事故回归:tc_prompts 残留废弃列必须被检出 ────────────────────────────
def test_tc_prompts_stale_version_columns_detected():
    pname = _prompt_table_name()
    rows = _all_model_rows() + [
        (pname, "version"),   # NOT NULL 无默认,插 prompt 即 500(dev 实测)
        (pname, "is_current"),
    ]
    extra = _run_cols(rows)
    assert any(f"{pname}.version" == e for e in extra), f"未检出 version,实际: {extra}"
    assert any(f"{pname}.is_current" == e for e in extra), f"未检出 is_current,实际: {extra}"


# ── 维护列豁免:created_at/updated_at 不算冗余 ──────────────────────────────
def test_maintenance_columns_not_reported():
    pname = _prompt_table_name()
    rows = _all_model_rows() + [
        (pname, "created_at"),  # DB server_default,模型可不声明
        (pname, "updated_at"),
    ]
    assert _run_cols(rows) == [], "created_at/updated_at 是约定维护列,不应报冗余"


# ── 其他表的任意模型外列同样要检出(通用性) ─────────────────────────────────
def test_arbitrary_extra_column_detected():
    any_table = next(t.name for t in Base.metadata.sorted_tables)
    extra = _run_cols(_all_model_rows() + [(any_table, "obsolete_col_xyz")])
    assert any("obsolete_col_xyz" in e for e in extra), f"通用冗余列未检出,实际: {extra}"


# ═══ 冗余表(_model_extra_table_drift) ════════════════════════════════════════

# ── 理想快照:库表 == 模型表 + schema_migrations => 0 冗余 ───────────────────
def test_ideal_tables_zero_extra():
    rows = _model_table_names() + [f"{P}schema_migrations"]
    assert _run_tables(rows) == []


# ── P 前缀废弃整表必须检出 ──────────────────────────────────────────────────
def test_stale_business_table_detected():
    stale = f"{P}legacy_obsolete_table"
    rows = _model_table_names() + [f"{P}schema_migrations", stale]
    extra = _run_tables(rows)
    assert stale in extra, f"冗余业务表未检出,实际: {extra}"


# ── schema_migrations(手建记录表)豁免 ───────────────────────────────────────
def test_schema_migrations_table_exempted():
    rows = _model_table_names() + [f"{P}schema_migrations"]
    assert _run_tables(rows) == [], "schema_migrations 是迁移记录表,不应报冗余"


# ── 非 P 前缀表(其他系统自建)忽略 ───────────────────────────────────────────
def test_non_project_table_ignored():
    rows = _model_table_names() + ["checkpoints", "checkpoint_writes", "random_user_table"]
    assert _run_tables(rows) == [], "非本项目命名空间表不应报冗余"
