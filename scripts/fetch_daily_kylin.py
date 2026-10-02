#!/usr/bin/env python3
"""逐天查询Kylin交易卡片数据并逐条写入CSV（使用TrendLine API）。
用法：python scripts/fetch_daily_kylin.py [天数] [输出csv路径]

数据来源：携程VBooking → 数据参谋 → 业务数据 → 交易卡片
API：getKylinTrendLineData (queryId=353)
指标：成交销售额、订单数、成交票量、核验票量、退订率

工作流：
  1. 登录检测 — 从 storage_state 加载 Cookie，验证 session 有效性
  2. 发现已签约类型 — 请求 Kylin 页面 HTML，解析头部下拉框得到已签约类型的 code
  3. 查询 TrendLine — 对每个已签约 code × 每种 date_type 调 Kylin TrendLine API → 逐天写入 CSV
"""

import json
import csv
import sys
import time
import os
import re
from datetime import date, timedelta

import requests

STORAGE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "docs/skills_temp/ctrip_scraper/storage_state/ctrip_auth.json"
)

# ---- 基础 API 配置 ----
API_BASE = "https://m.ctrip.com/restapi/soa2/13807"
KYLIN_TRENDLINE_URL = f"{API_BASE}/getKylinTrendLineData"
CODE_ENUM_URL = f"{API_BASE}/getVbkBuTypeMenuLinkage"
KYLIN_PAGE_URL = "https://vbooking.ctrip.com/micro/tour-bi-vendor-new/"
BATCH_DAYS = 30          # 每批查询天数（避免单次请求范围过大）
REQUEST_INTERVAL = 2.0    # 批次间间隔（秒），避免限流
MAX_RETRIES = 3           # 单批次最大重试次数
RETRY_BACKOFF = 5.0       # 重试退避基数（秒）

# ---- 日期类型（每个日期范围对每种 date_type 各查一次） ----
DATE_TYPES = ["使用日期", "预订日期"]

# ---- 指标定义（交易卡片5项指标） ----
METRICS = [
    "ttd_suc_nocoef_income",                     # 成交销售额
    "ttd_suc_ord_cnt",                           # 订单数
    "ttd_suc_nocoef_qty",                        # 成交票量
    "ttd_chk_qty",                               # 核验票量
    "ttd_arfd_ord_cnt|ttd_pay_ord_cnt|/",        # 退订率（拒单/支付）
]

# ---- 签约类型名称 → code 映射（页面 HTML 给出中文名，需映射为 code） ----
SIGNING_NAME_TO_CODE: dict[str, int] = {
    "跟团游": 1,
    "私家团": 6,
    "景点玩乐": 7,
}

# ---- CSV 列名 → 中文含义映射 ----
FIELD_LABELS = {
    "code": "签约类型编码",
    "signing_type": "签约类型",
    "vbk_bu_type": "业务类型",
    "date_type": "日期类型",
    "date_range": "日期范围",
    "date_start": "开始日期",
    "date_end": "结束日期",
    "ttd_suc_nocoef_income": "成交销售额",
    "ttd_suc_nocoef_income_MOMRate": "成交销售额环比",
    "ttd_suc_ord_cnt": "订单数",
    "ttd_suc_ord_cnt_MOMRate": "订单数环比",
    "ttd_suc_nocoef_qty": "成交票量",
    "ttd_suc_nocoef_qty_MOMRate": "成交票量环比",
    "ttd_chk_qty": "核验票量",
    "ttd_chk_qty_MOMRate": "核验票量环比",
    "ttd_arfd_ord_cnt|ttd_pay_ord_cnt|/": "退订率",
    "ttd_arfd_ord_cnt|ttd_pay_ord_cnt|/_MOMRate": "退订率环比",
}


def build_session(cookie_file: str) -> requests.Session:
    """从 Playwright storage_state JSON 加载 Cookie 到 requests.Session"""
    with open(cookie_file) as f:
        data = json.load(f)
    session = requests.Session()
    for c in data.get("cookies", []):
        session.cookies.set(
            name=c["name"], value=c["value"],
            domain=c.get("domain", "").lstrip("."), path=c.get("path", "/"),
        )
    session.headers.update({
        "Content-Type": "application/json",
        "appname": "vbkbusiness",
    })
    return session


