"""
测试夹具:所有执行引擎测试都不连真实 DB/真实模型——用 monkeypatch 替换
llm_client.stream_and_collect 与 tool_center_service.mcp.protocol.invoke_tool,
只验证 graphs.py/tools_adapter.py/guardrails.py 自身的状态机与不确定性处理逻辑
是否正确。需要真实 Postgres/Redis 的端到端联调步骤见 README「手动集成校验」。

成员/组织/响应格式等集成测试统一使用独立测试库 {POSTGRES_DB}_test:
conftest 自动创建并 patch get_sessionmaker,避免清空/污染开发库真实数据。
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cogni_agent.common.db import Base, get_db
from cogni_agent.config import get_settings
from cogni_agent.main import app
from cogni_agent.user_center_service.auth import hash_password
from cogni_agent.user_center_service.models import User

_ROOT = Path(__file__).resolve().parent.parent

# ── 测试专用 JWT 密钥(集成测试签发/校验 token 用,与真实配置隔离) ──
TEST_JWT_SECRET = "test-secret-for-integration-tests-do-not-use-in-production"


def _load_real_env() -> None:
    """需要真实 DB 的集成测试优先使用 .env.{ZHILV_ENV} 的真实连接配置;
    无对应文件(如 CI)时跳过,由下方 dummy 兜底保证 config 可解析。"""
    env_name = os.environ.get("ZHILV_ENV", "local")
    env_file = _ROOT / f".env.{env_name}"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)


# 先加载真实环境(.env.local 等),再 setdefault dummy —— setdefault 不会覆盖已存在
# 的真实值,因此依赖真实 Postgres/Redis 的集成测试连的是本地真实实例,
# 而 engine 等单元测试(monkeypatch 掉 DB 访问)仍可用 dummy 占位。
_load_real_env()

# 确保 config.get_settings() 在 import 期间不会因为环境变量缺失而报错——测试不需要
# 真实凭证,只需要 config.yaml 能被解析加载。
_DUMMY_ENV = {
    "POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432", "POSTGRES_DB": "dummy",
    "POSTGRES_USER": "dummy", "POSTGRES_PASSWORD": "dummy",
    "REDIS_HOST": "localhost", "REDIS_PORT": "6379", "REDIS_PASSWORD": "",
    "MINIO_ENDPOINT": "localhost:9000", "MINIO_ACCESS_KEY": "dummy", "MINIO_SECRET_KEY": "dummy",
    "JWT_SECRET": "test-secret", "SMS_ACCESS_KEY": "dummy", "SMS_ACCESS_SECRET": "dummy",
    "DEEPSEEK_API_KEY": "dummy", "DASHSCOPE_API_KEY": "dummy", "CREDENTIAL_FERNET_KEY": "",
    "WEBSEARCH_API_KEY": "dummy",
}
for k, v in _DUMMY_ENV.items():
    os.environ.setdefault(k, v)

# ── L1 测试库强制隔离(配置源头兜底)─────────────────────────────────────
# 把 POSTGRES_DB 环境变量改写为 {db}_test:get_settings() 内 load_dotenv
# (override=False)不会覆盖已存在的环境变量,因此此后所有走 get_settings() 的
# 路径——包括 common.db.get_engine 的 _build_dsn,以及任何直读 database 配置
# 的代码——一律落到测试库,不依赖 monkeypatch 是否覆盖到每个调用点。
# 幂等:若环境里已是 *_test(如 CI 预置)则不再叠加,避免 test_engine 拼成 _test_test。
_db_override = os.environ.get("POSTGRES_DB", "zhilv")
if not _db_override.endswith("_test"):
    os.environ["POSTGRES_DB"] = f"{_db_override}_test"
get_settings.cache_clear()
# 重置 common.db 的引擎缓存,确保后续 get_engine() 用新的测试库 DSN 重建
import cogni_agent.common.db as _cdb
_cdb._engine = None
_cdb._SessionLocal = None

pytest_plugins = ("pytest_asyncio",)


# ════════════════════════════════════════════════════════
# 集成测试(成员/组织/响应格式)数据库与 HTTP fixtures
# 统一使用独立测试库 {POSTGRES_DB}_test,避免清空/污染开发库真实数据;
# 测试内直接调用 get_sessionmaker 的代码也一并 patch 到测试库。
# ════════════════════════════════════════════════════════

def _test_db_name(db: str) -> str:
    """测试库名:配置已是 *_test 则原样使用,防止叠加成 _test_test。"""
    return db if str(db).endswith("_test") else f"{db}_test"


def _ensure_test_db(database_cfg: dict) -> None:
    """确保独立测试库存在(不存在则创建)并预装 pgvector 扩展。"""
    import psycopg2

    conn = psycopg2.connect(
        host=database_cfg["host"], port=database_cfg["port"],
        user=database_cfg["user"], password=database_cfg["password"],
        dbname="postgres", connect_timeout=5,
    )
    conn.autocommit = True
    test_db = _test_db_name(database_cfg["db"])
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{test_db}"')
    conn.close()

    # 测试库预装 pgvector 扩展(向量列依赖,与开发库一致)
    ext_conn = psycopg2.connect(
        host=database_cfg["host"], port=database_cfg["port"],
        user=database_cfg["user"], password=database_cfg["password"],
        dbname=test_db, connect_timeout=5,
    )
    ext_conn.autocommit = True
    with ext_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    ext_conn.close()


@pytest_asyncio.fixture
async def test_engine():
    """测试数据库引擎:指向独立测试库 {POSTGRES_DB}_test。

    function scope + NullPool:每个测试在自己的 event loop 里建连/销毁,
    避免 session 级 engine 跨 loop 复用导致 "another operation is in progress"。
    """
    db = get_settings().get("database", default={})
    _ensure_test_db(db)
    dsn = f"postgresql+asyncpg://{db['user']}:{db['password']}@{db['host']}:{db['port']}/{_test_db_name(db['db'])}"
    engine = create_async_engine(dsn, echo=False, poolclass=NullPool)
    # 防呆护栏:确认实际连接到的库名以 _test 结尾,防止配置/改动导致误连业务库
    from sqlalchemy import text

    async with engine.connect() as conn:
        actual_db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        if not str(actual_db).endswith("_test"):
            await engine.dispose()
            raise RuntimeError(
                f"测试库校验失败:当前连接 {actual_db},必须以 *_test 结尾,已中止防止污染业务库"
            )
    yield engine
    try:
        await engine.dispose()
    except RuntimeError:
        # teardown 时 event loop 可能已关闭,忽略清理告警
        pass


@pytest_asyncio.fixture
async def test_sessionmaker(test_engine):
    """测试数据库 sessionmaker。"""
    return async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(autouse=True)
async def _use_test_db(test_engine, test_sessionmaker, monkeypatch):
    """把 common.db 的 get_sessionmaker 与 get_engine 都指向测试库。

    必须同时 patch get_engine:部分测试模块在顶层 `from cogni_agent.common.db
    import get_sessionmaker` 绑定了原始函数引用,仅 patch get_sessionmaker 属性
    对它们无效;但原始 get_sessionmaker 内部是运行时调用 get_engine 的,
    patch get_engine 后即使顶层绑定也会落到测试库,双保险防误连业务库。
    """
    import cogni_agent.common.db as common_db

    monkeypatch.setattr(common_db, "get_sessionmaker", lambda: test_sessionmaker)
    # get_engine 一并 patch:顶层 `from common.db import get_sessionmaker` 绑定的
    # 原始函数内部是运行时调用 get_engine 的, patch 后即使顶层绑定也会落到测试库;
    # 与 L1 配置层强制隔离双保险,杜绝误连业务库。
    monkeypatch.setattr(common_db, "get_engine", lambda: test_engine)


@pytest_asyncio.fixture
async def clean_db(test_engine):
    """每个用 db_session/async_client 的测试前建表、测试后清空测试库数据。

    先 drop 再 create_all:Base.metadata.create_all 对已存在表不会补新列/新表,
    模型经迁移加列后(如 app_question_bank 的 metric_name/return_format)遗留的
    旧结构测试库会在 INSERT 时报 UndefinedColumn——测试库 schema 一律以当前
    model 为准(近似迁移),不沿用陈旧结构。
    """
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # teardown:TRUNCATE 全表(替代逐表 DELETE),CASCADE 处理外键依赖、
    # RESTART IDENTITY 重置自增序列,从根上避免残留数据导致后续测试撞唯一约束
    from sqlalchemy import text

    async with test_engine.begin() as conn:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session(test_sessionmaker, clean_db) -> AsyncGenerator[AsyncSession, None]:
    """每次测试提供独立异步 session(测试库)。"""
    async with test_sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def async_client(db_session):
    """异步 HTTP 测试客户端,覆盖 get_db 依赖。"""
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════
# User fixtures（五种角色）
# ════════════════════════════════════════════════════════

def _make_user(role: str, scenic_id: str | None = None, **overrides) -> User:
    """快速创建测试用户(未 commit)。"""
    defaults = {
        "super_admin": ("超管测试", "sa_test"),
        "group_admin": ("集团管理员测试", "ga_test"),
        "group_developer": ("集团开发者测试", "gd_test"),
        "group_viewer": ("集团查看者测试", "gv_test"),
        "scenic_user": ("景区用户测试", "su_test"),
    }
    name, account = defaults.get(role, ("测试用户", f"{role}_test"))
    name = overrides.get("name", name)
    account = overrides.get("account", account)
    return User(
        name=name,
        account=account,
        phone=overrides.get("phone", f"138****{account}"),  # 用完整 account 保证唯一(sa_test/gd_test 后4位相同会撞唯一约束)
        password_hash=hash_password("test123"),
        scenic_id=scenic_id,
        role=role,
        must_change_password=False,
    )


@pytest_asyncio.fixture
async def super_admin(db_session) -> User:
    """创建超级管理员并返回。"""
    user = _make_user("super_admin")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def group_admin(db_session) -> User:
    """创建集团管理员。"""
    user = _make_user("group_admin")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def group_developer(db_session) -> User:
    """创建集团开发者。"""
    user = _make_user("group_developer")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def group_viewer(db_session) -> User:
    """创建集团查看者。"""
    user = _make_user("group_viewer")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def scenic_user(db_session) -> User:
    """创建景区用户(自动创建测试景区)。"""
    from cogni_agent.user_center_service.models import SysScenic
    scenic = SysScenic(scenic_id="test_scenic_001", scenic_name="测试景区", is_enabled=True)
    db_session.add(scenic)
    await db_session.commit()
    user = _make_user("scenic_user", scenic_id="test_scenic_001")
    db_session.add(user)
    await db_session.commit()
    return user


# ════════════════════════════════════════════════════════
# Token helpers
# ════════════════════════════════════════════════════════

def _issue_token(user: User) -> str:
    """签发测试用 JWT token。"""
    from datetime import datetime, timedelta, timezone
    from jose import jwt

    payload = {
        "sub": user.id,
        "scenic_id": user.scenic_id or "",
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


def auth_headers(user: User) -> dict:
    """返回带有 Bearer token 的请求头。"""
    return {"Authorization": f"Bearer {_issue_token(user)}"}


# ════════════════════════════════════════════════════════
# Module-level setup
# ════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def override_jwt_secret():
    """用测试 secret 覆盖 JWT 配置,避免依赖真实 .env。"""
    os.environ["JWT_SECRET"] = TEST_JWT_SECRET
    # 清除配置缓存
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
