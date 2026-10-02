"""回归记录:401 定位过程的调试测试。

背景:test_members.py 中 `test_create_user_as_super_admin` 曾在 pytest 全量运行时 401,
单独运行却 200。定位后根因是 `Bearer Bearer xxx` 双前缀(`_token` 已含 "Bearer ",
外层又拼了一次),与测试顺序、JWT_SECRET 加载时序无关。已修复 test_members.py 第 18 行。

本文件保留为回归测试:验证同一请求链路下连续两个管理员操作均正常,
且 get_settings 中的 jwt_secret 与测试签发 secret 保持一致。
"""
import os

import pytest

from cogni_agent.config import get_settings
from cogni_agent.user_center_service.models import User


@pytest.mark.asyncio
async def test_continuous_admin_requests_stay_authenticated(async_client, db_session):
    from tests.conftest import TEST_JWT_SECRET, _make_user, _issue_token

    # 配置一致性:get_settings 解析出的 jwt_secret 必须与测试签发 token 的 secret 相同
    cfg = get_settings().get("user_center", default={})
    assert cfg.get("jwt_secret") == TEST_JWT_SECRET
    assert os.environ.get("JWT_SECRET") == TEST_JWT_SECRET

    # 同一 session 内连续两个管理员操作(创建用户)都应 200 —— 复现原 401 的请求链路
    sa = _make_user("super_admin", account="sa_keep", phone="138****sa_keep")
    db_session.add(sa)
    await db_session.commit()

    res1 = await async_client.post("/api/admin/users", json={
        "name": "新开发者", "account": "keep_dev", "phone": "13800000002", "role": "group_developer",
    }, headers={"Authorization": f"Bearer {_issue_token(sa)}"})
    assert res1.status_code == 200
    assert res1.json()["code"] == 0

    res2 = await async_client.post("/api/admin/users", json={
        "name": "新集团管理员", "account": "keep_admin", "phone": "13800000001", "role": "group_admin",
    }, headers={"Authorization": f"Bearer {_issue_token(sa)}"})
    assert res2.status_code == 200
    assert res2.json()["code"] == 0
