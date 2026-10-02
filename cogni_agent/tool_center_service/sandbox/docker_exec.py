"""
Docker 隔离沙箱(**可选扩展**)。

定位:本机 subprocess 沙箱(local.py)是默认与主力执行路径;这里是为"需要真正隔离
边界"的场景准备的扩展,通过 `tool_center.sandbox.mode: docker` 切换。

它能补上 local.py 明确做不到的三件事:
1. **网络隔离** —— `--network=none`,插件彻底连不出去(内网服务、数据库、云元数据
   端点都碰不到)。
2. **fork 炸弹防护** —— `--pids-limit`,靠 cgroup 限制进程数。本机沙箱做不到这点:
   RLIMIT_NPROC 是按用户计的,沙箱与服务同用户,设低了主服务自己受影响,设高了拦不住。
3. **文件系统隔离** —— `--read-only` + 独立 tmpfs,插件看不到宿主的源码与配置。

只有 python 类型需要走容器。http/db 本质上是"从本进程发一次网络请求 / 一条只读 SQL",
把它们塞进容器只会带来额外开销和网络配置麻烦(而且 `--network=none` 反而让 http 工具
无法工作),所以这两类直接委托回 LocalSandbox。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.tool_center_service.sandbox.base import SandboxExecutor
from cogni_agent.tool_center_service.sandbox.local import LocalSandbox

log = get_logger("tool_center_service.sandbox.docker")


class DockerUnavailable(RuntimeError):
    """docker 不可用。刻意不静默退回本机执行——运维以为开着强隔离、实际没开,
    比直接报错危险得多。"""


class DockerSandbox(SandboxExecutor):
    def __init__(self) -> None:
        self._local = LocalSandbox()

    async def execute(self, *, tool_id: str, tool_type: str, type_config: dict, params: dict,
                      context: dict | None = None) -> dict:
        if tool_type in ("http", "db"):
            # 这两类不需要容器隔离,交回本机执行器(见模块说明)
            return await self._local.execute(
                tool_id=tool_id, tool_type=tool_type, type_config=type_config, params=params,
                context=context,
            )
        if tool_type != "python":
            raise ValueError(f"未知工具类型: {tool_type}")
        return await self._run_python_in_container(type_config, params)

    async def _run_python_in_container(self, type_config: dict, params: dict) -> dict:
        script = type_config.get("script", "")
        if not script:
            raise ValueError("Python 能力插件未配置 script")

        cfg = get_settings()
        image = cfg.get("tool_center", "sandbox", "docker_image", default="python:3.12-slim")
        timeout = int(cfg.get("tool_center", "sandbox", "python_timeout_seconds", default=60))
        memory = cfg.get("tool_center", "sandbox", "docker_memory", default="512m")
        cpus = str(cfg.get("tool_center", "sandbox", "docker_cpus", default="1.0"))
        pids_limit = int(cfg.get("tool_center", "sandbox", "docker_pids_limit", default=64))
        network = cfg.get("tool_center", "sandbox", "docker_network", default="none")
        max_output = int(cfg.get("tool_center", "sandbox", "python_max_output_bytes", default=1024 * 1024))

        docker_bin = shutil.which("docker")
        if not docker_bin:
            raise DockerUnavailable(
                "沙箱模式配置为 docker,但当前环境找不到 docker 命令。"
                "请安装 Docker 并确保服务进程有权访问 docker socket,"
                "或把 tool_center.sandbox.mode 改回 local。"
            )

        with tempfile.TemporaryDirectory(prefix="cogni_docker_") as tmp:
            script_path = Path(tmp) / "tool_script.py"
            script_path.write_text(script, encoding="utf-8")

            args = [
                docker_bin, "run", "--rm", "-i",
                f"--network={network}",          # 默认 none:插件连不出去
                f"--memory={memory}", f"--memory-swap={memory}",   # 禁用 swap,否则内存限额可被绕过
                f"--cpus={cpus}",
                f"--pids-limit={pids_limit}",    # cgroup 级进程数限制,这是本机沙箱做不到的
                "--read-only",                    # 根文件系统只读
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--cap-drop=ALL",                 # 丢弃全部 capability
                "--security-opt", "no-new-privileges",
                "--user", "65534:65534",         # nobody,不以 root 跑
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONIOENCODING=utf-8",
                "-v", f"{script_path}:/app/tool_script.py:ro",
                "-w", "/tmp",
                image, "python", "-I", "/app/tool_script.py",
            ]

            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            payload = json.dumps(params, ensure_ascii=False).encode("utf-8")
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=timeout + 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"ok": False, "error": f"容器执行超时(>{timeout}s)"}

            if len(stdout) > max_output:
                return {"ok": False, "error": f"插件输出超过上限({max_output} 字节)"}
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:4000]
                if "Unable to find image" in err or "pull access denied" in err:
                    err += f"\n提示:请先在服务器上 `docker pull {image}`,或改用已存在的镜像。"
                return {"ok": False, "error": err}

            raw = stdout.decode("utf-8", errors="replace").strip()
            try:
                return {"ok": True, "result": json.loads(raw) if raw else None}
            except json.JSONDecodeError:
                return {"ok": True, "result": raw}


async def preflight() -> tuple[bool, str]:
    """启动自检:确认 docker 可用且配置的镜像已就绪。

    由 main.py 在 sandbox.mode=docker 时调用。宁可启动时就把问题喊出来,也不要等到
    第一个用户真的触发了 Python 插件才发现跑不起来。
    """
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False, "找不到 docker 命令"
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_bin, "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, OSError) as exc:
        return False, f"docker info 调用失败:{exc}"
    if proc.returncode != 0:
        return False, f"docker 守护进程不可用:{err.decode('utf-8', errors='replace')[:200]}"

    image = get_settings().get("tool_center", "sandbox", "docker_image", default="python:3.12-slim")
    check = await asyncio.create_subprocess_exec(
        docker_bin, "image", "inspect", image,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await check.wait()
    if check.returncode != 0:
        return False, f"docker 可用(v{out.decode().strip()}),但镜像 {image} 尚未拉取,请先 `docker pull {image}`"
    return True, f"docker v{out.decode().strip()},镜像 {image} 就绪"
