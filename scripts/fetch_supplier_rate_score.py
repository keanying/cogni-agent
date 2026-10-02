#!/usr/bin/env python3
"""查询供应商评级雷达图数据（服务分+5项子指标），按目的地×单月导出CSV。

用法：python scripts/fetch_supplier_rate_score.py [月份] [输出路径]

数据来源：携程VBooking → 数据参谋 → 供应商评级页面
API：getVendorDstList（目的地列表） + getGetVendorClassifyScoreMetric（评分明细）

工作流：
  1. 登录检测 — 验证 Cookie 有效性
  2. 获取目的地列表 — 调用 getVendorDstList，不同账号可能返回不同目的地
  3. 获取评分数据 — 调用 getGetVendorClassifyScoreMetric
  4. 输出 — CSV 文件

CSV 列：月份、目的地、服务分、订单确认率、履约缺陷率、客服人工介入率、20秒回复及时率、IM差评率

示例：
  python scripts/fetch_supplier_rate_score.py                # 默认：上个月
  python scripts/fetch_supplier_rate_score.py 2026-07        # 指定月份
  python scripts/fetch_supplier_rate_score.py 2026-07 /tmp/out.csv
"""

import csv
import json
import os
import sys
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
DST_LIST_URL = f"{API_BASE}/getVendorDstList"
SCORE_METRIC_URL = f"{API_BASE}/getGetVendorClassifyScoreMetric"
AUTH_PAGE_URL = "https://vbooking.ctrip.com/micro/tour-bi-vendor-new/"

REQUEST_INTERVAL = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF = 5.0

# 5 项雷达图子指标 → CSV 列名映射
RADAR_METRICS = [
    ("orderConfirmationRate", "订单确认率"),
    ("fulfillmentDefectRate", "履约缺陷率"),
    ("customerServiceManualInterventionRate", "客服人工介入率"),
    ("20secondsResponseTimelinessRate", "20秒回复及时率"),
    ("IMNegativeReviewRate", "IM差评率"),
]

CSV_COLUMNS = ["月份", "目的地", "服务分"] + [label for _, label in RADAR_METRICS]


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
        resp = session.get(AUTH_PAGE_URL, timeout=15, allow_redirects=True)
        if ct is not None:
            session.headers["Content-Type"] = ct
        resp.raise_for_status()
        valid = "tour-bi-vendor" in resp.text or "已签约" in resp.text
        print(f"  {'✓ session 有效' if valid else '✗ 页面内容异常'}\n")
        return valid
    except requests.exceptions.RequestException as e:
        print(f"  ✗ 请求失败: {e}\n")
        return False


def fetch_destination_list(session: requests.Session) -> list[dict]:
    """获取当前账号下的目的地列表。返回 [{regionId, regionName, regionType}, ...]"""
    print("正在获取目的地列表...")
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(DST_LIST_URL, json={}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ResponseStatus", {}).get("Ack") == "Success":
                dst_list = data.get("dstList", [])
                print(f"  ✓ 获取到 {len(dst_list)} 个目的地")
                for d in dst_list:
                    print(f"    {d.get('regionId')} - {d.get('regionName')}")
                print()
                return dst_list
            print(f"  ✗ 接口返回异常: {data.get('ResponseStatus', {})}")
            return []
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"  ⚠ 重试 {attempt+1}/{MAX_RETRIES}: {e}，{wait:.0f}s 后...")
                time.sleep(wait)
    print(f"  ✗ 获取目的地列表失败\n")
    return []


