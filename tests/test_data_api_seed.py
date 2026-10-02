# -*- coding: utf-8 -*-
"""数据指标接口工具:随服务打包、启动自动补齐。

现场反馈「基于这个的工具你没有添加?」—— 上一版只交付了同步脚本,要有人拿管理员账号
去跑才会建;而且脚本调用的「按接口文档新建」接口漏传了 derive_version 的必填参数,
真跑也是 500。这里钉住:打包的文档能直接建出 30 个工具、重复执行不重复建、
按 URI 认已有工具、密钥不落库、刷新不覆盖别人的草稿。
"""
from __future__ import annotations

import asyncio
import functools
import json

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.gateway_service.deps import CurrentUser


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh_db():
    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    yield


async def _admin(db, uid="u-root", role="super_admin"):
    from cogni_agent.user_center_service.models import User

    db.add(User(id=uid, name="管理员", account=uid, phone="1300000" + uid[-4:].rjust(4, "0"),
                password_hash="x", scenic_id=None, role=role, is_active=True))
    await db.commit()


async def _published(db):
    """{工具名: 已发布快照}"""
    from sqlalchemy import select

    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.tool.models import Tool

    tools = (await db.execute(select(Tool).where(Tool.deleted_at.is_(None)))).scalars().all()
    snaps = await ver.latest_published_snapshots(db, "tool", [t.id for t in tools])
    return {t.name: snaps.get(t.id) for t in tools}, tools


def test_bundled_doc_has_no_secret_and_parses_to_30_tools():
    from cogni_agent.tool_center_service import data_api_seed as seed

    md = seed.DOC_PATH.read_text(encoding="utf-8")
    assert "pfk_" not in md, "打包的文档里不能带真 key"
    items = seed.build_items(md, base_url=None)
    assert not [i for i in items if "error" in i]
    assert len(items) == 30
    assert sum(i["is_supplier"] for i in items) == 1


@run_async
async def test_startup_seed_creates_all_tools_once():
    from cogni_agent.tool_center_service import data_api_seed as seed

    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
    await seed.ensure_on_startup()

    async with common_db.get_sessionmaker()() as db:
        snaps, tools = await _published(db)
    # 30 个接口里的「供应商接口」已内置为 sys_supplier_ids,库里只建 29 个
    assert len(tools) == 29 and all(s and s["type"] == "http" for s in snaps.values()), "每个都要有已发布版本"
    assert not any(s["type_config"]["url"].endswith("/getSupplierIdByScenicId") for s in snaps.values())
    assert all(t.visibility == "public" and t.scenic_id is None and t.owner_user_id == "u-root" for t in tools)

    blob = json.dumps(snaps, ensure_ascii=False)
    assert "pfk_" not in blob
    heads = [s["type_config"].get("headers", {}) for s in snaps.values()]
    assert all(h.get("xpftkey") == "{{data_platform_key}}" for h in heads)

    need_sup = [s for s in snaps.values()
                if "supplierId" in s["type_config"]["params_schema"]["properties"]
                and not s["type_config"]["url"].endswith("/getSupplierIdByScenicId")]
    assert need_sup, "文档里有要 supplierId 的接口"
    for s in need_sup:
        src = s["type_config"]["params_schema"]["properties"]["supplierId"]["x-param-source"]
        assert src == {"tool": "sys_supplier_ids", "key": "$ctx.scenicId", "path": "supplierId"}

    # 再启动一次:不重复建
    await seed.ensure_on_startup()
    async with common_db.get_sessionmaker()() as db:
        _, again = await _published(db)
    assert len(again) == 29


