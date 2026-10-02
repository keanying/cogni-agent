"""
系统配置加载器(G-LOG-06)。

固定从项目根目录 config/config.yaml 读取配置,YAML 中形如 ${VAR_NAME} 的
占位符从环境变量(.env,由 python-dotenv/系统环境注入)解析替换。
全局仅加载一次(懒加载单例),其余模块一律通过 get_settings() 获取,
不允许各模块自己零散读 os.environ。
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _resolve_env(value):
    """递归把 YAML 中的 ${ENV_VAR} 占位符替换为环境变量真实值。"""
    if isinstance(value, str):
        m = _ENV_PATTERN.fullmatch(value.strip())
        if m:
            return os.environ.get(m.group(1), "")
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


class Settings(BaseModel):
    """强类型配置对象的顶层容器;各子段以 dict 形式保留,模块内按需再取。"""

    raw: dict

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, *path, default=None):
        """按路径取值,如 settings.get('runtime', 'guardrails', 'max_steps')。"""
        node = self.raw
        for p in path:
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node


@lru_cache
def get_settings() -> Settings:
    # 多环境 .env 加载策略：
    #   1. 设置 ZHILV_ENV=local|dev|prod 会加载对应的 .env.{env} 文件
    #   2. 未设置时回退到 .env（兼容旧用法）
    #   3. 生产环境建议由容器编排直接注入环境变量，不依赖 .env 文件
    try:
        from dotenv import load_dotenv

        project_root = Path(__file__).resolve().parent.parent
        env_name = os.environ.get("ZHILV_ENV", "")
        if env_name:
            env_path = project_root / f".env.{env_name}"
        else:
            env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        # Quick BI 智能小Q 的 skill 配置单独放 .env.quickbi(同样不入库),与数据库等基础配置分开维护
        qbi_env = project_root / ".env.quickbi"
        if qbi_env.exists():
            load_dotenv(qbi_env)
    except ImportError:
        pass

    # 兜底强制 LiteLLM 使用随包本地计费表(与 cogni_agent/__init__.py 一致)。
    # 模型单价由「系统设置 · 模型单价」页面统一配置,不依赖任何外部网络拉取。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    config_path = Path(
        os.environ.get("ZHILV_CONFIG_PATH", Path(__file__).resolve().parent.parent / "config" / "config.yaml")
    )
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _resolve_env(raw)
    return Settings(raw=raw)


TABLE_PREFIX = get_settings().get("app", "table_prefix", default="numeri_core_")
