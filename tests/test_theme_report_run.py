# -*- coding: utf-8 -*-
"""主题解读(报告类固定问题)从管理端发起的回归。

钉住的是现场那个「点下去 → 请求失败 (500)」:

1. **列宽**:0079 之后运行记录的 fixed_question_id 存的是统一问题表的 issue_id
   (issue_<32位> 就有 38 字符),而那一列建表时是 VARCHAR(36)(老表 UUID 正好 36)——
   于是每次按固定问题发起,都会在写运行记录时 value too long 而整个请求 500。
   这类错最难查:报错在最后一步(写库),原因在半年前的一次建表。
2. **裸 500**:首事件之前的异常原先只兜 QuickBIError,别的直接冒到 FastAPI ——
   前端只剩一句「请求失败 (500)」,既不知道哪一步、也不知道找谁。
3. **口径**:「【主题解读】OTA直销」这七个字原样发给小Q是没法出报告的,
   真正的约束在 model_instruction 里,必须跟着发。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型(建表用)
from cogni_agent.common.db import Base
from cogni_agent.common.errors import ZhilvError
from cogni_agent.common.middleware import ApiResponseMiddleware
from cogni_agent.config import get_settings
from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from cogni_agent.tool_center_service.quickbi import client as qclient
from cogni_agent.tool_center_service.quickbi.routes_admin import router as admin_router
from tests.quickbi import mock_quickbi_server as mock

ADMIN = CurrentUser(user_id="u-admin", scenic_id=None, role="super_admin")
SCENIC = "PFTSCA01018088"


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from cogni_agent.tool_center_service.mcp import vault

    s = get_settings()
    tc = dict(s.raw.get("tool_center") or {})
    if not tc.get("credential_encrypt_key"):
        tc["credential_encrypt_key"] = Fernet.generate_key().decode()
        monkeypatch.setitem(s.raw, "tool_center", tc)
        monkeypatch.setattr(vault, "_fernet_singleton", None)
    q = dict(s.raw.get("quickbi") or {})
    q.update({"trial_api_key": mock.API_KEY, "trial_api_secret": mock.API_SECRET,
              "report_poll_seconds": 0, "snapshot_poll_seconds": 0,
              "default_connection": {"user_token": ""}})
    monkeypatch.setitem(s.raw, "quickbi", q)
    monkeypatch.setattr(qclient, "default_transport_factory", lambda: httpx.ASGITransport(app=mock.app))
    mock.STATE.__init__()

    async def reset():
        from cogni_agent.user_center_service.models import User

        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
        async with common_db.get_sessionmaker()() as db:
            db.add(User(id="u-admin", name="超管", account="admin", phone="13800000001",
                        password_hash="x", scenic_id="", role="super_admin"))
            await db.commit()
    asyncio.run(reset())
    yield


async def _error_handler(request, exc: ZhilvError):
    from fastapi.responses import JSONResponse

    from cogni_agent.common.schemas import ApiResponse
    return JSONResponse(status_code=exc.status_code,
                        content=ApiResponse.fail(code=exc.error_code or 1000, message=str(exc)).model_dump())


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ApiResponseMiddleware)
    app.add_exception_handler(ZhilvError, _error_handler)
    app.include_router(admin_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60)


def data(resp: httpx.Response):
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, (resp.status_code, body)
    return body["data"]


async def _seed(c: httpx.AsyncClient) -> str:
    """一条绑到景区的连接 + 一份仪表板资产,然后生成 5 条主题解读。"""
    from cogni_agent.gateway_service import scenic_binding as binding

    conn = data(await c.post("/api/admin/quickbi/connections", json={
        "name": "主连接", "use_trial": True, "visibility": "group",
        "scenic_id": SCENIC, "user_token": mock.USER_TOKEN}))
    data(await c.post("/api/admin/quickbi/assets/dashboards",
                      json={"url": mock.PAGE_ID, "connection_id": conn["id"], "scenic_id": SCENIC}))
    async with common_db.get_sessionmaker()() as db:
        await binding.ensure_theme_issues(db, SCENIC, conn["id"], overwrite=True)
    return conn["id"]


def _sse_events(text: str) -> list[dict]:
    import json as _json

    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        payload = "\n".join(ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:"))
        if payload:
            try:
                out.append(_json.loads(payload))
            except ValueError:
                pass
    return out


def test_theme_report_runs_and_does_not_500():
    """5 条主题解读逐条发起:都要 200,并且流里出现 run/done,done 不是 failed。

    issue_id 比老表的 UUID 长,这条测试正是按**每个主题各跑一遍**写的 ——
    最长的那个(issue_theme_ota_direct_sales_xxxxxxxx)才是压到列宽上限的那一个。
    """
    async def main():
        from cogni_agent.gateway_service import scenic_binding as binding

        app = _app()
        async with _client(app) as c:
            await _seed(c)
            for theme in binding.THEMES:
                async with common_db.get_sessionmaker()() as db:
                    row = await binding.theme_issue(db, SCENIC, theme["code"])
                    assert row is not None, f"{theme['name']} 没生成出来"
                    issue_id = row.issue_id
                r = await c.post("/api/admin/quickbi/run",
                                 json={"fixed_question_id": issue_id, "scenic_id": SCENIC, "stream": True})
                assert r.status_code == 200, (theme["name"], r.status_code, r.text[:400])
                events = _sse_events(r.text)
                kinds = [e.get("type") for e in events]
                assert "run" in kinds and "done" in kinds, (theme["name"], kinds)
                done = next(e for e in events if e.get("type") == "done")
                # v3.57.2:模拟看板上只有「销售概览」一张表 —— 客群 / 舆情 / OTA 在这块看板上
                # 本来就没有数据,现在明确失败并说清原因,而不是拿销售表凑一份跑题报告。
                # 这条测试钉的是「不 500、运行记录写得进去」,所以只放过这一种有原因的失败。
                if done["status"] == "failed":
                    assert "没有找到" in (done.get("error") or "") and theme["name"] in ("客群", "舆情", "OTA直销"), \
                        (theme["name"], done.get("error"))
                else:
                    assert theme["name"] in ("概览", "票务", "客群", "舆情", "OTA直销")
    asyncio.run(main())


def test_run_record_keeps_the_full_issue_id():
    """运行记录要**完整**存下 issue_id —— 截断的话「按问题看运行记录」会一条都查不到。"""
    async def main():
        from cogni_agent.gateway_service import scenic_binding as binding
        from cogni_agent.tool_center_service.quickbi.models import QbiRun
        from sqlalchemy import select

        app = _app()
        async with _client(app) as c:
            await _seed(c)
            async with common_db.get_sessionmaker()() as db:
                issue_id = (await binding.theme_issue(db, SCENIC, "ota_direct_sales")).issue_id
            assert len(issue_id) > 36, "这条测试的前提是 issue_id 比老表 UUID 长"
            await c.post("/api/admin/quickbi/run",
                         json={"fixed_question_id": issue_id, "scenic_id": SCENIC, "stream": True})
            async with common_db.get_sessionmaker()() as db:
                stored = (await db.execute(select(QbiRun.fixed_question_id))).scalars().all()
            assert issue_id in stored, stored
    asyncio.run(main())


def test_model_instruction_goes_upstream():
    """按钮文案短,模型需要的恰恰是那些啰嗦的约束 —— 口径必须跟着发出去,
    而留痕/展示用的 question 保持原样(否则运行记录里全是一大段口径)。"""
    async def main():
        from cogni_agent.gateway_service import scenic_binding as binding

        app = _app()
        async with _client(app) as c:
            await _seed(c)
            async with common_db.get_sessionmaker()() as db:
                row = await binding.theme_issue(db, SCENIC, "public_opinion")
                issue_id, instruction = row.issue_id, row.model_instruction
            assert instruction, "主题解读必须带口径说明"
            r = await c.post("/api/admin/quickbi/run",
                             json={"fixed_question_id": issue_id, "scenic_id": SCENIC, "stream": True})
            run_evt = next(e for e in _sse_events(r.text) if e.get("type") == "run")
            assert instruction[:12] in run_evt["sent_question"], run_evt["sent_question"]
            assert run_evt["sent_question"].startswith(f"{binding.THEME_PREFIX}舆情")
    asyncio.run(main())


def test_internal_failure_comes_back_as_a_readable_message_not_a_bare_500(monkeypatch):
    """首事件之前炸了,要回一句人话(而不是裸 500 让前端只显示「请求失败 (500)」)。"""
    async def main():
        from cogni_agent.gateway_service import scenic_binding as binding
        from cogni_agent.tool_center_service.quickbi import service as qsvc

        app = _app()
        async with _client(app) as c:
            await _seed(c)
            async with common_db.get_sessionmaker()() as db:
                issue_id = (await binding.theme_issue(db, SCENIC, "ticket")).issue_id

            real = qsvc._bind_scenic

            def boom(actor, scenic_id):        # 模拟准备阶段的任意内部异常
                raise RuntimeError("模拟的内部故障")

            monkeypatch.setattr(qsvc, "_bind_scenic", boom)
            try:
                r = await c.post("/api/admin/quickbi/run",
                                 json={"fixed_question_id": issue_id, "scenic_id": SCENIC, "stream": True})
            finally:
                monkeypatch.setattr(qsvc, "_bind_scenic", real)
            body = r.json()
            assert r.status_code == 500 and body["code"] != 0
            # 关键:消息里说得出「是什么」和「怎么办」,不是空的
            assert "模拟的内部故障" in body["message"] and "运行记录" in body["message"]
    asyncio.run(main())