@run_async
async def test_existing_tool_is_matched_by_uri_not_name():
    """工具库里原来那个叫「通过景区ID获取供应商ID列表」,文档叫「全局-…」—— 不能再建一个。
    其它工具的 supplierId 来源一律是内置 sys_supplier_ids(不依赖这条工具还在不在)。"""
    from cogni_agent.tool_center_service import data_api_seed as seed
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.tool.models import Tool

    md = seed.DOC_PATH.read_text(encoding="utf-8")
    sup = next(i for i in seed.build_items(md, base_url=None) if i["is_supplier"])
    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        old = Tool(name="通过景区ID获取供应商ID列表", owner_user_id="u-root", visibility="group", active=True)
        db.add(old)
        await db.flush()
        d = await ver.derive_version(db, "tool", old.id, base_version_id=None, created_by="u-root",
                                     snapshot={"type": "http", "type_config": sup["type_config"],
                                               "description": "运营手调过的描述"})
        await ver.publish_version(db, d.id)
        await db.commit()

        res = await seed.sync_data_api_tools(db)
        assert len(res["created"]) == 29 and res["skipped"] == ["通过景区ID获取供应商ID列表"]
        snaps, tools = await _published(db)
    assert len(tools) == 30
    assert snaps["通过景区ID获取供应商ID列表"]["description"] == "运营手调过的描述", "已有的默认不动"
    refs = {s["type_config"]["params_schema"]["properties"]["supplierId"]["x-param-source"]["tool"]
            for s in snaps.values()
            if "x-param-source" in json.dumps(s["type_config"]["params_schema"])}
    assert refs == {"sys_supplier_ids"}


@run_async
async def test_update_existing_refreshes_but_leaves_open_drafts_alone():
    from cogni_agent.tool_center_service import data_api_seed as seed
    from cogni_agent.tool_center_service import versioning as ver

    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        await seed.sync_data_api_tools(db)
        snaps, tools = await _published(db)
        busy = tools[0]
        pub = await ver.latest_published_version(db, "tool", busy.id)
        await ver.derive_version(db, "tool", busy.id, base_version_id=pub.id, created_by="u-root",
                                 snapshot={**snaps[busy.name], "description": "编辑中"})
        await db.commit()

        res = await seed.sync_data_api_tools(db, update_existing=True)
        assert len(res["updated"]) == 28 and busy.name in res["skipped"]
        assert any("草稿" in e for e in res["errors"])
        draft = await ver.latest_draft(db, "tool", busy.id)
        assert draft is not None and draft.snapshot["description"] == "编辑中", "别人的草稿不能被覆盖或发布"
        other = await ver.latest_published_version(db, "tool", tools[1].id)
        assert other.version == 2


@run_async
async def test_base_url_is_rebased():
    from cogni_agent.tool_center_service import data_api_seed as seed

    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        await seed.sync_data_api_tools(db, base_url="https://data.12301.com/")
        snaps, _ = await _published(db)
    urls = [s["type_config"]["url"] for s in snaps.values()]
    assert all(u.startswith("https://data.12301.com/") for u in urls)


@run_async
async def test_no_admin_yet_does_not_crash_startup():
    from cogni_agent.tool_center_service import data_api_seed as seed

    await seed.ensure_on_startup()   # 用户表为空
    async with common_db.get_sessionmaker()() as db:
        _, tools = await _published(db)
    assert tools == []


@run_async
async def test_manual_sync_route_is_group_only():
    from cogni_agent.common.errors import Forbidden
    from cogni_agent.tool_center_service.routes import DataApiSyncRequest, sync_data_api

    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        for role, scenic in (("scenic_admin", "PFTSCA01018088"), ("group_viewer", None)):
            with pytest.raises(Forbidden):
                await sync_data_api(DataApiSyncRequest(), CurrentUser("u-x", scenic, role), db)
        res = await sync_data_api(DataApiSyncRequest(), CurrentUser("u-root", None, "super_admin"), db)
    assert len(res["created"]) == 29 and "key_configured" in res


@run_async
async def test_create_from_api_doc_route_no_longer_500s():
    """漏传 base_version_id → TypeError → 500。「按接口文档新建」和同步脚本的 --apply 都走它。"""
    from cogni_agent.tool_center_service import data_api_seed as seed
    from cogni_agent.tool_center_service.routes import ApiDocToolRequest, create_tool_from_api_doc

    chunk = seed.split_doc(seed.DOC_PATH.read_text(encoding="utf-8"))[0]
    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        res = await create_tool_from_api_doc(ApiDocToolRequest(markdown=chunk),
                                             CurrentUser("u-root", None, "super_admin"), db)
        snaps, _ = await _published(db)
    assert res["id"] and len(snaps) == 1


def test_startup_calls_the_seed():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "cogni_agent" / "main.py").read_text(encoding="utf-8")
    life = src[src.index("async def lifespan"):src.index("yield", src.index("async def lifespan"))]
    assert "data_api_seed import ensure_on_startup as _ensure_data_api_tools" in life
    assert "await _ensure_data_api_tools()" in life, "启动流程里要真的调用"


