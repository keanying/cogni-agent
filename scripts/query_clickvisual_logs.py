#!/usr/bin/env python3
"""
ClickVisual 日志查询工具 - cogni_agent 项目

用法：
  # 方式1：直接粘贴浏览器复制的 curl 命令（最可靠，自动识别并执行）
  python scripts/query_clickvisual_logs.py --curl 'curl ... -b clickvisual_session=xxx ... --insecure'

  # 方式2：提供 session cookie，查询最近 N 小时（默认 3 小时）
  python scripts/query_clickvisual_logs.py --session "MTc4NTgz..." --hours 3

  # 方式3：指定时间范围（Unix 秒级时间戳）
  python scripts/query_clickvisual_logs.py --session "xxx" --start 1785907270 --end 1785918070

  # JSON 输出（含完整堆栈，不截断）
  python scripts/query_clickvisual_logs.py --curl '...' --output json

【重要】查询只允许时间参数（--hours / --start / --end）。
已移除一切内容/容器名过滤参数（--keyword / --extra-filter / --error-only
/ --no-container-filter）：过滤语法易写错导致静默返回 0 条，误导排查。
容器名固定为 cogni-agent，请勿自行拼接 ClickHouse 过滤条件。

API 说明：
  GET /api/v1/tables/{tid}/logs?st={start}&et={end}&query={filter}&pageSize={n}&page={p}&isQueryCount=0
  响应: { code: 0, data: { logs: [...], count: N, limited: N, keys: [...] } }
"""

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs, unquote, quote

# ============ 配置 ============
CLICKVISUAL_BASE = "http://cv.12301.test"
TABLE_ID = 25          # cogni-agent 日志表 ID
CONTAINER_NAME = 'cogni-agent'   # 固定容器名过滤（只查本项目容器，不可改）
DEFAULT_HOURS = 3      # 默认查询最近 N 小时
DEFAULT_PAGE_SIZE = 50


# ============ curl 解析 ============

def parse_curl_command(curl_str: str) -> Dict[str, Any]:
    """解析 curl 命令，提取 URL、headers、method、cookies 等"""
    curl_str = curl_str.strip()
    if curl_str.startswith('curl '):
        curl_str = curl_str[5:]

    try:
        args = shlex.split(curl_str)
    except ValueError:
        args = curl_str.split()

    result: Dict[str, Any] = {
        'url': '',
        'method': 'GET',
        'headers': {},
        'cookies': {},
        'data': None,
        'verify': True,
    }

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('-X', '--request'):
            i += 1
            if i < len(args):
                result['method'] = args[i].upper()
        elif arg in ('-H', '--header'):
            i += 1
            if i < len(args) and ':' in args[i]:
                key, value = args[i].split(':', 1)
                result['headers'][key.strip()] = value.strip()
        elif arg in ('-b', '--cookie'):
            i += 1
            if i < len(args):
                for pair in args[i].split(';'):
                    pair = pair.strip()
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        result['cookies'][k.strip()] = v.strip()
        elif arg in ('-d', '--data', '--data-raw'):
            i += 1
            if i < len(args):
                result['data'] = args[i]
                if result['method'] == 'GET':
                    result['method'] = 'POST'
        elif arg in ('-k', '--insecure'):
            result['verify'] = False
        elif not arg.startswith('-'):
            result['url'] = arg
        i += 1

    return result


def extract_session(curl_str: str) -> Optional[str]:
    return parse_curl_command(curl_str).get('cookies', {}).get('clickvisual_session')


