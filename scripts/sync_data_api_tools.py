#!/usr/bin/env python
"""把《数字大脑 · 数据指标接口文档》同步成工具库里的 HTTP 工具。

**一般不用跑它**:服务启动时会自动把缺的工具建上(cogni_agent/tool_center_service/data_api_seed.py),
工具中心页面上也有「同步数据接口工具」按钮。这个脚本留给「从本机对远端环境做预览/刷新」的场景。

    # 先看会做什么(默认只预览,不写任何东西)
    uv run python scripts/sync_data_api_tools.py --doc 数据指标接口文档.md --env dev --account <账号>

    # 确认无误后落库:新建缺的工具,已有的不动
    uv run python scripts/sync_data_api_tools.py --doc 数据指标接口文档.md --env dev --account <账号> --apply

    # 连同已存在的同名接口一起刷新成文档里的最新定义(会覆盖手工改过的描述,慎用)
    ... --apply --update-existing

    # 目标环境的数据平台地址和文档里的不一样(文档写的是 12301dev 测试环境)
    ... --base-url https://data.12301.com

密码从 --password 或环境变量 COGNI_ADMIN_PASSWORD 读,**脚本里不写任何默认密码**。

做了什么:
  1. 按文档里的 `# 1.x` 切出每个接口,用与后台「按接口文档新建」**同一个解析器**解析;
  2. **按 URI 认已有工具**,不按名字 —— 工具库里原来那个叫「通过景区ID获取供应商ID列表」,
     文档里叫「全局-通过景区ID获取供应商ID列表」,按名字认会建出第二个;
  3. `supplierId` 声明成由「按景区取供应商 ID」自动获取(x-param-source),模型看不到也填不了;
     `scenicId` 声明成缺省取当前景区(x-ctx-default),没有景区时由系统发起澄清;
  4. 数据平台密钥**永不上送、永不落库**:文档 curl 里的明文在本地就抹掉,
     工具请求头里写的是占位 `{{data_platform_key}}`,调用时由服务端从配置里取。
     目标环境没配这把 key 时拒绝 --apply(否则建出来的 30 个工具一调就是 401)。
  5. --apply 之后用「按景区取供应商 ID」真跑一次,确认返回里取得到 supplierId ——
     这一步是在**你的环境**里核对取值路径,本地没法替你验证。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cogni_agent.tool_center_service.apidoc import annotate_params  # noqa: E402
# 解析/补参规则与服务启动时的自动同步是**同一份实现**(data_api_seed),
# 否则「脚本预览的」和「服务建出来的」迟早对不上。
from cogni_agent.tool_center_service.data_api_seed import (  # noqa: E402,F401
    DOC_PATH, SUPPLIER_PATH_SUFFIX, SUPPLIER_VALUE_PATH, _KEY_LITERAL,
    annotations_for, build_items, rebase, scrub, split_doc,
)

ENVS = {
    "local": "http://localhost:8000",
    "dev": "https://cogni-agent.12301dev.com:9443",
}


# ── 与服务端交互 ───────────────────────────────────────────────────────

class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body=None):
        r = httpx.request(method, f"{self.base}/api/admin{path}",
                          headers={"Authorization": f"Bearer {self.token}"},
                          json=body, timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise SystemExit(f"{method} {path} → HTTP {r.status_code},返回不是 JSON:{r.text[:200]}")
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} 失败(HTTP {r.status_code}):{json.dumps(data, ensure_ascii=False)[:400]}")
        # 与仓库里其它脚本同一口径:统一响应包一层 {code, data} 时取 data
        if isinstance(data, dict) and "code" in data and "data" in data:
            return data["data"]
        return data


def login(base: str, account: str, password: str) -> str:
    r = httpx.post(f"{base.rstrip('/')}/api/admin/auth/login",
                   json={"phone": account, "password": password, "rsa_encrypt": False}, timeout=20)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    token = ((body or {}).get("data") or body or {}).get("access_token")
    if not token:
        raise SystemExit(f"登录失败(HTTP {r.status_code}):{str(body)[:200]}")
    return token


def index_existing(tools: list[dict]) -> dict[tuple[str, str], dict]:
    """按 (方法, 路径) 索引已有的 http 工具。已删除/非 http 的不算。"""
    out = {}
    for t in tools:
        tc = t.get("type_config") or {}
        if t.get("type") != "http" or not tc.get("url"):
            continue
        key = ((tc.get("method") or "GET").upper(), urlparse(tc["url"]).path)
        out.setdefault(key, t)
    return out


def plaintext_key_in(tool: dict) -> bool:
    """已有工具的请求头里是不是存着明文密钥(早先按文档导入的都是这样)。"""
    headers = (tool.get("type_config") or {}).get("headers") or {}
    return any(isinstance(v, str) and v and "{{" not in v and "***" not in v
               for k, v in headers.items() if k.lower() == "xpftkey")


def verify_supplier(api: Api, tool_id: str, scenic_id: str) -> tuple[bool, str]:
    """真跑一次供应商接口,确认取值路径在你的环境里取得到东西。"""
    started = api.call("POST", f"/tools/{tool_id}/test", {"params": {"scenicId": scenic_id}})
    task_id = started.get("task_id")
    if not task_id:
        return False, f"没拿到调试任务号:{started}"
    for _ in range(30):
        time.sleep(1)
        res = api.call("GET", f"/chat/result/{task_id}")
        if res.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            blob = json.dumps(res.get("result") or {}, ensure_ascii=False)
            m = re.search(r'"supplierId"\s*:\s*\[([^\]]*)\]', blob)
            if res.get("status") == "COMPLETED" and m and m.group(1).strip():
                return True, f"景区 {scenic_id} → supplierId [{m.group(1).strip()}]"
            return False, f"任务 {res.get('status')},结果里没取到 supplierId:{blob[:300]}"
    return False, "30 秒内没跑完"


# ── 主流程 ────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="把数据指标接口文档同步成工具库里的 HTTP 工具")
    ap.add_argument("--doc", default=str(DOC_PATH), help="接口文档 Markdown 路径(缺省用随服务打包的那份)")
    ap.add_argument("--env", default="local", choices=list(ENVS))
    ap.add_argument("--server", default=None, help="直接给服务地址(覆盖 --env)")
    ap.add_argument("--account", required=False, help="管理员账号(手机号);只预览本地解析时可不填")
    ap.add_argument("--password", default=None, help="缺省读环境变量 COGNI_ADMIN_PASSWORD")
    ap.add_argument("--base-url", default=None,
                    help="数据平台地址,替换文档里的 https://data.12301dev.com(文档写的是测试环境)")
    ap.add_argument("--visibility", default="public", choices=["private", "scenic", "group", "public"],
                    help="新建工具的可见范围(默认 public:所有景区的技能都能用,景区隔离靠参数与越权校验)")
    ap.add_argument("--apply", action="store_true", help="真的写入;不加只预览")
    ap.add_argument("--update-existing", action="store_true",
                    help="已存在的同接口工具也按文档刷新(新版本并发布);会覆盖手工改过的描述")
    ap.add_argument("--verify-scenic", default="PFTSCA01018088",
                    help="落库后用这个景区真跑一次供应商接口核对取值路径;传空字符串跳过")
    args = ap.parse_args()

    md = Path(args.doc).read_text(encoding="utf-8")
    items = build_items(md, base_url=args.base_url)
    bad = [i for i in items if "error" in i]
    good = [i for i in items if "error" not in i]
    supplier = next((i for i in good if i["is_supplier"]), None)

    print(f"文档里切出 {len(items)} 个接口,解析成功 {len(good)} 个" + (f",失败 {len(bad)} 个" if bad else ""))
    for b in bad:
        print(f"  ✘ {b['chunk']!r}:{b['error']}")
    if supplier is None:
        print("  ✘ 文档里没找到「通过景区ID获取供应商ID列表」—— 需要 supplierId 的接口将无法自动取参")

    if not args.account:
        # 纯本地预览:不连服务端,只看解析结果
        for i in good:
            print(f"  · {i['name']:<26} {i['method']} {i['path']}  参数:{','.join(i['props'])}")
        print("\n(没给 --account,只做了本地解析预览;连上服务端才能判断哪些已存在)")
        return 1 if bad else 0

    base = args.server or ENVS[args.env]
    password = args.password or os.environ.get("COGNI_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("没有密码:请用 --password 或设置环境变量 COGNI_ADMIN_PASSWORD")
    api = Api(base, login(base, args.account, password))

    configured = (api.call("GET", "/tools/builtin").get("secrets_configured") or {})
    key_ok = bool(configured.get("data_platform_key"))
    print(f"\n目标服务 {base}:数据平台密钥 data_platform_key "
          f"{'已配置' if key_ok else '**未配置**'}")
    if not key_ok:
        print("  这批工具的请求头用的是占位 {{data_platform_key}},服务端没配真实值的话一调就是 401。\n"
              "  请在目标环境设置环境变量 DATA_PLATFORM_XPFTKEY(见 config.yaml → tool_center.secrets)后再 --apply。")

    existing = index_existing(api.call("GET", "/tools").get("items", []))
    sup_existing = existing.get((supplier["method"], supplier["path"])) if supplier else None
    # 前置工具按**实际存在的名字**引用:工具库里原来那个可能不叫文档里的名字
    supplier_name = (sup_existing or {}).get("name") or (supplier or {}).get("name") or ""

    plan = []
    for i in good:
        ex = existing.get((i["method"], i["path"]))
        i["annotations"] = annotations_for(i["props"], is_supplier=i["is_supplier"],
                                           supplier_tool_name=supplier_name)
        if ex is None:
            action = "新建"
        elif args.update_existing:
            action = "刷新"
        else:
            action = "已存在,跳过"
        plan.append((i, ex, action))

    print(f"\n{'动作':<10}{'工具名':<28}{'路径':<52}系统补参")
    for i, ex, action in plan:
        filled = ",".join(f"{k}←{'供应商接口' if 'x-param-source' in v else '当前景区'}"
                          for k, v in i["annotations"].items()) or "-"
        note = ""
        if ex is not None:
            note = f"  [已有:{ex.get('name')}]"
            if plaintext_key_in(ex):
                note += "  ⚠ 请求头里是明文密钥" + ("(本次刷新会换成占位)" if action == "刷新" else
                                             "(加 --update-existing 可换成占位)")
            if "supplierId" in i["props"] and not i["is_supplier"]:
                ps = ((ex.get("type_config") or {}).get("params_schema") or {}).get("properties") or {}
                if not (ps.get("supplierId") or {}).get("x-param-source"):
                    note += "  ⚠ supplierId 仍交给模型填"
        print(f"{action:<10}{i['name']:<28}{i['path']:<52}{filled}{note}")
        for w in i["warnings"]:
            print(f"{'':<10}  · {w}")

    creates = [p for p in plan if p[2] == "新建"]
    updates = [p for p in plan if p[2] == "刷新"]
    print(f"\n合计:新建 {len(creates)},刷新 {len(updates)},跳过 {len(plan) - len(creates) - len(updates)}")
    if not args.apply:
        print("这是预览。确认无误后加 --apply 执行。")
        return 1 if bad else 0
    if not key_ok:
        raise SystemExit("目标环境没配数据平台密钥,拒绝写入(理由见上)。")

    # 供应商接口必须最先建:其它工具的 x-param-source 按名字引用它
    ordered = sorted(plan, key=lambda p: (not p[0]["is_supplier"]))
    supplier_id = (sup_existing or {}).get("id")
    for i, ex, action in ordered:
        if action == "新建":
            created = api.call("POST", "/tools/from-api-doc", {
                "markdown": i["markdown"], "name": i["name"], "visibility": args.visibility,
                "param_annotations": i["annotations"]})
            print(f"  ✔ 新建 {i['name']}({created.get('id')})")
            if i["is_supplier"]:
                supplier_id = created.get("id")
        elif action == "刷新":
            tc = annotate_params(i["type_config"], i["annotations"])
            api.call("PUT", f"/tools/{ex['id']}", {
                "type_config": tc, "description": i["doc"].summary or ex.get("description") or ""})
            vers = api.call("GET", f"/resources/tool/{ex['id']}/versions").get("items", [])
            draft = next((v for v in vers if v.get("status") == "draft"), None)
            if draft is None:
                print(f"  ✘ {i['name']}:保存后没找到草稿,未发布")
                continue
            api.call("POST", f"/versions/{draft['id']}/publish")
            print(f"  ✔ 刷新并发布 {i['name']}({ex['id']})")

    if args.verify_scenic and supplier_id:
        ok, msg = verify_supplier(api, supplier_id, args.verify_scenic)
        print(f"\n核对供应商取值路径({SUPPLIER_VALUE_PATH}):{'✔' if ok else '✘'} {msg}")
        if not ok:
            print("  取值路径和你环境里的实际返回对不上时,需要 supplierId 的 10 个接口都会调用失败。\n"
                  "  请把上面这段返回发给研发,调整 scripts/sync_data_api_tools.py 里的 SUPPLIER_VALUE_PATH。")
            return 2
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
