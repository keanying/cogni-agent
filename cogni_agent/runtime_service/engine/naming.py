"""
工具名 -> function-calling 函数名的规范化与去重。

**为什么必须有这一层**(两个都是会静默出错的真问题):

1. **厂商对函数名有格式约束**:OpenAI 及各家兼容层要求 tool function name 匹配
   `^[a-zA-Z0-9_-]{1,64}$`。而工具中心允许管理员用中文命名(管理后台的输入提示
   写的就是"如 查询订单记录"),这类名字直接发给模型会被 400 拒掉——整个 Skill
   一个工具都用不了,而且报错信息通常只说"invalid function name",很难定位到是
   哪个工具。

2. **同名工具会串**:`bound_tools_map()` 是按名字建索引的。L2 动态可选池会把多个
   Skill 的工具合并暴露,合并时按 tool.id 去重——但**两个不同 id 的工具完全可能
   同名**(比如集团级有一个"查询订单",某景区也自建了一个"查询订单")。结果是:
   发给模型的 tools 数组里出现重复函数名(厂商会报错),或者模型调用该名字时
   `bound_tools_map` 只能返回后写入的那一个——**执行了错误的工具,而且完全静默**。
   在有权限边界的系统里这是安全问题,不只是功能问题。

设计取舍:函数名只承担"稳定且唯一的标识符"职责,**语义完全交给 description**。
中文名被 mangle 成 `tool_xxxxxxxx` 不影响模型理解——function calling 的选择依据
主要就是 description,所以下面会把原始中文名拼进 description 里。这比引入拼音
转换依赖(pypinyin)更简单,也不会因为多音字产生更难排查的歧义。
"""
from __future__ import annotations

import re

from cogni_agent.common.logging import get_logger

log = get_logger("runtime_service.engine.naming")

_VALID_FUNC_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
MAX_NAME_LEN = 64


def is_valid_function_name(name: str) -> bool:
    return bool(name) and bool(_VALID_FUNC_NAME.match(name))


def sanitize_function_name(display_name: str, *, fallback_id: str) -> tuple[str, bool]:
    """把展示名转成合法函数名。返回 (函数名, 是否发生了改写)。

    改写规则:非法字符 -> 下划线,压缩连续下划线,去掉首尾下划线,截断到 64。
    如果结果为空(纯中文名的典型结果)或以数字开头,退回 `tool_<id前8位>` ——
    id 是稳定的,这样同一个工具在多次解析之间函数名不会漂移(函数名漂移会让
    模型在多轮里"记住的工具名"突然失效)。
    """
    if is_valid_function_name(display_name):
        return display_name, False

    slug = _INVALID_CHARS.sub("_", display_name or "")
    slug = re.sub(r"_{2,}", "_", slug).strip("_-")[:MAX_NAME_LEN]

    if not slug:
        # 纯中文/纯符号名 mangle 之后会一无所有,回落到基于 id 的稳定名。
        # 用 id 而不是随机值:函数名在多次解析之间必须可复现,漂移会让模型
        # 在多轮对话里记住的工具名突然失效。
        slug = f"tool_{(fallback_id or 'unknown').replace('-', '')[:8]}"
    return slug, True


def assign_function_names(items: list[tuple[str, str]]) -> dict[str, tuple[str, bool]]:
    """给一批工具分配**互不冲突**的函数名。

    入参 items 为 [(tool_id, display_name), ...],返回 {tool_id: (函数名, 是否被改写)}。

    冲突处理:后来者加 `_2` / `_3` 后缀(而不是覆盖)。加后缀本身也算"被改写",
    调用方据此决定要不要在 description 里补上原名。为保证同一批输入产出稳定结果,
    严格按传入顺序处理,不依赖 set/dict 的遍历顺序。
    """
    used: set[str] = set()
    out: dict[str, tuple[str, bool]] = {}
    for tool_id, display_name in items:
        # 同一个工具被多个 Skill 绑定时会重复出现在 items 里。它们是同一个工具,
        # 必须拿到同一个函数名——否则模型在 Skill A 里学到的工具名到了 Skill B 就失效了。
        if tool_id in out:
            continue
        base, renamed = sanitize_function_name(display_name, fallback_id=tool_id)
        candidate = base
        suffix = 2
        while candidate in used:
            tail = f"_{suffix}"
            candidate = base[: MAX_NAME_LEN - len(tail)] + tail
            suffix += 1
            renamed = True
        if candidate != base:
            log.warning(f"工具 {tool_id}「{display_name}」的函数名与其它工具冲突,已改用 {candidate}")
        elif renamed:
            log.debug(f"工具 {tool_id}「{display_name}」函数名不合法,已规范化为 {candidate}")
        used.add(candidate)
        out[tool_id] = (candidate, renamed)
    return out


def describe_for_model(display_name: str, description: str, func_name: str, renamed: bool) -> str:
    """构造给模型看的 description。

    函数名被改写过时,把原始展示名显式写进 description —— 否则模型看到的是
    `tool_a1b2c3d4` 这种毫无语义的名字,只能靠 description 判断该不该调用它。
    """
    desc = (description or "").strip()
    if renamed and display_name and display_name != func_name:
        prefix = f"【{display_name}】"
        return f"{prefix} {desc}" if desc else f"{prefix} 调用工具「{display_name}」"
    return desc or f"调用工具 {func_name}"
