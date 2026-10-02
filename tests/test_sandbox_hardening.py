"""
本机沙箱加固回归测试。

本机沙箱是生产主执行路径(Docker 是可选扩展),这里的每一条都对应一个真实发生过、
或真实可能发生的问题。
"""
from __future__ import annotations

import asyncio
import os

import pytest

import cogni_agent.config as cfgmod
from cogni_agent.tool_center_service.sandbox.local import LocalSandbox, _sandbox_env


@pytest.fixture(autouse=True)
def _fast_sandbox():
    sb = cfgmod.get_settings().raw.setdefault("tool_center", {}).setdefault("sandbox", {})
    saved = dict(sb)
    sb.update({"python_timeout_seconds": 5, "python_max_output_bytes": 100_000,
               "python_env_passthrough": []})
    yield
    sb.clear()
    sb.update(saved)


async def _run(script: str, params: dict | None = None) -> dict:
    return await LocalSandbox().execute(tool_id="t", tool_type="python",
                                        type_config={"script": script}, params=params or {})


@pytest.mark.asyncio
async def test_secrets_are_not_leaked_to_plugin():
    """加固前实测可复现:三行插件就能读走凭证库主密钥/JWT密钥/数据库口令。

    凭证库主密钥泄露 = 所有已存工具凭证可解密;JWT 密钥泄露 = 可伪造任意角色令牌。
    """
    os.environ["CREDENTIAL_FERNET_KEY"] = "master-key-should-never-leak"
    os.environ["JWT_SECRET"] = "jwt-should-never-leak"
    os.environ["POSTGRES_PASSWORD"] = "db-pw-should-never-leak"

    r = await _run("""
import os, json
bad = [k for k in os.environ if any(s in k for s in ("SECRET", "PASSWORD", "KEY", "TOKEN"))]
print(json.dumps({"leaked": bad}))
""")
    assert r["ok"] is True
    assert r["result"]["leaked"] == [], f"插件仍能读到敏感环境变量:{r['result']['leaked']}"


@pytest.mark.asyncio
async def test_cwd_is_confined_to_tempdir():
    """加固前 cwd 是项目根目录,插件可以直接读源码和 .env。"""
    r = await _run("import os, json; print(json.dumps({'cwd': os.getcwd()}))")
    cwd = r["result"]["cwd"]
    assert "cogni_tool_" in cwd, f"cwd 应被限制在一次性临时目录内,实际是 {cwd}"


@pytest.mark.asyncio
async def test_env_passthrough_allowlist_works():
    """确实需要的变量可以显式放行——但必须是显式的。"""
    os.environ["MY_PLUGIN_TOKEN"] = "allowed-value"
    cfgmod.get_settings().raw["tool_center"]["sandbox"]["python_env_passthrough"] = ["MY_PLUGIN_TOKEN"]
    r = await _run("import os, json; print(json.dumps({'v': os.environ.get('MY_PLUGIN_TOKEN')}))")
    assert r["result"]["v"] == "allowed-value"


@pytest.mark.asyncio
async def test_stdout_bomb_is_capped_quickly():
    """5MB 输出必须被截断并**快速**返回。

    这里断言耗时,是因为第一版实现虽然"逻辑上"会截断,却因为 gather 干等另一路的
    EOF 而挂死到外层超时——功能对了、行为完全错了,只断言返回值发现不了。
    """
    started = asyncio.get_event_loop().time()
    r = await asyncio.wait_for(_run("print('x' * 5_000_000)"), timeout=20)
    elapsed = asyncio.get_event_loop().time() - started
    assert r["ok"] is False and "超过上限" in r["error"]
    assert elapsed < 5, f"截断应当立即返回,实际用了 {elapsed:.1f}s(说明在等另一路 EOF)"


@pytest.mark.asyncio
async def test_stderr_bomb_is_capped():
    r = await asyncio.wait_for(_run("import sys; sys.stderr.write('e' * 5_000_000)"), timeout=20)
    assert r["ok"] is False and "超过上限" in r["error"]


@pytest.mark.asyncio
async def test_timeout_kills_process_group():
    r = await asyncio.wait_for(_run("while True: pass"), timeout=20)
    assert r["ok"] is False and "超时" in r["error"]


@pytest.mark.asyncio
async def test_normal_plugin_still_works():
    r = await _run(
        "import sys, json; d = json.loads(sys.stdin.read() or '{}');"
        " print(json.dumps({'echo': d.get('city')}, ensure_ascii=False))",
        {"city": "武夷山"},
    )
    assert r == {"ok": True, "result": {"echo": "武夷山"}}


def test_sandbox_env_is_minimal_by_default():
    env = _sandbox_env("/tmp/x")
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TMPDIR", "HOME",
                        "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"}
    # HOME 指向临时目录,避免插件往真实用户目录写东西
    assert env["HOME"] == "/tmp/x"


def test_docker_sandbox_delegates_http_and_db():
    """http/db 不需要容器隔离(而且 --network=none 会让 http 工具直接不可用),
    应委托回本机执行器。"""
    from cogni_agent.tool_center_service.sandbox.docker_exec import DockerSandbox
    from cogni_agent.tool_center_service.sandbox.local import LocalSandbox as L

    assert isinstance(DockerSandbox()._local, L)
