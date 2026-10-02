"""32 号补:参数覆盖全量审计——工具必填参数 vs 问题 param_template 的差集。

完备口径(反向于关键词扫描):凡问题绑定的技能含「参数 schema 声明 required 且
非系统注入」的工具,问题模板必须覆盖这些键(装配期全量生成,不给模型留填参权)。
系统注入键(x-param-source/身份类)排除;只审时间类与业务参数键。

用法: ZHILV_ENV=dev .venv/bin/python scripts/audit_param_coverage.py [--fix]
  缺省只读审计;--fix 按启发式补齐(对比窗口按 time_period 语义映射)。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402
from cogni_agent.common.db import get_engine  # noqa: E402

# 身份类键:由 JWT/框架在调用时携带,与工具声明无关,模板不需要配
IDENTITY = {"scenicId", "scenic_id", "userId", "user_id"}
# 本该系统注入的键:**仅当工具 schema 真的声明了注入来源**(x-param-source)
# 才排除。按名字排除会让「声明漏配」这类断线永久隐身——supplierId
# 就是这么漏的:名字一直在排除名单里,而门票/产品/报表比对三个工具的 schema 上
# 并无 x-param-source,supplierId 作为 required 暴露给模型,
# 模型要么反问(f400994c)要么编个值发出去拿空数据再空转(249488c8,4 轮 273s)。
INJECTABLE = {"supplierId", "supplierIds", "account", "otaAccount", "ota_account"}

INJECTED = IDENTITY | INJECTABLE  # 兼容旧引用;实际判定一律按 schema 声明

# time_period → 对比窗口占位符启发式(比对类 compareXxx 的补值依据)
# 环比窗口映射收在常驻模块(审计脚本与补全脚本共用同一份,避免两处漂移)
from cogni_agent.gateway_service.param_audit import PERIOD_CMP  # noqa: E402


async def main() -> None:
    fix = "--fix" in sys.argv
    eng = get_engine()
    async with eng.connect() as c:
        # ① 全部 published 工具快照:required 参数(剥离系统注入)
        tools = (await c.execute(text(
            "SELECT t.id, t.name, v.snapshot->'type_config'->'params_schema' AS ps, "
            "v.snapshot->'type_config'->'params_schema'->'properties' AS props "
            "FROM numeri_core_tc_resource_versions v "
            "JOIN numeri_core_tc_tools t ON t.id = v.resource_id "
            "WHERE v.resource_type='tool' AND v.status='published' "
            # 只看**最新 published 版本**:历史版本快照冻结不回改,用它们判定现状
            # 会把已修好的工具误报成断线(26 号迁移后就踩过:旧版本仍是无声明的)。
            "AND v.version = (SELECT MAX(v2.version) FROM numeri_core_tc_resource_versions v2 "
            "WHERE v2.resource_type='tool' AND v2.resource_id=t.id AND v2.status='published')"))).all()
        tool_req: dict[str, tuple[str, list]] = {}
        tool_unwired: dict[str, tuple[str, list]] = {}  # 本该注入却没声明来源的键
        for tid, tname, ps, props in tools:
            props = props if isinstance(props, dict) else {}
            req, unwired = [], []
            for r in ((ps or {}).get("required") or []):
                if r in IDENTITY:
                    continue
                p = props.get(r)
                if isinstance(p, dict) and p.get("x-param-source"):
                    continue  # 声明了注入来源 → 值由系统给,模板不需要配
                req.append(r)
                if r in INJECTABLE:
                    unwired.append(r)
            if req:
                tool_req[tid] = (tname, req)
            if unwired:
                tool_unwired[tid] = (tname, unwired)
        # ② 全部 published 技能快照:绑定工具
        skills = (await c.execute(text(
            "SELECT t.id, t.name, v.snapshot->'tools' AS bound "
            "FROM numeri_core_tc_resource_versions v "
            "JOIN numeri_core_tc_skills t ON t.id = v.resource_id "
            "WHERE v.resource_type='skill' AND v.status='published' "
            "AND v.version = (SELECT MAX(v2.version) FROM numeri_core_tc_resource_versions v2 "
            "WHERE v2.resource_type='skill' AND v2.resource_id=t.id AND v2.status='published')"))).all()
        skill_tools: dict[str, list[str]] = {}
        for sid, sname, bound in skills:
            ids = [b.get("tool_id") for b in (bound or []) if b.get("tool_id")]
            skill_tools[sid] = ids
        # ③ 问题(有 skill_id)× 模板 keys vs 工具 required
        qs = (await c.execute(text(
            "SELECT id, question_text, skill_id, time_period, param_template, active "
            "FROM numeri_core_app_question_bank WHERE skill_id IS NOT NULL"))).all()
        gaps = []
        for qid, qtext, sid, period, tpl, active in qs:
            need: set = set()
            bad: set = set()
            for tid in skill_tools.get(sid) or []:
                if tid in tool_req:
                    need.update(tool_req[tid][1])
                if tid in tool_unwired:
                    bad.update(tool_unwired[tid][1])  # 补模板治不好的键
            have = set((tpl or {}).keys())
            missing = need - have
            if missing:
                gaps.append((qid, qtext, period, sorted(missing), active, tpl,
                             sorted(missing & bad)))
        print(f"审计:问题 {len(qs)} 个(有技能绑定),工具 {len(tool_req)} 个有必填参数,缺口问题 {len(gaps)} 个:\n")
        if tool_unwired:
            print(f"⚠ 工具声明断线 {len(tool_unwired)} 个(本该系统注入,却没声明 "
                  "x-param-source)——必须修工具 schema,补模板无效:")
            for tid, (tname, keys) in sorted(tool_unwired.items(), key=lambda kv: kv[1][0]):
                print(f"    - {tname} ({str(tid)[:8]}): {keys}")
            print()
        for qid, qtext, period, miss, active, tpl, bad in gaps:
            flag = f"  ⚠其中 {bad} 补模板无效(工具未声明注入来源,必修 schema)" if bad else ""
            print(f"  [{str(qid)[:8]}]{'(停用)' if not active else ''} {qtext[:26]:28} period={period} 缺 {miss}{flag}")
        if fix:
            print("\n--fix:按 time_period 启发式补对比窗口(仅 compare 类;其他缺失键需人工定值)")
            async with eng.begin() as cw:
                for qid, qtext, period, miss, active, tpl, _bad in gaps:
                    add = {}
                    cmp_pair = PERIOD_CMP.get(period or "")
                    for k in miss:
                        if "compareStart" in k and cmp_pair:
                            add[k] = cmp_pair[0]
                        elif "compareEnd" in k and cmp_pair:
                            add[k] = cmp_pair[1]
                    if not add:
                        print(f"  ✗ {qtext[:24]}:缺 {miss} 无启发式,人工处理")
                        continue
                    new_tpl = {**(tpl or {}), **add}
                    await cw.execute(text(
                        "UPDATE numeri_core_app_question_bank SET param_template=:t WHERE id=:id"),
                        {"t": json.dumps(new_tpl, ensure_ascii=False), "id": qid})
                    print(f"  ✓ {qtext[:24]} +{sorted(add)}")
        else:
            print("\n(只读审计;--fix 补 compare 类缺口,其余人工)")
    await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
