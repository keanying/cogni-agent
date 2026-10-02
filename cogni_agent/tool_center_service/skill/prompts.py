"""M5-06:Prompt 变量渲染(简单占位实现,生产可换 Jinja2)。"""
from __future__ import annotations

import re


def render_prompt(template: str, variables: dict) -> str:
    def _sub(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", _sub, template)
