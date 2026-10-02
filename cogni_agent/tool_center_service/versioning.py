"""
Tool / Skill / Agent 的统一版本管理(并行版本体系)。

**为什么用一张统一的版本表,而不是给三种资源各建一张**:
版本管理的操作是完全同构的——列版本、发布、下线、回滚、对比、按版本解析。
分三套表意味着同一套逻辑写三遍,而且迟早会长歪(Tool 有版本历史、Skill 只有 Prompt
版本、Agent 完全没有,这就是分开写的结果)。统一之后新增资源类型只要注册一个
快照函数,其余全部复用。

**核心规则(15 号文档:删除默认版本,显式分发 + fail-closed),想清楚了再看代码会容易很多**:

1. **同一资源允许多个 published 版本并行在线**,分别服务于不同客户:
   - **指派版本**:通过 `tc_agent_assignments` 把「用户(user)」钉到 Agent 的具体版本(见 resolver)。
   - **不存在「默认版本」**:发布 vN 只让 vN 可被选择,对任何人都不自动生效;
     「谁用哪个版本」唯一由 user 指派(Agent)或使用链 pin / 显式指定(skill/tool)表达。
2. **published 版本不可直接编辑**。编辑一律作用于 draft(编辑区):
   - draft 是版本表里 status=draft 的行,是「编辑区」的载体;
   - **保存只改内容、不递增、不留痕**;每资源同时最多一个 draft;
   - **draft 永不参与运行时解析** —— 保存后任何人都看不到,只有点「发布」才对外。
3. **发布 = draft -> published(快照冻结)**,【不写主表、不设默认、不隐含任何生效语义】;
   - 发布成功且零 user 指派 -> 调用方提示「尚未分发,无用户可使用」(D7-A),引导去分发中心。
4. **set_default 已下线**(D1-B):曾有的「设为默认 = 全网未指派客户切换」语义彻底移除,
   is_default 列物理删除;「对谁生效」只由 user 指派表达。
5. **回滚 = 离线版本重新发布**(offline -> published);内容回滚 = 基于历史版本派生草稿再发布。
6. **下线规则**:
   - 被指派 / 被 pin -> **禁止下线**(必须先解除引用);
   - 无引用的 published -> 可下线,只影响该版本自己;
   - 全部 published 下线后资源才整体不可用。

**快照 vs 主表(15 号 D8-A)**:主表是**资源身份目录**(id/name/visibility/status/owner/scenic),
**不再物化内容**;内容一律以版本快照为权威(列表/详情内容展示走「最新 published 快照 >
最新草稿快照」)。运行时解析:显式指定 > user 指派 > 报错 agent_not_distributed(无隐式兜底)。

**编辑保存与版本无关(推荐方案)**:草稿不预占版本号 —— draft 行的 version 为 NULL,
正式版本号在「发布」那一刻按 max(published)+1 分配,杜绝"派生草稿又丢弃"导致的版本空洞;
「保存」(update_version)只改 draft 内容,不递增版本号、不产生新版本记录、不影响任何客户。

**发布预检与依赖锁(推荐方案)**:发布时对绑定资源做全链路预检(agent->skill->tool),
`version:null` 的模糊 pin 在 15 号删默认后**拦截发布**(fail-closed,要求显式选已发布版本,
无「默认/最新」隐式固化);绑定资源处于 DRAFT / 不存在 / 已下线同样**拦截发布**并给出清单;
通过后生成 locked_deps 依赖锁冻结完整闭包,运行时严格按锁解析。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from cogni_agent.common.db import Base, table_name
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.common.logging import get_logger

log = get_logger("tool_center.versioning")

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_OFFLINE = "offline"
_STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_OFFLINE)

RESOURCE_TYPES = ("tool", "skill", "agent")

# 版本指派的作用域粒度:15 号文档 D9-A —— scenic 维度下线,只留 user。
# 历史:global 兜底已收敛(13 号 Phase 4 G6)、scenic 继承已取消(D5-B:人必须本人被指派)。
# 现仅 user:解析命中 user 指派即用,否则 fail-closed 报 agent_not_distributed,无任何隐式兜底。
SCOPE_USER = "user"
_SCOPES = (SCOPE_USER,)


class ResourceVersion(Base):
    """Tool / Skill / Agent 的版本快照。

    - 同一资源可同时存在多个 published(并行服务不同客户,靠指派/pin/显式选版消费);
    - 无「默认版本」概念(15 号 D1-B):发布不隐含生效,谁用哪版由 user 指派(agent)
      与使用链 pin / 显式指定(skill/tool)表达;
    - `status=draft` 的行即「编辑区」,不参与运行时;`base_version_id` 记录草稿从哪个版本派生。
    """

    __tablename__ = table_name("tc_resource_versions")

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: f"ver_{uuid.uuid4().hex}")
    resource_type: Mapped[str] = mapped_column(String(16), index=True)   # tool / skill / agent
    resource_id: Mapped[str] = mapped_column(String(64), index=True)
    # 版本号:同一资源内自增。用整数而不是语义化版本——语义化版本需要人判断
    # "这算大改还是小改",而这里的诉求只是"能区分、能排序、能指名道姓"。
    # 推荐方案:草稿不预占版本号——draft 行的 version 为 NULL,正式版本号在 publish
    # 那一刻按 max(published)+1 分配,杜绝"派生了草稿又丢弃导致版本空洞"。
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_DRAFT, index=True)
    # (15 号 D1-B:is_default 列已删除 —— 无「默认版本」概念,见模块 docstring)
    # 草稿来源版本(仅 draft 有意义):派生时记录 base_version_id,UI 可展示「基于 vN 编辑中」。
    base_version_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # 该版本的完整定义快照(15 号 D8-A:快照即运行时唯一真实数据源,不再物化回主表)。
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 依赖锁(推荐方案):发布时冻结的完整依赖闭包 agent->skill->tool。
    # 结构:{"deps": [{"resource_type","resource_id","name","version","status"}...],
    #       "frozen_at": ISO 时间}
    # 15 号删默认后发布预检拦截 version:null,锁内 version 均为已固化的 published 版本号。
    # 发布后不可变,运行时严格按锁解析;「刷新依赖」= 重新发布一次生成新锁。
    locked_deps: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")        # 这个版本改了什么
    created_by: Mapped[str] = mapped_column(String(64), default="")
    # 0087:最近修改人(保存草稿 / 发布 / 下线 / 重新发布时写入),列表「修改人」取这里
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offlined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResourceAssignment(Base):
    """版本指派:把某个「用户(user)」钉到 Agent / Skill 的某个 published 版本。

    15 号 D9-A:scope 仅 user(user 本人必须被指派才能用,取消 scenic 继承);
    无 user 指派 = 运行时 fail-closed 报 agent_not_distributed / skill_not_distributed,
    不存在默认回落。同一 (resource_type, agent_id, scope_id) 只有一条,切换 = 更新 version_id。
    这是「按用户提供不同版本」的落地表(付费/定制/新旧客户分流),纯手动维护。

    **0074 起 Skill 也走这张表**:`resource_type` 区分 agent / skill,
    `agent_id` 列沿用为「资源 id」(改名要动索引与全部读点,收益只是名字好看)。
    代码里一律用 `resource_id` 属性读写,SQL 层面才见得到 agent_id 这个历史名字。
    """

    __tablename__ = table_name("tc_agent_assignments")

    id: Mapped[str] = mapped_column(String(48), primary_key=True,
                                    default=lambda: f"asg_{uuid.uuid4().hex}")
    # 资源类型:agent(存量)/ skill(0074 新增)。存量行迁移回填 'agent'。
    resource_type: Mapped[str] = mapped_column(String(16), default="agent", index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)   # 实为「资源 id」,见类 docstring
    scope_type: Mapped[str] = mapped_column(String(16))        # user / scenic
    scope_id: Mapped[str] = mapped_column(String(64), default="")  # user/scenic 必填;无 global 语义
    version_id: Mapped[str] = mapped_column(String(48))
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    @property
    def resource_id(self) -> str:
        """业务语义上的资源 id(列名 agent_id 是历史包袱,见类 docstring)。"""
        return self.agent_id


# 可被分发的资源类型:15 号只有 agent,0074 起 skill 也能按用户分发
# (APP 端可以直接点一个技能提问,不必非要挂在某个 agent 底下)。
ASSIGNABLE_TYPES = ("agent", "skill")

# 分发豁免角色:超管不受分发限制(平台方自己要能随时验证任意资源),
# 其余角色一律按指派 fail-closed。注意这是「调用豁免」,不是「可见性豁免」。
DISTRIBUTION_EXEMPT_ROLES = frozenset({"super_admin"})


def is_distribution_exempt(role: str | None) -> bool:
    return (role or "") in DISTRIBUTION_EXEMPT_ROLES


# ── 资源适配:新增一种资源类型只要在这里注册 ──────────────────────────────
@dataclass
class ResourceAdapter:
    """把某种资源在「ORM 对象」与「快照 dict」之间来回转换。

    绑定字段(agent→skill / skill→tool)在快照里是带版本的 pin 数组,
    主表里则是纯 id 数组 —— 快照键与主表键不同,由 `binds` 配置转换:

    - `bound_skills: [{skill_id, version}]`  <->  主表 `bound_skill_ids: ["sk_a"]`
    - `tools: [{tool_id, version}]`          <->  主表 `tool_ids: ["tl_x"]`
    - `version` 缺省 / `null` = 未显式指定版本 —— 15 号删默认后发布预检**拦截**,
      要求为该绑定显式选择已发布版本(fail-closed,无隐式「最新版」兜底)。
    """

    model: type
    # 参与版本化的普通字段。**刻意不整表快照**:id / created_at / owner 这些不属于
    # "定义",把它们纳入版本会导致回滚时把创建者都改了。
    fields: tuple[str, ...]
    label: str
    # 仅出现在快照里、不落在主表的额外键(推荐方案 skill 的 prompt 全文)。
    # apply 时不会 setattr 到 ORM,由 _materialize 单独写回对应表。
    extra_keys: tuple[str, ...] = ()
    # 绑定字段配置:主表字段名 -> {快照键, pin 的 id 键, 下游资源类型}
    # agent: {"bound_skill_ids": {"snapshot_key": "bound_skills", "item_id": "skill_id", ...}}
    # skill: {"tool_ids": {"snapshot_key": "tools", "item_id": "tool_id", ...}}
    binds: dict[str, dict] = field(default_factory=dict)

    def to_snapshot(self, obj: Any) -> dict:
        snap = {f: getattr(obj, f, None) for f in self.fields}
        for table_key, cfg in self.binds.items():
            ids = list(getattr(obj, table_key, None) or [])
            snap[cfg["snapshot_key"]] = [{cfg["item_id"]: i, "version": None} for i in ids]
        return snap

    def apply(self, obj: Any, snapshot: dict) -> None:
        for f in self.fields:
            if f in snapshot:
                setattr(obj, f, snapshot[f])
        for table_key, cfg in self.binds.items():
            pins = snapshot.get(cfg["snapshot_key"])
            if pins is None:
                # 兼容旧格式快照(纯 id 数组):直接沿用旧键
                old = snapshot.get(table_key)
                if old is not None:
                    setattr(obj, table_key, list(old))
                continue
            ids = [p[cfg["item_id"]] for p in pins
                   if isinstance(p, dict) and p.get(cfg["item_id"])]
            setattr(obj, table_key, ids)

    def accepted_keys(self) -> set[str]:
        """快照允许出现的键:普通字段 + 绑定新键 + 绑定旧键(兼容存量快照) + 额外快照键。"""
        keys = set(self.fields)
        for table_key, cfg in self.binds.items():
            keys.add(cfg["snapshot_key"])
            keys.add(table_key)
        keys.update(self.extra_keys)
        return keys


def _adapters() -> dict[str, ResourceAdapter]:
    from cogni_agent.tool_center_service.agent.models import Agent
    from cogni_agent.tool_center_service.skill.models import Skill
    from cogni_agent.tool_center_service.tool.models import Tool

    return {
        "tool": ResourceAdapter(Tool, (
            # v1.2(0051):Tool 与 Skill/Agent 同构 —— 内容键全部纯快照键(extra_keys),
            # 主表内容列已删除(0051,同 skill 0050 / agent 0045):描述/类型/执行配置/
            # 风险分级只存在于草稿/发布快照,随版本发布/回滚;不在 fields 里:主表无列,
            # to_snapshot/apply 均不触碰。未发布/老库无这些键属预期 —— 真正上线的版本,
            # 其发布快照里必有(创建即自动发 v1 / 迁移 0051 物化孤儿,快照都带全键)。
        ), "工具", extra_keys=(
            "description", "type", "type_config", "risk_level",
        )),
        "skill": ResourceAdapter(Skill, (
            # v1.1.10:内容键全部纯快照键(extra_keys) —— 主表列已删除(0050),
            # 同 agent description(0045) 模式:描述/使用说明/约束/模式/知识源只存在于
            # 草稿/发布快照,随版本发布/回滚;不在 fields 里:主表无列,to_snapshot/
            # apply 均不触碰。未发布/老库无这些键属预期 —— 真正上线的版本,
            # 其发布快照里必有(编辑表单路径)。
        ), "技能", extra_keys=(
            "description", "tool_usage", "knowledge_sources", "constraints",
            "suggested_mode", "prompt",
        ), binds={
            "tool_ids": {"snapshot_key": "tools", "item_id": "tool_id", "resource_type": "tool"},
        }),
        "agent": ResourceAdapter(Agent, (
            # v1.1.9:description 是纯版本快照键(extra_keys) —— 主表列已删除(0045),
            # 描述只存在于草稿/发布快照(编辑=草稿、发布冻结),随版本发布/回滚;
            # 不在 fields 里:主表无列,to_snapshot/apply 均不触碰。未发布/老库无
            # description 属预期——真正上线的版本,其发布快照里必有(编辑表单路径)。
            "persona_desc", "tone_style", "opening_line",
            "rate_limit_qps", "rate_limit_daily",
            # 关联推荐追问已删(24号/0056),旧快照中的残留键 apply 时按 fields 过滤自动忽略
            # v1.1.5:意图识别是 agent 级配置,随版本快照一起发布/回滚
            "intent_config",
            # v1.2(19 号 D4-①):内部协作模式(chain/dag/pool),随版本快照发布/回滚;
            # 快照缺键时运行时回退主表列(老 agent 兜底)
            "composition_mode",
        ), "智能体", extra_keys=("description",), binds={
            "bound_skill_ids": {"snapshot_key": "bound_skills", "item_id": "skill_id", "resource_type": "skill"},
        }),
    }


def _adapter(resource_type: str) -> ResourceAdapter:
    ad = _adapters().get(resource_type)
    if ad is None:
        raise ValidationError(f"不支持的资源类型:{resource_type}")
    return ad


def _normalize_binds(snapshot: dict, ad: ResourceAdapter) -> dict:
    """统一绑定键为 pin 格式(新键权威):仅旧键时转新键,新键存在时丢弃旧键。

    旧格式快照(纯 id 数组)只在「没被重新派生/保存过」时原样保留(apply 兼容);
    一旦经过 derive / update 合并就统一转成新格式,避免新旧键并存歧义
    (例如主表 to_snapshot 总带空新键,旧客户端传旧键时两键并存、旧键被空新键吃掉)。
    """
    out = dict(snapshot)
    for table_key, cfg in ad.binds.items():
        snap_key = cfg["snapshot_key"]
        if snap_key in out:
            out.pop(table_key, None)
        elif table_key in out:
            old = out.pop(table_key)
            out[snap_key] = [{cfg["item_id"]: i, "version": None} for i in (old or [])]
    return out


# ── 查询 ────────────────────────────────────────────────────────────────
async def list_versions(db: AsyncSession, resource_type: str, resource_id: str) -> list[dict]:
    rows = (await db.execute(
        select(ResourceVersion)
        .where(ResourceVersion.resource_type == resource_type,
               ResourceVersion.resource_id == resource_id)
        .order_by(ResourceVersion.version.desc().nulls_last())
    )).scalars().all()
    items = [_as_dict(r) for r in rows]
    # agent 版本列表:补 assigned_count —— 该版本被几个指派引用(§5.1)
    if resource_type == "agent":
        assigns = (await db.execute(
            select(ResourceAssignment).where(ResourceAssignment.agent_id == resource_id)
        )).scalars().all()
        counts: dict[str, int] = {}
        for a in assigns:
            counts[a.version_id] = counts.get(a.version_id, 0) + 1
        for it in items:
            it["assigned_count"] = counts.get(it["id"], 0)
    return items


async def get_version(db: AsyncSession, version_id: str) -> ResourceVersion:
    row = await db.get(ResourceVersion, version_id)
    if row is None:
        raise NotFound("版本不存在")
    return row





async def is_resource_available(db: AsyncSession, resource_type: str, resource_id: str) -> tuple[bool, str]:
    """资源当前是否可用。返回 (可用, 原因)。

    **没有任何版本时视为可用** —— 版本化是后加的能力,存量资源没有版本记录,
    不能因为升级就把它们全部停用。只有"曾经有过版本、但现在没有任何 published 的",
    才判定为已下线。
    """
    rows = (await db.execute(
        select(ResourceVersion.status).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id)
    )).scalars().all()
    if not rows:
        return True, ""
    if STATUS_PUBLISHED in rows:
        return True, ""
    return False, f"该{_adapter(resource_type).label}的所有版本均已下线,请先发布一个版本"


# ── 变更 ────────────────────────────────────────────────────────────────
async def snapshot_current(db: AsyncSession, resource_type: str, resource_id: str,
                           *, created_by: str, note: str = "", status: str = STATUS_PUBLISHED) -> ResourceVersion:
    """把主表当前状态存成一个版本(仅 D8-A 迁移「无版本资源补 v1」使用)。

    15 号删默认后**没有**「首版本自动默认」语义:补出的 published 版本只是快照,
    不隐含对任何人生效;内容从主表物化列拷贝(迁移执行时机在 DROP 内容列之前)。
    """
    ad = _adapter(resource_type)
    obj = await db.get(ad.model, resource_id)
    if obj is None:
        raise NotFound(f"{ad.label} {resource_id} 不存在")

    next_no = await _next_version_no(db, resource_type, resource_id)

    row = ResourceVersion(
        resource_type=resource_type, resource_id=resource_id, version=next_no,
        status=status,
        snapshot=await _fill_skill_prompt(db, resource_type, resource_id, ad.to_snapshot(obj)),
        note=note, created_by=created_by, updated_by=created_by,
        published_at=datetime.now().astimezone() if status == STATUS_PUBLISHED else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def derive_version(db: AsyncSession, resource_type: str, resource_id: str,
                         *, base_version_id: str | None, snapshot: dict,
                         created_by: str, note: str = "") -> ResourceVersion:
    """派生一个新的 draft 版本 —— 即打开「编辑区」。

    - **每资源同时最多一个 draft**:已有未发布草稿时拒绝(先发布或放弃);
    - **草稿不预占正式版本号**(version=NULL),版本号在发布时分配,杜绝版本空洞;
    - 记录 base_version_id,UI 可展示「基于 vN 编辑中」。
    """
    existing = (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.status == STATUS_DRAFT,
        )
    )).scalars().all()
    if existing:
        raise Forbidden(
            f"已存在未发布的草稿,请先「发布」或「放弃编辑」后再派生。")

    ad = _adapter(resource_type)
    base_snapshot: dict = {}
    base_id: str | None = None
    if base_version_id:
        base = await get_version(db, base_version_id)
        base_snapshot = dict(base.snapshot or {})
        base_id = base.id
    else:
        obj = await db.get(ad.model, resource_id)
        if obj is not None:
            base_snapshot = await _fill_skill_prompt(
                db, resource_type, resource_id, ad.to_snapshot(obj))

    merged = {**base_snapshot, **_normalize_binds(snapshot or {}, ad)}
    # 只保留该资源类型认可的字段(普通字段 + 绑定新旧键 + 额外键):前端多传的字段直接丢弃,
    # 避免把 id / created_at 这类东西混进快照,回滚时把创建者都改了
    merged = {k: v for k, v in merged.items() if k in ad.accepted_keys()}
    merged = _normalize_binds(merged, ad)

    row = ResourceVersion(
        resource_type=resource_type, resource_id=resource_id,
        version=None,  # 草稿不预占版本号,发布时才分配
        status=STATUS_DRAFT, snapshot=merged, base_version_id=base_id,
        note=note, created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_version(db: AsyncSession, version_id: str, *, snapshot: dict,
                         note: str | None = None, updated_by: str | None = None) -> ResourceVersion:
    """编辑某个版本(「保存」)。

    **只允许 draft(编辑区)**:保存只更新内容,版本号不变、不留痕、不影响任何客户。
    **published 版本不允许直接改** —— 那等于绕过版本管理直接改线上行为,
    没有留痕也没有回滚点。要改已发布的版本,请基于它派生新草稿再改(见 derive_version)。
    """
    row = await get_version(db, version_id)
    if row.status != STATUS_DRAFT:
        raise Forbidden(
            f"v{row.version} 当前状态为「{row.status}」,不能直接编辑。"
            f"请基于它派生一个新草稿再改,改完发布即可,原版本不受影响。")
    ad = _adapter(row.resource_type)
    merged = {**(row.snapshot or {}), **_normalize_binds(snapshot or {}, ad)}
    merged = {k: v for k, v in merged.items() if k in ad.accepted_keys()}
    row.snapshot = _normalize_binds(merged, ad)
    if note is not None:
        row.note = note
    if updated_by:
        row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


# ── 依赖锁 / 发布预检 / skill prompt 入快照(推荐方案) ─────────────────────
async def latest_draft(db: AsyncSession, resource_type: str, resource_id: str
                       ) -> ResourceVersion | None:
    """该资源当前未发布的草稿(无则 None)。旧路径直改主表时用它同步草稿快照。"""
    return (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.status == STATUS_DRAFT)
        .order_by(ResourceVersion.created_at.desc()).limit(1)
    )).scalars().first()


async def _current_prompt(db: AsyncSession, skill_id: str) -> str | None:
    """tc_prompts 里最新一条 prompt(旧发布链路物化产物,仅草稿派生时兜底参考;
    15 号 D8-A 停用该表后由版本快照内 prompt 字段接管)。"""
    from cogni_agent.tool_center_service.skill.models import Prompt
    row = (await db.execute(
        select(Prompt).where(Prompt.skill_id == skill_id)
        .order_by(Prompt.created_at.desc()).limit(1)
    )).scalars().first()
    return row.content if row is not None else None


async def _replace_prompt(db: AsyncSession, skill_id: str, content: str) -> None:
    """把快照里的 prompt 写回 tc_prompts(物化主表语义)。"""
    from cogni_agent.tool_center_service.skill.models import Prompt
    await db.execute(delete(Prompt).where(Prompt.skill_id == skill_id))
    db.add(Prompt(skill_id=skill_id, content=content))


async def _fill_skill_prompt(db: AsyncSession, resource_type: str, resource_id: str,
                             snapshot: dict) -> dict:
    """skill 快照补 prompt 全文(推荐方案:prompt 入快照,运行时从快照取)。"""
    if resource_type == "skill":
        prompt = await _current_prompt(db, resource_id)
        snapshot["prompt"] = prompt or ""
    return snapshot


async def _resolve_pin_target(db: AsyncSession, res_type: str, res_id: str,
                              ver_no: int | None) -> tuple[int | None, dict]:
    """解析某个绑定 pin 的目标版本,返回 (固化版本号, 详情)。

    - ver_no 有值:该版本必须已发布(published),否则 (None, 原因) 预检拦截;
    - ver_no 为 None(「跟随最新」):固化为该资源**当前最新的已发布版本**;
      一版都没发布过才拦截。
    - 都不满足 -> (None, 原因),发布被拦截。

    **为什么从「一律拦截」改成「固化最新已发布」**:

    删掉默认版本之后,这里一度改成 fail-closed —— 未指定版本一律拦。但那条规则和
    运行时对不上:解析层早就把未 pin 的绑定解析到最新已发布版(source=latest_published),
    于是出现一种谁也想不到的状态 —— **同一个技能,跑得动,却发不出去**。
    运营看到的是四条「默认版本已下线(15 号)」,而他从没设过什么默认版本,
    那个括号里的日期对他更是毫无意义。

    而且「跟随最新」本身是个合理诉求:工具修了个 bug 发了新版,绑它的十几个技能
    不该为此每个都进去重新选一遍版本 —— 那正是多版本在线要解决的事。
    要钉死版本的人显式选版即可,两种都支持。

    固化仍然发生在**发布这一刻**:快照里记下的是当时的版本号,之后工具再发新版
    也不会改动已发布的技能快照(锁依赖的意义就在这里)。
    """
    ad = _adapter(res_type)
    obj = await db.get(ad.model, res_id)
    name = (getattr(obj, "name", "") or res_id) if obj is not None else res_id

    if ver_no is not None:
        v = (await db.execute(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == res_type,
                ResourceVersion.resource_id == res_id,
                ResourceVersion.version == ver_no)
        )).scalars().first()
        if v is None:
            return None, {"name": name, "reason": f"版本 v{ver_no} 不存在", "status": "missing"}
        if v.status != STATUS_PUBLISHED:
            return None, {"name": name,
                          "reason": f"v{ver_no} 状态为「{v.status}」,需先发布",
                          "status": v.status}
        return ver_no, {"name": name, "status": STATUS_PUBLISHED}

    # 「跟随最新」:固化为当前最新的已发布版本(与运行时解析同一口径)
    if obj is None:
        return None, {"name": name, "reason": f"「{name}」不存在,请先创建并发布",
                      "status": "missing"}
    latest = (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == res_type,
            ResourceVersion.resource_id == res_id,
            ResourceVersion.status == STATUS_PUBLISHED)
        .order_by(ResourceVersion.version.desc()).limit(1))).scalars().first()
    if latest is None:
        # 真正该拦的只有这一种:一版都没发布过,跑起来也拿不到任何快照。
        # 话要说清「去哪儿发」—— 只说「不可用」的话,运营会以为是绑定填错了。
        return None, {"name": name, "status": "unpublished",
                      "reason": f"{ad.label}「{name}」还没有任何已发布版本。"
                                f"请先到它的详情页发布一版,或在绑定处改选一个已发布版本"}
    return latest.version, {"name": name, "status": STATUS_PUBLISHED,
                            "resolved_from": "latest_published"}


async def _bound_snapshot(db: AsyncSession, res_type: str, res_id: str,
                          ver_no: int | None) -> dict | None:
    """取绑定资源的快照(递归闭包用):有版本号取该版本快照,否则取主表。"""
    if ver_no is not None:
        v = (await db.execute(
            select(ResourceVersion).where(
                ResourceVersion.resource_type == res_type,
                ResourceVersion.resource_id == res_id,
                ResourceVersion.version == ver_no)
        )).scalars().first()
        if v is not None:
            return dict(v.snapshot or {})
        return None
    ad = _adapter(res_type)
    obj = await db.get(ad.model, res_id)
    if obj is None:
        return None
    return await _fill_skill_prompt(db, res_type, res_id, ad.to_snapshot(obj))


async def _resolve_locked_deps(db: AsyncSession, row: ResourceVersion
                               ) -> tuple[dict, list[dict], list[str]]:
    """发布预检 + 生成依赖锁。

    遍历快照的绑定 pin(agent->skills / skill->tools),逐层解析:
    - 固化 version:null 的模糊 pin 为绑定资源默认版本号;
    - 递归展开 skill 的 tool 闭包(agent -> skill -> tool);
    - 任何绑定资源不可用(DRAFT/不存在/已下线) -> 收集问题,发布被拦截。

    返回 (新快照, 依赖闭包列表, 问题清单)。
    """
    ad = _adapter(row.resource_type)
    snap = dict(row.snapshot or {})
    problems: list[str] = []
    deps: list[dict] = []

    async def _walk(res_type: str, res_id: str, snapshot: dict) -> list[str]:
        sub_problems: list[str] = []
        sub = _adapter(res_type)
        for table_key, cfg in sub.binds.items():
            pins = snapshot.get(cfg["snapshot_key"]) or []
            new_pins: list[dict] = []
            for p in pins:
                if not isinstance(p, dict):
                    continue
                rid = p.get(cfg["item_id"])
                if not rid:
                    continue
                down_type = cfg["resource_type"]
                resolved, info = await _resolve_pin_target(db, down_type, rid, p.get("version"))
                if resolved is None:
                    # Phase 2:主表兜底已删,任何解析失败(无版本/未发布/已下线)都拦截
                    sub_problems.append(f"{_adapter(down_type).label}「{info['name']}」"
                                        f"({info.get('reason') or info.get('status')})")
                    new_pins.append(p)   # 保留原 pin,发布被拦截
                    continue
                new_pins.append({**p, "version": resolved})
                if not any(d["resource_type"] == down_type and d["resource_id"] == rid
                           and d["version"] == resolved for d in deps):
                    deps.append({"resource_type": down_type, "resource_id": rid,
                                 "name": info["name"], "version": resolved,
                                 "status": info["status"]})
                # 递归:skill 的 tool 闭包
                if down_type == "skill":
                    child = await _bound_snapshot(db, down_type, rid, resolved)
                    if child is not None:
                        sub_problems.extend(await _walk(down_type, rid, child))
            snapshot[cfg["snapshot_key"]] = new_pins
        return sub_problems

    problems = await _walk(row.resource_type, row.resource_id, snap)
    return snap, deps, problems


def _require_skill_quality(snap: dict) -> None:
    """技能发布质量校验:description(能力概述)与 constraints(业务约束)不得为空。

    v1.1.3 平台治理:空壳技能(description 为空 或 constraints={})发布后,Triager
    能力清单里既没有语义描述可路由、也没有约束可依,路由与执行全靠猜 —— 直接把
    问题暴露在发布环节,而不是让运行时悄悄降级。校验规则对全平台所有技能生效,
    不特判任何技能名(框架级通用)。

    校验对象 = **待发布内容(草稿快照)**而非主表对象:收敛双轨后编辑全落草稿快照,
    主表内容列(description/constraints)只在创建时写一次、之后不再更新 —— 读主表
    会误拦(草稿已补全但主表仍是创建时空值)或漏拦(主表有创建初值但草稿已清空),
    必须与「发布即冻结草稿快照」的语义对齐。
    """
    missing: list[str] = []
    if not (snap.get("description") or "").strip():
        missing.append("description(能力概述)")
    if not snap.get("constraints"):
        missing.append("constraints(业务约束,如 {\"max_steps\": 40, \"缺参时\": \"反问用户\"})")
    if missing:
        raise ValidationError(
            f"技能缺少发布必需内容:{'、'.join(missing)}。"
            f"请先在管理后台补充完整再发布(描述驱动路由依赖这两项做决策)。"
        )


async def precheck_version(db: AsyncSession, version_id: str) -> dict:
    """发布预检(dry-run):不改变任何状态,返回依赖闭包与问题清单。

    供前端「发布预检」按钮调用,发布前给管理员预览依赖树;发布流程本身也会
    内部调用同一套解析,双重保险。
    """
    row = await get_version(db, version_id)
    if row.status != STATUS_DRAFT:
        raise Forbidden(f"只有草稿可以预检(当前状态为「{row.status}」)")
    new_snapshot, deps, problems = await _resolve_locked_deps(db, row)
    return {
        "ok": True,
        "version_id": row.id,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "will_version": await _next_version_no(db, row.resource_type, row.resource_id),
        "deps": deps,
        "problems": problems,
        "pass": not problems,
        "snapshot": new_snapshot,
    }


async def publish_version(db: AsyncSession, version_id: str, *, updated_by: str | None = None) -> dict:
    """发布草稿:draft -> published,快照冻结。

    推荐方案的发布语义:
    1. **发布预检**:对绑定资源做全链路解析(agent->skill->tool),`version:null` 的模糊
       pin 固化为绑定资源默认版本号;任何绑定资源处于 DRAFT / 不存在 / 已下线 -> **拦截发布**;
    2. **版本号在发布时分配**(max(published)+1),草稿不再预占号;
    3. 生成 **locked_deps 依赖锁**,冻结完整依赖闭包(运行时严格按锁解析);
    4. **不写主表、不设默认、不隐含对任何人生效**(15 号:默认版本概念已删除,
       "发布=写回主表全网生效"旧语义废弃);发布成功但零 user 指派时返回黄条 hint
       (assigned_users=0),提醒发布者去「分发中心」按用户指派,否则该版本 fail-closed;
    5. 返回 hint / assigned_users 供调用方提示分发。
    """
    row = await get_version(db, version_id)
    if row.status != STATUS_DRAFT:
        raise Forbidden(
            f"只有草稿版本可以发布(当前状态为「{row.status}」)"
            if row.version is None else
            f"只有草稿版本可以发布(v{row.version} 当前状态为「{row.status}」)")
    ad = _adapter(row.resource_type)
    obj = await db.get(ad.model, row.resource_id)
    if obj is None:
        raise NotFound(f"{ad.label} {row.resource_id} 不存在")

    # v1.1.3:C 层平台治理 —— 空壳技能不得发布。description 是描述驱动路由(Triager
    # 能力清单)的唯一决策依据、constraints 承载业务约束(缺参行为/数据口径/禁止编造),
    # 两者皆空的技能发布后路由与执行全靠猜(conv_d23a24de 事故:参数解析 constraints={}
    # 且 description 不带依赖声明,Triager 判了缺失却不知可挂载补齐技能)。发布即拦截、
    # 提示补齐,而不是让运行时悄悄降级。草稿保存不拦,编辑自由、发布质检。
    # 校验读 row.snapshot(待发布的草稿内容):主表内容列是创建初值不随编辑更新,
    # 读主表会误拦/漏拦(与 _require_skill_quality 修订配套)。
    if row.resource_type == "skill":
        _require_skill_quality(row.snapshot or {})

    # 1) 发布预检 + 依赖锁:固化模糊 pin,拦截 DRAFT/不存在/已下线
    new_snapshot, deps, problems = await _resolve_locked_deps(db, row)
    if problems:
        detail = "；".join(problems[:20])
        if len(problems) > 20:
            detail += f" 等共 {len(problems)} 项"
        raise ValidationError(
            f"发布前预检未通过,存在不可用的依赖:{detail}。"
            f"请先处理后再发布。")

    # 2) 分配正式版本号(发布时,只数 published —— 草稿不占号,无空洞)
    row.version = await _next_version_no(db, row.resource_type, row.resource_id)
    # 3) 固化 pin 后的快照 + 依赖锁
    row.snapshot = new_snapshot
    row.locked_deps = {"deps": deps, "frozen_at": datetime.now().astimezone().isoformat()}

    # 4) 发布(15 号:不再有任何「自动设为默认 / 物化主表」分支 —— 发布只冻结快照,
    #    不隐含对任何人生效;由 D7-A 提醒发布者去分发)
    row.status = STATUS_PUBLISHED
    row.published_at = datetime.now().astimezone()
    row.offlined_at = None
    if updated_by:
        row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    log.info(f"{ad.label} {row.resource_id} 发布版本 v{row.version}(deps={len(deps)})")

    # D7-A:发布成功但零 user 指派 -> 黄条提醒去分发,防止漏配导致 fail-closed 误伤。
    # 15 号语义:仅 agent 进用户指派体系(skill/tool 恒 0 且无独立指派,由绑定它的
    # agent 间接生效),文案按资源类型区分,避免 skill/tool 发布时误引导去指派。
    assigned = await _assigned_user_count(db, row.resource_type, row.resource_id)
    if assigned == 0:
        if row.resource_type == "agent":
            hint = (f"v{row.version} 已发布。当前无任何用户被指派,该版本对所有人不可用;"
                    f"请到「分发中心-按用户/按 Agent」为需要的用户指派。")
        elif row.resource_type == "skill":
            # 0074:技能也能独立分发(APP 可直接按 skill_id 提问)。未指派时它仍可
            # 通过绑定它的 Agent 间接生效,所以这里是提示而不是警告。
            hint = (f"v{row.version} 已发布。当前无用户被直接指派到本技能 ——"
                    f"可在「分发中心-按 Skill」按用户指派(APP 可直接调用该技能),"
                    f"或由绑定它的 Agent 指派后间接生效。")
        else:
            hint = (f"v{row.version} 已发布。{ad.label} 无需直接指派,"
                    f"由绑定它的 Agent 在分发中心按用户指派后间接生效。")
    else:
        hint = f"v{row.version} 已发布(当前 {assigned} 位用户已指派到此{ad.label})。"
    return {"ok": True, "version": row.version, "status": STATUS_PUBLISHED,
            "hint": hint, "assigned_users": assigned,
            "locked_deps": row.locked_deps}


async def _assigned_user_count(db: AsyncSession, resource_type: str, resource_id: str) -> int:
    """该资源当前被指派到具体版本的 user 数量(0074 起 agent 与 skill 都有;tool 恒 0)。"""
    if resource_type not in ASSIGNABLE_TYPES:
        return 0
    ad = _adapters().get(resource_type)
    if ad is None or (await db.get(ad.model, resource_id)) is None:
        return 0
    return len((await db.execute(
        select(ResourceAssignment.id).where(
            ResourceAssignment.resource_type == resource_type,
            ResourceAssignment.agent_id == resource_id,
            ResourceAssignment.scope_type == SCOPE_USER,
        )
    )).scalars().all())


async def offline_version(db: AsyncSession, version_id: str, *, updated_by: str | None = None) -> dict:
    """下线某个 published 版本。

    仅影响该版本自己;**被指派 / 被 pin / 是默认版本 -> 禁止下线**(必须先解除引用或切换默认)。
    全部 published 下线后,该资源才整体不可用(不会自动回退到别的版本)。
    """
    row = await get_version(db, version_id)
    ad = _adapter(row.resource_type)
    if row.status == STATUS_OFFLINE:
        raise ValidationError(f"v{row.version} 已处于下线状态")
    if row.status != STATUS_PUBLISHED:
        raise Forbidden(f"只有已发布的版本可以下线(v{row.version} 当前状态为「{row.status}」)")

    # (15 号 D1-B:「默认版本不可下线」拦截已删除 —— 无默认概念;下面只保留引用拦截)

    refs = await referenced_by(db, row)
    # 两类引用要区别对待,混在一起处理必然错一头:
    #
    # - **用户指派**:只是「谁能用这一版」。下线的意思本来就是「这一版不再对外」,
    #   顺手收回权限正是运营的本意 —— 让他先一个个去解指派再回来下线,
    #   中间那段时间版本还在线上跑,而他以为已经下了。
    # - **被别的已发布版本 pin**:那是另一个 Agent/技能**正在执行的内容**。
    #   自动解掉等于悄悄改了别人 Agent 的行为,而且改完不报错 ——
    #   下一次有人用那个 Agent,跑的已经不是他验收过的东西了。这种必须拦。
    #
    # 草稿/已下线版本上的 pin 不拦:它们本来就没在跑,拦了只会让人卡在一条
    # 早就作废的引用上,而那条引用他多半根本找不到在哪。
    blocking = [r for r in refs
                if r.get("kind") != "assignment" and r.get("status") == STATUS_PUBLISHED]
    if blocking:
        detail = "; ".join(r["note"] for r in blocking[:5])
        raise Forbidden(
            f"v{row.version} 正被 {len(blocking)} 个**已发布**的上层版本绑定,不能下线 —— "
            f"直接下线会改掉它们正在执行的内容。请先在那边改绑到别的版本:{detail}")

    released = [r for r in refs if r.get("kind") == "assignment"]
    if released:
        await db.execute(
            delete(ResourceAssignment).where(ResourceAssignment.version_id == row.id))

    row.status = STATUS_OFFLINE
    row.offlined_at = datetime.now().astimezone()
    if updated_by:
        row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)

    still = await _has_published(db, row.resource_type, row.resource_id)
    parts: list[str] = []
    if released:
        who = "、".join(r.get("user_label") or r.get("scope_id") or "?" for r in released[:5])
        more = f" 等 {len(released)} 人" if len(released) > 5 else ""
        # 说清**收回了谁的权限**。只说「已解除 3 处指派」的话,被收回的人来问
        # 「我为什么用不了了」时,运营翻不出是哪一次操作动的他
        parts.append(f"已同时解除 {len(released)} 处用户指派并收回权限:{who}{more}。"
                     f"这些用户将无法再使用本版本" + ("" if still else ""))
    if not still:
        parts.append(f"该{ad.label}的所有版本均已下线,当前不可用,请发布一个版本。")
    log.info(f"{ad.label} {row.resource_id} v{row.version} 下线"
             f"(still_available={still}, released={len(released)})")
    return {"ok": True, "was_active": True, "hint": " ".join(parts),
            "released_assignments": [
                {"scope_id": r.get("scope_id"), "user_label": r.get("user_label")}
                for r in released]}


async def rollback_to(db: AsyncSession, version_id: str, *, updated_by: str | None = None) -> dict:
    """回滚(15 号重定义:无「默认」可回,回滚 = 把离线历史版本重新发布为在线快照)。

    - 目标 offline:重新发布(offline -> published),可重新被指派/pin 消费;
    - 目标 published:无需回滚,直接提示;
    - 目标 draft:禁止(草稿不是历史版本);
    - 内容回滚(想"回到旧版内容")= 基于历史版本派生草稿再发布,见 routes derive。
    """
    row = await get_version(db, version_id)
    ad = _adapter(row.resource_type)
    if row.status == STATUS_DRAFT:
        raise Forbidden(f"草稿尚未发布,不能回滚。请先发布或放弃编辑。")
    if row.status == STATUS_PUBLISHED:
        return {"ok": True, "version": row.version,
                "message": f"v{row.version} 已是发布状态,无需回滚。"
                           f"如需内容回到更早版本,请基于目标版本新建草稿后再发布。"}
    # offline -> published 重新发布
    row.status = STATUS_PUBLISHED
    row.published_at = datetime.now().astimezone()
    row.offlined_at = None
    if updated_by:
        row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    log.info(f"{ad.label} {row.resource_id} v{row.version} 重新发布(rollback_to)")
    return {"ok": True, "version": row.version,
            "message": f"v{row.version} 已重新发布。它现在可被重新指派/绑定;"
                       f"15 号起不再有任何「设为默认」语义。"}


async def discard_draft(db: AsyncSession, version_id: str) -> dict:
    """放弃编辑:丢弃 draft(编辑区),删除草稿行 —— 草稿不留痕,直接删除。"""
    row = await get_version(db, version_id)
    if row.status != STATUS_DRAFT:
        raise Forbidden(
            f"只有草稿可以放弃(当前状态为「{row.status}」)"
            if row.version is None else
            f"只有草稿可以放弃(v{row.version} 当前状态为「{row.status}」)")
    ad = _adapter(row.resource_type)
    await db.delete(row)
    await db.commit()
    log.info(f"{ad.label} {row.resource_id} 放弃草稿(version={row.version})")
    return {"ok": True, "message": f"已放弃草稿的编辑。"}


# ── pin 辅助:绑定版本下拉 + 一键刷新依赖 ────────────────────────────────
async def latest_published_version(db: AsyncSession, resource_type: str,
                                   resource_id: str) -> ResourceVersion | None:
    """该资源最新 published 版本(按版本号倒序)。「刷新到最新依赖」用它。"""
    return (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.status == STATUS_PUBLISHED,
        ).order_by(ResourceVersion.version.desc())
    )).scalars().first()


async def latest_published_snapshots(db: AsyncSession, resource_type: str,
                                     resource_ids: list[str]) -> dict[str, dict]:
    """批量:各资源「最新已发布版本」的快照(无任何 published 版本的资源不在结果里)。

    管理列表 / 网关读取「当前线上内容」时统一用它,一次 in_ 查询避免逐条 N+1。
    快照里缺失的键(老版本发布于字段加入版本管理之前)由调用方回退主表初值。
    """
    if not resource_ids:
        return {}
    rows = (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id.in_(resource_ids),
            ResourceVersion.status == STATUS_PUBLISHED,
        ).order_by(ResourceVersion.version.desc())
    )).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        if r.resource_id not in out:  # version desc → 首见即最新
            out[r.resource_id] = r.snapshot or {}
    return out


# ── 指派链版本锚点(27 号:固定问题的技能版本严格随用户指派,fail-closed) ──────

async def published_version_by_no(db: AsyncSession, resource_type: str,
                                  resource_id: str, version_no: int) -> ResourceVersion | None:
    """按版本号取该资源 published 版本行(不存在/非 published → None)。"""
    return (await db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.version == version_no,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
    )).scalars().first()


async def assigned_agent_version(db: AsyncSession, agent_id: str,
                                 user_id: str | None) -> ResourceVersion | None:
    """用户对某 agent 的指派版本(15 号 D9-A:无指派 → None = fail-closed,无默认回落)。"""
    if not user_id:
        return None
    a = (await db.execute(
        select(ResourceAssignment).where(
            ResourceAssignment.resource_type == "agent",
            ResourceAssignment.agent_id == agent_id,
            ResourceAssignment.scope_type == "user",
            ResourceAssignment.scope_id == user_id,
        )
    )).scalars().first()
    if a is None:
        return None
    return await get_version(db, a.version_id)


async def assigned_skill_version(db: AsyncSession, agent_id: str, user_id: str | None,
                                 skill_id: str) -> int | None:
    """技能版本锚点:先看**技能自己的用户指派**,再退到该 agent 指派版本快照里的 pin。

    0074 起技能可以独立分发 —— 那条指派是「这个人用这个技能的哪一版」的直接答案,
    比绕着 agent 快照找 pin 更准(也支持根本没给 agent 分发、只给了技能的场景)。
    两处都没有 → None(fail-closed:调用方降级或报错,绝不回退「最新 published」)。
    """
    direct = await assigned_version(db, "skill", skill_id, user_id)
    if direct is not None:
        return direct.version
    if not agent_id:
        return None
    ver = await assigned_agent_version(db, agent_id, user_id)
    if ver is None:
        return None
    binds = (ver.snapshot or {}).get("bound_skills")
    if not isinstance(binds, list):
        return None
    for p in binds:
        if isinstance(p, dict) and p.get("skill_id") == skill_id:
            return p.get("version")
    return None


async def scenic_pinned_skill_versions(db: AsyncSession, agent_id: str, scenic_id: str,
                                       skill_id: str) -> set[int]:
    """该景区 active 用户对该 agent 的指派版本中,该技能 pin 的去重集合(预热派单用)。

    景区用户指派版本一致(常态)→ 单元素;版本分流 → 多元素各派一行;
    无任何指派 → 空集(该景区该问题不可用,不派)。
    """
    from cogni_agent.user_center_service.models import User
    from sqlalchemy import distinct

    rows = (await db.execute(
        select(ResourceAssignment.version_id).join(
            User, User.id == ResourceAssignment.scope_id).where(
            ResourceAssignment.resource_type == "agent",
            ResourceAssignment.agent_id == agent_id,
            ResourceAssignment.scope_type == "user",
            User.scenic_id == scenic_id,
            User.is_active.is_(True),
        ).distinct()
    )).scalars().all()
    out: set[int] = set()
    for vid in rows:
        ver = await get_version(db, vid)
        binds = (ver.snapshot or {}).get("bound_skills") if ver else None
        if not isinstance(binds, list):
            continue
        for p in binds:
            if isinstance(p, dict) and p.get("skill_id") == skill_id \
                    and isinstance(p.get("version"), int):
                out.add(p["version"])
    return out


def _bind_ids(snapshot: dict, ad: ResourceAdapter, table_key: str) -> list[str]:
    """从快照取绑定资源的 id 列表:优先新 pin 格式,兼容旧 id 数组格式。"""
    cfg = ad.binds[table_key]
    pins = snapshot.get(cfg["snapshot_key"])
    if pins is None:
        return list(snapshot.get(table_key) or [])
    return [p[cfg["item_id"]] for p in pins if isinstance(p, dict) and p.get(cfg["item_id"])]


async def refresh_pins(db: AsyncSession, version_id: str) -> dict:
    """一键刷新依赖:把 draft 快照里所有绑定 pin 更新为对应资源的最新 published 版本。

    - 只允许 draft —— published 快照已冻结,不可变;
    - 绑定资源无任何 published 时保留原 pin 并提示(不静默清掉);
    - 旧格式快照(纯 id 数组)先转为新格式再刷新。
    """
    row = await get_version(db, version_id)
    if row.status != STATUS_DRAFT:
        raise Forbidden(
            f"只有草稿可以刷新依赖(v{row.version} 当前状态为「{row.status}」,快照已冻结)。"
            f"请基于它派生新草稿后再刷新。")
    ad = _adapter(row.resource_type)
    snap = dict(row.snapshot or {})
    changed: list[str] = []
    for table_key, cfg in ad.binds.items():
        snap_key = cfg["snapshot_key"]
        pins = snap.get(snap_key)
        if pins is None:
            pins = [{cfg["item_id"]: i, "version": None} for i in (snap.get(table_key) or [])]
        new_pins: list[dict] = []
        for p in pins:
            if not isinstance(p, dict):
                continue
            rid = p.get(cfg["item_id"])
            if not rid:
                continue
            latest = await latest_published_version(db, cfg["resource_type"], rid)
            if latest is None:
                new_pins.append(p)
                changed.append(f"{cfg['resource_type']} {rid}:无已发布版本,保持原 pin")
                continue
            new_pins.append({**p, "version": latest.version})
            if latest.version != p.get("version"):
                changed.append(
                    f"{cfg['resource_type']} {rid}:v{p.get('version')} -> v{latest.version}")
        snap[snap_key] = new_pins
    row.snapshot = {k: v for k, v in snap.items() if k in ad.accepted_keys()}
    await db.commit()
    await db.refresh(row)
    log.info(f"{ad.label} {row.resource_id} 刷新依赖 pins:version={row.version}")
    return {"ok": True, "version": row.version, "snapshot": row.snapshot, "changed": changed}


async def pin_options(db: AsyncSession, resource_type: str, resource_id: str,
                      *, version_id: str | None = None) -> dict:
    """编辑区版本下拉数据:每个绑定资源 + 其全部版本(draft/published 都可选,标注状态)。

    v1.1.5 版本规则:未发布的都可以被选到(编辑/预览/联调不限制),只有已发布的
    才能在生产生效、才能被钉版本 pin —— 因此这里放开 draft 版本展示并标注 status;
    「生效/pin」侧的门禁(publish 预检拦 DRAFT 依赖、指派只允许 published)保持不变。

    - `version_id` 有值:从该版本快照取绑定列表(基于任意版本编辑的场景);
    - 否则从主表当前绑定取(存量资源直读场景;D8-A 瘦身后改为取最新 draft 快照)。
    """
    ad = _adapter(resource_type)
    if version_id:
        ver = await get_version(db, version_id)
        snap = dict(ver.snapshot or {})
    else:
        obj = await db.get(ad.model, resource_id)
        snap = ad.to_snapshot(obj) if obj is not None else {}

    bindings: list[dict] = []
    for table_key in ad.binds:
        cfg = ad.binds[table_key]
        down_model = _adapter(cfg["resource_type"]).model
        for rid in _bind_ids(snap, ad, table_key):
            bind_obj = await db.get(down_model, rid)
            # v1.1.5:放开未发布版本可选 —— 编辑区把 draft/published 全部列出并标注 status,
            # 运营可预览/联调未发布版本;只有已发布才能生产生效/pin(门禁在 publish 预检与
            # 指派校验,这里不设限)。draft 无 version 号排最前,前端按 status 展示「未发布」。
            versions = (await db.execute(
                select(ResourceVersion).where(
                    ResourceVersion.resource_type == cfg["resource_type"],
                    ResourceVersion.resource_id == rid,
                ).order_by(ResourceVersion.version.desc().nulls_last())
            )).scalars().all()
            bindings.append({
                "resource_type": cfg["resource_type"],
                "resource_id": rid,
                "name": getattr(bind_obj, "name", rid) if bind_obj else rid,
                "versions": [{"version": v.version, "version_id": v.id, "status": v.status}
                             for v in versions],
            })
    return {"bindings": bindings}


async def referenced_by(db: AsyncSession, row: ResourceVersion) -> list[dict]:
    """扫描引用某版本的所有位置:上层资源版本快照的 pin + 版本指派。

    返回 [{"resource_type", "resource_id", "version", "status", "note"}]
    """
    refs: list[dict] = []

    # 1) Agent 版本快照 pin 了某个 skill 版本
    if row.resource_type == "skill":
        agents = (await db.execute(
            select(ResourceVersion).where(ResourceVersion.resource_type == "agent")
        )).scalars().all()
        for av in agents:
            for pin in (av.snapshot or {}).get("bound_skills", []) or []:
                if isinstance(pin, dict) and pin.get("skill_id") == row.resource_id \
                        and pin.get("version") == row.version:
                    refs.append({"resource_type": "agent", "resource_id": av.resource_id,
                                 "version": av.version, "status": av.status,
                                 "note": f"Agent {av.resource_id} v{av.version} 绑定了本版本"})

    # 2) Skill 版本快照 pin 了某个 tool 版本
    if row.resource_type == "tool":
        skills = (await db.execute(
            select(ResourceVersion).where(ResourceVersion.resource_type == "skill")
        )).scalars().all()
        for sv in skills:
            for pin in (sv.snapshot or {}).get("tools", []) or []:
                if isinstance(pin, dict) and pin.get("tool_id") == row.resource_id \
                        and pin.get("version") == row.version:
                    refs.append({"resource_type": "skill", "resource_id": sv.resource_id,
                                 "version": sv.version, "status": sv.status,
                                 "note": f"Skill {sv.resource_id} v{sv.version} 绑定了本版本"})

    # 3) 版本指派(0074 起 agent 与 skill 都可能被直接指派)
    if row.resource_type in ASSIGNABLE_TYPES:
        assigns = (await db.execute(
            select(ResourceAssignment).where(ResourceAssignment.version_id == row.id)
        )).scalars().all()
        # 名字一次查出来。原先这里直接把 scope_id 拼进提示里,于是运营看到的是
        # 「用户指派(user:1898973f-2a0b-…)」—— 那串 id 他查不到是谁,
        # 而这条提示的**全部作用**就是告诉他「去把谁的指派解掉」。
        names = await _user_labels(db, [a.scope_id for a in assigns])
        for a in assigns:
            refs.append({"resource_type": a.resource_type, "resource_id": a.agent_id,
                         "version": None, "status": None,
                         "scope_id": a.scope_id, "user_label": names.get(a.scope_id, ""),
                         "kind": "assignment",
                         "note": f"用户指派({names.get(a.scope_id) or a.scope_id})"})

    return refs


async def _user_labels(db: AsyncSession, user_ids) -> dict[str, str]:
    """user_id → 「姓名(手机号)」。查不到的返回空,由调用方回落 id。

    查不到时回落 id 而不是写「未知用户」:那串 id 至少还能拿去数据库里查,
    而「未知用户」什么线索都不给。
    """
    from cogni_agent.user_center_service.models import User

    ids = list(dict.fromkeys(u for u in user_ids if u))
    if not ids:
        return {}
    rows = (await db.execute(select(User.id, User.name, User.phone, User.account)
                             .where(User.id.in_(ids)))).all()
    out: dict[str, str] = {}
    for uid, name, phone, account in rows:
        who = (name or account or "").strip()
        tail = (phone or "").strip()
        out[uid] = f"{who}({tail})" if who and tail else (who or tail)
    return out


# ── 版本指派:用户(user)钉到 agent 的某个 published 版本 ──
# 15 号 D9-A:scenic 维度下线,只留 user;global 已于 13 号 Phase 4 G6 收敛。
# 无 user 指派 = fail-closed(agent_not_distributed),无任何隐式兜底。
SCOPE_TYPES = ("user",)


def _scope_desc(scope_type: str, scope_id: str) -> str:
    return f"{scope_type}:{scope_id}"


async def _user_desc(db: AsyncSession, scope_id: str) -> str:
    """提示文案里的用户:「用户 姓名(账号)」。

    原先直接拼 `user:<uuid>`,管理员在通知里根本认不出指派给了谁;
    查不到用户(已删除等)时才退回 ID,保证文案永远可定位。
    """
    from cogni_agent.user_center_service.models import User  # 延迟导入避免循环
    u = await db.get(User, scope_id) if scope_id else None
    if u is None:
        return f"用户 {scope_id}"
    name = (u.name or "").strip() or scope_id
    return f"用户「{name}」({u.account})" if u.account else f"用户「{name}」"


def _res_desc(label: str, res: Any, resource_id: str) -> str:
    """提示文案里的资源:「技能「we」」/「智能体「xx」」,没有名字才退回 ID。"""
    name = (getattr(res, "name", "") or "").strip() if res is not None else ""
    return f"{label}「{name}」" if name else f"{label} {resource_id}"


async def _assignment_dict(db: AsyncSession, row: ResourceAssignment) -> dict:
    ver_row = await db.get(ResourceVersion, row.version_id)
    return {
        # resource_type / resource_id 是 0074 后的正式字段;agent_id 保留是为了
        # 不打断存量前端(agent 指派时两者同值),新代码一律读 resource_id。
        "id": row.id, "resource_type": row.resource_type, "resource_id": row.agent_id,
        "agent_id": row.agent_id, "scope_type": row.scope_type,
        "scope_id": row.scope_id, "version_id": row.version_id,
        "version": ver_row.version if ver_row else None,
        "version_status": ver_row.status if ver_row else None,
        "note": row.note, "created_by": row.created_by,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


async def _check_scope_visibility(db: AsyncSession, agent: Any, version: ResourceVersion,
                                  scope_id: str) -> None:
    """目标版本对该 user 是否可指派(15 号 D9-A 后唯一 scope 是 user)。

    用**目标版本快照的 visibility** 判定(快照缺省时回退主表;name/visibility 已于
    15 号移出版本化定义,此处仅对历史快照兼容):
    - public/group:任意 user 可指派(scenic 维度下线,group 不再有「景区对象」可比对);
    - scenic:只能指派给该 agent 所属景区内的 user(需要 user.scenic_id 归属核对);
    - private:个人私有,只能指派给创建者本人。
    """
    vis = (version.snapshot or {}).get("visibility") or getattr(agent, "visibility", "public")
    if vis in ("public", "group"):
        return
    if vis == "scenic":
        if scope_id == agent.owner_user_id:  # 创建者本人必属本景区
            return
        from cogni_agent.user_center_service.models import User  # 延迟导入避免循环
        u = (await db.get(User, scope_id))
        if u is None or getattr(u, "scenic_id", None) != agent.scenic_id:
            raise Forbidden("目标版本可见范围是 scenic(景区内),只能指派给本景区的用户")
        return
    if vis == "private":
        if scope_id != agent.owner_user_id:
            raise Forbidden("目标版本可见范围是 private(个人私有),只能指派给创建者本人")
        return


def _assert_assignable(resource_type: str) -> str:
    rt = (resource_type or "agent").lower()
    if rt not in ASSIGNABLE_TYPES:
        raise ValidationError(f"{rt} 不支持按用户分发(仅 {'/'.join(ASSIGNABLE_TYPES)})")
    return rt


async def list_assignments(db: AsyncSession, resource_id: str,
                           resource_type: str = "agent") -> dict:
    """指派列表:该资源的所有指派(scope / 版本 / 时间 / 操作人),新的在前。"""
    rt = _assert_assignable(resource_type)
    rows = (await db.execute(
        select(ResourceAssignment).where(
            ResourceAssignment.resource_type == rt,
            ResourceAssignment.agent_id == resource_id,
        ).order_by(ResourceAssignment.created_at.desc())
    )).scalars().all()
    return {"items": [await _assignment_dict(db, r) for r in rows]}


async def upsert_assignment(db: AsyncSession, resource_id: str, scope_id: str, version_id: str,
                            *, created_by: str = "", note: str = "",
                            resource_type: str = "agent") -> dict:
    """创建 / 覆盖指派:把某个 user 钉到该资源的某个 published 版本(15 号仅 user)。

    - `version_id` 必须属于该资源且已 published;
    - 用目标版本的 visibility 校验该 user 是否可被指派;
    - 同一 (resource_type, resource_id, scope_id) 重复指派 = 覆盖更新,返回原版本 -> 新版本;
    - scope_type 恒为 user,不再接受其他取值(D9-A);
    - 0074:resource_type 可为 agent 或 skill —— 技能独立分发后 APP 能直接调用它,
      不必非得挂在某个 agent 底下(问题库绑技能的老路径继续可用)。
    """
    rt = _assert_assignable(resource_type)
    scope_type = SCOPE_USER
    if not scope_id:
        raise ValidationError("user 指派必须提供 scope_id(user id)")

    ad = _adapter(rt)
    res = await db.get(ad.model, resource_id)
    if res is None:
        raise NotFound(f"{ad.label} 不存在")

    ver_row = await db.get(ResourceVersion, version_id)
    if ver_row is None or ver_row.resource_type != rt or ver_row.resource_id != resource_id:
        raise ValidationError(f"目标版本不属于该 {ad.label}")
    if ver_row.status != STATUS_PUBLISHED:
        raise ValidationError(f"只能指派已发布(published)的版本,当前为「{ver_row.status}」")
    await _check_scope_visibility(db, res, ver_row, scope_id)

    row = (await db.execute(select(ResourceAssignment).where(
        ResourceAssignment.resource_type == rt,
        ResourceAssignment.agent_id == resource_id,
        ResourceAssignment.scope_type == scope_type,
        ResourceAssignment.scope_id == scope_id,
    ))).scalars().first()
    old_version_no = None
    if row is None:
        row = ResourceAssignment(resource_type=rt, agent_id=resource_id, scope_type=scope_type,
                                 scope_id=scope_id, version_id=version_id, note=note,
                                 created_by=created_by)
        db.add(row)
    else:
        old_ver = await db.get(ResourceVersion, row.version_id)
        old_version_no = old_ver.version if old_ver else None
        row.version_id = version_id
        if note:
            row.note = note
    await db.commit()
    await db.refresh(row)

    scope = _scope_desc(scope_type, scope_id)
    who = await _user_desc(db, scope_id)
    message = f"已将{who}指派到{_res_desc(ad.label, res, resource_id)} v{ver_row.version}"
    if old_version_no is not None and old_version_no != ver_row.version:
        message += f"(原 v{old_version_no} → 新 v{ver_row.version},仅该用户切换)"
    log.info(f"指派 {rt}:{resource_id} {scope} -> v{ver_row.version}({version_id})")
    return {"ok": True, "assignment": await _assignment_dict(db, row), "message": message}


async def remove_assignment(db: AsyncSession, assignment_id: str) -> dict:
    """解除指派:该 user 不再被钉到任何版本 —— 15 号 fail-closed,解除后即未分发。"""
    row = await db.get(ResourceAssignment, assignment_id)
    if row is None:
        raise NotFound("指派不存在")
    scope = _scope_desc(row.scope_type, row.scope_id)
    label = _adapters().get(row.resource_type).label if row.resource_type in ASSIGNABLE_TYPES else "资源"
    res = None
    if row.resource_type in ASSIGNABLE_TYPES:
        res = await db.get(_adapter(row.resource_type).model, row.agent_id)
    who = await _user_desc(db, row.scope_id)
    what = _res_desc(label, res, row.agent_id)
    await db.delete(row)
    await db.commit()
    log.info(f"解除指派 {row.resource_type}:{row.agent_id} {scope}")
    return {"ok": True, "message": f"已解除{who}对{what}的版本指派。15 号起无默认回落,"
                                   f"该用户对本{label}将不可用(fail-closed),请按需重新指派。"}


async def assigned_version(db: AsyncSession, resource_type: str, resource_id: str,
                           user_id: str | None) -> ResourceVersion | None:
    """用户对某资源(agent / skill)的指派版本;无指派 → None(fail-closed,无默认回落)。"""
    if not user_id:
        return None
    rt = (resource_type or "agent").lower()
    a = (await db.execute(
        select(ResourceAssignment).where(
            ResourceAssignment.resource_type == rt,
            ResourceAssignment.agent_id == resource_id,
            ResourceAssignment.scope_type == SCOPE_USER,
            ResourceAssignment.scope_id == user_id,
        )
    )).scalars().first()
    if a is None:
        return None
    ver = await get_version(db, a.version_id)
    if ver is None or ver.resource_type != rt or ver.resource_id != resource_id \
            or ver.status != STATUS_PUBLISHED:
        return None
    return ver


async def assigned_resource_ids(db: AsyncSession, resource_type: str,
                                user_id: str | None) -> set[str]:
    """该用户被指派的全部资源 id(某一类型)。分发中心人视角 / APP 可用清单用。"""
    if not user_id:
        return set()
    rows = (await db.execute(
        select(ResourceAssignment.agent_id).where(
            ResourceAssignment.resource_type == (resource_type or "agent").lower(),
            ResourceAssignment.scope_type == SCOPE_USER,
            ResourceAssignment.scope_id == user_id,
        )
    )).scalars().all()
    return set(rows)


# ── 内部 ────────────────────────────────────────────────────────────────
async def _next_version_no(db: AsyncSession, resource_type: str, resource_id: str) -> int:
    """下一个正式版本号 = max(已分配号)+1。

    **口径必须与唯一索引 uq_res_ver_no 一致 —— 它管的是全部行**
    (resource_type, resource_id, version),不分状态。曾经这里只数 published,
    于是「发布 v1 → 下线 v1 → 再发布」这条完全正常的运维路径必炸:
    published 一条不剩,算出来又是 1,而那个 1 还占在已下线的那行上,
    数据库直接甩一句 duplicate key,页面只剩一个 500 —— 技能改好了却发不出去。

    草稿不占号这件事不受影响:草稿的 version 是 NULL(见 derive_version),
    Postgres 的唯一索引不约束 NULL,max() 也自然跳过它们,
    所以「派生草稿再丢弃不留版本空洞」仍然成立。
    """
    cur = (await db.execute(
        select(func.max(ResourceVersion.version)).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id)
    )).scalar()
    return int(cur or 0) + 1


async def _has_published(db: AsyncSession, resource_type: str, resource_id: str) -> bool:
    cnt = (await db.execute(
        select(func.count()).select_from(ResourceVersion).where(
            ResourceVersion.resource_type == resource_type,
            ResourceVersion.resource_id == resource_id,
            ResourceVersion.status == STATUS_PUBLISHED,
        )
    )).scalar()
    return bool(cnt)


def _as_dict(r: ResourceVersion) -> dict:
    return {
        "id": r.id, "resource_type": r.resource_type, "resource_id": r.resource_id,
        "version": r.version, "status": r.status,
        "base_version_id": r.base_version_id, "note": r.note,
        "snapshot": r.snapshot, "locked_deps": r.locked_deps, "created_by": r.created_by,
        "created_at": r.created_at, "updated_at": r.updated_at,
        "published_at": r.published_at, "offlined_at": r.offlined_at,
    }