def extract_url_params(url: str) -> Dict[str, str]:
    """从 URL 中提取查询参数"""
    parsed = urlparse(url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


# ============ curl 执行（通过子进程） ============

def execute_raw_curl(curl_str: str) -> Dict[str, Any]:
    """通过 curl 子进程执行请求并返回 JSON 结果"""
    parsed = parse_curl_command(curl_str)
    cmd_parts = ['curl', '-s']
    if not parsed['verify']:
        cmd_parts.append('--insecure')
    cmd_parts.append(parsed['url'])
    if parsed['method'] != 'GET':
        cmd_parts.extend(['-X', parsed['method']])
    for k, v in parsed['headers'].items():
        cmd_parts.extend(['-H', f'{k}: {v}'])
    if parsed['cookies']:
        cookie_str = '; '.join(f'{k}={v}' for k, v in parsed['cookies'].items())
        cmd_parts.extend(['-b', cookie_str])
    if parsed['data']:
        cmd_parts.extend(['-d', parsed['data']])

    try:
        result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"❌ curl 执行失败 (exit={result.returncode}): {result.stderr}", file=sys.stderr)
            sys.exit(1)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("❌ 请求超时", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ 返回不是有效 JSON: {e}", file=sys.stderr)
        print(f"原始返回（前 1000 字符）:\n{result.stdout[:1000]}", file=sys.stderr)
        sys.exit(1)


# ============ API 查询（Python requests） ============

def _build_query_filter() -> str:
    """构建 ClickVisual query 过滤条件（仅固定容器名过滤，不做内容过滤）"""
    return f"`_container_name_` like '%{CONTAINER_NAME}%'"


def query_logs(session_cookie: str, tid: int = TABLE_ID,
               start_ts: int = 0, end_ts: int = 0,
               query_filter: str = '', page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
               is_query_count: int = 0) -> Dict[str, Any]:
    """通过 ClickVisual API 查询日志"""
    import requests as req
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = f"{CLICKVISUAL_BASE}/api/v1/tables/{tid}/logs"
    params = {
        'st': start_ts,
        'et': end_ts,
        'query': query_filter,
        'pageSize': page_size,
        'page': page,
        'isQueryCount': is_query_count,
    }

    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Referer': f'{CLICKVISUAL_BASE}/query',
    }

    resp = req.get(url, params=params, headers=headers,
                   cookies={'clickvisual_session': session_cookie},
                   verify=False, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ============ 日志格式化输出 ============

def _extract_time(item: Dict[str, Any]) -> str:
    """从日志条目提取可读时间"""
    # 优先用 _time_second_（Unix 秒级）
    ts = item.get('_time_second_')
    if ts:
        try:
            return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError):
            pass

    # 其次 _time_（可能是字符串时间戳）
    ts = item.get('_time_')
    if ts:
        try:
            return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError):
            pass

    # 最后 _receive_time_（ISO 格式字符串）
    rt = item.get('_receive_time_', '')
    if rt:
        # 格式如 "2026-08-05T16:18:02+08:00" 或 "2026-08-05T16:18:02+"
        return rt[:19] if len(rt) >= 19 else rt

    return '-'


def _extract_content(item: Dict[str, Any]) -> str:
    """提取日志正文"""
    content = item.get('_content_', '')
    if content and isinstance(content, str):
        return content.strip()
    # 兼容其他可能的字段名
    for key in ['_log_', 'log', 'message', 'content']:
        val = item.get(key, '')
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ''


def _extract_meta(item: Dict[str, Any]) -> str:
    """提取关键元数据"""
    parts = []
    for key in ['_pod_name_', '_image_name_', '_namespace_', '_node_ip_']:
        val = item.get(key, '')
        if val:
            parts.append(f"{key.strip('_')}={val}")
    return ' '.join(parts)


def _colorize(content: str) -> str:
    """根据日志级别着色"""
    upper = content.upper()
    if any(kw in upper for kw in ('ERROR', 'FATAL', 'CRITICAL', 'EXCEPTION', 'TRACEBACK')):
        return f"\033[31m{content}\033[0m"   # red
    if 'WARN' in upper:
        return f"\033[33m{content}\033[0m"   # yellow
    return content


def _format_single(item: Dict[str, Any]) -> str:
    """格式化单条日志"""
    time_str = _extract_time(item)
    meta_str = _extract_meta(item)
    content = _extract_content(item)

    header = f"[{time_str}]"
    if meta_str:
        header += f" {meta_str}"

    if content:
        return f"{header}\n  {_colorize(content)}"
    else:
        return f"{header}\n  {json.dumps(item, ensure_ascii=False)}"


def format_output(data: Dict[str, Any], fmt: str = 'text') -> str:
    """格式化 API 响应"""
    if fmt == 'json':
        return json.dumps(data, ensure_ascii=False, indent=2)

    code = data.get('code', -1)
    msg = data.get('msg', '')
    if code is not None and code != 0:
        return f"❌ API 返回错误 (code={code}): {msg}"

    result = data.get('data', data)

    # ClickVisual /api/v1/tables/{tid}/logs 响应格式
    if isinstance(result, dict) and 'logs' in result:
        log_list = result.get('logs', [])
        total = result.get('count', len(log_list))
        limited = result.get('limited', len(log_list))

        lines = [
            "=" * 90,
            f"📊 总匹配 {total} 条，当前返回 {limited} 条",
            "=" * 90,
        ]

        if not log_list:
            lines.append("(无日志)")
        else:
            for item in log_list:
                if isinstance(item, dict):
                    lines.append(_format_single(item))
                    lines.append("-" * 70)
                else:
                    lines.append(str(item))

        return '\n'.join(lines)

    # 兼容：直接是列表
    if isinstance(result, list):
        lines = ["=" * 90, f"📊 共 {len(result)} 条日志", "=" * 90]
        for item in result:
            if isinstance(item, dict):
                lines.append(_format_single(item))
                lines.append("-" * 70)
            else:
                lines.append(str(item))
        return '\n'.join(lines)

    # 兜底：原样 JSON 输出
    return json.dumps(result, ensure_ascii=False, indent=2)


# ============ 主入口 ============

