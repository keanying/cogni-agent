# -*- coding: utf-8 -*-
"""上传文件的存放:对象存储优先,数据库兜底。

**为什么要有对象存储**:原先文件是整份存在 Postgres 的 bytea 列里(QbiFile.content),
上限 5MB。那个 5MB 不是数据库的限制,是 Quick BI OpenAPI 的 —— 但它把整套上传都框住了。
十兆的报表、扫描件 PDF 传不进来,而 minio 的配置(bucket zhilv-artifacts)在
config.yaml 里躺了很久,一次都没被 import 过。

**为什么还要兜底**:只走对象存储的话,MinIO 一挂,上传功能整个消失 ——
而上传往往是用户一整套操作的第一步,第一步断了后面全断。所以:

    传:先试对象存储 → 不可用/失败 → 落库(inline,受 INLINE_MAX 限制)
    取:行上记了存哪儿,就去哪儿取;对象存储取不到再看有没有 inline 兜底

代价是同一份文件可能存在两个地方之一,所以**行上必须记清楚存在哪** ——
靠猜的话会出现「库里没有、桶里也没有,但记录还在」,而那种文件删不掉也读不了。

大小上限分两层:对象存储 MAX_FILE_BYTES(十兆),落库 INLINE_MAX(五兆)。
兜底比主路小是故意的:兜底是为了让服务在 MinIO 挂掉时还能用,不是为了长期扛大文件 ——
十兆的 bytea 每读一次就要从数据库连接里拖十兆过来,备份也跟着涨。
"""
from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings

log = get_logger("tool_center_service.filestore")

# 对象存储的单文件上限
MAX_FILE_BYTES = 10 * 1024 * 1024
# 落库兜底的上限。比主路小 —— 兜底是为了别断,不是为了扛大文件
INLINE_MAX = 5 * 1024 * 1024

STORAGE_OBJECT = "minio"
STORAGE_INLINE = "db"


class FileStoreError(Exception):
    """存取失败。message 会一路回到界面上,所以要说清是哪一步、能怎么办。"""


def _cfg(key: str, default=None):
    return get_settings().get("minio", key, default=default)


def enabled() -> bool:
    """对象存储配没配齐。配不齐就一直走落库兜底,不报错 —— 小文件本来也够用。"""
    return bool(_cfg("enabled", False) and _cfg("endpoint") and _cfg("bucket"))


_client = None
_client_failed = False


def _get_client():
    """拿 MinIO 客户端。拿不到返回 None(调用方落兜底),不抛。

    失败只记一次 —— 每次上传都打一条「连不上 MinIO」的 ERROR,真正的问题会被淹掉。
    """
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    if not enabled():
        _client_failed = True
        return None
    try:
        from minio import Minio

        _client = Minio(
            str(_cfg("endpoint")),
            access_key=str(_cfg("access_key") or ""),
            secret_key=str(_cfg("secret_key") or ""),
            secure=bool(_cfg("secure", False)),
        )
        bucket = str(_cfg("bucket"))
        if not _client.bucket_exists(bucket):
            _client.make_bucket(bucket)
        return _client
    except Exception as exc:                    # noqa: BLE001 - 对象存储不可用不该让上传整个失败
        log.warning(f"对象存储不可用,上传将落库兜底:{type(exc).__name__}: {exc}")
        _client_failed = True
        _client = None
        return None


def reset_client_cache() -> None:
    """测试/改配置后重置。生产不用调 —— 进程重启自然会重建。"""
    global _client, _client_failed
    _client, _client_failed = None, False


def object_key(scenic_id: str | None, file_name: str) -> str:
    """对象键:按景区和日期分目录。

    平铺在一个前缀下的话,几万个对象之后连 list 都很慢,而运维想按景区清理某一批
    文件时无从下手 —— 只能一个一个比对数据库。
    """
    day = datetime.now().strftime("%Y/%m/%d")
    safe = (file_name or "f").replace("/", "_").replace("\\", "_")[-80:]
    return f"agent-files/{scenic_id or 'group'}/{day}/{uuid.uuid4().hex[:12]}_{safe}"


def put(content: bytes, *, file_name: str, scenic_id: str | None = None,
        content_type: str = "application/octet-stream") -> dict:
    """存一份文件。返回 {storage, object_key, inline, size, sha256}。

    inline 非空 = 这份存在数据库里(对象存储不可用时的兜底)。
    """
    if not content:
        raise FileStoreError("文件是空的")
    size = len(content)
    if size > MAX_FILE_BYTES:
        raise FileStoreError(
            f"文件 {size / 1048576:.1f}MB,超过 {MAX_FILE_BYTES // 1048576}MB 上限。"
            "更大的数据建议走数据源接入,而不是当附件传")
    sha = hashlib.sha256(content).hexdigest()
    cli = _get_client()
    if cli is not None:
        key = object_key(scenic_id, file_name)
        try:
            cli.put_object(str(_cfg("bucket")), key, io.BytesIO(content), size,
                           content_type=content_type)
            return {"storage": STORAGE_OBJECT, "object_key": key, "inline": None,
                    "size": size, "sha256": sha}
        except Exception as exc:                # noqa: BLE001
            log.warning(f"对象存储写入失败,改为落库兜底:{type(exc).__name__}: {exc}")
    # 兜底
    if size > INLINE_MAX:
        raise FileStoreError(
            f"对象存储当前不可用,只能暂存到数据库,而数据库暂存上限是 "
            f"{INLINE_MAX // 1048576}MB(这个文件 {size / 1048576:.1f}MB)。"
            "请联系运维恢复对象存储后重传,或先传一份小一些的")
    return {"storage": STORAGE_INLINE, "object_key": None, "inline": content,
            "size": size, "sha256": sha}


def get(*, storage: str, object_key: str | None, inline: bytes | None) -> bytes:
    """取回内容。按行上记的存放位置去取,不猜。"""
    if storage == STORAGE_OBJECT and object_key:
        cli = _get_client()
        if cli is None:
            if inline:
                return inline                   # 极少数:两边都写过
            raise FileStoreError("这份文件存在对象存储里,而对象存储当前不可用。"
                                 "请联系运维恢复后重试 —— **不要猜文件里有什么**")
        try:
            resp = cli.get_object(str(_cfg("bucket")), object_key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception as exc:                # noqa: BLE001
            if inline:
                return inline
            raise FileStoreError(
                f"从对象存储读取失败({type(exc).__name__})。文件可能已被清理,"
                "请让用户重新上传") from exc
    if inline:
        return inline
    raise FileStoreError("这份文件没有内容(上传时可能就失败了),请让用户重新上传")


def delete(*, storage: str, object_key: str | None) -> None:
    """删对象。删不掉只记日志 —— 数据库那行已经删了,留个孤儿对象不影响使用,
    而为了删不掉一个对象就让整个删除操作失败,用户会一直卡在那条删不掉的记录上。"""
    if storage != STORAGE_OBJECT or not object_key:
        return
    cli = _get_client()
    if cli is None:
        log.warning(f"对象存储不可用,残留对象未清理:{object_key}")
        return
    try:
        cli.remove_object(str(_cfg("bucket")), object_key)
    except Exception as exc:                    # noqa: BLE001
        log.warning(f"对象删除失败(留下孤儿对象,不影响使用):{object_key} {exc}")
