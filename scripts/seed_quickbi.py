#!/usr/bin/env python
"""
Quick BI 智能小Q 连接初始化(可重复执行)。

    # 1) 只有 user_token(试用):需同时在 .env 配 QUICKBI_TRIAL_API_KEY / QUICKBI_TRIAL_API_SECRET
    uv run python scripts/seed_quickbi.py --user-token a959173c... \\
        --dashboard "https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=88c9e5de-...&accessTicket=..."

    # 2) 自有凭证
    uv run python scripts/seed_quickbi.py --api-key AK --api-secret SK --user-token UT --check

    # 3) 直接粘贴「一键复制 skill 配置」文本
    uv run python scripts/seed_quickbi.py --paste-file skill_config.txt

    # 4) 使用 .env.quickbi / 环境变量里的 QUICKBI_* 配置(推荐)
    uv run python scripts/seed_quickbi.py --from-env --check --adopt \
        --dashboard "https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=88c9e5de-2d4d-4ba9-a045-6f9b71cf0c66&accessTicket=807cc156-9109-4dfc-b4d7-ce5c670ba566&dd_orientation=auto"

行为:按 --name 查找连接,存在则更新(未传的机密字段保持不变),不存在则创建并设为默认;
--check 做连通性自检;--dashboard 登记仪表板资产并把候选问题存为固定问题(--adopt)。
连接归属 --owner 指定的账号(默认超管 pftroot),可见范围 group。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.tool_center_service.quickbi import service as svc
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
from cogni_agent.tool_center_service.quickbi.models import QbiConnection
from cogni_agent.user_center_service.models import User


def parse_paste(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*[-*]?\s*([A-Za-z_]+)\s*[::=]\s*(.*?)\s*$", line)
        if m and m.group(1).lower() in ("api_key", "api_secret", "user_token", "server_domain"):
            out[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 Quick BI 智能小Q 连接")
    ap.add_argument("--name", default="Quick BI(默认)")
    ap.add_argument("--server-domain", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--api-secret", default=None)
    ap.add_argument("--user-token", default=None)
    ap.add_argument("--paste-file", default=None, help="「一键复制 skill 配置」文本文件")
    ap.add_argument("--from-env", action="store_true",
                    help="从环境变量 / .env.quickbi 读取 QUICKBI_API_KEY 等(config.yaml quickbi.default_connection)")
    ap.add_argument("--owner", default="pftroot", help="连接归属账号")
    ap.add_argument("--check", action="store_true", help="保存后做连通性自检")
    ap.add_argument("--dashboard", default=None, help="登记一个仪表板(URL / pageId)")
    ap.add_argument("--adopt", action="store_true", help="把仪表板候选问题存为固定问题")
    args = ap.parse_args()

    data: dict = {}
    if args.from_env:
        from cogni_agent.config import get_settings

        dc = get_settings().get("quickbi", "default_connection", default={}) or {}
        data.update({k: str(dc.get(k) or "") for k in ("server_domain", "api_key", "api_secret", "user_token")})
        data = {k: v for k, v in data.items() if v}
    if args.paste_file:
        data.update(parse_paste(open(args.paste_file, encoding="utf-8").read()))
    for k in ("server_domain", "api_key", "api_secret", "user_token"):
        v = getattr(args, k)
        if v is not None:
            data[k] = v
    if not data.get("user_token") and not args.check and not args.dashboard:
        print("✗ 至少需要 --user-token(或 --paste-file)", file=sys.stderr)
        return 2
    data.setdefault("server_domain", "https://bi.aliyun.com")
    data["use_trial"] = not (data.get("api_key") and data.get("api_secret"))
    data["name"] = args.name

    async with get_sessionmaker()() as db:
        owner = (await db.execute(select(User).where(User.account == args.owner))).scalar_one_or_none()
        if owner is None:
            print(f"✗ 账号 {args.owner} 不存在,请先执行 scripts/init_db.py --with-seed", file=sys.stderr)
            return 2
        existing = (await db.execute(select(QbiConnection).where(QbiConnection.name == args.name))).scalars().first()
    actor = svc.Actor(user_id=owner.id, scenic_id=owner.scenic_id or None, role=owner.role or "super_admin")
    if existing is None:
        data["is_default"] = True
    view = await svc.save_connection(actor, data, existing.id if existing else None)
    d = svc.connection_dict(view)
    print(f"✓ 连接已{'更新' if existing else '创建'}:{d['name']}  id={d['id']}")
    print(f"  server_domain={d['server_domain']}  api_key={d['api_key'] or '(空)'}  "
          f"user_token={d['user_token']}  试用凭证={'是' if d['trial_active'] else '否'}")
    if d["missing"]:
        print(f"  ⚠ 仍缺少:{', '.join(d['missing'])} —— 调用会失败。"
              "api_key/api_secret 请在 Quick BI 控制台头像菜单「一键复制 skill 配置」获取,"
              "或在 .env 配置 QUICKBI_TRIAL_API_KEY / QUICKBI_TRIAL_API_SECRET")

    if args.check:
        r = await svc.check_connection(actor, view.id)
        print(("✓ " if r["ok"] else "✗ ") + r["message"])
    if args.dashboard:
        try:
            asset = await svc.register_dashboard(actor, view.id, args.dashboard)
        except QuickBIError as exc:
            print(f"✗ 仪表板登记失败:{exc}")
            return 1
        print(f"✓ 仪表板已登记:{asset['name']}  pageId={asset['ref_id']}  图表 {asset['chart_count']} 个 "
              f"/ 数据集 {asset['cube_count']} 个  asset_id={asset['id']}")
        if args.adopt:
            created = await svc.adopt_suggestions(actor, asset["id"])
            print(f"✓ 已生成固定问题 {len(created)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