def main():
    parser = argparse.ArgumentParser(
        description='ClickVisual 日志查询工具 - cogni_agent 项目',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 直接粘贴浏览器复制的 curl
  python scripts/query_clickvisual_logs.py --curl 'curl ... -b clickvisual_session=xxx ... --insecure'

  # 查最近 1 小时的日志
  python scripts/query_clickvisual_logs.py --session "MTc4NTgz..." --hours 1

  # 指定精确时间范围（Unix 秒级时间戳）
  python scripts/query_clickvisual_logs.py --session "xxx" --start 1785907270 --end 1785918070

  # JSON 输出
  python scripts/query_clickvisual_logs.py --curl '...' --output json | python -m json.tool

注意：只允许时间参数（--hours/--start/--end），无任何内容过滤参数。
        """
    )

    # 输入来源
    inp = parser.add_argument_group('输入来源（至少提供一种）')
    inp.add_argument('--curl', type=str, help='浏览器 Network 面板复制的完整 curl 命令')
    inp.add_argument('--session', type=str, help='clickvisual_session cookie 值')

    # 时间范围（--hours 是快捷方式，--start/--end 精确控制）
    time_g = parser.add_argument_group('时间范围')
    time_g.add_argument('--hours', type=float, default=DEFAULT_HOURS,
                        help=f'查询最近 N 小时（默认 {DEFAULT_HOURS}）')
    time_g.add_argument('--start', type=int, help='起始时间戳（Unix 秒）')
    time_g.add_argument('--end', type=int, help='结束时间戳（Unix 秒）')

    # 输出
    out = parser.add_argument_group('输出')
    out.add_argument('--output', choices=['text', 'json'], default='text',
                     help='输出格式（默认 text，带颜色）')
    out.add_argument('--save', type=str, help='原始 JSON 响应保存路径')

    args = parser.parse_args()

    # ---- 确定 session ----
    session_cookie = args.session
    data = None

    if args.curl:
        parsed = parse_curl_command(args.curl)
        url = parsed.get('url', '')
        if not session_cookie:
            session_cookie = parsed.get('cookies', {}).get('clickvisual_session')

        # 如果是 /api/ 路径
        if '/api/' in url:
            # 检查用户是否显式传了覆盖参数（仅时间类：--hours/--start/--end）
            has_explicit_overrides = any([
                '--hours' in sys.argv,
                '--start' in sys.argv,
                '--end' in sys.argv,
            ])

            if has_explicit_overrides:
                # 有覆盖参数：从 curl 提取 session，走 Python requests 路径。
                # URL 中的 st/et 仅当用户未指定 --hours/--start/--end 时才作为默认值。
                url_params = extract_url_params(url)
                _user_set_time = ('--hours' in sys.argv or '--start' in sys.argv or '--end' in sys.argv)
                if not _user_set_time:
                    if not args.start and 'st' in url_params:
                        args.start = int(url_params['st'])
                    if not args.end and 'et' in url_params:
                        args.end = int(url_params['et'])
                print("🔍 从 /api/ URL 提取 session，应用 --hours 等时间覆盖参数...", file=sys.stderr)
                # 不 return，继续走 Python requests 查询流程
            else:
                # 无覆盖参数：快速模式，直接执行原始 curl（完全保留浏览器请求参数）
                print("🔍 执行原始 curl（保留浏览器请求参数）...", file=sys.stderr)
                data = execute_raw_curl(args.curl)
                if args.save:
                    with open(args.save, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    print(f"💾 已保存到 {args.save}", file=sys.stderr)
                print(format_output(data, args.output))
                return

        # 如果是 Web UI /query URL → 从 URL 中提取时间范围
        if '/query' in url:
            url_params = extract_url_params(url)
            if not args.start and 'start' in url_params:
                args.start = int(url_params['start'])
            if not args.end and 'end' in url_params:
                args.end = int(url_params['end'])
            print("🔍 从 Web UI URL 提取了时间范围", file=sys.stderr)

    if not session_cookie:
        print("❌ 缺少 clickvisual_session cookie", file=sys.stderr)
        print("   请通过 --session 提供，或使用 --curl 粘贴包含 cookie 的完整 curl 命令", file=sys.stderr)
        sys.exit(1)

    # ---- 计算时间范围 ----
    now = int(datetime.now().timestamp())
    start_ts = args.start if args.start else (now - int(args.hours * 3600))
    end_ts = args.end if args.end else now

    # ---- 构建查询条件 ----
    query_filter = _build_query_filter()

    # ---- 输出查询概览 ----
    tz_local = datetime.now().astimezone().tzinfo
    print(f"🔍 查询条件:", file=sys.stderr)
    print(f"   时间: {datetime.fromtimestamp(start_ts, tz=tz_local)} ~ {datetime.fromtimestamp(end_ts, tz=tz_local)}", file=sys.stderr)
    print(f"   条件: {query_filter}", file=sys.stderr)
    print(file=sys.stderr)

    # ---- 执行查询 ----
    try:
        data = query_logs(
            session_cookie=session_cookie,
            start_ts=start_ts,
            end_ts=end_ts,
            query_filter=query_filter,
        )
    except Exception as e:
        print(f"❌ 查询失败: {e}", file=sys.stderr)
        sys.exit(1)

    if args.save:
        with open(args.save, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"💾 已保存到 {args.save}", file=sys.stderr)

    print(format_output(data, args.output))


if __name__ == '__main__':
    main()
