#!/usr/bin/env python3
"""通过管理后台 API(纯 API、不直连数据库)创建「文档驱动Agent生成器」skill 及配套管理工具。

用法:
    ZHILV_ENV=local uv run python scripts/create_doc_driven_builder.py --env local

说明:
- 登录账号取各环境 .env.{env} 注入的 COGNI_ADMIN_BASE_URL / COGNI_ADMIN_BOT_PHONE / COGNI_ADMIN_BOT_PASSWORD
  (各环境统一 pftroot 账号,由部署方配置,脚本不硬编码)。
- 幂等:同名工具/skill 已存在则跳过,不覆盖不删除。
- 创建的资源均 visibility=public(全平台所有用户可见)。
- 创建出的 7 个 python 工具,其脚本内部自行登录管理后台并调用对应 API;
  它们通过沙箱 python_env_passthrough 读取上述三个环境变量。
"""
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────────
# 管理后台访问
# ──────────────────────────────────────────────────────────────────────────
def load_env(env: str) -> None:
    """加载 .env.{env} 到进程环境变量(不覆盖已存在的值)。"""
    path = REPO_ROOT / f".env.{env}"
    if not path.exists():
        print(f"[warn] 未找到 {path},将仅使用已有环境变量", file=sys.stderr)
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v


def api(base: str, token: str, method: str, path: str, body=None):
    r = httpx.request(
        method, f"{base}/api/admin{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body, timeout=30,
    )
    try:
        data = r.json()
    except Exception:
        raise SystemExit(f"{method} /api/admin{path} -> {r.status_code} 非JSON: {r.text[:300]}")
    if r.status_code >= 400:
        raise SystemExit(f"{method} /api/admin{path} -> {r.status_code}: {json.dumps(data, ensure_ascii=False)[:400]}")
    # 管理端接口统一包裹为 {code, message, data, trace_id},这里解一层让调用方直接拿业务数据
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


def login(base: str) -> str:
    phone = os.environ.get("COGNI_ADMIN_BOT_PHONE", "").strip()
    pwd = os.environ.get("COGNI_ADMIN_BOT_PASSWORD", "").strip()
    if not phone or not pwd:
        raise SystemExit("未配置 COGNI_ADMIN_BOT_PHONE/PASSWORD(见 .env.example)")
    # 优先管理端 /api/admin/auth/login,404 时回退 /api/auth/login
    for p in ("/api/admin/auth/login", "/api/auth/login"):
        r = httpx.post(f"{base}{p}", json={"phone": phone, "password": pwd, "rsa_encrypt": False}, timeout=20)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code in (200, 404):
            data = body.get("data", body) if isinstance(body, dict) else {}
            token = (data or {}).get("access_token")
            if token:
                return token
            if r.status_code == 200:
                raise SystemExit(f"登录响应无 access_token: {str(body)[:300]}")
    raise SystemExit(f"登录失败: 管理后台 {base} 不可达或接口路径不符")


def find_by_name(token: str, base: str, rtype: str, name: str):
    items = api(base, token, "GET", f"/{rtype}").get("items", [])
    for it in items:
        if it.get("name") == name:
            return it
    return None


# ──────────────────────────────────────────────────────────────────────────
# python 工具脚本公共头:各工具内部自行登录并调用管理 API
# ──────────────────────────────────────────────────────────────────────────
PY_HEAD = """import json, os, sys
import httpx


def _cfg():
    base = os.environ.get("COGNI_ADMIN_BASE_URL", "").strip()
    phone = os.environ.get("COGNI_ADMIN_BOT_PHONE", "").strip()
    pwd = os.environ.get("COGNI_ADMIN_BOT_PASSWORD", "").strip()
    if not base:
        raise RuntimeError("未配置 COGNI_ADMIN_BASE_URL:部署方需在 .env 注入管理后台地址")
    if not phone or not pwd:
        raise RuntimeError("未配置 COGNI_ADMIN_BOT_PHONE/PASSWORD:部署方需在 .env 注入管理员账号")
    return base, phone, pwd


def _login(base, phone, pwd):
    r = httpx.post(f"{base}/api/admin/auth/login",
                   json={"phone": phone, "password": pwd, "rsa_encrypt": False}, timeout=20)
    body = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"登录失败: {r.status_code} {str(body)[:300]}")
    data = body.get("data", body) if isinstance(body, dict) else {}
    token = (data or {}).get("access_token")
    if not token:
        raise RuntimeError(f"登录响应无 access_token: {str(body)[:300]}")
    return token


def _call(base, token, method, path, body=None):
    r = httpx.request(method, f"{base}/api/admin{path}",
                      headers={"Authorization": f"Bearer {token}"}, json=body, timeout=60)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"{method} {path} 返回非JSON: {r.status_code} {r.text[:300]}")
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} 失败: {r.status_code} {str(data)[:400]}")
    # 管理端接口统一包裹 {code, message, data, trace_id},解一层再返回
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


def _finish(result):
    print(json.dumps(result, ensure_ascii=False))
"""

