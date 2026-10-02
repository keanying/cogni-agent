"""数据指标接口文档 → 工具库里的 HTTP 工具(服务内置同步)。

以前只给了一个脚本(scripts/sync_data_api_tools.py),要有人拿管理员账号去跑一遍
工具才会出现 —— 没人跑,工具库里就一直没有,而交付物看起来「已经做了」。
现在文档随服务打包(seed_data/data_metric_api_doc.md),**启动时自动把缺的建上**,
工具中心也有一个「同步数据接口工具」按钮可以手动再跑(可选刷新已有的)。

规则(与脚本同一套,脚本现在也调这里):
  - 按 (方法, URI 路径) 认已有工具,不按名字 —— 工具库里原来那个叫
    「通过景区ID获取供应商ID列表」,文档里叫「全局-通过景区ID获取供应商ID列表」;
  - 已存在的默认不动(避免冲掉运营手调过的描述),`update_existing=True` 才按文档刷新;
  - supplierId 声明为由供应商接口自动取(x-param-source),scenicId 缺省取当前景区
    (x-ctx-default),没有景区时系统发起澄清;
  - 数据平台密钥永不落库:请求头写占位 `{{data_platform_key}}`,调用时服务端从配置取。
    没配也照样建(工具要先出现在库里才能绑技能),调用时会明确报「密钥未配置」。
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cogni_agent.common.logging import get_logger
from cogni_agent.tool_center_service.apidoc import ApiDocParseError, annotate_params, from_markdown

log = get_logger("tool_center.data_api_seed")

DOC_PATH = Path(__file__).resolve().parent / "seed_data" / "data_metric_api_doc.md"
DOC_BASE_URL = "https://data.12301dev.com"   # 文档里写的是测试环境

SUPPLIER_PATH_SUFFIX = "/getSupplierIdByScenicId"
# supplierId 的来源:内置工具 sys_supplier_ids(v3.52 起)。不再指向工具库里那条普通工具 ——
# 那条要绑、要发布、要对账号可见,任何一环断了票务类就全部调不通。
SUPPLIER_SOURCE_TOOL = "sys_supplier_ids"
SUPPLIER_SOURCE_PATH = "supplierId"
# HTTP 工具的调用结果是沙箱包的一层 {"status_code", "body"},接口原始返回在 body 里;
# 文档 1.12 的返回是 {status, code, msg, data: [{scenicId, supplierId: [...]}]}。
SUPPLIER_VALUE_PATH = "body.data[0].supplierId"
# 文档 curl 示例里的平台密钥形态。**只在本地抹掉用**,不作为任何判断依据。
_KEY_LITERAL = re.compile(r"pfk_[A-Za-z0-9]{16,}")
# 本模块写的版本都带这个前缀的 note。用来判断「这个工具有没有人动过」。
SEED_NOTE_PREFIX = "按数据指标接口文档"


# ── 文档处理(纯函数) ───────────────────────────────────────────────

def split_doc(md: str) -> list[str]:
    """按一级标题 `# 1.x` 切成一个接口一段。空标题(文档里 1.17 那种)自然被跳过。"""
    parts = re.split(r"(?m)^# (?=1\\?\.\d+)", md)
    return ["# " + p for p in parts[1:] if p.strip()]


def scrub(md: str) -> str:
    """把文档里的明文密钥抹掉。解析器虽然也会脱敏,但密钥本来就不该往下传。"""
    return _KEY_LITERAL.sub("***", md)


def rebase(md: str, base_url: str | None) -> str:
    """把文档里的数据平台地址换成目标环境的。"""
    if not base_url:
        return md
    return md.replace(DOC_BASE_URL, base_url.rstrip("/"))


def annotations_for(props, *, is_supplier: bool, supplier_tool_name: str) -> dict:
    """按参数名给出「系统怎么补这个参数」的声明。

    - supplierId:由「按景区取供应商 ID」自动取,key 取本轮景区。模型没有任何办法知道
      供应商 ID,让它填就只能编。
    - scenicId:模型可以填(用户点名了景区时),没填则用本轮景区,都没有就澄清。
    """
    out: dict[str, dict] = {}
    if "supplierId" in props and not is_supplier:
        # supplier_tool_name 保留入参兼容老调用方,来源一律是内置 sys_supplier_ids
        out["supplierId"] = {"x-param-source": {
            "tool": SUPPLIER_SOURCE_TOOL, "key": "$ctx.scenicId", "path": SUPPLIER_SOURCE_PATH}}
    if "scenicId" in props:
        out["scenicId"] = {"x-ctx-default": "$ctx.scenicId"}
    return out


def build_items(md: str, *, base_url: str | None) -> list[dict]:
    items = []
    for chunk in split_doc(md):
        safe = rebase(scrub(chunk), base_url)
        try:
            doc = from_markdown(safe)
        except ApiDocParseError as exc:
            items.append({"error": str(exc), "chunk": safe[:80]})
            continue
        tc = doc.to_type_config()
        path = urlparse(tc["url"]).path
        items.append({
            "name": doc.title[:64], "method": doc.method or "POST", "path": path,
            "url": tc["url"], "markdown": safe, "doc": doc, "type_config": tc,
            "props": list((tc.get("params_schema") or {}).get("properties") or {}),
            "is_supplier": path.endswith(SUPPLIER_PATH_SUFFIX),
            "warnings": [w for w in doc.warnings if "凭证" not in w],
        })
    return items


def tool_key(tool_type: str | None, type_config: dict | None) -> tuple[str, str] | None:
    """已有工具的 (方法, 路径);非 http / 没 url 的返回 None。"""
    tc = type_config or {}
    if tool_type != "http" or not tc.get("url"):
        return None
    return ((tc.get("method") or "GET").upper(), urlparse(tc["url"]).path)


# ── 落库 ─────────────────────────────────────────────────────────────

async def _existing_http_tools(db: AsyncSession) -> dict[tuple[str, str], dict]:
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.tool.models import Tool

    tools = (await db.execute(select(Tool).where(Tool.deleted_at.is_(None)))).scalars().all()
    snaps = await ver.latest_published_snapshots(db, "tool", [t.id for t in tools])
    out: dict[tuple[str, str], dict] = {}
    for t in tools:
        s = snaps.get(t.id) or {}
        k = tool_key(s.get("type"), s.get("type_config"))
        if k:
            out.setdefault(k, {"id": t.id, "name": t.name, "snapshot": s})
    return out


async def _seed_owner(db: AsyncSession) -> str | None:
    """内置工具挂在最早的超级管理员名下(工具主表要求有 owner)。"""
    from cogni_agent.user_center_service.models import User

    row = (await db.execute(
        select(User.id).where(User.role == "super_admin", User.is_active.is_(True))
        .order_by(User.created_at).limit(1))).first()
    return row[0] if row else None


async def _untouched_by_people(db: AsyncSession, tool_id: str) -> bool:
    """这个工具的每一个版本都是本模块写的(没人手工改过、也没有未发布草稿)。"""
    from cogni_agent.tool_center_service.versioning import ResourceVersion

    rows = (await db.execute(select(ResourceVersion.note, ResourceVersion.status).where(
        ResourceVersion.resource_type == "tool", ResourceVersion.resource_id == tool_id))).all()
    return bool(rows) and all((n or "").startswith(SEED_NOTE_PREFIX) and st != "draft" for n, st in rows)


async def sync_data_api_tools(db: AsyncSession, *, owner_user_id: str | None = None,
                              base_url: str | None = None, update_existing: bool = False,
                              refresh_untouched: bool = False,
                              visibility: str = "public", md: str | None = None) -> dict:
    """把文档里缺的工具建上(已有的按需刷新)。返回 {created, updated, skipped, errors, ...}。

    refresh_untouched:已有工具里**只由本模块写过、没人动过**的,定义和文档对不上时自动刷新。
    用于修正早先按错误解析建出来的工具(v3.49.0 的响应字段整列错位),又不碰人改过的。
    """
    from cogni_agent.tool_center_service import versioning as ver
    from cogni_agent.tool_center_service.tool.models import Tool

    md = md if md is not None else DOC_PATH.read_text(encoding="utf-8")
    items = build_items(md, base_url=base_url)
    good = [i for i in items if "error" not in i]
    errors = [f"{i['chunk'][:40]!r}: {i['error']}" for i in items if "error" in i]

    owner = owner_user_id or await _seed_owner(db)
    if owner is None:
        return {"created": [], "updated": [], "skipped": [], "errors": errors,
                "total": len(good), "message": "还没有超级管理员账号,暂不创建(首次部署建好账号后会自动补上)"}

    existing = await _existing_http_tools(db)
    supplier = next((i for i in good if i["is_supplier"]), None)
    sup_ex = existing.get((supplier["method"], supplier["path"])) if supplier else None
    # 前置工具按**实际存在的名字**引用:工具库里原来那个可能不叫文档里的名字
    supplier_name = (sup_ex or {}).get("name") or (supplier or {}).get("name") or ""

    created, updated, skipped = [], [], []
    # 供应商接口最先建:其它工具的 x-param-source 按名字引用它
    for i in sorted(good, key=lambda x: not x["is_supplier"]):
        ann = annotations_for(i["props"], is_supplier=i["is_supplier"], supplier_tool_name=supplier_name)
        tc = annotate_params(i["type_config"], ann)
        desc = i["doc"].summary or f"{i['method']} {i['path']}"
        ex = existing.get((i["method"], i["path"]))
        if ex is None and i["is_supplier"]:
            # 供应商接口已内置为 sys_supplier_ids(对每个技能都可用、系统自动调),
            # 不再在工具库里另建一份 —— 两份做同一件事,模型会在两个里随机挑
            skipped.append(f"{i['name']}(已内置为 {SUPPLIER_SOURCE_TOOL})")
            continue
        if ex is None:
            tool = Tool(name=i["name"], owner_user_id=owner, scenic_id=None,
                        visibility=visibility, active=True)
            db.add(tool)
            await db.flush()
            draft = await ver.derive_version(db, "tool", tool.id, base_version_id=None, snapshot={
                "description": desc, "type": "http", "type_config": tc, "risk_level": "low",
            }, created_by=owner, note="按数据指标接口文档内置")
            await ver.publish_version(db, draft.id)
            existing[(i["method"], i["path"])] = {"id": tool.id, "name": tool.name}
            created.append(i["name"])
        elif (not update_existing and refresh_untouched
              and ((ex.get("snapshot") or {}).get("type_config") != tc
                   or (ex.get("snapshot") or {}).get("description") != desc)
              and await _untouched_by_people(db, ex["id"])):
            snap = dict(ex.get("snapshot") or {})
            snap.update({"description": desc, "type": "http", "type_config": tc})
            snap.setdefault("risk_level", "low")
            pub = await ver.latest_published_version(db, "tool", ex["id"])
            draft = await ver.derive_version(db, "tool", ex["id"], snapshot=snap,
                                             base_version_id=pub.id if pub else None,
                                             created_by=owner, note=SEED_NOTE_PREFIX + "自动修正")
            await ver.publish_version(db, draft.id)
            updated.append(ex["name"])
        elif update_existing:
            if await ver.latest_draft(db, "tool", ex["id"]) is not None:
                # 有人正在编辑(未发布草稿):不替他覆盖,也不替他发布
                errors.append(f"{ex['name']}: 有未发布的草稿,跳过刷新(先发布或放弃草稿)")
                skipped.append(ex["name"])
                continue
            snap = dict(ex.get("snapshot") or {})
            snap.update({"description": desc, "type": "http", "type_config": tc})
            snap.setdefault("risk_level", "low")
            pub = await ver.latest_published_version(db, "tool", ex["id"])
            draft = await ver.derive_version(db, "tool", ex["id"], snapshot=snap,
                                             base_version_id=pub.id if pub else None,
                                             created_by=owner, note="按数据指标接口文档刷新")
            await ver.publish_version(db, draft.id)
            updated.append(ex["name"])
        else:
            skipped.append(ex["name"])
    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors,
            "total": len(good),
            "message": f"文档共 {len(good)} 个接口:新建 {len(created)},刷新 {len(updated)},"
                       f"已存在未动 {len(skipped)}"}


async def ensure_on_startup() -> None:
    """启动时补齐。新建缺的;已有的只修正「没人动过」的那些;任何失败只记日志,不拦启动。"""
    from cogni_agent.common.db import get_sessionmaker
    from cogni_agent.config import get_settings

    cfg = get_settings().get("tool_center", "data_api_seed", default={}) or {}
    if cfg.get("enabled", True) is False:
        return
    try:
        async with get_sessionmaker()() as db:
            # 只由本模块写过的工具,和文档对不上就自动修正(人改过的一律不动)
            res = await sync_data_api_tools(db, base_url=cfg.get("base_url") or None,
                                            refresh_untouched=True)
        msg = f"数据指标接口工具:{res['message']}"
        (log.info if not res["errors"] else log.warning)(
            msg + (f";解析失败 {res['errors']}" if res["errors"] else ""))
        from cogni_agent.governance_event_bus_service import platform_secrets as ps

        await ps.refresh(force=True)
        if not ps.get("data_platform_key"):
            log.warning("数据平台密钥未配置:数据接口工具已建好,但调用时会报「密钥未配置」。"
                        "请到管理后台「系统设置 → 平台密钥」填写(或设置环境变量 DATA_PLATFORM_XPFTKEY)")
    except Exception as exc:  # noqa: BLE001  内置数据补齐失败不应让整个服务起不来
        log.warning(f"数据指标接口工具补齐失败(不影响启动,可在工具中心手动同步):{exc!r}")
