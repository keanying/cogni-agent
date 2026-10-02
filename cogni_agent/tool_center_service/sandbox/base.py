"""M5-11:沙箱执行器统一接口——拆分预埋点(P0 subprocess -> P1 Docker -> P2 K8s Job)。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from cogni_agent.config import get_settings


class SandboxExecutor(ABC):
    @abstractmethod
    async def execute(self, *, tool_id: str, tool_type: str, type_config: dict, params: dict,
                      context: dict | None = None) -> dict:
        """context = 执行上下文(scenic_id / user_id / org_id / role)。

        模板里的 {{scenic_id}} 这类系统变量由实现方按它渲染 —— 不渲染的话
        HTTP 工具会把字面量打出去、DB 工具会把它绑成 NULL(查出来永远空集,且不报错)。
        """
        ...


_INSTANCES: dict[str, SandboxExecutor] = {}


def get_sandbox() -> SandboxExecutor:
    """按 tool_center.sandbox.mode 返回执行器。

    local(默认,主力路径)= subprocess + 资源限额 + 环境清洗;
    docker(可选扩展)   = 容器隔离,补上本机做不到的网络隔离 / pids 限制 / 只读根文件系统。

    实例按 mode 缓存:执行器本身无状态,没必要每次工具调用都新建。
    """
    mode = get_settings().get("tool_center", "sandbox", "mode", default="local")
    if mode in _INSTANCES:
        return _INSTANCES[mode]
    if mode == "docker":
        from cogni_agent.tool_center_service.sandbox.docker_exec import DockerSandbox

        executor: SandboxExecutor = DockerSandbox()
    else:
        from cogni_agent.tool_center_service.sandbox.local import LocalSandbox

        executor = LocalSandbox()
    _INSTANCES[mode] = executor
    return executor