PUBLISH_HELPER = """

def _publish_skill(base, token, skill_id):
    \"\"\"派生草稿版本 -> 发布 -> 设为默认。\"\"\"
    ver = _call(base, token, "POST", f"/resources/skill/{skill_id}/versions", {"note": "文档驱动Agent生成器 v1"})
    vid = ver.get("id") if isinstance(ver, dict) else None
    if not vid:
        raise RuntimeError(f"派生版本未返回 id: {str(ver)[:300]}")
    pub = _call(base, token, "POST", f"/versions/{vid}/publish")
    try:
        _call(base, token, "POST", f"/versions/{vid}/set-default")
    except RuntimeError:
        pass  # 首次发布通常已自动设为默认
    return {"version_id": vid, "publish": pub}
"""


def env_prop(required: bool = False, default: str = "local") -> dict:
    p = {
        "type": "string", "enum": ["local", "dev", "prod"],
        "title": "目标环境",
        "description": "在哪个环境操作(local=本地/dev=开发/prod=生产),默认 local",
    }
    if default is not None:
        p["default"] = default
    return p


def build_script(biz: str) -> str:
    return PY_HEAD + (PUBLISH_HELPER if "publish" in biz else "") + "\n\n" + biz + "\n"


# ──────────────────────────────────────────────────────────────────────────
# 工具定义
# ──────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "list_resources",
        "description": "查看平台现有资源(工具/技能/Agent)的清单与状态,用于创建前查重、定位资源ID、核对发布状态。",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "resource_type": {
                    "type": "string", "enum": ["tools", "skills", "agents"],
                    "title": "资源类型", "description": "要查询的资源类型: tools=HTTP/脚本工具, skills=技能, agents=智能体",
                },
                "keyword": {"type": "string", "title": "关键字", "description": "按名称模糊过滤(可选)"},
            },
            "required": ["env", "resource_type"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    rtype = p.get("resource_type") or "tools"
    data = _call(base, token, "GET", f"/{rtype}")
    items = data.get("items", []) if isinstance(data, dict) else []
    kw = (p.get("keyword") or "").strip().lower()
    out = []
    for it in items:
        name = it.get("name") or ""
        if kw and kw not in name.lower():
            continue
        row = {
            "id": it.get("id"), "name": name,
            "status": it.get("status"), "visibility": it.get("visibility"),
            "version": it.get("version"), "type": it.get("type"),
        }
        if rtype == "skills":
            row["tool_ids"] = it.get("tool_ids")
        if rtype == "agents":
            row["bound_skill_ids"] = it.get("bound_skill_ids")
        out.append(row)
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "resource_type": rtype, "count": len(out), "items": out})


