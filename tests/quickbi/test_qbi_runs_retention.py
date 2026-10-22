"""tc_qbi_runs 保留任务:到点判定 + 只删保留天数之前的行。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from cogni_agent.common import db as common_db
from cogni_agent.cronjobs import qbi_runs_retention as job
from cogni_agent.tool_center_service.quickbi.models import QbiRun


def test_due_is_after_one_oclock_once_per_day():
    d = date(2026, 10, 22)
    assert not job.due(datetime(2026, 10, 22, 0, 59), None)
    assert job.due(datetime(2026, 10, 22, 1, 0), None)
    assert job.due(datetime(2026, 10, 22, 3, 30), date(2026, 10, 21))    # 晚起也补上
    assert not job.due(datetime(2026, 10, 22, 13, 0), d)                 # 今天跑过了


def test_retention_days_has_a_floor(monkeypatch):
    from cogni_agent.config import get_settings

    settings = get_settings()
    q = dict(settings.raw.get("quickbi") or {})
    for raw, want in ((3, 7), (30, 30), (0, 0), ("x", 7)):
        q["run_retention_days"] = raw
        monkeypatch.setitem(settings.raw, "quickbi", q)
        assert job.retention_days() == want


async def test_run_deletes_only_expired_rows(test_engine):
    async with test_engine.begin() as conn:
        await conn.run_sync(lambda c: QbiRun.__table__.create(c, checkfirst=True))
    now = datetime.now(timezone.utc)
    async with common_db.get_sessionmaker()() as db:
        await db.execute(QbiRun.__table__.delete())
        db.add_all([
            QbiRun(id="old", connection_id="c", capability="dataset_qa", mode="direct", question="q",
                   status="success", user_id="u", created_at=now - timedelta(days=8)),
            QbiRun(id="new", connection_id="c", capability="dataset_qa", mode="direct", question="q",
                   status="success", user_id="u", created_at=now - timedelta(days=6)),
        ])
        await db.commit()
    job._last_run_date = None
    assert await job.run(force=True) == 1
    async with common_db.get_sessionmaker()() as db:
        left = set((await db.execute(select(QbiRun.id))).scalars())
    assert left == {"new"}
