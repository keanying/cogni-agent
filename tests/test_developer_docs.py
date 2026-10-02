# -*- coding: utf-8 -*-
"""开发者 API 文档的体检。

这份文档是**公开页面**(/developer 免登录),两件事必须钉死:
1. 随包发布的 HTML 与生成脚本一致 —— 有人手改了 HTML 而没改脚本的话,
   下次一生成就被覆盖,改动白做;
2. 页面里不能出现任何真实密钥 —— 公开页泄一次密钥,换密钥的成本远高于这条用例。

另外顺带检查「每个接口都写全了」:漏掉请求示例或返回示例的接口,
集成方就得去翻代码 —— 而那正是这份文档要避免的事。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_developer_docs as gen  # noqa: E402


def test_shipped_html_matches_generator():
    """随包的 HTML 必须是脚本生成的当前结果(改文档请改脚本,再跑一次生成)。"""
    assert gen.OUT.exists(), "开发者文档没有随包发布"
    assert gen.OUT.read_text(encoding="utf-8") == gen.render(gen.CHAPTERS), (
        "cogni_agent/static/developer/index.html 与 scripts/build_developer_docs.py 不一致,"
        "请运行:uv run python scripts/build_developer_docs.py")


def test_no_real_credentials_in_public_doc():
    """公开页面里只能有占位密钥。"""
    text = gen.OUT.read_text(encoding="utf-8")
    # Quick BI 的 api_key / user_token 是 32 位十六进制 —— 文档里没有任何合法用途
    assert not re.findall(r"\b[0-9a-f]{32}\b", text), "文档里出现疑似 32 位密钥"
    # UUID 本身是合法的示例(资源 ID 就长这样),但**挨着 secret/token/key 出现**就不正常了
    bad = re.findall(r"(?:secret|token|api[_-]?key)[^<\n]{0,40}"
                     r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I)
    assert not bad, f"密钥字段旁出现了具体值:{bad[:2]}"
    assert "sk-zhilv-xxxxxxxx" in text, "示例 Key 应当是占位串"
    assert "data.12301dev.com" not in text, "文档里不应出现内部地址"

    # 现场的 .env.quickbi 若在(本地开发环境),逐个值比对一遍 —— 这是最直接的一道
    env = gen.OUT.parents[3] / ".env.quickbi"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, val = (x.strip().strip('"').strip("'") for x in line.split("=", 1))
            # 只比对密钥类的值:server_domain 是阿里云的公开地址,文档里本来就该出现
            if any(w in key.lower() for w in ("secret", "token", "key", "password")) and len(val) >= 12:
                assert val not in text, f"文档里出现了 .env.quickbi 的 {key} 真实值"


@pytest.mark.parametrize("api", [it for ch in gen.CHAPTERS for it in ch.items if isinstance(it, gen.Api)],
                         ids=lambda a: a.uri + ":" + a.method)
def test_every_api_is_complete(api):
    """每个接口都要有:一句话说明、请求示例、返回示例(SSE 接口除外,它给的是事件流)。"""
    assert api.summary.strip(), f"{api.uri} 缺少一句话说明"
    assert api.curl.strip(), f"{api.uri} 缺少 curl 示例"
    assert api.resp_json.strip(), f"{api.uri} 缺少返回示例"
    assert api.method in ("GET", "POST", "PUT", "PATCH", "DELETE")
    # 三类:开放平台(/v1,API Key)、前端 APP、管理后端(后两者账号登录)。
    # 其它内部路由不进这份文档 —— 它是公开页
    assert api.uri.startswith(("/v1/", "/api/app/", "/api/admin/")), f"{api.uri} 不该出现在公开文档里"


def test_uris_match_real_routes():
    """文档里的 URI 必须真的存在 —— 文档写了、代码没有,是最伤人的一种错。"""
    from cogni_agent.gateway_service import routes_openai
    # routes_admin = 管理端的 Quick BI 接口(/api/admin/quickbi/*)。
    # 它原先不在这份清单里,于是文档里写了一条管理端 Quick BI 接口,
    # 这个检查反而报「路由里没有」—— 检查漏扫了一个模块,比不检查更误导人。
    from cogni_agent.tool_center_service import routes as tool_center_routes
    from cogni_agent.tool_center_service.quickbi import routes_admin as qbi_admin
    from cogni_agent.tool_center_service.quickbi import routes_open

    from cogni_agent.gateway_service import routes_api_keys, routes_app, routes_app_ops
    from cogni_agent.user_center_service import routes as user_routes

    real = set()
    for mod in (routes_openai, routes_open, qbi_admin, tool_center_routes, routes_app,
                routes_app_ops, routes_api_keys, user_routes):
        # r.path 已经含了 router 自己的 prefix(FastAPI 建路由时就拼好了),不要再拼一次
        # /v1 与 /api/app 这两层在 main.py 里直接挂(路径里已含 /api),其余挂在 /api 下
        mount = "" if mod.router.prefix.startswith(("/v1", "/api")) else "/api"
        for r in mod.router.routes:
            for m in getattr(r, "methods", ()):
                real.add((m, mount + r.path))

    documented = {(a.method, a.uri) for ch in gen.CHAPTERS for a in ch.items if isinstance(a, gen.Api)}
    # 文档里的路径参数写成 {id}/{run_id},与路由声明可能叫法不同,按「段数 + 前缀」归一化比较
    def norm(m, u):
        return (m, re.sub(r"\{[^}]+\}", "{}", u))

    missing = {norm(*d) for d in documented} - {norm(*r) for r in real}
    assert not missing, f"文档写了但路由里没有:{sorted(missing)}"