main()
"""),
    },
    {
        "name": "create_http_tool",
        "description": "创建一个 HTTP 类型的工具(每个业务接口一个工具)。支持 url/headers 用 {{变量}} 模板注入请求参数;params_schema 定义模型侧参数。创建后全平台可见(public)。",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "name": {"type": "string", "title": "工具名", "description": "英文标识(字母数字下划线),如 query_realtime_flow"},
                "description": {"type": "string", "title": "工具说明", "description": "做什么+何时用(中文),会展示给大模型"},
                "url": {"type": "string", "title": "接口地址", "description": "支持 {{变量}} 模板,如 https://x.com/api/v1/realtime?scenicId={{scenic_id}}"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "title": "请求方法"},
                "headers": {"type": "object", "title": "请求头", "description": "固定请求头;支持 {{变量}} 模板,如 {\"Authorization\": \"Bearer {{token}}\"}"},
                "params_schema": {"type": "object", "title": "参数JSONSchema", "description": "模型侧参数的 JSON Schema(type=object,properties 每字段含 title/type/description,required 列必填项)"},
                "risk_level": {"type": "string", "enum": ["low", "medium", "high"], "title": "风险等级", "description": "只读查询=low,写操作=medium 以上"},
                "visibility": {"type": "string", "enum": ["private", "group", "public"], "title": "可见性", "description": "默认 public(所有用户可见)"},
            },
            "required": ["env", "name", "description", "url", "method", "params_schema"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    name = p.get("name")
    existing = _call(base, token, "GET", "/tools").get("items", [])
    for it in existing:
        if it.get("name") == name:
            _finish({"ok": False, "error": f"工具[{name}]已存在(id={it.get('id')}),未重复创建",
                     "existing_id": it.get("id")})
            return
    type_config = {
        "method": p.get("method", "GET"),
        "url": p.get("url"),
        "headers": p.get("headers") or {},
        "params_schema": p.get("params_schema") or {"type": "object", "properties": {}},
    }
    body = {
        "name": name,
        "description": p.get("description", ""),
        "type": "http",
        "type_config": type_config,
        "risk_level": p.get("risk_level", "low"),
        "visibility": p.get("visibility", "public"),
    }
    res = _call(base, token, "POST", "/tools", body)
    item = res.get("data", res) if isinstance(res, dict) else res
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "created": {"id": item.get("id"), "name": name, "type": "http", "visibility": body["visibility"]}})


main()
"""),
    },
    {
        "name": "create_skill",
        "description": "创建一个技能(Skill)。技能是 Agent 与工具之间的中间层:绑定若干工具(tool_ids)+ 说明(tool_usage)+ 使用说明书(prompt)。创建后为草稿,需用 publish_skill 发布。全平台可见(public)。",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "name": {"type": "string", "title": "技能名", "description": "中文业务名,如 客流分析"},
                "description": {"type": "string", "title": "技能说明"},
                "tool_ids": {"type": "array", "items": {"type": "string"}, "title": "绑定的工具ID列表"},
                "tool_usage": {"type": "object", "title": "工具使用说明", "description": "{tool_id: \"什么时候用它\"},解析时自动合成进系统提示"},
                "prompt": {"type": "string", "title": "使用说明书", "description": "该技能能做什么/工具协作顺序/数据口径提醒/常见问题"},
                "suggested_mode": {"type": "string", "enum": ["L1", "L2", "L3"], "title": "建议模式", "description": "默认 L2"},
                "knowledge_sources": {"type": "array", "items": {"type": "string"}, "title": "知识来源"},
                "constraints": {"type": "object", "title": "约束", "description": "业务约束,必填非空(发布校验要求):如 {\"max_steps\": 40, \"缺参时\": \"反问用户\", \"supplierId\": \"必须先用『通过景区ID获取供应商ID列表』获取,禁止编造\"}"},
                "visibility": {"type": "string", "enum": ["private", "group", "public"], "title": "可见性", "description": "默认 public(所有用户可见)"},
            },
            "required": ["env", "name", "tool_ids"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    name = p.get("name")
    existing = _call(base, token, "GET", "/skills").get("items", [])
    for it in existing:
        if it.get("name") == name:
            _finish({"ok": False, "error": f"技能[{name}]已存在(id={it.get('id')}),未重复创建",
                     "existing_id": it.get("id")})
            return
    body = {
        "name": name,
        "description": p.get("description", ""),
        "tool_ids": p.get("tool_ids") or [],
        "tool_usage": p.get("tool_usage") or {},
        "prompt": p.get("prompt", ""),
        "suggested_mode": p.get("suggested_mode", "L2"),
        "knowledge_sources": p.get("knowledge_sources") or [],
        "constraints": p.get("constraints") or {},
        "visibility": p.get("visibility", "public"),
    }
    res = _call(base, token, "POST", "/skills", body)
    item = res.get("data", res) if isinstance(res, dict) else res
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "created": {"id": item.get("id"), "name": name,
                         "status": item.get("status"), "visibility": body["visibility"],
                         "tool_ids": body["tool_ids"]}})


