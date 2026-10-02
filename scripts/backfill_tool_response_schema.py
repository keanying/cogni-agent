"""31 号:按飞书接口文档为现有工具批量回填 response_schema(响应字段说明)。

用法:
    ZHILV_ENV=dev .venv/bin/python scripts/backfill_tool_response_schema.py [--dry-run]

规则:
- 解析文档(/tmp/iface_doc.md,来源:数字大脑·数据指标接口文档)各接口的
  「返回参数」表:顶层字段(第2列)path=字段名;data 子字段(第3列)path=data.0.字段名
  (data 是数组,元素结构即子字段);
- 工具按 URL 尾段匹配接口;d-导入副本同接口同表;
- UPDATE 所有 published+draft 快照的 type_config.response_schema
  (jsonb_set 增量,不动其他键,幂等可重跑)。

为什么直接改快照而不走发布流:response_schema 是纯展示/注入元数据
(29 号),不改执行语义;走发布流要 70 次发布+版本推进,收益为零。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402
from cogni_agent.common.db import get_engine  # noqa: E402

DOC = Path("/tmp/iface_doc.md")


def parse_doc() -> dict[str, list[dict]]:
    """解析文档 → {uri 尾段: fields}。"""
    lines = DOC.read_text(encoding="utf-8").splitlines()
    out: dict[str, list[dict]] = {}
    cur_uri, cur_title, in_resp = None, None, False
    for ln in lines:
        m = re.match(r"^# \d+\.\d+ (.+)$", ln)
        if m:
            cur_title = m.group(1).strip()
            in_resp = False
            continue
        if ln.startswith("URI:"):
            cur_uri = ln[4:].strip().rstrip("/").split("digitalBrain/")[-1]
            in_resp = False
            continue
        if ln.startswith("### ") and "返回参数" in ln:
            in_resp = True
            if cur_uri:
                out.setdefault(cur_uri, [])
            continue
        if in_resp and ln.startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            # 分隔行 = 整行都是 - 或空;表头行 = 第2列是「字段」
            if all(set(c) <= set("- ") for c in cells) or cells[1] == "字段":
                continue
            desc = cells[-1]
            if len(cells) >= 6:
                # 6 列:顶层字段在第2列;子字段(序号/第2列空,字段名在第3列)
                if cells[1]:
                    out[cur_uri].append({"path": cells[1], "type": cells[3], "desc": desc})
                elif cells[2]:
                    out[cur_uri].append({"path": f"data.0.{cells[2]}", "type": cells[3], "desc": desc})
            else:
                # 5 列行:有序号=顶层;无序号=data 子字段或带 data[]. 路径的单级表
                name, typ = cells[1], cells[2]
                if not name:
                    continue
                if re.fullmatch(r"\d+", cells[0]) and "[]" not in name:
                    path = name
                elif "[]" in name:
                    path = re.sub(r"\[\]", ".0", name)
                else:
                    path = f"data.0.{name}"
                out[cur_uri].append({"path": path, "type": typ, "desc": desc})
        elif in_resp and ln.startswith("#") and not ln.startswith("####"):
            in_resp = False
    # 「data」顶层字段本身冗余(子字段已展开),保留占位说明
    return out


async def main() -> None:
    fields_by_uri = parse_doc()
    dry = "--dry-run" in sys.argv
    print(f"文档解析:{len(fields_by_uri)} 个接口")
    for uri, fs in fields_by_uri.items():
        print(f"  {uri}: {len(fs)} 字段")

    eng = get_engine()
    async with eng.begin() as c:
        rows = (await c.execute(text(
            "SELECT v.id, t.name, v.version, v.status, v.snapshot "
            "FROM numeri_core_tc_resource_versions v "
            "JOIN numeri_core_tc_tools t ON t.id = v.resource_id "
            "WHERE v.resource_type = 'tool' AND v.status IN ('published','draft')"))).all()
        matched = 0
        for vid, name, ver, status, snap in rows:
            if not isinstance(snap, dict):
                continue
            tc = snap.get("type_config") or {}
            url = tc.get("url") or ""
            tail = url.rstrip("/").split("digitalBrain/")[-1]
            # 每日报表趋势:工具 URL 尾段 dailyReportTrend ↔ 文档 api/touristDailyReportTrend
            key = tail
            if key not in fields_by_uri and "dailyReportTrend" in key:
                key = "api/touristDailyReportTrend"
            if key not in fields_by_uri:
                continue
            rs = {"fields": fields_by_uri[key]}
            matched += 1
            if dry:
                print(f"  [dry] {name} v{ver}({status}) ← {key}({len(rs['fields'])} 字段)")
                continue
            snap["type_config"] = {**tc, "response_schema": rs}
            await c.execute(text(
                "UPDATE numeri_core_tc_resource_versions "
                "SET snapshot = CAST(:snap AS json) WHERE id = :vid"),
                {"snap": json.dumps(snap, ensure_ascii=False), "vid": vid})
        print(f"\n{'[dry] ' if dry else ''}匹配更新 {matched} 个快照"
              f"({'未写库' if dry else '已写库'})")
    await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