# =============================================================================
# Step 1: 登录检测
# =============================================================================

def check_session(session: requests.Session) -> bool:
    """验证 session 是否有效：请求 Kylin 页面 HTML，检查返回内容是否为有效页面。

    返回 True 表示 session 有效，False 表示需要重新登录。
    """
    print("正在验证 session 是否有效...")
    try:
        # 请求 Kylin 页面（不带 Content-Type: application/json）
        ct = session.headers.pop("Content-Type", None)
        resp = session.get(KYLIN_PAGE_URL, timeout=15, allow_redirects=True)
        if ct is not None:
            session.headers["Content-Type"] = ct

        # 检查是否被重定向到登录页
        if resp.status_code != 200:
            print(f"  ❌ 页面返回状态码 {resp.status_code}，session 可能已过期")
            return False
        if "login" in resp.url.lower() or "sign_in" in resp.url.lower():
            print(f"  ❌ 被重定向到登录页: {resp.url}")
            return False

        html = resp.text
        if len(html) < 5000:
            print(f"  ❌ 页面内容异常（仅 {len(html)} 字节），session 可能已过期")
            return False

        print("  ✓ session 有效")
        return True

    except requests.exceptions.RequestException as e:
        print(f"  ❌ 请求页面失败: {e}")
        return False


# =============================================================================
# Step 2: 发现已签约类型
# =============================================================================

