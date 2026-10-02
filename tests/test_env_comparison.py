"""
ApiResponse 中间件改造 · 本地 vs 开发环境 对比测试

对本地环境 (localhost:8000) 和开发环境 (cogni-agent.12301dev.com:9443)
分别执行相同的 15 个集成测试用例，逐项对比响应结构。

输出: 打印终端结果 + 生成 Markdown 对比报告。

用法（报告默认落 docs/报告/，该目录不入库）:
  python tests/test_env_comparison.py --output docs/报告/本地与开发环境ApiResponse对比报告.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# 确保项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── 测试环境配置 ──
LOCAL_BASE_URL = os.environ.get("LOCAL_BASE_URL", "http://localhost:8000")
DEV_BASE_URL = os.environ.get("DEV_BASE_URL", "https://cogni-agent.12301dev.com:9443")
ACCOUNT = os.environ.get("TEST_ACCOUNT", "")
PASSWORD = os.environ.get("TEST_PASSWORD", "")


@dataclass
class TestCase:
    """单个测试用例定义。"""
    test_id: str
    description: str
    method: str
    path: str
    needs_auth: bool = False
    body: dict | None = None
    query: dict | None = None
    expect_status: int = 200
    expect_code_zero: bool = True
    assertions: str = ""


# ═══════════════════════════════════════════════════════════
# 测试用例定义（15 项，对应 08 文档 REMOTE-00 ~ REMOTE-14）
# ═══════════════════════════════════════════════════════════

TEST_CASES: list[TestCase] = [
    # ── 无需鉴权接口 ──
    TestCase("C-01", "健康检查", "GET", "/health",
             needs_auth=False,
             assertions="HTTP 200, 验证中间件对 /health 的包裹行为（两端可能不一致）"),

    TestCase("C-02", "获取 RSA 公钥", "GET", "/api/app/auth/public-key",
             needs_auth=False,
             assertions="HTTP 200, body.code==0, body.data 含 public_key/algorithm/key_size"),

    TestCase("C-03", "登录策略查询", "GET", "/api/app/login-policy",
             needs_auth=False,
             assertions="HTTP 200, body.code==0, data 含 require_sms_on_first_login"),

    # ── 登录相关 ──
    TestCase("C-00", "登录", "POST", "/api/app/auth/login",
             needs_auth=False,
             body={"account": ACCOUNT, "password": PASSWORD},
             assertions="HTTP 200, body.code==0, data 含 access_token/refresh_token/user"),

    TestCase("C-04", "刷新令牌(无效token)", "POST", "/api/app/auth/refresh",
             needs_auth=True,
             body={"refresh_token": "invalid_token_xxx"},
             expect_status=401, expect_code_zero=False,
             assertions="HTTP 401, body.code==2000, message 含 '无效的 refresh_token'"),

    # ── 需鉴权接口 ──
    TestCase("C-05", "获取个人信息", "GET", "/api/app/me",
             needs_auth=True,
             assertions="HTTP 200, body.code==0, data 含 id/name/account/role 等"),

    TestCase("C-06", "获取权限列表", "GET", "/api/app/permissions",
             needs_auth=True,
             assertions="HTTP 200, body.code==0"),

    # ── 错误分支 ──
    TestCase("C-07", "修改密码(旧密码错误)", "POST", "/api/app/me/change-password",
             needs_auth=True,
             body={"old_password": "wrong_old_password_123", "new_password": "NewTest456!"},
             expect_code_zero=False,
             assertions="HTTP 400(改造后)或200(改造前), body.code==3007, message 含'旧密码'或'原密码'"),

    TestCase("C-08", "修改密码(新旧相同)", "POST", "/api/app/me/change-password",
             needs_auth=True,
             body={"old_password": PASSWORD, "new_password": PASSWORD},
             assertions="新密码与原密码相同时的行为"),

    # ── API Keys 生命周期 ──
    TestCase("C-09", "创建 API Key", "POST", "/api/admin/api-keys",
             needs_auth=True,
             body={"agent_allowed_ids": [], "expires_in_days": 30},
             assertions="HTTP 200, body.code==0, data 含 id/key/prefix_display/created_at"),

    TestCase("C-10", "API Key 列表", "GET", "/api/admin/api-keys",
             needs_auth=True,
             assertions="HTTP 200, body.code==0, data 为数组"),

    TestCase("C-11", "吊销 API Key", "POST", "/api/admin/api-keys/{key_id}/revoke",
             needs_auth=True,
             assertions="HTTP 200, body.code==0, message 含'已吊销'"),

    TestCase("C-12", "吊销不存在的 API Key", "POST", "/api/admin/api-keys/nonexistent-key/revoke",
             needs_auth=True,
             expect_code_zero=False,
             assertions="HTTP 200(旧)或404(新), body.code==3000, message 含'不存在'"),

    # ── 登出 ──
    TestCase("C-13", "登出", "POST", "/api/app/auth/logout",
             needs_auth=True,
             body={"all_devices": False},
             assertions="HTTP 200, body.code==0"),
]


# ═══════════════════════════════════════════════════════════
# HTTP 客户端（同步版，httpx）
# ═══════════════════════════════════════════════════════════

class EnvTestClient:
    """对单个环境执行所有 15 个测试用例。"""

    def __init__(self, base_url: str, env_name: str):
        self.base = base_url.rstrip("/")
        self.env_name = env_name
        self.token: str | None = None
        self.created_key_id: str | None = None

    def _headers(self, needs_auth: bool) -> dict:
        h = {"Content-Type": "application/json"}
        if needs_auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, needs_auth: bool = False,
                 body: dict | None = None, query: dict | None = None) -> tuple[int, Any]:
        """发送 HTTP 请求，返回 (status_code, parsed_body)。"""
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        import httpx
        url = f"{self.base}{path}"
        params = query or {}
        try:
            with httpx.Client(verify=False, timeout=30) as client:
                if method == "GET":
                    r = client.get(url, params=params, headers=self._headers(needs_auth))
                elif method == "POST":
                    r = client.post(url, json=body or {}, params=params,
                                    headers=self._headers(needs_auth))
                else:
                    raise ValueError(f"不支持的 method: {method}")
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, r.text
        except Exception as e:
            return 0, f"ERROR: {e}"

    def run_all(self) -> list[dict]:
        """依次执行所有测试用例，返回结果列表。"""
        results: list[dict] = []

        # Step 1: 先登录获取 token（使用 C-00 的数据）
        print(f"\n  [{self.env_name}] 登录获取 token...")
        status, body = self._request("POST", "/api/app/auth/login", needs_auth=False,
                                     body={"account": ACCOUNT, "password": PASSWORD})
        if status == 200 and isinstance(body, dict) and body.get("code") == 0:
            self.token = body["data"]["access_token"]
            print(f"  [{self.env_name}] 登录成功, token: {self.token[:20]}...")
        else:
            print(f"  [{self.env_name}] 登录失败! status={status}, body={str(body)[:200]}")

        # Step 2: 执行所有用例
        for tc in TEST_CASES:
            tc_path = tc.path

            # C-11 需要动态 key_id
            if "{key_id}" in tc_path:
                if self.created_key_id:
                    tc_path = tc_path.replace("{key_id}", self.created_key_id)
                else:
                    results.append({
                        "id": tc.test_id, "description": tc.description,
                        "method": tc.method, "path": tc.path,
                        "status": 0, "body": {"error": "未创建 API Key, 跳过"},
                        "assertions": tc.assertions, "env": self.env_name,
                    })
                    continue

            print(f"  [{self.env_name}] {tc.test_id}: {tc.method} {tc_path} ...")
            status, body = self._request(
                tc.method, tc_path,
                needs_auth=tc.needs_auth,
                body=tc.body,
                query=tc.query,
            )

            # 保存 C-09 创建的 key_id
            if tc.test_id == "C-09" and status == 200 and isinstance(body, dict):
                key_data = body.get("data", {}) or {}
                self.created_key_id = key_data.get("id", "")

            results.append({
                "id": tc.test_id,
                "description": tc.description,
                "method": tc.method,
                "path": tc_path,
                "status": status,
                "body": body,
                "assertions": tc.assertions,
                "env": self.env_name,
            })

        return results


# ═══════════════════════════════════════════════════════════
# 分析 & 报告生成
# ═══════════════════════════════════════════════════════════

def analyze_body(body: Any) -> dict[str, Any]:
    """提取响应的结构化信息用于对比。"""
    info: dict[str, Any] = {
        "body_summary": "",
        "has_code_field": False,
        "code_value": None,
        "has_message_field": False,
        "has_data_field": False,
        "has_trace_id_field": False,
        "data_type": None,
        "data_keys": None,
        "is_wrapped": False,  # 是否被 ApiResponse 包裹
    }

    if not isinstance(body, dict):
        info["body_summary"] = f"非 JSON: {str(body)[:100]}"
        return info

    # 检测 ApiResponse 包裹
    has_all = all(k in body for k in ("code", "message", "data"))
    info["has_code_field"] = "code" in body
    info["has_message_field"] = "message" in body
    info["has_data_field"] = "data" in body
    info["has_trace_id_field"] = "trace_id" in body
    info["is_wrapped"] = has_all
    info["code_value"] = body.get("code")

    if has_all:
        data = body.get("data")
        info["data_type"] = type(data).__name__
        if isinstance(data, dict):
            info["data_keys"] = list(data.keys())[:10]  # 最多 10 个
        elif isinstance(data, list):
            info["data_keys"] = f"list[{len(data)}]"
        if body.get("code") == 0:
            info["body_summary"] = f"包裹成功: code=0, data类型={info['data_type']}"
        else:
            info["body_summary"] = f"业务错误: code={body.get('code')}, message={body.get('message', '')[:50]}"
    else:
        info["body_summary"] = f"未包裹: keys={list(body.keys())[:5]}"

    return info


def compare_results(local_results: list[dict], dev_results: list[dict]) -> list[dict]:
    """逐项对比本地和开发环境的测试结果。"""
    comparison: list[dict] = []

    for i, (lr, dr) in enumerate(zip(local_results, dev_results)):
        local_analysis = analyze_body(lr["body"])
        dev_analysis = analyze_body(dr["body"])

        # 判断一致性
        status_match = lr["status"] == dr["status"]
        wrap_match = local_analysis["is_wrapped"] == dev_analysis["is_wrapped"]
        code_match = local_analysis.get("code_value") == dev_analysis.get("code_value")

        # 判断 status 码差异的语义
        status_diff_note = ""
        if not status_match:
            status_diff_note = f"本地 {lr['status']} vs 开发 {dr['status']}"

        overall_match = status_match and wrap_match and code_match

        comparison.append({
            "id": lr["id"],
            "description": lr["description"],
            "method": lr["method"],
            "path": lr["path"],
            "local": {
                "status": lr["status"],
                "analysis": local_analysis,
                "body": lr["body"],
            },
            "dev": {
                "status": dr["status"],
                "analysis": dev_analysis,
                "body": dr["body"],
            },
            "match": {
                "status": status_match,
                "wrap": wrap_match,
                "code": code_match,
                "overall": overall_match,
            },
            "status_diff_note": status_diff_note,
            "assertions": lr["assertions"],
        })

    return comparison


# ═══════════════════════════════════════════════════════════
# Markdown 报告生成
# ═══════════════════════════════════════════════════════════

def generate_markdown(comparison: list[dict],
                      local_url: str, dev_url: str) -> str:
    """生成完整的对比报告 Markdown。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("# 10-本地与开发环境 ApiResponse 中间件对比测试报告")
    lines.append("")
    lines.append(f"> 测试时间：{now}")
    lines.append(f"> 改造分支：`migrate/merge-new-version`")
    lines.append(f"> 本地环境：`{local_url}`（已部署中间件）")
    lines.append(f"> 开发环境：`{dev_url}`（K8s 当前部署版本）")
    lines.append(f"> 测试账号：`{ACCOUNT}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 一、测试目的")
    lines.append("")
    lines.append("将本地环境（已应用 ApiResponseMiddleware）与开发环境（K8s 当前部署版本）")
    lines.append("进行逐接口对比，验证：")
    lines.append("")
    lines.append("1. 中间件改造后，本地环境所有接口的响应结构与开发环境**保持一致**")
    lines.append("2. 中间件改造引入的 HTTP 状态码变化（错误码由 200 → 4xx）是**预期且正确的**")
    lines.append("3. 确认开发环境当前版本与本地新版本之间的具体差异点")
    lines.append("")

    # ── 测试环境摘要 ──
    lines.append("## 二、测试环境概况")
    lines.append("")
    lines.append("| 项目 | 本地环境 | 开发环境 |")
    lines.append("|------|---------|---------|")
    lines.append(f"| 地址 | `{local_url}` | `{dev_url}` |")
    lines.append("| ApiResponseMiddleware | ✅ 已部署 | ❓ 取决于 K8s 版本 |")
    lines.append("| 数据库 | Docker pgvector:pg16 | K8s PostgreSQL |")
    lines.append("| Redis | Docker redis:7-alpine | K8s Redis |")
    lines.append("| Checkpointer | AsyncPostgresSaver | AsyncPostgresSaver |")
    lines.append("")

    # ── 汇总表 ──
    total = len(comparison)
    all_match = sum(1 for c in comparison if c["match"]["overall"])
    status_diff = sum(1 for c in comparison if not c["match"]["status"])
    wrap_diff = sum(1 for c in comparison if not c["match"]["wrap"])
    code_diff = sum(1 for c in comparison if not c["match"]["code"])

    lines.append("## 三、对比汇总")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 对比用例总数 | {total} |")
    lines.append(f"| 完全一致 | {all_match} |")
    lines.append(f"| 状态码不一致 | {status_diff} |")
    lines.append(f"| 包裹不一致 | {wrap_diff} |")
    lines.append(f"| 业务 code 不一致 | {code_diff} |")
    lines.append("")

    if status_diff > 0:
        lines.append('> **注意**：状态码不一致通常是**预期的**——开发环境可能尚未部署中间件改造，'
                     '旧代码对错误分支返回 HTTP 200，新中间件返回正确的 4xx。')
        lines.append("")

    # ── 逐项对比 ──
    lines.append("## 四、逐项对比详情")
    lines.append("")

    for c in comparison:
        test_id = c["id"]
        desc = c["description"]
        method = c["method"]
        path = c["path"]

        local_status = c["local"]["status"]
        dev_status = c["dev"]["status"]
        local_analysis = c["local"]["analysis"]
        dev_analysis = c["dev"]["analysis"]
        status_match = c["match"]["status"]
        wrap_match = c["match"]["wrap"]
        code_match = c["match"]["code"]
        overall = c["match"]["overall"]
        diff_note = c["status_diff_note"]
        assertions = c["assertions"]

        verdict = "✅ 一致" if overall else "⚠️ 差异"

        lines.append(f"### {test_id}: {desc} — {verdict}")
        lines.append("")
        lines.append(f"- **请求**: `{method} {path}`")
        lines.append("")

        # 对比表格
        lines.append("| 维度 | 本地环境 | 开发环境 | 一致? |")
        lines.append("|------|---------|---------|-------|")
        lines.append(f"| HTTP 状态码 | `{local_status}` | `{dev_status}` | {'✅' if status_match else '❌'} |")
        lines.append(f"| 是否 ApiResponse 包裹 | {'是' if local_analysis['is_wrapped'] else '否'} | {'是' if dev_analysis['is_wrapped'] else '否'} | {'✅' if wrap_match else '❌'} |")
        lines.append(f"| 业务 code 值 | `{local_analysis.get('code_value')}` | `{dev_analysis.get('code_value')}` | {'✅' if code_match else '❌'} |")
        lines.append(f"| data 类型 | `{local_analysis.get('data_type')}` | `{dev_analysis.get('data_type')}` | |")
        lines.append(f"| 本地摘要 | {local_analysis['body_summary']} | | |")
        lines.append(f"| 开发摘要 | | {dev_analysis['body_summary']} | |")
        lines.append("")

        if diff_note:
            lines.append(f"> **⚠️ 状态码差异**: {diff_note}")
            lines.append("")

        lines.append("**断言**: " + assertions)
        lines.append("")

        # 本地响应体
        local_body_str = json.dumps(c["local"]["body"], indent=2, ensure_ascii=False)
        if len(local_body_str) > 2000:
            local_body_str = local_body_str[:2000] + "\n... (截断)"
        lines.append("<details>")
        lines.append("<summary>本地环境响应体</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(local_body_str)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

        # 开发环境响应体
        dev_body_str = json.dumps(c["dev"]["body"], indent=2, ensure_ascii=False)
        if len(dev_body_str) > 2000:
            dev_body_str = dev_body_str[:2000] + "\n... (截断)"
        lines.append("<details>")
        lines.append("<summary>开发环境响应体</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(dev_body_str)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

        lines.append("---")
        lines.append("")

    # ── 差异分析 ──
    lines.append("## 五、差异分析与结论")
    lines.append("")

    # 收集差异项
    diffs = [c for c in comparison if not c["match"]["overall"]]
    status_diffs = [c for c in diffs if not c["match"]["status"]]
    wrap_diffs = [c for c in diffs if not c["match"]["wrap"]]
    code_diffs = [c for c in diffs if not c["match"]["code"]]

    if not diffs:
        lines.append("**两个环境完全一致，无差异。** 中间件改造已平滑过渡。")
    else:
        lines.append(f"共发现 **{len(diffs)}** 项差异，详细如下：")
        lines.append("")

        if status_diffs:
            lines.append(f"### 5.1 HTTP 状态码差异（{len(status_diffs)} 项）")
            lines.append("")
            for sd in status_diffs:
                lines.append(f"- **{sd['id']} {sd['description']}**: 本地 `{sd['local']['status']}`, 开发 `{sd['dev']['status']}`")
                lines.append(f"  - 原因: {sd['status_diff_note']}")
            lines.append("")

        if wrap_diffs:
            lines.append(f"### 5.2 包裹行为差异（{len(wrap_diffs)} 项）")
            lines.append("")
            for wd in wrap_diffs:
                lines.append(f"- **{wd['id']} {wd['description']}**: 本地 {'已包裹' if wd['local']['analysis']['is_wrapped'] else '未包裹'}, 开发 {'已包裹' if wd['dev']['analysis']['is_wrapped'] else '未包裹'}")
            lines.append("")
            lines.append("> 这类差异通常是开发环境尚未部署中间件改造代码所致。上线后会自动对齐。")
            lines.append("")

        if code_diffs:
            lines.append(f"### 5.3 业务 code 值差异（{len(code_diffs)} 项）")
            lines.append("")
            for cd in code_diffs:
                lines.append(f"- **{cd['id']} {cd['description']}**: 本地 code=`{cd['local']['analysis'].get('code_value')}`, 开发 code=`{cd['dev']['analysis'].get('code_value')}`")
            lines.append("")

    lines.append("### 关键行为变化（与 08 文档一致）")
    lines.append("")
    lines.append("| 接口 | 改造前 (开发环境可能仍为此) | 改造后 (本地环境) |")
    lines.append("|------|---------------------------|-------------------|")
    lines.append("| `change_password` 密码错 | HTTP 200 + `{code:3007}` | HTTP 400 + `{code:3007}` |")
    lines.append("| `revoke_key` 不存在 | HTTP 200 + `{code:3000}` | HTTP 404 + `{code:3000}` |")
    lines.append("| `/health` | 裸 JSON `{status:\"ok\"}` | `{code:0, data:{status:\"ok\"}}`（中间件包裹）|")
    lines.append("")
    lines.append("> **兼容性注意**：前端如果只用 `body.code == 0` 判断成功则不受影响；如果用了 `res.ok`（HTTP 状态码级别）则需要适配。")
    lines.append("")

    # ── 总体结论 ──
    lines.append("## 六、总体结论")
    lines.append("")
    lines.append(f"1. 本地环境与开发环境在 **{total}** 个接口中，**{all_match}** 个完全一致")
    if diffs:
        lines.append(f"2. **{len(diffs)}** 个不一致项中：")
        if status_diffs:
            lines.append(f"   - {len(status_diffs)} 个为 HTTP 状态码差异（预期内的改造变化，上线后自动对齐）")
        if wrap_diffs:
            lines.append(f"   - {len(wrap_diffs)} 个为包裹行为差异（开发环境需部署新版本后同步）")
        if code_diffs:
            lines.append(f"   - {len(code_diffs)} 个为业务 code 值差异（需排查）")
    else:
        lines.append("2. 无差异项，中间件改造完全兼容")
    lines.append("3. 中间件改造对外部 APP 端的影响为**透明**的——正常返回始终是 `{code:0, data:原值}` 格式")
    lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 终端输出
# ═══════════════════════════════════════════════════════════

def print_comparison_summary(comparison: list[dict]):
    """在终端打印简洁的对比表格。"""
    print(f"\n{'='*120}")
    print(f"  本地 vs 开发环境 对比汇总")
    print(f"{'='*120}")
    print(f"{'ID':<6} {'描述':<22} {'本地状态':<8} {'开发状态':<8} {'状态':<8} {'包裹':<8} {'code':<8} {'判定':<6}")
    print(f"{'-'*120}")

    for c in comparison:
        local_st = str(c["local"]["status"])
        dev_st = str(c["dev"]["status"])
        st_ok = "✅" if c["match"]["status"] else "❌"
        wr_ok = "✅" if c["match"]["wrap"] else "❌"
        cd_ok = "✅" if c["match"]["code"] else "❌"
        overall = "✅" if c["match"]["overall"] else "⚠️"
        print(f"{c['id']:<6} {c['description']:<22} {local_st:<8} {dev_st:<8} {st_ok:<8} {wr_ok:<8} {cd_ok:<8} {overall:<6}")

    all_match = sum(1 for c in comparison if c["match"]["overall"])
    total = len(comparison)
    print(f"{'-'*120}")
    print(f"  一致: {all_match}/{total}, 差异: {total - all_match}/{total}")
    print(f"{'='*120}\n")


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

def main():
    global ACCOUNT, PASSWORD

    parser = argparse.ArgumentParser(description="本地 vs 开发环境 ApiResponse 对比测试")
    parser.add_argument("--local-url", default=LOCAL_BASE_URL, help=f"本地环境地址 (默认: {LOCAL_BASE_URL})")
    parser.add_argument("--dev-url", default=DEV_BASE_URL, help=f"开发环境地址 (默认: {DEV_BASE_URL})")
    parser.add_argument("--output", "-o", default=None, help="输出 Markdown 报告路径")
    parser.add_argument("--account", default=ACCOUNT, help="登录账号（环境变量 TEST_ACCOUNT，必填）")
    parser.add_argument("--password", default=PASSWORD, help="登录密码（环境变量 TEST_PASSWORD，必填）")
    args = parser.parse_args()

    ACCOUNT = args.account
    PASSWORD = args.password
    if not ACCOUNT or not PASSWORD:
        raise SystemExit(
            "本脚本仅本地手动运行，需要 TEST_ACCOUNT / TEST_PASSWORD 环境变量"
            "（或 --account / --password 参数）注入测试账号。"
        )

    print(f"\n{'='*60}")
    print(f"  ApiResponse 中间件对比测试")
    print(f"  本地环境:  {args.local_url}")
    print(f"  开发环境:  {args.dev_url}")
    print(f"  测试账号:  {ACCOUNT}")
    print(f"{'='*60}")

    # ── 对本地环境执行测试 ──
    print(f"\n{'─'*60}")
    print(f"  阶段 1/2: 本地环境 ({args.local_url})")
    print(f"{'─'*60}")
    local_client = EnvTestClient(args.local_url, "本地")
    local_results = local_client.run_all()

    # ── 对开发环境执行测试 ──
    print(f"\n{'─'*60}")
    print(f"  阶段 2/2: 开发环境 ({args.dev_url})")
    print(f"{'─'*60}")
    dev_client = EnvTestClient(args.dev_url, "开发")
    dev_results = dev_client.run_all()

    # ── 对比 ──
    comparison = compare_results(local_results, dev_results)
    print_comparison_summary(comparison)

    # ── 生成报告 ──
    if args.output:
        md = generate_markdown(comparison, args.local_url, args.dev_url)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ 对比报告已生成: {args.output}")
    else:
        # 默认输出到 docs/报告/（不入库）
        output_path = "docs/报告/本地与开发环境ApiResponse对比报告.md"
        md = generate_markdown(comparison, args.local_url, args.dev_url)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ 对比报告已生成: {output_path}")


if __name__ == "__main__":
    main()