def test_tool_center_has_a_manual_sync_button():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "web" / "src"
    page = (web / "pages" / "Tools" / "ToolList.tsx").read_text(encoding="utf-8")
    api = (web / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "api.syncDataApiTools(" in page and "同步数据接口工具" in page
    assert '"/admin/tools/sync-data-api"' in api


# ══════════════════════════════════════════════════════════════════════
# 响应字段说明:列错位 + 存储结构不一致(现场截图:字段名整列是空的)
# ══════════════════════════════════════════════════════════════════════

def test_child_rows_with_empty_index_cell_keep_their_columns():
    """`||data[].scenicId|string|…` 的序号列是空的;strip("|") 会把它吃掉,整行左移一列。"""
    from cogni_agent.tool_center_service.apidoc.parser import _split_row

    assert _split_row(r"||data\[\]\.scenicId|string|PFTSCA01018088|景区ID|") == [
        "", r"data\[\]\.scenicId", "string", "PFTSCA01018088", "景区ID"]
    assert _split_row("|1|status|bool|true|请求状态|")[1] == "status"
    assert _split_row(r"|3|mode|string|a\|b|取值 a 或 b|")[3] == r"a\|b", "转义竖线不是分隔符"


def test_every_response_field_of_all_30_tools_is_well_formed():
    import re

    from cogni_agent.tool_center_service import data_api_seed as seed

    types = {"string", "int", "integer", "bool", "boolean", "array", "object", "number"}
    items = seed.build_items(seed.DOC_PATH.read_text(encoding="utf-8"), base_url=None)
    total = 0
    for it in items:
        for f in it["type_config"]["response_schema"]["fields"]:
            total += 1
            assert set(f) >= {"path", "type", "unit", "desc"}, "要与编辑页同一结构(path/type/unit/desc)"
            assert f["path"].lower() not in types and re.fullmatch(r"[A-Za-z_][\w\[\]\.]*", f["path"]), f
            assert f["desc"], f
    assert total > 300
    cc = next(i for i in items if i["path"].endswith("channelCommentDistribution"))
    by = {f["path"]: f for f in cc["type_config"]["response_schema"]["fields"]}
    assert by["data[].publishTime"]["type"] == "integer" and by["data[].publishTime"]["desc"] == "发布时间"
    assert by["trace_id"]["desc"] == "追踪ID"


def test_model_gets_response_notes_for_new_and_legacy_shapes():
    """给模型的「响应字段说明」按 path 读 —— 按 name 存的(早先导入的)以前整份被跳过。"""
    from types import SimpleNamespace

    import ast
    from pathlib import Path

    from cogni_agent.tool_center_service.apidoc.digest import compress_tool_result

    # graphs.py 依赖 langchain(测试环境不一定装了):只把这一个函数摘出来单独跑
    src = (Path(__file__).resolve().parent.parent / "cogni_agent/runtime_service/engine/graphs.py"
           ).read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_response_note")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "graphs.py", "exec"), ns)
    _response_note = ns["_response_note"]

    for key in ("path", "name"):
        spec = SimpleNamespace(type_config={"response_schema": {"fields": [
            {key: "data[].commentCnt", "type": "integer", "desc": "总评数(近1日)"}]}})
        note = _response_note(spec) or ""
        assert "data[].commentCnt(integer)" in note and "总评数" in note, key

        rows = [{"commentCnt": i} for i in range(40)]
        d = compress_tool_result({"data": rows}, response_schema={
            "data_path": ["data"], "fields": [{key: "data[].commentCnt", "desc": "总评数(近1日)"}]})
        assert "总评数" in str(d), key


def test_frontend_reads_legacy_name_too():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "web" / "src"
    assert "{f.path || f.name}" in (web / "pages/Tools/ToolDetail.tsx").read_text(encoding="utf-8")
    assert "path: f.path ?? f.name" in (web / "components/common/FormBits.tsx").read_text(encoding="utf-8")


@run_async
async def test_startup_repairs_tools_nobody_touched_and_leaves_edited_ones():
    """库里已经有按错误解析建出来的 30 个工具。启动时:只由系统写过的自动修正;
    有人改过(版本 note 不是系统写的)的一律不动。"""
    from cogni_agent.tool_center_service import data_api_seed as seed
    from cogni_agent.tool_center_service import versioning as ver

    async with common_db.get_sessionmaker()() as db:
        await _admin(db)
        await seed.sync_data_api_tools(db)
        snaps, tools = await _published(db)
        # 把两个工具弄成「旧的坏形状」:一个保持系统写的,一个模拟人手改过
        broken = {"data_path": ["data"], "fields": [{"name": "string", "type": "string", "desc": ""}]}
        sys_t, human_t = tools[0], tools[1]
        for t, note in ((sys_t, seed.SEED_NOTE_PREFIX + "内置"), (human_t, "")):
            s = dict(snaps[t.name])
            s["type_config"] = {**s["type_config"], "response_schema": broken}
            pub = await ver.latest_published_version(db, "tool", t.id)
            d = await ver.derive_version(db, "tool", t.id, base_version_id=pub.id, snapshot=s,
                                         created_by="u-root", note=note)
            await ver.publish_version(db, d.id)
        await db.commit()

    await seed.ensure_on_startup()

    async with common_db.get_sessionmaker()() as db:
        snaps, _ = await _published(db)
    fixed = snaps[sys_t.name]["type_config"]["response_schema"]["fields"]
    assert all("path" in f for f in fixed) and fixed[0]["path"] == "status"
    assert snaps[human_t.name]["type_config"]["response_schema"] == broken, "人改过的不能被覆盖"

    # 再启动一次:已经一致,不再发版本
    await seed.ensure_on_startup()
    async with common_db.get_sessionmaker()() as db:
        pub = await ver.latest_published_version(db, "tool", sys_t.id)
    assert pub.version == 3


# ══════════════════════════════════════════════════════════════════════
# 多级字段列:票务类接口的返回字段以前一个都没进来(文档 34 行,解析 3 行)
# ══════════════════════════════════════════════════════════════════════

def test_every_documented_response_row_is_parsed():
    """逐个接口核对:文档返回参数表有几行,解析出来就有几个字段 —— 一个都不能丢。"""
    import re

    from cogni_agent.tool_center_service import data_api_seed as seed

    total = 0
    for chunk in seed.split_doc(seed.DOC_PATH.read_text(encoding="utf-8")):
        ret = chunk[chunk.find("返回参数"):]
        ret = ret[:ret.find("###")] if "###" in ret else ret
        doc_rows = [ln for ln in ret.splitlines() if ln.startswith("|")
                    and not re.match(r"\|[\s:\-|]+\|?$", ln) and "序号" not in ln]
        fields = seed.build_items(chunk, base_url=None)[0]["type_config"]["response_schema"]["fields"]
        assert len(fields) == len(doc_rows), chunk.splitlines()[0]
        total += len(fields)
        tops = [f["path"] for f in fields if "." not in f["path"]]
        assert set(tops) <= {"status", "data", "msg", "code", "trace_id"}, (chunk.splitlines()[0], tops)
    assert total > 550


def test_nested_paths_follow_the_nearest_container():
    from cogni_agent.tool_center_service.apidoc.parser import _fields_from_rows, _parse_table

    # 票务-分时:msg(标量)夹在 data 和子字段之间 —— 子字段仍归 data
    rows = _parse_table([
        "|序号|字段||类型|示例|注释|", "|---|---|---|---|---|---|",
        "|1|status||bool|true|请求状态|", "|2|data||array|[...]|数据体|",
        "|3|msg||string|认证无效|错误信息|", "|||hour|string|8:00|小时|",
    ])
    assert [f.name for f in _fields_from_rows(rows, with_required=False)] == [
        "status", "data", "msg", "data[].hour"]
    # 总览-雷达图:三级字段列,对象套对象
    rows = _parse_table([
        "|序号|字段|字段|字段|类型|示例|注释|", "|---|---|---|---|---|---|---|",
        "|2|data|||object|{...}|数据体|", "|||max||object|{...}|极值|",
        "||||boxOfficeAmount|number|100|票房|", "|||among||object|{...}|及格线|",
        "||||boxOfficeAmount|number|50|票房|",
    ])
    assert [f.name for f in _fields_from_rows(rows, with_required=False)] == [
        "data", "data.max", "data.max.boxOfficeAmount", "data.among", "data.among.boxOfficeAmount"]