def discover_signed_signings(session: requests.Session) -> list[dict]:
    """请求 Kylin 页面 HTML，解析头部下拉框得到已签约类型的 (code, signing_type, vbk_bu_type) 列表。

    工作方式：
      1. GET Kylin 页面 HTML（服务端渲染，包含签约状态下拉框）
      2. 正则提取每个签约类型名 + 签约状态（"已签约" / "未签约"）
      3. 通过 SIGNING_NAME_TO_CODE 映射为 code，只保留"已签约"的
      4. 对每个已签约 code 调用 getVbkBuTypeMenuLinkage 获取 vbk_bu_type

    返回：[{"code": int, "signing_type": str, "vbk_bu_type": str}, ...]
    """
    print("正在请求 Kylin 页面，解析签约状态下拉框...")
    try:
        ct = session.headers.pop("Content-Type", None)
        resp = session.get(KYLIN_PAGE_URL, timeout=15, allow_redirects=True)
        if ct is not None:
            session.headers["Content-Type"] = ct
        resp.raise_for_status()
        html = resp.text
    except requests.exceptions.RequestException as e:
        print(f"  ❌ 请求页面失败: {e}")
        return []

    # 正则匹配下拉框中的签约类型名 + 签约状态
    # HTML 结构：<span>类型名</span><span class="...gpznbmm..."><span class="...gpznbmn...">状态</span>
    pattern = (
        r'<span>([^<]+)</span>'
        r'<span[^>]*class="[^"]*gpznbmm[^"]*"[^>]*>'
        r'<span[^>]*class="[^"]*gpznbmn[^"]*"[^>]*>'
        r'([^<]+)'
        r'</span>'
    )
    matches = re.findall(pattern, html)

    if not matches:
        print("  ❌ 未从页面解析到签约类型信息")
        return []

    # 收集已签约且能映射到 code 的类型
    signed_items: list[tuple[int, str]] = []  # [(code, type_name), ...]
    for type_name, status in matches:
        code = SIGNING_NAME_TO_CODE.get(type_name)
        is_signed = "已签约" in status
        tag = "✓ 已签约" if is_signed else "✗ 未签约"
        if code:
            print(f"  {type_name} → {tag} (code={code})")
            if is_signed:
                signed_items.append((code, type_name))
        else:
            print(f"  {type_name} → {tag} (⚠️ 未知 code，请在 SIGNING_NAME_TO_CODE 中添加映射)")

    if not signed_items:
        print("  ❌ 未发现任何已签约类型")
        return []

    # 对每个已签约 code 调 getVbkBuTypeMenuLinkage 获取 vbk_bu_type
    print(f"\n  正在查询已签约类型的业务子类型...")
    result: list[dict] = []
    for code, type_name in signed_items:
        try:
            resp = session.post(CODE_ENUM_URL, json={"code": code, "isOffline": False}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            bu_list = data.get("innerVbkBuTypeBaseInfoList", [])

            for bu_item in bu_list:
                vbk_bu_type = bu_item.get("vbkBuType", "未知")
                result.append({
                    "code": code,
                    "signing_type": type_name,
                    "vbk_bu_type": vbk_bu_type,
                })
                print(f"    code={code} ({type_name}) → vbkBuType={vbk_bu_type}")

            if not bu_list:
                # 无子类型，仍然记录（vbk_bu_type 填未知）
                result.append({
                    "code": code,
                    "signing_type": type_name,
                    "vbk_bu_type": "未知",
                })
                print(f"    code={code} ({type_name}) → 无子类型")

        except requests.exceptions.RequestException as e:
            print(f"    code={code} 查询子类型失败: {e}，跳过")
            continue

    return result


# =============================================================================
# Step 3: 查询 Kylin TrendLine 数据
# =============================================================================

def fetch_range(
    session: requests.Session,
    start: date,
    end: date,
    date_type: str = "使用日期",
    code: int = 7,
    signing_type: str = "未知",
    vbk_bu_type: str = "未知",
) -> list[dict]:
    """查询日期范围内的每日数据，返回按日期排序的行列表。

    使用 getKylinTrendLineData 而非 getKylinDataWithSublineV2，
    因为后者在单日查询时大部分返回0，TrendLine 能正确返回每日粒度数据。
    """
    body = {
        "queryId": [353],
        "needAggregate": False,
        "groupTagList": [],
        "calculator": "sum",
        "startdate": start.isoformat(),
        "enddate": end.isoformat(),
        "tagMap": {"date_type": date_type, "d": date.today().isoformat()},
        "metricList": [
            {"metric": m, "sublineTypeList": ["MOMRate"]} for m in METRICS
        ],
        "calculate": {"unit": "d", "value": 1},
    }

    resp = session.post(KYLIN_TRENDLINE_URL, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("ResponseStatus", {}).get("Ack") != "Success":
        return []

    result_list = data.get("resultList", [])
    if not result_list:
        return []

    # 收集所有日期（指标值 + MOMRate 中的日期取并集）
    all_dates = set()
    for item in result_list:
        for dvm in item.get("dateValueMapList", []):
            all_dates.update(dvm.get("valueDateMap", {}).keys())
        sub = item.get("sublineDataMap", {}).get("MOMRate", {})
        for dl in sub.get("dataList", []):
            all_dates.update(dl.get("valueDateMap", {}).keys())

    # 按日期构建行数据
    rows = []
    for d_str in sorted(all_dates):
        row = {
            "code": code,
            "signing_type": signing_type,
            "vbk_bu_type": vbk_bu_type,
            "date_type": date_type,
            "date_range": "单日",
            "date_start": d_str,
            "date_end": d_str,
        }
        for item in result_list:
            metric = item["metric"]
            # 主指标值
            for dvm in item.get("dateValueMapList", []):
                vdm = dvm.get("valueDateMap", {})
                if d_str in vdm:
                    row[metric] = vdm[d_str]
            # MOMRate（环比）
            sub = item.get("sublineDataMap", {}).get("MOMRate", {})
            for dl in sub.get("dataList", []):
                vdm = dl.get("valueDateMap", {})
                if d_str in vdm:
                    row[metric + "_MOMRate"] = vdm[d_str]
            # 补默认值
            if metric not in row:
                row[metric] = 0
            if metric + "_MOMRate" not in row:
                row[metric + "_MOMRate"] = None
        rows.append(row)
    return rows


def fetch_range_with_retry(session, start, end, date_type, code, signing_type, vbk_bu_type,
                           max_retries=MAX_RETRIES, backoff=RETRY_BACKOFF):
    """带重试的日期范围查询，指数退避避免限流"""
    for attempt in range(max_retries):
        try:
            return fetch_range(session, start, end, date_type, code, signing_type, vbk_bu_type)
        except requests.exceptions.RequestException as e:
            wait = backoff * (2 ** attempt)
            print(f"  请求失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"  等待 {wait:.0f}s 后重试...")
                time.sleep(wait)
            else:
                raise


# =============================================================================
# main
# =============================================================================

def main():
    # 解析参数：--cookie / [天数] / [输出路径]
    cookie_file = STORAGE_FILE
    args_to_parse = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--cookie" and i + 1 < len(sys.argv):
            cookie_file = sys.argv[i + 1]
            i += 2
        else:
            args_to_parse.append(sys.argv[i])
            i += 1

    days = int(args_to_parse[0]) if len(args_to_parse) > 0 else 30
    output = args_to_parse[1] if len(args_to_parse) > 1 else None
    if output is None:
        output = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "docs/skills_temp/ctrip_scraper/output/daily_kylin_30days.csv",
        )

    # ---- Step 1: 登录检测 ----
    print("=" * 60)
    print("Step 1: 登录检测")
    print("=" * 60)

    if not os.path.exists(cookie_file):
        print(f"❌ Cookie 文件不存在: {cookie_file}")
        print("请先执行 python scripts/scraper.py --login 登录")
        sys.exit(1)

    session = build_session(cookie_file)
    if not check_session(session):
        print("\n❌ Session 已过期，请重新登录：")
        print("  python scripts/scraper.py --login")
        sys.exit(1)

    # ---- Step 2: 发现已签约类型 ----
    print(f"\n{'=' * 60}")
    print("Step 2: 发现已签约类型")
    print("=" * 60)

    signings = discover_signed_signings(session)
    if not signings:
        print("\n❌ 未发现任何已签约类型，退出")
        sys.exit(1)

    print(f"\n✅ 发现 {len(signings)} 个已签约类型组合: "
          f"{[(s['code'], s['signing_type'], s['vbk_bu_type']) for s in signings]}")

    # ---- Step 3: 查询 Kylin TrendLine 数据 ----
    end_date = date.today() - timedelta(days=1)  # 昨天（不含今天）

    # 字段顺序（CSV 列）
    fieldnames = [
        "code", "signing_type", "vbk_bu_type",
        "date_type", "date_range", "date_start", "date_end",
    ]
    for m in METRICS:
        fieldnames.append(m)
        fieldnames.append(m + "_MOMRate")

    # 写入 CSV 双行表头：第一行含义，第二行字段名
    need_header = not os.path.exists(output) or os.path.getsize(output) == 0
    if need_header:
        label_row = [FIELD_LABELS.get(fn, fn) for fn in fieldnames]
        with open(output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(label_row)       # 第一行：中文含义
            writer.writerow(fieldnames)      # 第二行：字段名

    total_written = 0

    for si, s in enumerate(signings):
        print(f"\n{'=' * 60}")
        print(f"Step 3.{si+1}: 查询 TrendLine（code={s['code']}, "
              f"signing={s['signing_type']}, vbkBuType={s['vbk_bu_type']}）")
        print(f"{'=' * 60}")

        remaining = days
        cursor = end_date

        while remaining > 0:
            batch_size = min(BATCH_DAYS, remaining)
            batch_start = cursor - timedelta(days=batch_size - 1)

            for dt in DATE_TYPES:
                print(f"  [批次] {batch_start} ~ {cursor} date_type={dt} ({batch_size}天)...", end=" ")

                try:
                    rows = fetch_range_with_retry(
                        session, batch_start, cursor, dt,
                        s["code"], s["signing_type"], s["vbk_bu_type"],
                    )
                    print(f"返回 {len(rows)} 天")
                    # 逐天立即写入 CSV
                    with open(output, "a", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        for row in rows:
                            writer.writerow(row)
                            total_written += 1
                            income = row.get("ttd_suc_nocoef_income", "?")
                            print(f"    [{row['date_type']}] {row['date_start']}  income={income}")
                except Exception as e:
                    print(f"ERROR（跳过本批次）: {e}")

                time.sleep(1.0)  # date_type 间短暂间隔

            remaining -= batch_size
            cursor = batch_start - timedelta(days=1)
            if remaining > 0:
                time.sleep(REQUEST_INTERVAL)

    print(f"\n{'=' * 60}")
    print(f"完成！{len(signings)} 个签约类型 × {days} 天 × {len(DATE_TYPES)} 种日期类型")
    print(f"共写入 {total_written} 行数据 → {output}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
