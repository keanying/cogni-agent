"""
P0:subprocess + 资源限额沙箱。仅建议用于受信工具,隔离强度有限(见 README 安全说明)。

按 Tool.type 分发到三套真实执行器:
- http:  httpx 发起真实请求,{{now}}/{{org_id}} 等全局变量在发起前渲染。
- db:    SQL 模板变量转参数化占位符执行(不做字符串拼接,规避注入),强制只读校验
         (只允许 SELECT),按 db_type 分发到 asyncpg(postgres)/aiomysql(mysql 协议兼容的
         MySQL/SelectDB/StarRocks)/clickhouse-connect(ClickHouse,可选依赖,未安装时给
         出明确报错而不是让人以为是别的原因失败)。
- python:子进程执行,JSON 走 stdin/stdout,CPU/内存限额 + 超时 kill;不提供网络隔离
         (需要真正的网络隔离时切到 DockerSandbox,见 tool_center.sandbox.mode=docker)。
"""
from __future__ import annotations

import asyncio
import json
import datetime as dt
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from cogni_agent.common.logging import get_logger
from cogni_agent.common.time import now_cst
from cogni_agent.config import get_settings
from cogni_agent.tool_center_service.sandbox.base import SandboxExecutor

log = get_logger("tool_center_service.sandbox.local")

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def system_vars(ctx: dict | None = None) -> dict:
    """系统变量:模板里 {{scenic_id}} / {{today}} 这类不由模型填、由执行引擎注入的值。

    **这一层原先是缺的**,后果很具体:工具模板里写 `{{scenic_id}}` ——
    HTTP 工具的 URL 里留着字面量 `{{scenic_id}}` 打出去,DB 工具则把它绑成 NULL,
    查出来永远是空集。而 `POST /admin/tools/preview` 又是渲染过的,
    于是管理员预览一切正常、线上一跑就是空 —— 这种「预览对、线上错」最难查。

    时间变量一律用景区本地时区(CST)算好再给出去:让模型自己推日期,
    推错了很难发现 —— 它照样会给你一份结构完整、数字却对不上的回答。
    """
    now = now_cst()
    today = now.date()
    ctx = ctx or {}
    out = {
        "now": now.isoformat(),
        "today": today.isoformat(),
        "yesterday": (today - dt.timedelta(days=1)).isoformat(),
        "month_start": today.replace(day=1).isoformat(),
        "year": str(today.year),
        "month": f"{today.month:02d}",
        "day": f"{today.day:02d}",
    }
    for k in ("scenic_id", "user_id", "org_id", "role"):
        v = ctx.get(k)
        if v not in (None, ""):
            out[k] = str(v)
    # 系统参数({{ota_account}},系统设置 · 参数配置 + 景区覆盖):不许覆盖上面的内置变量 ——
    # 否则一个叫 scenic_id 的参数就能把请求改到别的景区去
    for k, v in (ctx.get("extra_vars") or {}).items():
        if k not in out and v not in (None, ""):
            out[str(k)] = str(v)
    return out


def _render_template_str(template: str, variables: dict) -> str:
    def _sub(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))

    return _VAR_PATTERN.sub(_sub, template)


