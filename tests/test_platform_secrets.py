# -*- coding: utf-8 -*-
"""平台密钥在后台配置 + 前置工具不受「给模型看哪些工具」影响。

现场任务 4cd9a4d9:
  1. 第一轮只给模型暴露了 4 个票务工具,sys_supplier_ids 没被挑中 —— 4 个调用全报
     「前置工具不在本次可用工具中」;第二轮它碰巧被挑进来,同样的调用就走到了下一步;
  2. 下一步报「未配置数据平台密钥」:这把 key 只能写服务器环境变量,问「在哪里设置」没有页面可指。
"""
from __future__ import annotations

import ast
import asyncio
import functools
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.gateway_service.deps import CurrentUser
from cogni_agent.governance_event_bus_service import platform_secrets as ps

ROOT = Path(__file__).resolve().parent.parent
KEY = "pfk_FAKEtestKEYforFixtureONLY0000"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    ps._cache.clear()
    ps._loaded_at = 0.0
    monkeypatch.setattr(ps, "_env_value", lambda name: "")     # 环境变量默认没配
    yield
    ps._cache.clear()
    ps._loaded_at = 0.0


@run_async
async def test_admin_value_wins_and_is_never_returned():
    from cogni_agent.tool_center_service import builtin_tools as bi

    bi._SUPPLIER_CACHE["S"] = (0.0, [1])
    async with common_db.get_sessionmaker()() as db:
        await ps.set_value(db, "data_platform_key", KEY, user_id="u-root")
    assert ps.get("data_platform_key") == KEY and ps.source("data_platform_key") == "admin"
    assert bi._SUPPLIER_CACHE == {}, "换了 key,按旧 key 查的供应商结果不能再用"
    items = await ps.describe()
    assert KEY not in str(items), "接口永不回明文"
    it = items[0]
    assert it["configured"] and it["source"] == "admin" and it["masked"] == "****0000"
    async with common_db.get_sessionmaker()() as db:
        row = await db.get(ps.PlatformSecret, "data_platform_key")
    assert KEY not in row.value_cipher, "落库的是密文"


@run_async
async def test_env_fallback_and_clear(monkeypatch):
    monkeypatch.setattr(ps, "_env_value", lambda name: "pfk_FROMenvENVIRONMENTvalue9999")
    await ps.refresh(force=True)
    assert ps.source("data_platform_key") == "env"
    async with common_db.get_sessionmaker()() as db:
        await ps.set_value(db, "data_platform_key", KEY, user_id="u")
        assert ps.get("data_platform_key") == KEY
        await ps.clear_value(db, "data_platform_key")
    assert ps.get("data_platform_key").endswith("9999") and ps.source("data_platform_key") == "env"


@run_async
async def test_unknown_names_and_empty_values_are_refused():
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(ValueError):
            await ps.set_value(db, "anything_else", "x" * 20, user_id="u")
        with pytest.raises(ValueError):
            await ps.set_value(db, "data_platform_key", "   ", user_id="u")


@run_async
async def test_sandbox_uses_the_admin_value():
    from cogni_agent.tool_center_service.sandbox.local import _inject_platform_secrets

    with pytest.raises(ValueError) as ei:
        _inject_platform_secrets({"xpftkey": "{{data_platform_key}}"})
    assert "系统设置 → 平台密钥" in str(ei.value), "报错要指到能设置的地方"
    async with common_db.get_sessionmaker()() as db:
        await ps.set_value(db, "data_platform_key", KEY, user_id="u")
    assert _inject_platform_secrets({"xpftkey": "{{data_platform_key}}"}) == {"xpftkey": KEY}


@run_async
async def test_routes_are_group_admin_only_and_masked():
    from cogni_agent.common.errors import Forbidden
    from cogni_agent.governance_event_bus_service import routes as gr

    scenic = CurrentUser("u-s", "PFTSCA01018088", "scenic_admin")
    root = CurrentUser("u-root", None, "super_admin")
    with pytest.raises(Forbidden):
        await gr.list_platform_secrets(scenic)
    async with common_db.get_sessionmaker()() as db:
        with pytest.raises(Forbidden):
            await gr.set_platform_secret("data_platform_key", gr.PlatformSecretRequest(value=KEY), scenic, db)
        r = await gr.set_platform_secret("data_platform_key", gr.PlatformSecretRequest(value=KEY), root, db)
    assert KEY not in str(r) and r["items"][0]["configured"]


def _helper():
    """graphs.py 顶层依赖 langchain(测试环境不一定有):只把这个函数摘出来跑。"""
    src = (ROOT / "cogni_agent/runtime_service/engine/graphs.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_with_param_source_upstreams")
    ns: dict = {"ToolSpec": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "graphs.py", "exec"), ns)
    return ns["_with_param_source_upstreams"]


def test_upstreams_are_added_even_when_not_exposed():
    from types import SimpleNamespace as T

    f = _helper()
    ticket = T(name="票务-产品报表汇总", param_sources={"supplierId": {"tool": "全局-通过景区ID获取供应商ID列表"}})
    sup = T(name="sys_supplier_ids", param_sources={})
    other = T(name="舆情-情绪词云", param_sources={})
    out = f([ticket], [ticket, other, sup])
    assert [t.name for t in out] == ["票务-产品报表汇总", "sys_supplier_ids"], "老名字也要换成内置补进来"
    assert f([other], [other, sup]) == [other], "没声明前置的不多加"


def test_tools_node_and_direct_tool_use_it():
    src = (ROOT / "cogni_agent/runtime_service/engine/graphs.py").read_text(encoding="utf-8")
    assert "tools = _with_param_source_upstreams(_exposed_tools(state, skill_chain, composition_hint)," in src
    assert "bound_tools=bound_tools_map(_with_param_source_upstreams(\n                [tool_spec], _builtin_tool_specs()))" in src


def test_migration_and_comments():
    from cogni_agent.common import schema

    assert any(m.id == "0086_platform_secrets" for m in schema.MIGRATIONS)
    assert "gov_platform_secrets" in schema.TABLE_COMMENTS
    assert set(schema.COLUMN_COMMENTS["gov_platform_secrets"]) == {"name", "value_cipher", "updated_by", "updated_at"}


def test_settings_page_has_the_card():
    page = (ROOT / "web/src/pages/System/SystemSettings.tsx").read_text(encoding="utf-8")
    card = (ROOT / "web/src/components/system/PlatformSecretsCard.tsx").read_text(encoding="utf-8")
    assert "<PlatformSecretsCard />" in page
    assert 'type="password"' in card and "api.setPlatformSecret(" in card and "api.clearPlatformSecret(" in card