main()
"""),
    },
    {
        "name": "publish_skill",
        "description": "发布一个技能:派生草稿版本 -> 发布 -> 设为默认。发布后技能状态变为 PUBLISHED,才可被 Agent 使用。",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "skill_id": {"type": "string", "title": "技能ID", "description": "要发布的技能 id,可先用 list_resources 查询"},
            },
            "required": ["env", "skill_id"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    skill_id = p.get("skill_id")
    info = _publish_skill(base, token, skill_id)
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "skill_id": skill_id, **info})


main()
"""),
    },
    {
        "name": "create_agent",
        "description": "创建一个智能体(Agent)。Agent 只能直接绑定技能(bound_skill_ids),不能直接绑工具。创建后自动发布(PUBLISHED)。全平台可见(public)。",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "name": {"type": "string", "title": "智能体名", "description": "如 数字大脑数据助手"},
                "description": {"type": "string", "title": "功能说明"},
                "persona_desc": {"type": "string", "title": "人格描述", "description": "你是谁/擅长什么/语气,会作为系统提示"},
                "opening_line": {"type": "string", "title": "开场白"},
                "tone_style": {"type": "string", "enum": ["正式", "亲切", "简洁", "活泼"], "title": "语气风格"},
                "bound_skill_ids": {"type": "array", "items": {"type": "string"}, "title": "绑定技能ID列表"},
                "visibility": {"type": "string", "enum": ["private", "group", "public"], "title": "可见性", "description": "默认 public(所有用户可见)"},
            },
            "required": ["env", "name", "bound_skill_ids"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    name = p.get("name")
    existing = _call(base, token, "GET", "/agents").get("items", [])
    for it in existing:
        if it.get("name") == name:
            _finish({"ok": False, "error": f"Agent[{name}]已存在(id={it.get('id')}),未重复创建",
                     "existing_id": it.get("id")})
            return
    body = {
        "name": name,
        "description": p.get("description", ""),
        "persona_desc": p.get("persona_desc", ""),
        "opening_line": p.get("opening_line", ""),
        "tone_style": p.get("tone_style"),
        "bound_skill_ids": p.get("bound_skill_ids") or [],
        "visibility": p.get("visibility", "public"),
    }
    res = _call(base, token, "POST", "/agents", body)
    item = res.get("data", res) if isinstance(res, dict) else res
    agent_id = item.get("id")
    pub = _call(base, token, "POST", f"/agents/{agent_id}/publish")
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "created": {"id": agent_id, "name": name,
                         "status": item.get("status"), "visibility": body["visibility"],
                         "bound_skill_ids": body["bound_skill_ids"]},
             "publish": pub.get("ok", True) if isinstance(pub, dict) else True})


main()
"""),
    },
    {
        "name": "update_agent_bindings",
        "description": "调整一个 Agent 绑定的技能(bound_skill_ids),并重新发布使其立即生效。用于给 Agent 改绑/换绑技能。",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "env": env_prop(required=True),
                "agent_id": {"type": "string", "title": "Agent ID"},
                "bound_skill_ids": {"type": "array", "items": {"type": "string"}, "title": "新的技能ID列表", "description": "完整替换,传全部要绑的技能 id"},
                "visibility": {"type": "string", "enum": ["private", "group", "public"], "title": "可见性", "description": "默认保持不变(不传)"},
            },
            "required": ["env", "agent_id", "bound_skill_ids"],
        },
        "script": build_script("""def main():
    p = json.load(sys.stdin)
    base, phone, pwd = _cfg()
    token = _login(base, phone, pwd)
    agent_id = p.get("agent_id")
    body = {"bound_skill_ids": p.get("bound_skill_ids") or []}
    if p.get("visibility"):
        body["visibility"] = p["visibility"]
    _call(base, token, "PUT", f"/agents/{agent_id}", body)
    pub = _call(base, token, "POST", f"/agents/{agent_id}/publish")
    _finish({"ok": True, "env": p.get("env"), "base_url": base,
             "agent_id": agent_id, "bound_skill_ids": body["bound_skill_ids"],
             "publish": pub.get("ok", True) if isinstance(pub, dict) else True})