def fetch_score_metric(session: requests.Session, dst_info: dict, month: str) -> dict | None:
    """查询指定目的地+月份的评分指标。返回解析后的数据字典，失败返回 None。"""
    body = {"dstInfo": dst_info, "month": month}
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(SCORE_METRIC_URL, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ResponseStatus", {}).get("Ack") != "Success":
                return None

            # 解析服务分
            service_card = data.get("serviceScoreMetricCardInfo", {})
            service_values = service_card.get("values", {})
            service_score = service_values.get("score", "")

            # 解析 5 项雷达图子指标
            atomic = service_card.get("calculationFormula", {}).get("atomicMetrics", [])
            metric_map = {m.get("metricName"): m.get("score", "") for m in atomic}

            return {
                "serviceScore": service_score,
                "orderConfirmationRate": metric_map.get("orderConfirmationRate", ""),
                "fulfillmentDefectRate": metric_map.get("fulfillmentDefectRate", ""),
                "customerServiceManualInterventionRate": metric_map.get("customerServiceManualInterventionRate", ""),
                "20secondsResponseTimelinessRate": metric_map.get("20secondsResponseTimelinessRate", ""),
                "IMNegativeReviewRate": metric_map.get("IMNegativeReviewRate", ""),
            }
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF * (2 ** attempt)
                print(f"      ⚠ 重试 {attempt+1}/{MAX_RETRIES}: {e}，{wait:.0f}s 后...")
                time.sleep(wait)
    return None


def main():
    # 解析参数：--cookie / 月份 YYYY-MM（可选） / 输出路径（可选）
    cookie_file = STORAGE_FILE
    month_str = None
    output_path = None
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--cookie" and i + 1 < len(sys.argv):
            cookie_file = sys.argv[i + 1]
            i += 2
        elif len(sys.argv[i]) == 7 and sys.argv[i][4] == "-" \
                and sys.argv[i][:4].isdigit() and sys.argv[i][5:].isdigit():
            month_str = sys.argv[i]
            i += 1
        elif sys.argv[i].startswith("/") or sys.argv[i].endswith(".csv"):
            output_path = sys.argv[i]
            i += 1
        else:
            i += 1

    today = date.today()
    if month_str:
        month = month_str
    else:
        y, m = today.year, today.month - 1
        if m <= 0:
            m = 12
            y -= 1
        month = f"{y}-{m:02d}"

    if output_path is None:
        output_path = os.path.join(OUTPUT_DIR, "supplier_rate_score.csv")

    print(f"供应商评级雷达图数据采集")
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

    # Step 2: 获取目的地列表
    print("=" * 60)
    print("Step 2: 获取目的地列表")
    print("=" * 60)
    dst_list = fetch_destination_list(session)
    if not dst_list:
        print("❌ 未获取到目的地列表")
        sys.exit(1)

    # Step 3: 逐目的地查询
    print("=" * 60)
    print(f"Step 3: {month} 逐目的地获取评分数据")
    print("=" * 60)

    rows = []

    total = len(dst_list)
    current = 0

    for dst in dst_list:
        region_id = dst.get("regionId", "")
        region_name = dst.get("regionName", "未知")
        dst_info = {
            "regionType": str(dst.get("regionType", "2")),
            "regionId": str(region_id),
            "regionName": region_name,
        }

        current += 1
        print(f"  [{current}/{total}] {month} {region_name} ({region_id}) ... ", end="", flush=True)

        metric = fetch_score_metric(session, dst_info, month)
        if metric:
            row = [
                month,
                region_name,
                metric["serviceScore"],
                metric["orderConfirmationRate"],
                metric["fulfillmentDefectRate"],
                metric["customerServiceManualInterventionRate"],
                metric["20secondsResponseTimelinessRate"],
                metric["IMNegativeReviewRate"],
            ]
            rows.append(row)
            print("✓")
        else:
            print("✗ 无数据")

        time.sleep(REQUEST_INTERVAL)

    # Step 4: 输出 CSV
    print(f"\n{'=' * 60}")
    print("Step 4: 输出 CSV")
    print("=" * 60)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)

    print(f"\n✅ 完成！共 {len(rows)} 条记录")
    print(f"   输出文件: {output_path}")


if __name__ == "__main__":
    main()
