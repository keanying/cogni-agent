# -*- coding: utf-8 -*-
"""上传给智能体读的文件:存、列、删、取内容。

**上传时就解析一次**,把结果(是表格还是文档、多少行、有哪些列)记在行上。原因有两条:

- 传完就知道能不能读。解析失败的话,此刻还能让用户换个格式重传;
  等到对话里才发现读不了,用户已经在等回答了,而那时他多半会觉得是智能体不行。
- 列表上能直接显示「客源.xlsx · 表格 · 216 行 · 城市/热度/口径」,
  运营一眼看得出传对了没有 —— 而不是一排只有文件名的灰条。

解析**不落文本**:表格类的行数据不存(要用时现读现解析,存一份就多一处会不同步),
只存元信息。文档类同理。真正要用的时候由 sys_read_file 重新解析,
那一步才把表格变成数据集句柄。
"""
from __future__ import annotations

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.errors import Forbidden, NotFound, ValidationError
from cogni_agent.common.logging import get_logger
from cogni_agent.tool_center_service import filereader as fr
from cogni_agent.tool_center_service import filestore
from cogni_agent.tool_center_service.quickbi.models import AgentFile

log = get_logger("tool_center_service.agent_files")


def file_dict(row: AgentFile) -> dict:
    """对外结构。**不带 content** —— 列表接口返回十兆二进制是个事故。"""
    return {
        "id": row.id, "file_name": row.file_name, "size": row.size,
        "kind": row.parsed_kind, "parse_error": row.parse_error,
        "storage": row.storage, "detail": row.detail or {},
        "scenic_id": row.scenic_id, "conversation_id": row.conversation_id,
        "owner_user_id": row.owner_user_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _probe(name: str, content: bytes) -> tuple[str | None, str | None, dict]:
    """上传时试解析一次。返回 (kind, error, detail) —— 失败不阻断上传。

    不阻断是有意的:文件已经传上来了,拒收等于让用户白传一次。
    记下失败原因,列表上标出来,用户可以直接看到「这份读不了,因为…」。
    """
    try:
        got = fr.read(name, content)
    except fr.FileReadError as exc:
        return None, str(exc), {}
    except Exception as exc:                    # noqa: BLE001
        log.warning(f"上传预解析失败 {name}:{type(exc).__name__}: {exc}")
        return None, f"解析失败({type(exc).__name__}),文件可能已损坏", {}
    if got["kind"] == "table":
        rows = got["rows"]
        cols = list(rows[0].keys()) if rows else []
        return "table", None, {"row_count": len(rows), "columns": cols,
                               "sheets": got.get("sheets") or []}
    text = got.get("text") or ""
    return got["kind"], None, {"chars": len(text), "preview": text[:200]}


async def save(*, user_id: str, scenic_id: str | None, file_name: str, content: bytes,
               conversation_id: str | None = None) -> dict:
    fr_kind = fr.kind_of(file_name)
    if fr_kind == "unsupported":
        raise ValidationError(
            f"不支持的文件类型。支持:表格 {'/'.join(sorted(fr.TABLE_SUFFIXES))}、"
            f"文档 {'/'.join(sorted(fr.DOC_SUFFIXES))}、"
            f"图片 {'/'.join(sorted(fr.IMAGE_SUFFIXES))}")
    try:
        placed = filestore.put(content, file_name=file_name, scenic_id=scenic_id)
    except filestore.FileStoreError as exc:
        raise ValidationError(str(exc)) from exc
    kind, err, detail = _probe(file_name, content)
    row = AgentFile(
        file_name=file_name, size=placed["size"], sha256=placed["sha256"],
        storage=placed["storage"], object_key=placed["object_key"],
        content=placed["inline"], parsed_kind=kind, parse_error=err, detail=detail,
        owner_user_id=user_id, scenic_id=scenic_id, conversation_id=conversation_id)
    async with get_sessionmaker()() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return file_dict(row)


async def load_content(file_id: str) -> tuple[AgentFile, bytes]:
    """取一份文件的内容(sys_read_file 用)。"""
    async with get_sessionmaker()() as db:
        row = await db.get(AgentFile, file_id)
    if row is None:
        raise NotFound(f"文件不存在:{file_id}")
    data = filestore.get(storage=row.storage, object_key=row.object_key, inline=row.content)
    return row, data


def _visible(user_id: str, role: str, scenic_id: str | None):
    """可见范围:集团角色看全部,景区角色只看本景区(含集团级的公共文件)。

    与其它资源同一套口径 —— 各写一套的话,总会有一处漏判,
    而漏判的表现是「别的景区的报表出现在我的列表里」,不报错。
    """
    from cogni_agent.common import permissions as perm

    if perm.is_group_role(role):
        return None
    conds = [AgentFile.scenic_id == scenic_id, AgentFile.scenic_id.is_(None)]
    from sqlalchemy import or_

    return or_(*conds)


async def list_files(*, user_id: str, role: str, scenic_id: str | None,
                     conversation_id: str | None = None, limit: int = 50) -> list[dict]:
    async with get_sessionmaker()() as db:
        stmt = select(AgentFile).order_by(AgentFile.created_at.desc()).limit(min(limit, 200))
        cond = _visible(user_id, role, scenic_id)
        if cond is not None:
            stmt = stmt.where(cond)
        if conversation_id:
            stmt = stmt.where(AgentFile.conversation_id == conversation_id)
        return [file_dict(r) for r in (await db.execute(stmt)).scalars().all()]


async def delete(*, file_id: str, user_id: str, role: str, scenic_id: str | None) -> None:
    from cogni_agent.common import permissions as perm

    async with get_sessionmaker()() as db:
        row = await db.get(AgentFile, file_id)
        if row is None:
            raise NotFound(f"文件不存在:{file_id}")
        if not perm.is_group_role(role) and row.scenic_id and row.scenic_id != scenic_id:
            raise Forbidden("无权删除其它景区的文件")
        storage, key = row.storage, row.object_key
        await db.delete(row)
        await db.commit()
    # 先删行再删对象:反过来的话对象删了、行还在,用户看得到一条点开就报错的记录。
    # 现在最坏是留一个孤儿对象,不影响任何人使用。
    filestore.delete(storage=storage, object_key=key)
