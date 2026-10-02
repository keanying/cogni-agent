#!/usr/bin/env python
"""
13 号文档 Phase 4:存量资源版本回填(一次性迁移,幂等)。

背景:版本化是后加的能力 —— 历史 Tool/Skill/Agent 主表没有 ResourceVersion 记录,
历史 skill/agent 快照里 tool pin 是 version:null(解析器旧语义按主表兜底)。Phase 3
要删掉 resolver 的「主表兜底」路径(运行时 100% 走版本快照),**必须先**跑本脚本,
否则存量资源在运行时全部被拒。铁律:迁移先于 Phase 3 上线。

用法:
    uv run python scripts/backfill_resource_versions.py            # 真实执行
    uv run python scripts/backfill_resource_versions.py --dry-run  # 只打印将要做什么

幂等:
    按 (resource_type, resource_id) 检查是否已有版本记录,已有则跳过;
    重复执行安全,第二次起基本全跳过。

迁移步骤(§4.1 顺序):
    1. 工具补版本:主表 status=PUBLISHED 的无版本工具 snapshot_current 打 v1;
    2. skill/agent 补版本:同上(未上线/草稿态不补,输出跳过清单供人工核对);
    3. skill/agent published 快照中 version:null 的 tool/skill pin 固化为
       绑定资源当前默认版本号(工具已在步骤 1 补齐,固化必成功);
    4. 重新生成缺失的 locked_deps(published 行统一补全依赖锁);
    5. 主表 status 与版本表对齐:有 published 版本 → PUBLISHED,
       无 published 版本 → DRAFT(Agent 的 DISABLED 保留,不覆盖停用语义);
    6. 清理 G6 收敛后的存量 global 指派(scope_type=global 语义已废,兜底统一归
       默认版本 is_default;删除并输出受影响 agent 清单供人工核对默认版本已设)。

输出:每个资源补了几版 / pin 固化为几 / 生成了几条锁,供人工核对。

备份建议:迁移前对 tc_resource_versions 与三张主表做 SQL dump。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.tool_center_service import versioning as ver
from cogni_agent.tool_center_service.versioning import (
    STATUS_PUBLISHED,
    ResourceAssignment,
    ResourceVersion,
)

# 主表 status 枚举(主表用大写,ResourceVersion.status 用 ver.STATUS_* 小写)
_MT_PUBLISHED = "PUBLISHED"
_MT_DRAFT = "DRAFT"
_MT_DISABLED = "DISABLED"  # 仅 Agent 有


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_first_version(db, resource_type: str, resource_id: str,
                                main_status: str, dry_run: bool,
                                changes: list[str]) -> bool:
    """无版本记录且主表已上线的资源,补 published v1。返回是否执行了补版本。"""
    existing = (await db.execute(
        select(ResourceVersion.id).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
        ).limit(1)
    )).scalars().first()
    if existing is not None:
        return False

    if main_status != _MT_PUBLISHED:
        changes.append(
            f"[跳过] {resource_type} {resource_id}:主表状态为 {main_status},"
            f"未上线不补版本(如需发布请走管理端)")
        return False

    if dry_run:
        changes.append(f"[将补] {resource_type} {resource_id}:snapshot_current 打 v1")
        return True

    await ver.snapshot_current(db, resource_type, resource_id,
                               created_by="migration",
                               note="13 号文档 Phase 4 迁移:存量资源回填首个版本")
    changes.append(f"[已补] {resource_type} {resource_id}:v1(PUBLISHED,is_default)")
    return True


async def _refresh_published_locks(db, resource_type: str, dry_run: bool,
                                   changes: list[str]) -> None:
    """对某类型全部 published 版本:固化 version:null pin + 补全缺失依赖锁。

    复用发布时的同一套解析(_resolve_locked_deps):返回固化后的快照与依赖闭包;
    解析失败(绑定资源 DRAFT/不存在)收集问题并跳过该行,输出人工核对。

    仅处理主表仍存活的资源 —— 已删除资源的孤儿 published 版本行(资源删除
    后版本行保留供追溯)直接跳过:其绑定可能引用同批删除的依赖(如 09-01
    清理的「文档驱动Agent生成器」演示数据),解析必然失败且固化无意义。
    """
    from cogni_agent.tool_center_service.agent.models import Agent
    from cogni_agent.tool_center_service.skill.models import Skill
    from cogni_agent.tool_center_service.tool.models import Tool
    main_model = {"skill": Skill, "agent": Agent, "tool": Tool}[resource_type]
    deleted_ids = set((await db.execute(
        select(main_model.id).where(main_model.deleted_at.is_not(None))
    )).scalars().all())

    rows = (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
    )).scalars().all()

    for row in rows:
        if row.resource_id in deleted_ids:
            changes.append(
                f"[跳过孤儿] {resource_type} {row.resource_id} v{row.version}:"
                f"主表已删除,孤儿 published 版本不固化")
            continue

        if dry_run:
            # 预演路径只做静态扫描(真实解析依赖「补版本已落库」,dry-run 不落库,
            # 直接解析会把将成功的行误报为失败):
            #   - 快照里 version:null 的模糊 pin → 将固化
            #   - locked_deps 缺失 → 将生成
            null_pins = _null_pins(row.snapshot or {})
            if null_pins or row.locked_deps is None:
                changes.append(
                    f"[将修] {resource_type} {row.resource_id} v{row.version}:"
                    f"{('固化 pin(' + str(len(null_pins)) + ' 个 null→默认): ' + ', '.join(null_pins[:5])) if null_pins else ''}"
                    f"{'生成 locked_deps' if row.locked_deps is None else ''}")
            continue

        new_snapshot, deps, problems = await ver._resolve_locked_deps(db, row)
        if problems:
            changes.append(
                f"[失败跳过] {resource_type} {row.resource_id} v{row.version}:"
                f"依赖不可用({'; '.join(problems[:5])}),需人工处理后再跑")
            continue

        pin_notes = _pin_diff(row.snapshot or {}, new_snapshot)
        lock_missing = row.locked_deps is None
        if not pin_notes and not lock_missing:
            continue  # 已固化且已有锁,无需变更
        # 快照与锁同源重写:任何 version:null pin 固化都要落到锁里,
        # 避免「快照固化了但锁仍指向旧闭包」的漂移
        row.snapshot = new_snapshot
        row.locked_deps = {
            "deps": deps,
            "frozen_at": _now(),
        }
        what = []
        if pin_notes:
            what.append("固化 pin: " + ", ".join(pin_notes))
        if lock_missing:
            what.append(f"生成 locked_deps({len(deps)} 条)")
        changes.append(f"[已修] {resource_type} {row.resource_id} v{row.version}:" + "; ".join(what))


def _null_pins(snapshot: dict) -> list[str]:
    """静态扫描快照中 version:null 的模糊绑定 pin,返回资源 id 列表。"""
    out: list[str] = []
    for snap_key, id_key in (("tools", "tool_id"), ("bound_skills", "skill_id")):
        for p in snapshot.get(snap_key) or []:
            if isinstance(p, dict) and p.get("version") is None and p.get(id_key):
                out.append(str(p[id_key]))
    return out


def _pin_diff(old: dict, new: dict) -> list[str]:
    """比较快照里绑定 pin 的 version:null → N 固化情况,返回人类可读说明。"""
    out: list[str] = []
    for snap_key, id_key in (("tools", "tool_id"), ("bound_skills", "skill_id")):
        old_pins = {p.get(id_key): p.get("version") for p in old.get(snap_key) or []
                    if isinstance(p, dict)}
        new_pins = {p.get(id_key): p.get("version") for p in new.get(snap_key) or []
                    if isinstance(p, dict)}
        for rid, new_ver in new_pins.items():
            old_ver = old_pins.get(rid)
            if new_ver is not None and old_ver is None:
                out.append(f"{rid} → v{new_ver}")
    return out


async def _align_main_status(db, rows, resource_type: str, dry_run: bool,
                             changes: list[str]) -> None:
    """主表 status 与版本表对齐:有 published → PUBLISHED;否则 DRAFT(Agent DISABLED 保留)。"""
    for obj in rows:
        has_pub = (await db.execute(
            select(ResourceVersion.id).where(
                ResourceVersion.resource_type == resource_type,
                ResourceVersion.resource_id == obj.id,
                ResourceVersion.status == STATUS_PUBLISHED,
            ).limit(1)
        )).scalars().first() is not None

        target = _MT_PUBLISHED if has_pub else _MT_DRAFT
        if getattr(obj, "status", None) == _MT_DISABLED:
            # 停用是独立语义,不随版本表翻转;仍记录一行供核对
            changes.append(f"[保留] {resource_type} {obj.id}:status=DISABLED(停用语义不覆盖)")
            continue
        if obj.status != target:
            changes.append(
                f"[{('将' if dry_run else '已')}对齐] {resource_type} {obj.id}:"
                f"主表 status {obj.status} → {target}")
            if not dry_run:
                obj.status = target


async def _purge_global_assignments(db, dry_run: bool, changes: list[str]) -> None:
    """清理 G6 收敛后的存量 global 指派(13 号文档 Phase 4 G6 + §4 第 288 行)。

    global 兜底语义已统一归默认版本(is_default),scope_type=global 的指派行不再
    被解析(versioning SCOPE_TYPES 已收窄为 user/scenic)。删除存量行,并列出受
    影响的 agent 供人工核对:该 agent 是否已有默认版本(无默认则此前 global 兜底
    会静默消失,需在管理端补设)。
    """
    rows = (await db.execute(
        select(ResourceAssignment).where(ResourceAssignment.scope_type == "global")
    )).scalars().all()
    if not rows:
        return
    agent_ids = sorted({r.agent_id for r in rows})
    agent_hint = "agent 清单(请核对各 agent 已设默认版本): " + ", ".join(agent_ids)
    if dry_run:
        changes.append(f"[将删] global 指派 {len(rows)} 条;{agent_hint}")
        return
    for r in rows:
        await db.delete(r)
    changes.append(f"[已删] global 指派 {len(rows)} 条;{agent_hint}")


async def main(dry_run: bool = False) -> None:
    from cogni_agent.tool_center_service.agent.models import Agent
    from cogni_agent.tool_center_service.skill.models import Skill
    from cogni_agent.tool_center_service.tool.models import Tool

    changes: list[str] = []
    mode = "DRY-RUN(不落库)" if dry_run else "执行"
    print(f"=== 13 号文档 Phase 4 存量版本回填 [{mode}] ===")

    async with get_sessionmaker()() as db:
        # 1) 工具补版本(先于一切 —— skill/agent 的 pin 固化依赖工具版本存在)
        tools = (await db.execute(
            select(Tool).where(Tool.deleted_at.is_(None)))).scalars().all()
        n_tool = 0
        for t in tools:
            if await _ensure_first_version(db, "tool", t.id, t.status, dry_run, changes):
                n_tool += 1
        print(f"  工具共 {len(tools)} 个,本次补版本 {n_tool} 个")

        # 2) skill/agent 补版本
        skills = (await db.execute(
            select(Skill).where(Skill.deleted_at.is_(None)))).scalars().all()
        agents = (await db.execute(
            select(Agent).where(Agent.deleted_at.is_(None)))).scalars().all()
        n_sk, n_ag = 0, 0
        for s in skills:
            if await _ensure_first_version(db, "skill", s.id, s.status, dry_run, changes):
                n_sk += 1
        for a in agents:
            if await _ensure_first_version(db, "agent", a.id, a.status, dry_run, changes):
                n_ag += 1
        print(f"  skill 共 {len(skills)} 个,本次补版本 {n_sk} 个;"
              f"agent 共 {len(agents)} 个,本次补版本 {n_ag} 个")

        # 3+4) published 行固化 pin + 补全依赖锁(先补版本的行此刻已在库中,
        #      但 dry-run 不落库 → dry-run 下固化校验可能因版本尚未写入而失败,
        #      这是 dry-run 已知限制,以真实执行结果为准)
        for rt in ("skill", "agent", "tool"):
            await _refresh_published_locks(db, rt, dry_run, changes)

        # 5) 主表 status 对齐
        await _align_main_status(db, tools, "tool", dry_run, changes)
        await _align_main_status(db, skills, "skill", dry_run, changes)
        await _align_main_status(db, agents, "agent", dry_run, changes)

        # 6) 清理 G6 收敛后的存量 global 指派
        await _purge_global_assignments(db, dry_run, changes)

        if not dry_run:
            await db.commit()
            print(f"✓ 已提交。共 {len(changes)} 条变更:")
        else:
            print(f"(dry-run) 预计 {len(changes)} 条变更:")

    for c in changes:
        print(f"  {c}")

    if not dry_run:
        print("\n✓ 回填完成。请人工核对以上变更清单;建议先对数据库做备份再执行。")
    else:
        print("\n(以上为预演,未写入任何数据;确认无误后去掉 --dry-run 执行)")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