def _render_any(value, variables: dict):
    if isinstance(value, str):
        return _render_template_str(value, variables)
    if isinstance(value, dict):
        return {k: _render_any(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_any(v, variables) for v in value]
    return value


def _inject_platform_secrets(headers: dict) -> dict:
    """把请求头里的 `{{平台密钥名}}` 换成服务端配置里的真实值。

    **只作用于请求头**,不进 url / 请求体的变量表 —— 放进通用变量表的话,
    一个恰好同名的模型参数就能决定它的值,或者被拼进 url 里打到日志上。
    而且在模型参数与系统变量之后单独做:谁都覆盖不了它。

    配了占位却没配值时**直接报错**。把 `{{data_platform_key}}` 原样发给对方,
    换回来的是一个 401 —— 看起来像「接口坏了 / 权限不够」,
    没人会想到是本机少配了一个环境变量。
    """
    # 后台「系统设置 · 平台密钥」优先,其次环境变量(见 platform_secrets)
    from cogni_agent.governance_event_bus_service import platform_secrets as ps

    secrets = ps.all_values()
    out = {}
    for k, v in (headers or {}).items():
        if isinstance(v, str) and "{{" in v:
            for name, val in secrets.items():
                token = "{{" + str(name) + "}}"
                if token in v:
                    if not val:
                        label = ps.KNOWN.get(name, {}).get("label", name)
                        raise ValueError(
                            f"请求头 {k} 需要平台密钥「{label}」,但还没有配置。"
                            f"请到管理后台「系统设置 → 平台密钥」填写(或设置环境变量 "
                            f"{ps.KNOWN.get(name, {}).get('env', name.upper())})")
                    v = v.replace(token, str(val))
        out[k] = v
    return out


class LocalSandbox(SandboxExecutor):
    async def execute(self, *, tool_id: str, tool_type: str, type_config: dict, params: dict,
                      context: dict | None = None) -> dict:
        """context = 执行上下文(scenic_id / user_id / org_id / role),用于渲染系统变量。

        系统变量**优先于**模型传的同名参数:允许模型覆盖 scenic_id 的话,
        它就能把请求打到别的景区去 —— 那是跨租户,不是参数。
        """
        timeout = get_settings().get("tool_center", "sandbox", "timeout_seconds", default=30)
        sysv = system_vars(context)
        if tool_type == "http":
            return await self._execute_http(type_config, params, timeout, sysv)
        if tool_type == "db":
            return await self._execute_db(type_config, params, sysv)
        if tool_type == "python":
            return await self._execute_python(type_config, params)
        raise ValueError(f"未知工具类型: {tool_type}")

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _execute_http(self, type_config: dict, params: dict, timeout: float,
                            sysv: dict | None = None) -> dict:
        # 顺序要紧:模型传的参数在前、系统变量在后 —— 系统变量赢。
        # 反过来的话,模型传一个 scenic_id 就能把请求打到别的景区去。
        variables = {**{k: v for k, v in params.items() if isinstance(v, (str, int, float))},
                     **(sysv or system_vars())}
        method = (type_config.get("method") or "GET").upper()
        url = _render_template_str(type_config.get("url", ""), variables)
        headers = _render_any(type_config.get("headers") or {}, variables)
        if any(isinstance(v, str) and "{{" in v for v in headers.values()):
            from cogni_agent.governance_event_bus_service import platform_secrets as _ps

            await _ps.refresh()          # 后台改了密钥,最迟 30 秒生效,不用重启
        headers = _inject_platform_secrets(headers)
        if not url:
            raise ValueError("HTTP 工具未配置 url")

        async with httpx.AsyncClient(timeout=timeout) as client:
            if method in ("GET", "DELETE"):
                resp = await client.request(method, url, headers=headers, params=params)
            else:
                resp = await client.request(method, url, headers=headers, json=params)
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = resp.text
        return {"status_code": resp.status_code, "body": body}

    # ── DB(只读) ──────────────────────────────────────────────────────
    async def _execute_db(self, type_config: dict, params: dict, sysv: dict | None = None) -> dict:
        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.knowledge_service.database_knowledge.catalog import DbDatasource

        datasource_id = type_config.get("datasource_id")
        sql_template = type_config.get("sql_template", "")
        if not datasource_id or not sql_template:
            raise ValueError("数据库查询工具未配置 datasource_id/sql_template")

        stripped = sql_template.strip().lstrip("(")
        if not re.match(r"(?is)^\s*select\b", stripped):
            raise ValueError("只读强校验:仅允许 SELECT 语句")

        async with get_sessionmaker()() as db:
            ds = await db.get(DbDatasource, datasource_id)
        if ds is None:
            raise ValueError(f"数据源 {datasource_id} 不存在")

        max_rows = get_settings().get("tool_center", "sandbox", "max_sql_rows", default=10000)
        # 同 HTTP:系统变量覆盖模型传的同名参数,{{scenic_id}} 才不会被绑成 NULL
        bind = {**params, **(sysv or system_vars())}
        sql, values = self._render_sql_params(sql_template, bind, ds.db_type)
        if not re.search(r"(?is)\blimit\b", sql):
            sql = f"{sql} LIMIT {int(max_rows)}"

        if ds.db_type == "postgres":
            return await self._run_asyncpg(ds.connection_config, sql, values)
        if ds.db_type in ("mysql", "selectdb", "starrocks"):
            return await self._run_mysql(ds.connection_config, sql, values)
        if ds.db_type == "clickhouse":
            return await self._run_clickhouse(ds.connection_config, sql, values)
        raise ValueError(f"不支持的数据源类型: {ds.db_type}")

    @staticmethod
    def _render_sql_params(sql_template: str, params: dict, db_type: str) -> tuple[str, list]:
        """把 {{var}} 依次替换成驱动对应的参数化占位符,值单独作为参数传给驱动执行——
        不做任何字符串拼接/转义,从根上规避 SQL 注入,而不是依赖转义函数是否覆盖全面。"""
        values: list = []
        counter = {"n": 0}

        def _sub(m: re.Match) -> str:
            key = m.group(1).strip()
            values.append(params.get(key))
            counter["n"] += 1
            if db_type == "postgres":
                return f"${counter['n']}"
            return "%s"

        rendered = _VAR_PATTERN.sub(_sub, sql_template)
        return rendered, values

    @staticmethod
    async def _run_asyncpg(conn_cfg: dict, sql: str, values: list) -> dict:
        import asyncpg

        conn = await asyncpg.connect(
            host=conn_cfg["host"], port=conn_cfg.get("port", 5432), user=conn_cfg["user"],
            password=conn_cfg.get("password"), database=conn_cfg["database"],
        )
        try:
            rows = await conn.fetch(sql, *values)
            return {"columns": list(rows[0].keys()) if rows else [], "rows": [dict(r) for r in rows]}
        finally:
            await conn.close()

    @staticmethod
    async def _run_mysql(conn_cfg: dict, sql: str, values: list) -> dict:
        try:
            import aiomysql
        except ImportError as exc:
            raise RuntimeError(
                "MySQL/SelectDB/StarRocks 数据源需要安装可选依赖 aiomysql(`uv sync --extra db-connectors`)"
            ) from exc

        conn = await aiomysql.connect(
            host=conn_cfg["host"], port=conn_cfg.get("port", 3306), user=conn_cfg["user"],
            password=conn_cfg.get("password"), db=conn_cfg["database"], cursorclass=aiomysql.cursors.DictCursor,
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, values)
                rows = await cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
                return {"columns": columns, "rows": rows}
        finally:
            conn.close()

    @staticmethod
    async def _run_clickhouse(conn_cfg: dict, sql: str, values: list) -> dict:
        try:
            import clickhouse_connect
        except ImportError as exc:
            raise RuntimeError(
                "ClickHouse 数据源需要安装可选依赖 clickhouse-connect(`uv sync --extra db-connectors`)"
            ) from exc

        def _run():
            client = clickhouse_connect.get_client(
                host=conn_cfg["host"], port=conn_cfg.get("port", 8123), username=conn_cfg.get("user"),
                password=conn_cfg.get("password"), database=conn_cfg.get("database"),
            )
            # clickhouse-connect 用 %(name)s 风格具名参数,这里退化为按位置生成的参数字典
            named_sql = sql
            param_dict = {}
            for i, v in enumerate(values):
                placeholder = f"$${i}$$"
                named_sql = named_sql.replace("%s", placeholder, 1)
                param_dict[f"p{i}"] = v
                named_sql = named_sql.replace(placeholder, f"%(p{i})s", 1)
            result = client.query(named_sql, parameters=param_dict)
            return {"columns": list(result.column_names), "rows": [dict(zip(result.column_names, row)) for row in result.result_rows]}

        return await asyncio.to_thread(_run)

    # ── Python 插件 ───────────────────────────────────────────────────
    async def _execute_python(self, type_config: dict, params: dict) -> dict:
        """
        Python 插件执行。**这是生产环境的主执行路径**(Docker 沙箱是可选扩展),
        所以这里的加固要认真做,但也必须对做不到的部分说实话。

        已经做到的:
        - **环境变量清洗**:子进程只拿到一份显式构造的最小环境,不继承父进程 os.environ。
          这条最重要——父进程环境里有 CREDENTIAL_FERNET_KEY(凭证库主密钥)、JWT_SECRET、
          数据库口令、各家模型 API Key。一个三行插件 `print(dict(os.environ))` 就能全部
          带走:凭证库主密钥泄露 = 所有已存工具凭证可解密;JWT 密钥泄露 = 可伪造任意
          角色的令牌。(这个漏洞在加固前是实际存在且已复现的。)
        - **工作目录限制**:cwd 设为一次性临时目录,不再是服务进程的 cwd(原来是项目
          根目录,插件可以直接读源码和 .env)。
        - **解释器隔离** `-I`:忽略 PYTHONPATH / 用户 site-packages / PYTHON* 环境变量。
        - **资源限额**:CPU 时间、地址空间、单文件大小、禁 core dump。
        - **输出上限**:按上限读取而不是无脑 communicate(),避免插件狂打日志把父进程
          内存吃光;stdout/stderr 并发读,避免管道写满导致双方僵死。
        - **进程组回收**:超时后杀整个进程组,而不是只杀直接子进程。

        **明确做不到的**(不说清楚会给人错误的安全感):
        - **fork 炸弹防不住**。`RLIMIT_NPROC` 是**按用户**计数的,而沙箱与服务进程跑在
          同一个用户下:限额设低了父服务自己也 fork 不出线程,设高了拦不住炸弹。真正
          有效的手段是 cgroup 的 pids.max(即 Docker)或给沙箱单独建一个系统用户。
          (这一条是实测出来的——测试时的 fork 炸弹把整个容器搞崩了。)
        - **网络隔离**。不借助 root/namespace 无法阻止子进程发起连接,插件仍可访问内网
          服务、数据库、云元数据端点(169.254.169.254)。
        - **绝对路径的文件读取**、**系统调用过滤(seccomp)**。

        结论:本机沙箱适用于**受信任的、管理员自己编写**的插件,它防的是"手滑"和
        "顺手拿了不该拿的",不是防有意攻击的敌手。要真正的隔离边界就切
        `tool_center.sandbox.mode: docker`。若必须在本机跑不完全受信的插件,至少
        按 README「沙箱安全」一节用 `python_run_as_user` 配一个专用低权限用户,
        让 RLIMIT_NPROC 真正起到隔离作用。
        """
        script = type_config.get("script", "")
        if not script:
            raise ValueError("Python 能力插件未配置 script")

        cfg = get_settings()
        timeout = cfg.get("tool_center", "sandbox", "python_timeout_seconds", default=60)
        max_output = int(cfg.get("tool_center", "sandbox", "python_max_output_bytes", default=1024 * 1024))
        mem_bytes = int(cfg.get("tool_center", "sandbox", "python_memory_bytes", default=512 * 1024 * 1024))
        max_procs = int(cfg.get("tool_center", "sandbox", "python_max_processes", default=64))
        max_file_bytes = int(cfg.get("tool_center", "sandbox", "python_max_file_bytes", default=64 * 1024 * 1024))
        run_as = cfg.get("tool_center", "sandbox", "python_run_as_user", default=None)

        with tempfile.TemporaryDirectory(prefix="cogni_tool_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "tool_script.py").write_text(script, encoding="utf-8")
            argv = [sys.executable, "-I", str(tmp_path / "tool_script.py")]
            env = _sandbox_env(str(tmp_path))
            payload = json.dumps(params, ensure_ascii=False).encode("utf-8")
            preexec = (_make_preexec(timeout, mem_bytes, max_procs, max_file_bytes, run_as)
                       if sys.platform != "win32" else None)

            if _async_subprocess_supported():
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(tmp_path), env=env, preexec_fn=preexec,
                    start_new_session=(sys.platform != "win32"),
                )
                try:
                    stdout, stderr, truncated = await asyncio.wait_for(
                        _communicate_capped(proc, payload, max_output), timeout=timeout)
                except asyncio.TimeoutError:
                    _kill_process_tree(proc)
                    await _reap(proc)
                    return {"ok": False, "error": f"执行超时(>{timeout}s),已终止整个进程组"}
                returncode = proc.returncode or 0
            else:
                # 事件循环不支持 asyncio 子进程(Windows 的 Selector 循环),
                # 退回同步执行并丢到线程里,保证不阻塞事件循环。
                # 保证与异步路径一致:超时、输出上限、进程树回收。
                stdout, stderr, truncated, timed_out, returncode = await asyncio.to_thread(
                    _blocking_exec, argv, payload=payload, cwd=str(tmp_path), env=env,
                    timeout=timeout, max_output=max_output, preexec=preexec)
                if timed_out:
                    return {"ok": False, "error": f"执行超时(>{timeout}s),已终止整个进程树"}

            if truncated:
                return {"ok": False,
                        "error": f"插件输出超过上限({max_output} 字节),已中止。"
                                 f"请只返回必要的结果,不要把原始数据整体打印出来。"}
            if returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:4000].strip()
                return {"ok": False, "error": err or _signal_reason(returncode, timeout, mem_bytes)}

            raw = stdout.decode("utf-8", errors="replace").strip()
            try:
                return {"ok": True, "result": json.loads(raw) if raw else None}
            except json.JSONDecodeError:
                return {"ok": True, "result": raw}