main()
"""),
    },
]

# ──────────────────────────────────────────────────────────────────────────
# 「文档驱动Agent生成器」skill 的 prompt(方法论,符合行业标准)
# ──────────────────────────────────────────────────────────────────────────
BUILDER_SKILL_NAME = "文档驱动Agent生成器"
BUILDER_SKILL_DESCRIPTION = (
    "根据需求文档/接口文档一键生成 Agent+Skill+Tool 的智能体生成器:自动拆解接口、归类技能、"
    "发布与编排,支持分环境(local/dev/prod)操作,所有资产公共可见(public)。"
)

BUILDER_SKILL_PROMPT = """【角色与使命】
你是 cogni_agent 平台的「智能体生成工程师」。你的职责是:根据用户提供的需求文档与接口文档,
在该平台上生成一套可用的 Agent + Skill + Tool 资产,让用户后续直接在对话中问数/办事。
所有资产默认 public(全平台所有用户可见);统一由 pftroot 管理员账号创建(工具自动读取配置,
你无需也绝不能要求用户提供密码)。

【工作流程(严格按顺序执行)】
第一步 文档分析
  1. 通读需求文档与接口文档,提取接口清单:每个接口的 URI、HTTP 方法、鉴权方式、请求参数
     (必填/可选、类型、含义)、返回结构、业务域。
  2. 识别接口间依赖(如"先按景区ID取供应商ID,再查报表"这类桥接接口)。
第二步 设计并创建 HTTP 工具(create_http_tool)
  1. 一个业务接口 -> 一个工具。工具名用英文标识。
  2. description 是模型路由层的核心依据(OpenAI function calling 官方:description 是模型选择工具
     的核心依据,工具多时先做路由/预筛选基于描述缩小候选集),必须用中文写清四要素:
     做什么 + 何时用 + 关键参数来源 + 返回什么,不超过 200 字。
     **依赖/前置条件必须写在描述第一句**:路由层 Triager 只读工具描述前 120 字,依赖不前置会被
     截断读不到(如「须先用『参数解析』由景区ID换 supplierId,禁止编造」放第一句)。例如:
     "查询景区今日客流,须先用『数字大脑-参数解析』由景区ID换取 supplierId,禁止编造;
     当用户询问今日/实时入园出园人数时使用;返回今日客流汇总(入园/出园)。"
  3. url 用 {{变量}} 模板替换动态参数(如 {{scenic_id}}、{{start_date}});固定不变的查询参数直接写死在 url。
  4. headers 放固定请求头;需要动态传的(如 {{token}})也走 {{变量}} 模板。
  5. params_schema 必须是合法 JSON Schema:每个参数写 title(中文)、type、description(字段含义+取值口径);
     文档标为必填的参数必须进 required;日期参数约定格式(如 YYYY-MM-DD)。
     必填参数若无 description,模型会编造参数值——发布前自检会拦截。
  6. 风险等级:只读查询=low;写操作(下单/改数据)=medium 以上。
  7. visibility=public。
第三步 归类并创建 Skill(create_skill)
  1. 分类依据:接口文档的业务域/参数体系(如"supplierId 系报表"与"scenicId 系雷达/OTA"分开),
     不要把无关接口强行塞进一个技能。
  2. 每个 Skill 绑定该业务域的工具,并配 tool_usage:{tool_id: "什么时候用它"}。
  3. description 必填非空(发布校验要求):一句话能力概述,供 Triager 能力清单路由,写清"做什么+何时用"。
  4. prompt 写清:该技能能做什么、工具间协作顺序、数据口径提醒(如字段含义与字段名相反这类坑)、常见问题。
  5. constraints 必填非空(发布校验要求),写清业务硬约束:必填参数来源(如 supplierId 必须先用参数解析技能获取,禁止编造)、
     缺参时怎么办(能补则按说明补,必填关键缺失反问用户)、数据口径、禁止事项等。
  6. suggested_mode=L2;visibility=public。
第四步 逐个发布 Skill(publish_skill)
  对每个新建的 skill 执行派生+发布,确保状态变为 PUBLISHED。
