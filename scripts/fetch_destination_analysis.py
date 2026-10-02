#!/usr/bin/env python3
"""查询目的地评级分析数据并导出原始分析文本。
用法：python scripts/fetch_destination_analysis.py [月份] [输出路径]

数据来源：携程VBooking → 数据参谋 → 目的地评级分析
API：getVendorClassifyAIAnalysisData

工作流：
  1. 登录检测 — 验证 Cookie 有效性
  2. 查询 — 调 getVendorClassifyAIAnalysisData，取 message 原始文本
  3. 输出 — JSON 文件

说明：该API仅对上一个月有数据。默认自动查上个月，也可手动指定月份。
      如：python scripts/fetch_destination_analysis.py 2026-06
"""

import json
import sys
import os
import time
from datetime import date

import requests

STORAGE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "docs/skills_temp/ctrip_scraper/storage_state/ctrip_auth.json"
)

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "docs/skills_temp/ctrip_scraper/output"
)

API_BASE = "https://m.ctrip.com/restapi/soa2/13807"
ANALYSIS_URL = f"{API_BASE}/getVendorClassifyAIAnalysisData"
KYLIN_PAGE_URL = "https://vbooking.ctrip.com/micro/tour-bi-vendor-new/"
MAX_RETRIES = 3
RETRY_BACKOFF = 5.0


def build_session(cookie_file: str) -> requests.Session:
    if not os.path.exists(cookie_file):
        raise FileNotFoundError(
            f"Cookie 文件不存在: {cookie_file}\n"
            f"请先执行 scraper.py --login 登录获取 Cookie"
        )
    with open(cookie_file) as f:
        data = json.load(f)
    session = requests.Session()
    for c in data.get("cookies", []):
        session.cookies.set(
            name=c["name"], value=c["value"],
            domain="." + c.get("domain", "").lstrip(".").replace("vbooking.ctrip.com", "ctrip.com"),
            path=c.get("path", "/"),
        )
    session.headers.update({
        "Content-Type": "application/json;charset=UTF-8",
        "appname": "vbkbusiness",
        "Origin": "https://vbooking.ctrip.com",
        "Referer": "https://vbooking.ctrip.com/",
    })
    return session


def check_session(session: requests.Session) -> bool:
    print("正在验证 session 是否有效...")
    try:
        ct = session.headers.pop("Content-Type", None)
        resp = session.get(KYLIN_PAGE_URL, timeout=15, allow_redirects=True)
        if ct is not None:
            session.headers["Content-Type"] = ct
        resp.raise_for_status()
        valid = "tour-bi-vendor" in resp.text or "已签约" in resp.text
        print(f"  {'✓ session 有效' if valid else '✗ 页面内容异常'}\n")
        return valid
    except requests.exceptions.RequestException as e:
        print(f"  ✗ 请求失败: {e}\n")
        return False


def fetch_analysis(session: requests.Session, month: str) -> str:
    """查询指定月份，返回原始 message 文本。失败或无数据返回空字符串。"""
    body = {"dstInfo": {"regionType": "0"}, "month": month}
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(ANALYSIS_URL, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ResponseStatus", {}).get("Ack") == "Success":
                return data.get("message", "")
            return ""
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"    ⚠ 重试 {attempt+1}/{MAX_RETRIES}: {e}，{wait:.0f}s 后...")
                time.sleep(wait)
    print(f"    ✗ 请求失败: {last_error}")
    return ""


def main():
    # 解析参数：--cookie / [月份] / [输出路径]
    cookie_file = STORAGE_FILE
    positional = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--cookie" and i + 1 < len(sys.argv):
            cookie_file = sys.argv[i + 1]
            i += 2
        else:
            positional.append(sys.argv[i])
            i += 1

    # 月份：默认上个月（API 只对上一个月有数据），可手动指定
    if len(positional) > 0 and not positional[0].startswith("/"):
        month = positional[0]
    else:
        today = date.today()
        y, m = today.year, today.month - 1
        if m <= 0:
            m += 12
            y -= 1
        month = f"{y}-{m:02d}"

    output_path = positional[-1] if len(positional) > 0 and positional[-1].startswith("/") else os.path.join(
        OUTPUT_DIR, "destination_analysis.json"
    )

    print(f"目的地评级分析采集")
    print(f"  查询月份: {month}")
    print(f"  输出路径: {output_path}\n")

    # Step 1: 登录检测
    print("=" * 60)
    print("Step 1: 登录检测")
    print("=" * 60)
    session = build_session(cookie_file)
    if not check_session(session):
        print("❌ Session 无效，请重新登录")
        sys.exit(1)

    # Step 2: 查询
    print("=" * 60)
    print("Step 2: 查询 AI 分析")
    print("=" * 60)

    print(f"\n查询 {month} ... ", end="", flush=True)
    message = fetch_analysis(session, month)
    has_data = bool(message.strip())
    status = f"✓ ({len(message)} 字符)" if has_data else "✗ 无数据"
    print(status)

    if has_data:
        print(f"    {message}")

    # Step 3: 输出
    print(f"\n{'=' * 60}")
    print("Step 3: 输出结果")
    print("=" * 60)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    output = {
        "source": "getVendorClassifyAIAnalysisData",
        "generated_at": date.today().isoformat(),
        "month": month,
        "message": message,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if has_data:
        print(f"\n✅ 完成！已获取 {month} 的分析数据")
    else:
        print(f"\n⚠️ {month} 无数据（该 API 仅最新月份有数据）")
    print(f"   输出文件: {output_path}")


if __name__ == "__main__":
    main()
