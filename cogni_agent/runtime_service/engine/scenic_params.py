"""系统参数(sys_param_def + sys_param_value)的解析、引用渲染与注入。

两层取值:**系统设置 · 参数配置**里定义参数(标识、说明、单值/多值、默认值),
**景区配置 · 接口参数**里按景区覆盖(可以多个值,各带备注)。景区没覆盖就用系统默认。

在哪里生效:

1. **`{{参数标识}}` 引用**(Tool / MCP / Skill 编辑时手写):
   - HTTP / DB 工具的地址、请求头、SQL 模板:沙箱渲染(`template_vars`);
   - 工具参数定义里某个参数的 `default` 写成 `"{{ota_account}}"`:该参数从模型可见 Schema
     剥离,执行时由系统填入(`ToolSpec.param_refs`,MCP 工具同样适用);
   - 任何入参值里的 `{{…}}`(含运行时覆盖的固定参数):执行前渲染(`render_obj`);
   - Skill 的 Prompt:解析时渲染(`prompt_vars`;敏感参数渲染成「已配置」,值不进提示词)。
2. **按入参名自动填入**(定义里的 arg_names):模型没填 → 填;fill_mode=force → 一律覆盖。
3. **提示词说明**(`prompt_block`):告诉模型这些值已经有了,不要再向用户要。

多值参数怎么用(multi_mode,添加参数时设置):
- fanout:每个值各调用一次工具,结果合并后交给模型(接口一次只收一个商户 ID 时用);
- list:作为数组一次传入(接口本身收列表时用);
- join:逗号拼成一个字符串一次传入。

读取按景区缓存 30 秒;后台保存时 `invalidate()` 立即失效。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.logging import get_logger

log = get_logger("runtime_service.engine.scenic_params")

_TTL_SECONDS = 30.0
_CACHE: dict[str, tuple[float, list["ResolvedParam"]]] = {}

MULTI_MODES = ("single", "fanout", "list", "join")
FANOUT_LIMIT = 20            # 多值逐个调用的上限:再多就不是「查几个商户」了,多半是配错
_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_WHOLE_REF = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$")

# 已被系统占用的变量名:参数标识不能和它们同名(否则 {{scenic_id}} 到底取谁说不清)
RESERVED_KEYS = frozenset({
    "scenic_id", "org_id", "user_id", "role", "now", "today", "yesterday", "month_start",
    "year", "month", "day", "credential",
})


@dataclass(frozen=True)
class ParamValue:
    value: str
    label: str = ""


@dataclass(frozen=True)
class ResolvedParam:
    key: str
    label: str
    values: tuple[ParamValue, ...]
    multi_mode: str = "single"
    arg_names: tuple[str, ...] = field(default_factory=tuple)
    fill_mode: str = "fill"
    is_secret: bool = False
    description: str = ""
    source: str = "default"        # scenic = 景区覆盖 / default = 系统默认

    @property
    def is_multi(self) -> bool:
        return self.multi_mode != "single"

    @property
    def strings(self) -> list[str]:
        return [v.value for v in self.values]

    def scalar(self) -> str:
        """拼进字符串时的取值:单值 = 第一个;多值 = 逗号拼接。"""
        return self.values[0].value if not self.is_multi else ",".join(self.strings)

    def typed(self, *, want_array: bool = False) -> Any:
        """整段替换(入参值恰好是 {{key}})时的取值:list 模式或目标是数组 → 列表。"""
        if self.multi_mode == "list" or (self.is_multi and want_array):
            return self.strings
        if self.multi_mode == "single":
            return self.values[0].value if not want_array else [self.values[0].value]
        return self.scalar()


# ── 读取 ───────────────────────────────────────────────────────────────

def invalidate(scenic_id: str | None = None) -> None:
    """后台改了定义或景区取值后调用。改定义影响所有景区,传 None 全清。"""
    if scenic_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(scenic_id, None)


def _norm_values(raw) -> tuple[ParamValue, ...]:
    out = []
    for x in raw or []:
        if isinstance(x, dict):
            v = str(x.get("value") or "").strip()
            if v:
                out.append(ParamValue(v, str(x.get("label") or "").strip()))
        elif str(x or "").strip():
            out.append(ParamValue(str(x).strip()))
    return tuple(out)


async def load(db: AsyncSession, scenic_id: str | None) -> list[ResolvedParam]:
    """本景区可用的参数(景区覆盖优先,没有就用系统默认;都没有值的不返回)。

    查询失败 → 空列表:读配置出错不该让工具调用失败。
    """
    ck = scenic_id or ""
    hit = _CACHE.get(ck)
    now = time.monotonic()
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    try:
        from cogni_agent.gateway_service.models import SysParamDef, SysParamValue

        defs = (await db.execute(select(SysParamDef).where(SysParamDef.enabled.is_(True))
                                 .order_by(SysParamDef.param_key))).scalars().all()
        overrides: dict[str, Any] = {}
        if scenic_id and defs:
            rows = (await db.execute(select(SysParamValue).where(SysParamValue.scenic_id == scenic_id))).scalars().all()
            overrides = {r.param_key: r.values for r in rows}
    except Exception as exc:  # noqa: BLE001 - 读配置失败按「没配」处理,执行照常
        log.warning(f"读取系统参数失败(景区 {scenic_id}),本次不注入:{exc}")
        return []
    out: list[ResolvedParam] = []
    for d in defs:
        scenic_vals = _norm_values(overrides.get(d.param_key)) if d.param_key in overrides else ()
        vals, src = (scenic_vals, "scenic") if scenic_vals else (_norm_values(d.default_values), "default")
        if not vals:
            continue
        mode = d.multi_mode if d.multi_mode in MULTI_MODES else "single"
        if mode == "single":
            vals = vals[:1]
        out.append(ResolvedParam(
            key=d.param_key, label=d.label or d.param_key, values=vals, multi_mode=mode,
            arg_names=tuple(str(a).strip() for a in (d.arg_names or []) if str(a).strip()),
            fill_mode=d.fill_mode if d.fill_mode in ("fill", "force") else "fill",
            is_secret=bool(d.is_secret), description=d.description or "", source=src))
    _CACHE[ck] = (now, out)
    return out


def by_key(params: list[ResolvedParam]) -> dict[str, ResolvedParam]:
    return {p.key: p for p in params}


# ── 引用渲染 ───────────────────────────────────────────────────────────

def refs_in(obj: Any) -> set[str]:
    """一段配置(dict / list / 字符串)里引用了哪些 {{key}}。"""
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(_REF.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= refs_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= refs_in(v)
    return found


def render_obj(obj: Any, params: dict[str, ResolvedParam], *, pick: dict[str, str] | None = None) -> Any:
    """把 {{key}} 换成参数值。整段就是 {{key}} → 按类型给(list 模式是数组);夹在文字里 → 字符串。

    pick:多值逐个调用时本次用哪个值({key: value})。未知的 {{…}} 原样保留(内置变量由沙箱渲染)。
    """
    pick = pick or {}

    def one(s: str) -> Any:
        m = _WHOLE_REF.match(s)
        if m and m.group(1) in params:
            k = m.group(1)
            return pick[k] if k in pick else params[k].typed()
        return _REF.sub(lambda mm: (pick.get(mm.group(1)) or params[mm.group(1)].scalar())
                        if mm.group(1) in params else mm.group(0), s)

    if isinstance(obj, str):
        return one(obj) if "{{" in obj else obj
    if isinstance(obj, dict):
        return {k: render_obj(v, params, pick=pick) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_obj(v, params, pick=pick) for v in obj]
    return obj


def schema_param_refs(schema: dict | None) -> dict[str, str]:
    """工具参数定义里 default 写成 "{{key}}" 的参数 → {入参名: 参数标识}。

    这些参数由系统填,解析期从模型可见 Schema 剥离(见 resolver._to_tool_spec)。
    """
    props = ((schema or {}).get("properties") or {}) if isinstance(schema, dict) else {}
    out: dict[str, str] = {}
    for name, prop in props.items():
        d = (prop or {}).get("default") if isinstance(prop, dict) else None
        m = _WHOLE_REF.match(d) if isinstance(d, str) else None
        if m and m.group(1) not in RESERVED_KEYS:
            out[name] = m.group(1)
    return out


# ── 注入到一次工具调用 ─────────────────────────────────────────────────

@dataclass
class Injection:
    args: dict
    notes: list[dict]
    fanout: dict | None = None          # {"key", "label", "values": [ParamValue], "args": [入参名]}
    missing: list[str] = field(default_factory=list)   # 引用了但本景区没值的参数标识


def apply_to_args(schema: dict | None, args: dict, params: list[ResolvedParam], *,
                  param_refs: dict[str, str] | None = None,
                  config_refs: set[str] | None = None) -> Injection:
    """把参数填进一次工具调用的入参。

    - param_refs:Schema 里 default="{{key}}" 的入参(系统填,模型看不到);
    - arg_names:定义里声明的入参名(模型没填才填;force 一律覆盖);
    - config_refs:工具配置(地址 / 请求头 / SQL / 固定参数)里引用到的参数 —— 只用来决定要不要逐个调用。
    只填 Schema 里声明了的入参:没声明的不往里塞(多出来的字段有的接口会报错,有的会被当成过滤条件)。
    多值 + fanout:先填第一个值过校验,执行时按 Injection.fanout 逐个替换。同一次调用只按一个参数逐个调用。
    注入记录只写入参名 / 参数 / 方式,不写值。
    """
    pmap = by_key(params)
    props = ((schema or {}).get("properties") or {}) if isinstance(schema, dict) else {}
    out = dict(args or {})
    inj = Injection(args=out, notes=[])

    def _use_fanout(p: ResolvedParam, arg: str | None) -> bool:
        if p.multi_mode != "fanout" or len(p.values) < 2:
            return False
        if inj.fanout is None:
            inj.fanout = {"key": p.key, "label": p.label, "values": list(p.values[:FANOUT_LIMIT]), "args": []}
        if inj.fanout["key"] != p.key:
            return False            # 另一个参数已经在逐个调用了,这个就拼成字符串(不做笛卡尔积)
        if arg:
            inj.fanout["args"].append(arg)
        return True

    def _put(arg: str, p: ResolvedParam, how: str, had: bool) -> None:
        # 入参本身是数组:多值一次传进去就行,不必逐个调用
        want_array = ((props.get(arg) or {}).get("type") == "array")
        fan = False
        if want_array:
            out[arg] = p.typed(want_array=True)
        elif _use_fanout(p, arg):
            out[arg], fan = p.values[0].value, True     # 先用第一个值过校验,执行时逐个替换
        else:
            out[arg] = p.typed()
        inj.notes.append({"arg": arg, "param": p.key, "how": how, "overrode_model_value": had,
                          **({"fanout": True} if fan else {})})

    for arg, key in (param_refs or {}).items():
        p = pmap.get(key)
        if p is None:
            inj.missing.append(key)
            continue
        _put(arg, p, "ref", out.get(arg) not in (None, ""))
    for p in params:
        for arg in p.arg_names:
            if arg not in props or arg in (param_refs or {}):
                continue
            had = out.get(arg) not in (None, "")
            if had and (p.fill_mode != "force" or str(out.get(arg)) in p.strings):
                continue
            _put(arg, p, p.fill_mode, had)
    for key in sorted(config_refs or ()):
        p = pmap.get(key)
        if p is not None:
            _use_fanout(p, None)
    # 入参值里手写的 {{key}}(运行时固定参数等):多值逐个调用的那个先留着,执行时再换
    fan_key = inj.fanout["key"] if inj.fanout else None
    rendered = render_obj(out, {k: v for k, v in pmap.items() if k != fan_key})
    out.clear()
    out.update(rendered)
    return inj


def template_vars(params: list[ResolvedParam], *, pick: dict[str, str] | None = None) -> dict[str, str]:
    """沙箱模板变量:{{key}}(多值逗号拼接;逐个调用时用 pick 里的那个值)。

    兼容 v3.58.0 的写法 {{scenic_<key>}}。
    """
    out: dict[str, str] = {}
    for p in params:
        v = (pick or {}).get(p.key) or p.scalar()
        out[p.key] = v
        out[f"scenic_{p.key}"] = v
    return out


# ── 提示词 ─────────────────────────────────────────────────────────────

def prompt_vars(params: list[ResolvedParam]) -> dict[str, str]:
    """Skill Prompt 里 {{key}} 的渲染值。敏感参数不给值 —— 提示词会进日志、也会被模型复述。"""
    return {p.key: ("(已配置)" if p.is_secret else p.scalar()) for p in params}


_MODE_TEXT = {"fanout": "系统会对每个值各调用一次并合并结果", "list": "作为列表一次传入", "join": "逗号拼接后一次传入"}


def prompt_block(params: list[ResolvedParam]) -> str:
    """系统提示里的一段:本景区已配置的参数,不要再向用户索要。只列有自动填入入参名的。"""
    rows = []
    for p in params:
        if not p.arg_names:
            continue
        args = "/".join(p.arg_names)
        if p.is_secret:
            shown = "(已配置,系统自动填入)"
        elif p.is_multi:
            shown = ";".join(f"{v.value}" + (f"({v.label})" if v.label else "") for v in p.values)
            shown += f" —— 共 {len(p.values)} 个,{_MODE_TEXT.get(p.multi_mode, '')}"
        else:
            shown = p.values[0].value
        rows.append(f"- {p.label}(工具参数 {args}):{shown}"
                    + ("(固定值,以配置为准)" if p.fill_mode == "force" else ""))
    if not rows:
        return ""
    return ("\n\n本景区已配置的接口参数(调用工具时系统会自动填入,**不要再向用户索要**;"
            "未标「固定值」的,用户在对话里点名了其中某一个时,把那个值填进参数即可):\n" + "\n".join(rows))