第五步 创建并发布 Agent(create_agent)
  1. name=业务名;bound_skill_ids=本次创建的全部 skill id(按"先解析参数 -> 再查数据 ->
     兜底/汇总"的业务顺序排列;Agent 内部多技能协作由 agent 自身 composition_mode 决定);
     persona_desc 写明人格与能力;opening_line 给开场白;
     description 写清"面向谁+能做什么",供路由层识别场景。
  2. 创建后立即发布(PUBLISHED);visibility=public。
第六步 汇总报告
  输出本次创建清单:每个资产的名称、ID、类型、状态、可见性、绑定关系,供用户排查。
第七步 description 自检评分(只评分,不自动改写)
  对照第二步/第三步/第五步的 description 标准,对本次创建的每个 tool/skill/agent 的 description
  逐条打分(0~100,六维度:何时用 20/何时不用 20/关键参数 15/依赖前置 15/返回说明 10/简洁无歧义 20),给出:
  - 每条 description 的得分与等级(≥85 优秀/70~84 良好/50~69 及格/<50 不及格)
  - 低于 70 分的条目列出具体问题(缺何时用?缺参数来源?依赖未前置到 120 字内?太长?有歧义?)
  只评分并列出问题清单,不自动修改;把报告给用户,后续优化逐个进行(可对照评分修订后重跑对比)。

【行业规范(必须遵守,违反即返工)】
1. 先问后做(ask-before-act):文档缺失必填参数说明、或用户未指定目标环境时,先询问用户,
   绝不编造参数值、绝不虚构接口 URI/字段名/ID/日期。
2. 幂等:创建任何资源前先 list_resources 查重;同名已存在则报告"已存在,未重复创建",不覆盖、不删除。
3. 环境:操作前必须确认目标环境(env:local/dev/prod,用户未指定时询问,不默认跳过);
   所有工具自动连接该环境的管理后台,你从返回的 base_url 确认环境正确后再继续。
4. 安全:回复中不得输出密码、token、密钥;账号由工具内部从部署配置读取。
5. 失败处理:工具报错先读错误信息,可自纠则修正后重试(最多1次),否则如实告知用户并给下一步。
6. 可读性:展示用中文,状态附中文含义(如 PUBLISHED=已发布、DRAFT=草稿)。

【工具使用速查】
- list_resources:创建前查重、定位资源 id、核对状态。
- create_http_tool:创建 HTTP 工具(见第二步)。
- create_skill:创建 Skill(见第三步)。
- publish_skill:给 skill 派生草稿版本并发布(入参 skill_id)。
- create_agent:创建 Agent 并发布(入参含 bound_skill_ids)。
- update_agent_bindings:改绑 Agent 的技能并重新发布。"""


# ──────────────────────────────────────────────────────────────────────────
# 主流程:登录 -> 幂等创建工具 -> 创建/发布 skill
# ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="创建「文档驱动Agent生成器」skill 及配套管理工具")
    ap.add_argument("--env", choices=["local", "dev", "prod"], default="local", help="目标环境(默认 local)")
    ap.add_argument("--force-update", action="store_true",
                    help="已存在同名工具时,强制用本脚本的最新脚本内容更新其 type_config(用于修正工具脚本 bug)")
    args = ap.parse_args()

    load_env(args.env)
    base = os.environ.get("COGNI_ADMIN_BASE_URL", "").strip()
    if not base:
        raise SystemExit("未配置 COGNI_ADMIN_BASE_URL(请在 .env.{args.env} 中配置管理后台地址)")
    print(f"[*] 目标环境: {args.env} -> {base}")

    token = login(base)
    print(f"[*] 已用 {os.environ.get('COGNI_ADMIN_BOT_PHONE')} 登录管理后台")

    created_tool_ids: dict[str, str] = {}
    for t in TOOLS:
        existing = find_by_name(token, base, "tools", t["name"])
        if existing:
            tid = existing["id"]
            if args.force_update:
                api(base, token, "PUT", f"/tools/{tid}", {
                    "description": t["description"],
                    "type_config": {
                        "script": t["script"],
                        "input_schema": t["input_schema"],
                        "output_schema": {"type": "object"},
                    },
                    "risk_level": t["risk_level"],
                    "visibility": "public",
                })
                print(f"[~] 工具[{t['name']}]已存在,已用最新脚本更新(id={tid})")
            else:
                print(f"[~] 工具[{t['name']}]已存在(id={tid}),跳过(如需更新脚本加 --force-update)")
            created_tool_ids[t["name"]] = tid
            continue
        body = {
            "name": t["name"],
            "description": t["description"],
            "type": "python",
            "type_config": {
                "script": t["script"],
                "input_schema": t["input_schema"],
                "output_schema": {"type": "object"},
            },
            "risk_level": t["risk_level"],
            "visibility": "public",
        }
        res = api(base, token, "POST", "/tools", body)
        item = res.get("data", res) if isinstance(res, dict) else res
        tid = item.get("id")
        print(f"[+] 工具[{t['name']}]已创建(id={tid}, visibility=public)")
        created_tool_ids[t["name"]] = tid

    tool_ids = list(created_tool_ids.values())
    print(f"[*] 工具就绪: {len(tool_ids)} 个 -> {tool_ids}")

    existing_skill = find_by_name(token, base, "skills", BUILDER_SKILL_NAME)
    if existing_skill:
        print(f"[~] 技能[{BUILDER_SKILL_NAME}]已存在(id={existing_skill['id']}),跳过")
        skill_id = existing_skill["id"]
    else:
        body = {
            "name": BUILDER_SKILL_NAME,
            "description": BUILDER_SKILL_DESCRIPTION,
            "tool_ids": tool_ids,
            "tool_usage": {
                tid: desc for tid, desc in (
                    (created_tool_ids["list_resources"], "创建/改绑前查重、定位资源id、核对状态"),
                    (created_tool_ids["create_http_tool"], "按接口文档创建 HTTP 工具"),
                    (created_tool_ids["create_skill"], "归类创建技能并绑定工具"),
                    (created_tool_ids["publish_skill"], "发布技能(派生+发布+默认)"),
                    (created_tool_ids["create_composition"], "多技能编排为 chain/pool"),
                    (created_tool_ids["create_agent"], "创建并发布智能体"),
                    (created_tool_ids["update_agent_bindings"], "改绑智能体技能并重新发布"),
                )
            },
            "prompt": BUILDER_SKILL_PROMPT,
            "suggested_mode": "L2",
            "constraints": {"max_steps": 40},
            "visibility": "public",
        }
        res = api(base, token, "POST", "/skills", body)
        item = res.get("data", res) if isinstance(res, dict) else res
        skill_id = item.get("id")
        print(f"[+] 技能[{BUILDER_SKILL_NAME}]已创建(id={skill_id}, visibility=public, DRAFT)")

    # 发布(幂等:已 PUBLISHED 则跳过)
    cur = find_by_name(token, base, "skills", BUILDER_SKILL_NAME)
    if cur and cur.get("status") == "PUBLISHED":
        print(f"[~] 技能已是 PUBLISHED,无需重新发布")
    else:
        ver = api(base, token, "POST", f"/resources/skill/{skill_id}/versions", {"note": "文档驱动Agent生成器 v1"})
        # 派生接口返回 {code, data:{id}} 包裹,id 在 data.id
        vid = (ver.get("data") or {}).get("id") or ver.get("id")
        if not vid:
            raise SystemExit(f"派生版本未返回 id: {str(ver)[:300]}")
        api(base, token, "POST", f"/versions/{vid}/publish")
        try:
            api(base, token, "POST", f"/versions/{vid}/set-default")
        except SystemExit as e:
            print(f"[warn] set-default 失败(首次发布通常已自动默认): {e}")
        print(f"[+] 技能已发布(version={vid})")

    print("\n[✓] 完成。汇总:")
    print(f"  - Skill: {BUILDER_SKILL_NAME} (id={skill_id}, visibility=public)")
    for name, tid in created_tool_ids.items():
        print(f"  - Tool : {name} (id={tid}, visibility=public)")
    print("\n后续:将该技能绑定到任意 Agent(或新建一个 Agent),即可在对话中提供接口/需求文档让其生成全套资产。")


if __name__ == "__main__":
    main()
