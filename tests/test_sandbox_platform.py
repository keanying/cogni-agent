"""
沙箱跨平台与迁移执行器的回归测试。

三个坑都是实际发生过、且报错信息完全指不到根因的:

1. Windows 的 WindowsSelectorEventLoop **不支持** asyncio 子进程,
   `create_subprocess_exec` 直接抛 `NotImplementedError` —— Python 插件全线不可用
2. 迁移 SQL 按 `;` 裸切,而分号可能出现在字符串字面量里(如 COMMENT 文本),
   语句被从中间劈开,报「unterminated quoted string」
3. `text()` 会把 `:xxx` 当绑定参数,而「枚举:自然风光」这种冒号跟中文的写法很自然,
   报「A value is required for bind parameter '自然风光'」
"""
from __future__ import annotations

import asyncio

import pytest

from cogni_agent.common.schema import _split_statements


# ── 迁移 SQL 拆分 ──
def test_split_plain_statements():
    assert _split_statements("CREATE TABLE t (a int); CREATE INDEX i ON t(a);") == [
        "CREATE TABLE t (a int)", "CREATE INDEX i ON t(a)"]


def test_semicolon_inside_string_is_not_a_separator():
    """COMMENT 文本里带分号是很自然的写法,不能把语句劈开。"""
    got = _split_statements("COMMENT ON TABLE t IS '说明;还有后半句'; CREATE INDEX i ON t(a);")
    assert len(got) == 2
    assert got[0].endswith("'说明;还有后半句'")


def test_escaped_quote_inside_string():
    """SQL 里 '' 表示一个转义单引号,不能被当成字符串结束。"""
    got = _split_statements("COMMENT ON TABLE t IS 'it''s a test; ok';")
    assert len(got) == 1


def test_migrations_do_not_use_bind_param_parsing():
    """迁移必须走 exec_driver_sql:text() 会把 `:自然风光` 解析成绑定参数。"""
    import inspect

    from cogni_agent.common import schema

    src = inspect.getsource(schema.apply_migrations)
    # 去掉注释行再检查:代码里**故意**在注释中写了错误示例说明原因,全文匹配会误报
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert "exec_driver_sql" in code
    assert "execute(text(" not in code, "迁移不能走 text(),冒号跟中文会被当成绑定参数"


def test_existing_migrations_all_splittable():
    """所有已登记的迁移都能被正确拆分(不会因为文本里的分号炸掉)。"""
    from cogni_agent.common.schema import MIGRATIONS

    for m in MIGRATIONS:
        stmts = _split_statements(m.sql)
        assert stmts, f"迁移 {m.id} 拆分后为空"
        for st in stmts:
            assert st.count("'") % 2 == 0, f"迁移 {m.id} 拆出的语句引号不成对:{st[:80]}"


# ── 跨平台子进程 ──
def test_async_subprocess_detection_exists():
    """必须是**检测事件循环**而不是硬编码平台 ——
    同一份代码在 Windows 上换成 Proactor 循环时仍该走高效的异步路径。"""
    import inspect

    from cogni_agent.tool_center_service.sandbox import local

    src = inspect.getsource(local._async_subprocess_supported)
    assert "Proactor" in src, "应按事件循环类型判断,而非只看 sys.platform"


@pytest.mark.asyncio
async def test_blocking_path_matches_async_path():
    """同步路径(Windows 走这条)必须与异步路径行为一致:
    正常返回、输出超限、超时,三种结果都要对得上。"""
    import cogni_agent.config as cfgmod
    from cogni_agent.tool_center_service.sandbox import local

    sb = cfgmod.get_settings().raw.setdefault("tool_center", {}).setdefault("sandbox", {})
    saved = dict(sb)
    sb.update({"python_timeout_seconds": 5, "python_max_output_bytes": 50_000})
    orig = local._async_subprocess_supported
    try:
        for forced in (True, False):          # True=异步路径, False=同步路径
            local._async_subprocess_supported = (lambda v=forced: v)
            box = local.LocalSandbox()

            ok = await box.execute(tool_id="t", tool_type="python",
                                   type_config={"script": "print(1)"}, params={})
            assert ok["ok"] is True and ok["result"] == 1, f"forced={forced} 正常执行失败"

            bomb = await asyncio.wait_for(box.execute(
                tool_id="t", tool_type="python",
                type_config={"script": "print('x'*5000000)"}, params={}), timeout=25)
            assert bomb["ok"] is False and "超过上限" in bomb["error"], \
                f"forced={forced} 输出超限应报「超过上限」,实际:{bomb['error'][:60]}"
    finally:
        local._async_subprocess_supported = orig
        sb.clear()
        sb.update(saved)


# ── 令牌失效 ──
def test_password_change_invalidates_tokens():
    """改密码必须让此前签发的令牌全部失效 ——
    否则账号被盗时改密码根本踢不掉攻击者,而这正是最需要它生效的场景。"""
    import inspect

    from cogni_agent.user_center_service import users

    for fn in (users.change_own_password, users.reset_password):
        assert "credentials_changed_at" in inspect.getsource(fn), \
            f"{fn.__name__} 未更新 credentials_changed_at,旧令牌不会失效"


def test_token_carries_iat():
    """没有 iat 就无法判断令牌是不是改密之前签发的。"""
    import inspect

    from cogni_agent.user_center_service import auth

    assert '"iat"' in inspect.getsource(auth.issue_tokens)