def _async_subprocess_supported() -> bool:
    """当前事件循环是否支持 asyncio 子进程。

    **Windows 上 WindowsSelectorEventLoop 不支持** —— `create_subprocess_exec` 会直接抛
    `NotImplementedError`,只有 ProactorEventLoop 才支持。而 uvicorn / 某些数据库驱动
    出于兼容性会把策略设成 Selector,于是 Python 插件在 Windows 上**完全跑不了**,
    报的还是一个没有任何上下文的 NotImplementedError。

    检测而不是硬编码平台:同一份代码在 Windows 上换成 Proactor 循环时仍走高效的异步路径。
    """
    if sys.platform != "win32":
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return "Proactor" in type(loop).__name__


def _blocking_exec(argv: list[str], *, payload: bytes, cwd: str, env: dict,
                   timeout: float, max_output: int, preexec) -> tuple[bytes, bytes, bool, bool, int]:
    """同步方式执行子进程,由调用方丢到线程里跑。

    给不支持 asyncio 子进程的事件循环(主要是 Windows 的 Selector 循环)兜底,
    并且保持与异步路径**完全相同的保证**:超时、输出上限、进程树回收。

    输出上限用独立读线程 + 分块读实现,而不是 `subprocess.run(capture_output=True)`
    —— 后者会把全部输出读进内存,插件循环打印时父进程会先 OOM,上限就形同虚设。

    返回 (stdout, stderr, 是否截断, 是否超时, 退出码)。
    **超时用独立的布尔位而不是特殊退出码**:最初拿 -9 当超时哨兵,但 SIGKILL 杀掉进程
    后 returncode 本来就是 -9 —— 于是"输出超限被杀"会被误判成"超时",给用户的
    错误提示指向完全错误的方向。
    """
    import subprocess
    import threading

    creationflags = 0
    if sys.platform == "win32":
        # 新建进程组,超时才能连同它派生的子进程一起杀掉
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env,
        preexec_fn=preexec if sys.platform != "win32" else None,
        start_new_session=(sys.platform != "win32"),
        creationflags=creationflags,
    )

    truncated = threading.Event()
    buffers: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}

    def _pump(stream, key: str) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                buffers[key].extend(chunk)
                if len(buffers[key]) > max_output:
                    truncated.set()
                    _kill_blocking(proc)     # 让另一路也立刻收到 EOF,不然会互相等死
                    return
        except (ValueError, OSError):
            return   # 进程被杀后管道关闭,属正常

    threads = [threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()

    try:
        if proc.stdin:
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass            # 插件不读 stdin 直接退出是允许的
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_blocking(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning(f"沙箱子进程 pid={proc.pid} 在强杀后仍未回收")
        return b"", b"", False, True, -9
    finally:
        for t in threads:
            t.join(timeout=2)

    return (bytes(buffers["out"][:max_output]), bytes(buffers["err"][:max_output]),
            truncated.is_set(), False, proc.returncode or 0)


def _kill_blocking(proc) -> None:
    """杀掉同步子进程及其派生进程。"""
    import os
    import signal
    import subprocess

    try:
        if sys.platform == "win32":
            # Windows 没有进程组信号,用 taskkill /T 连子孙一起杀
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _signal_reason(returncode: int, timeout: float, mem_bytes: int) -> str:
    """进程被信号杀死时的解释。

    **被资源限额杀掉的进程 stderr 是空的** —— 内核直接终止它,没机会打印任何东西。
    不处理的话用户拿到的是 `{"ok": false, "error": ""}`,一个什么都没说的错误。
    而这种情况恰恰最需要解释:插件看起来「就是不工作」,真正的原因
    (跑了死循环把 CPU 配额用完)完全没有线索。

    返回码为负数表示被信号终止,绝对值就是信号编号。
    """
    if returncode >= 0:
        return f"插件以非零状态退出(code={returncode}),但没有输出任何错误信息。"

    sig = -returncode
    known = {
        24: (f"CPU 时间超过 {timeout:.0f} 秒限额被终止(SIGXCPU)。"
             f"通常是死循环,或计算量太大 —— 插件应该做数据加工,重活交给数据库。"),
        9:  "被强制终止(SIGKILL)。通常是执行超时,或内存超过限额被系统回收。",
        11: "段错误(SIGSEGV)。一般是调用了有问题的 C 扩展。",
        25: "写入文件超过大小限额被终止(SIGXFSZ)。插件不应该往磁盘写大文件。",
        6:  (f"内存分配失败被终止(SIGABRT)。当前限额 {mem_bytes // 1024 // 1024}MB,"
             f"一次性读入过大的数据会触发。"),
    }
    return known.get(sig, f"被信号 {sig} 终止,且没有输出错误信息。")


def _sandbox_env(tmp_dir: str) -> dict:
    """构造给插件子进程的**最小环境**。

    默认只给 PATH / LANG / TMPDIR / HOME(HOME 指向临时目录,避免插件往真实用户目录
    里写东西)。确实需要额外变量的部署,在 config.yaml 的
    tool_center.sandbox.python_env_passthrough 里按名字显式列出——**显式放行**比
    "默认全给再想办法删掉"安全得多:后者永远会漏掉新加的变量。
    """
    import os

    allow = get_settings().get("tool_center", "sandbox", "python_env_passthrough", default=[]) or []
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "TMPDIR": tmp_dir,
        "HOME": tmp_dir,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in allow:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _make_preexec(timeout: int, mem_bytes: int, max_procs: int, max_file_bytes: int, run_as_user):
    """fork 之后、exec 之前设置资源限额并(可选)降权(仅 POSIX)。"""
    def _preexec() -> None:
        import os
        import resource

        # 降权必须在 setrlimit 之前:切成专用用户后,RLIMIT_NPROC 才是"这个沙箱用户
        # 能开多少进程",而不是和主服务共享同一份配额——否则 fork 炸弹会把主服务
        # 一起拖死(实测会直接搞崩宿主)。需要主进程以 root 启动才能 setuid。
        if run_as_user:
            try:
                import pwd

                pw = pwd.getpwnam(str(run_as_user))
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)
            except Exception:  # noqa: BLE001 - 非 root 或用户不存在时不降权,由文档提示风险
                pass
        for res_name, limit in (
            # CPU 限额给墙钟超时留出余量。
            #
            # 原来软限 = timeout,与墙钟超时**完全相同** —— 死循环时两条防线几乎同时
            # 触发,谁先取决于机器负载:空闲时 CPU 时间≈墙钟时间,满载时 CPU 累积更慢。
            # 走 RLIMIT_CPU 那条会被 SIGXCPU 直接杀掉、**stderr 为空**,
            # 错误信息与走墙钟超时那条完全不同,表现为「同样的插件、有时报超时、
            # 有时报被信号终止」,排查时会以为是两个问题。
            #
            # 现在让**墙钟超时成为主防线**(它的错误信息更有指向性),
            # CPU 限额退为兜底 —— 只在「墙钟没到但 CPU 烧了远超预期」时才生效,
            # 比如插件开了多线程把多个核跑满。
            ("RLIMIT_CPU", (int(timeout) + 5, int(timeout) + 10)),
            ("RLIMIT_AS", (mem_bytes, mem_bytes)),
            ("RLIMIT_NPROC", (max_procs, max_procs)),
            ("RLIMIT_FSIZE", (max_file_bytes, max_file_bytes)),
            ("RLIMIT_CORE", (0, 0)),
        ):
            try:
                setattr_target = getattr(resource, res_name, None)
                if setattr_target is not None:
                    resource.setrlimit(setattr_target, limit)
            except Exception:  # noqa: BLE001 - 单项限额不支持时跳过,不影响其它限额生效
                pass

    return _preexec


def _kill_process_tree(proc) -> None:
    """杀掉整个进程组。只 kill 直接子进程的话,它派生的孙子进程会活下来变成孤儿。"""
    import os
    import signal

    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _communicate_capped(proc, payload: bytes, max_output: int) -> tuple[bytes, bytes, bool]:
    """写 stdin 后按上限读 stdout/stderr,任一路超限即杀掉进程组。

    这里有三个坑,都是实测踩出来的:

    1. **不能用 proc.communicate()**:它把子进程全部输出读进内存,插件循环打印时
       父进程会先 OOM。
    2. **不能用 `stream.read(max_output + 1)` 判超限**:StreamReader.read(n) 是
       "最多读 n 字节",有数据就立刻返回,不保证读满。拿到一个分片就以为没超、
       随后停止读取,子进程会把管道写满并被永久阻塞。必须循环累积。
    3. **一路超限时必须立刻杀进程,不能干等另一路**:stdout 超限返回了,但子进程
       正阻塞在写 stdout 上,永远不会关闭 stderr —— 如果 gather 傻等 stderr 的 EOF,
       整个调用就挂死了。所以超限的那一路要立即 kill,让另一路自然收到 EOF。
    """
    killed = False

    async def _read(stream) -> tuple[bytes, bool]:
        nonlocal killed
        buf = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(buf), False
            buf.extend(chunk)
            if len(buf) > max_output:
                if not killed:
                    killed = True
                    _kill_process_tree(proc)   # 让另一路立刻收到 EOF,否则会互相等死
                return bytes(buf[:max_output]), True

    if proc.stdin is not None:
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 插件不读 stdin 直接退出是允许的
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    (out, out_trunc), (err, err_trunc) = await asyncio.gather(_read(proc.stdout), _read(proc.stderr))
    await _reap(proc)
    return out, err, (out_trunc or err_trunc)


async def _reap(proc, grace: float = 5.0) -> None:
    """回收子进程。

    `await proc.wait()` 在管道仍有未读数据时可能死锁(asyncio 官方文档对此有警告,
    并建议用 communicate()——但我们出于内存原因不能用它)。所以这里给 wait() 加一个
    上限,超时就直接杀进程组再等一次;宁可留下一条告警,也不能让 worker 槽位挂死。
    """
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    _kill_process_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        log.warning(f"沙箱子进程 pid={proc.pid} 在 SIGKILL 后仍未回收,可能已成僵尸进程")
