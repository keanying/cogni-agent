# -*- coding: utf-8 -*-
"""上传文件的存放:对象存储优先,数据库兜底。

**这里没有 MinIO 服务器可连**,所以对象存储那条路用一个假客户端验证 ——
验的是「我们的调用代码对不对」(传了什么、桶取自哪、失败了怎么办),
而不是「MinIO 能不能用」。真正的网络往返只能在有 MinIO 的环境里验,
这一点在 CHANGELOG 里写明了,不假装测过。

守住的几条,每一条都是「不报错但出事」:

- MinIO 挂了整个上传功能消失 —— 而上传往往是一整套操作的第一步,第一步断了后面全断;
- storage 记错 → 取的时候两边都找不到,而记录还在列表上挂着,点开就报错、删也删不干净;
- 列表接口把十兆二进制一起返回;
- 景区隔离漏判 —— 别的景区的报表出现在我的列表里,不报错。
"""
from __future__ import annotations

import asyncio
import functools
import io

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.tool_center_service import filestore

SCENIC = "PFTSCA01018088"
OTHER = "PFTSCB02000052"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture(autouse=True)
def _fresh_db():
    async def reset():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
    asyncio.run(reset())
    filestore.reset_client_cache()
    yield
    filestore.reset_client_cache()


class FakeMinio:
    """够用的假 MinIO:记下每次调用,便于验证我们传了什么。"""

    def __init__(self, *, fail_put=False, fail_get=False):
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = set()
        self.fail_put, self.fail_get = fail_put, fail_get
        self.put_calls: list[tuple] = []

    def bucket_exists(self, b): return b in self.buckets
    def make_bucket(self, b): self.buckets.add(b)

    def put_object(self, bucket, key, data, size, content_type=None):
        if self.fail_put:
            raise RuntimeError("对象存储写入失败(模拟)")
        self.put_calls.append((bucket, key, size, content_type))
        self.objects[key] = data.read()

    def get_object(self, bucket, key):
        if self.fail_get or key not in self.objects:
            raise RuntimeError("对象不存在(模拟)")
        return _Resp(self.objects[key])

    def remove_object(self, bucket, key):
        self.objects.pop(key, None)


class _Resp:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def close(self): pass
    def release_conn(self): pass


def _use_fake(monkeypatch, fake):
    monkeypatch.setattr(filestore, "_client", fake)
    monkeypatch.setattr(filestore, "_client_failed", False)
    monkeypatch.setattr(filestore, "_cfg",
                        lambda k, d=None: {"enabled": True, "endpoint": "x:9000",
                                           "bucket": "zhilv-artifacts", "secure": False}.get(k, d))


# ══════════════════════════════════════════════════════════════════════
# 一、对象存储可用时
# ══════════════════════════════════════════════════════════════════════

def test_object_storage_is_used_when_available(monkeypatch):
    fake = FakeMinio()
    _use_fake(monkeypatch, fake)
    got = filestore.put(b"hello", file_name="a.csv", scenic_id=SCENIC)
    assert got["storage"] == filestore.STORAGE_OBJECT
    assert got["inline"] is None, "走了对象存储就不该再往库里存一份"
    bucket, key, size, _ = fake.put_calls[0]
    assert bucket == "zhilv-artifacts" and size == 5
    assert SCENIC in key, "对象键要带景区,否则运维没法按景区清理"


def test_object_round_trip(monkeypatch):
    fake = FakeMinio()
    _use_fake(monkeypatch, fake)
    got = filestore.put(b"round trip", file_name="a.csv")
    back = filestore.get(storage=got["storage"], object_key=got["object_key"], inline=None)
    assert back == b"round trip"


def test_object_key_is_unique_per_upload(monkeypatch):
    """同名文件反复传不能互相覆盖 —— 覆盖了之后,前一次的记录还在,内容却已经变了。"""
    fake = FakeMinio()
    _use_fake(monkeypatch, fake)
    a = filestore.put(b"one", file_name="同名.csv")
    b = filestore.put(b"two", file_name="同名.csv")
    assert a["object_key"] != b["object_key"]
    assert filestore.get(storage="minio", object_key=a["object_key"], inline=None) == b"one"


# ══════════════════════════════════════════════════════════════════════
# 二、对象存储不可用时:上传不能整个消失
# ══════════════════════════════════════════════════════════════════════

def test_falls_back_to_database_when_object_storage_is_down(monkeypatch):
    """**MinIO 挂了,上传还得能用。**

    上传往往是一整套操作的第一步 —— 第一步断了,后面全断,而用户看到的只是
    「上传失败」四个字,完全不知道是存储挂了还是自己的文件有问题。
    """
    _use_fake(monkeypatch, FakeMinio(fail_put=True))
    got = filestore.put(b"small file", file_name="a.csv")
    assert got["storage"] == filestore.STORAGE_INLINE
    assert got["inline"] == b"small file"


def test_fallback_refuses_big_files_with_a_clear_reason(monkeypatch):
    """兜底比主路小是故意的。但拒绝时必须说清**为什么**、能怎么办 ——
    只说「上传失败」的话,用户会把同一个文件反复传三遍。"""
    _use_fake(monkeypatch, FakeMinio(fail_put=True))
    with pytest.raises(filestore.FileStoreError) as e:
        filestore.put(b"x" * (filestore.INLINE_MAX + 1), file_name="big.csv")
    msg = str(e.value)
    assert "对象存储当前不可用" in msg and "联系运维" in msg


def test_no_object_storage_configured_means_inline(monkeypatch):
    """没配 MinIO 也要能用 —— 小文件本来就够,不该逼着所有部署都装一套对象存储。"""
    monkeypatch.setattr(filestore, "_cfg", lambda k, d=None: {"enabled": False}.get(k, d))
    filestore.reset_client_cache()
    got = filestore.put(b"tiny", file_name="a.csv")
    assert got["storage"] == filestore.STORAGE_INLINE


def test_oversize_is_refused_even_with_object_storage(monkeypatch):
    _use_fake(monkeypatch, FakeMinio())
    with pytest.raises(filestore.FileStoreError) as e:
        filestore.put(b"x" * (filestore.MAX_FILE_BYTES + 1), file_name="huge.csv")
    assert "上限" in str(e.value)


def test_ten_megabytes_is_allowed():
    """用户说的就是 10MB —— 这条钉住上限不会被人随手改小。"""
    assert filestore.MAX_FILE_BYTES == 10 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════
# 三、取不到的时候,话要说清
# ══════════════════════════════════════════════════════════════════════

def test_missing_object_with_no_fallback_tells_the_model_not_to_guess(monkeypatch):
    """**最危险的一句话是「没有数据」。**

    取不到内容时如果只回一句含糊的失败,模型很可能转头就凭文件名猜内容 ——
    「客源.xlsx,那应该是各省客源占比…」然后编一份出来。
    """
    _use_fake(monkeypatch, FakeMinio(fail_get=True))
    with pytest.raises(filestore.FileStoreError) as e:
        filestore.get(storage="minio", object_key="k", inline=None)
    assert "重新上传" in str(e.value)


def test_object_storage_down_but_inline_exists_still_works(monkeypatch):
    _use_fake(monkeypatch, FakeMinio(fail_get=True))
    assert filestore.get(storage="minio", object_key="k", inline=b"backup") == b"backup"


def test_delete_never_blocks_on_a_dead_object_store(monkeypatch):
    """删不掉对象只留个孤儿,不能让用户卡在一条删不掉的记录上。"""
    _use_fake(monkeypatch, FakeMinio(fail_get=True))
    filestore.delete(storage="minio", object_key="whatever")   # 不抛


# ══════════════════════════════════════════════════════════════════════
# 四、落库、列表、删除、隔离
# ══════════════════════════════════════════════════════════════════════

def _xlsx(rows):
    import openpyxl

    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


@run_async
async def test_upload_parses_once_and_records_what_it_found():
    """上传时就解析一次:传完立刻知道能不能读。

    等到对话里才发现读不了的话,用户已经在等回答了 —— 那时他多半会觉得是智能体不行。
    """
    from cogni_agent.tool_center_service import agent_files as af

    got = await af.save(user_id="u1", scenic_id=SCENIC, file_name="客源.xlsx",
                        content=_xlsx([["城市", "热度"], ["福建省", 62.12], ["江西省", 8.2]]))
    assert got["kind"] == "table"
    assert got["detail"]["row_count"] == 2
    assert got["detail"]["columns"] == ["城市", "热度"]
    assert got["parse_error"] is None


@run_async
async def test_unreadable_upload_is_kept_with_the_reason():
    """读不了也别拒收 —— 文件已经传上来了,拒收等于让用户白传一次。
    记下原因,列表上标出来,他能直接看到「这份读不了,因为…」。"""
    from cogni_agent.tool_center_service import agent_files as af

    got = await af.save(user_id="u1", scenic_id=SCENIC, file_name="坏的.xlsx",
                        content=b"this is not a real xlsx")
    assert got["kind"] is None
    assert got["parse_error"], "失败原因必须留痕,否则没法回答「为什么读不了」"


@run_async
async def test_unsupported_type_is_refused_at_upload():
    from cogni_agent.common.errors import ValidationError
    from cogni_agent.tool_center_service import agent_files as af

    with pytest.raises(ValidationError) as e:
        await af.save(user_id="u1", scenic_id=None, file_name="x.exe", content=b"MZ")
    assert ".xlsx" in str(e.value), "要顺带告诉用户支持什么"


@run_async
async def test_listing_never_returns_file_bytes():
    """列表接口返回十兆二进制是个事故 —— 页面会直接卡死,而它只是想显示个文件名。"""
    from cogni_agent.tool_center_service import agent_files as af

    await af.save(user_id="u1", scenic_id=SCENIC, file_name="a.csv", content=b"a,b\n1,2\n")
    items = await af.list_files(user_id="u1", role="scenic_admin", scenic_id=SCENIC)
    assert items and "content" not in items[0] and "inline" not in items[0]


@run_async
async def test_scenic_isolation_on_listing():
    """景区隔离漏判的表现是:别的景区的报表出现在我的列表里,而且不报错。"""
    from cogni_agent.tool_center_service import agent_files as af

    await af.save(user_id="u1", scenic_id=SCENIC, file_name="mine.csv", content=b"a\n1\n")
    await af.save(user_id="u2", scenic_id=OTHER, file_name="theirs.csv", content=b"a\n1\n")
    await af.save(user_id="u3", scenic_id=None, file_name="group.csv", content=b"a\n1\n")

    mine = {i["file_name"] for i in await af.list_files(
        user_id="u1", role="scenic_admin", scenic_id=SCENIC)}
    assert mine == {"mine.csv", "group.csv"}, "本景区 + 集团级,不该看到别人的"

    allf = {i["file_name"] for i in await af.list_files(
        user_id="a", role="super_admin", scenic_id=None)}
    assert "theirs.csv" in allf, "集团角色看全部"


@run_async
async def test_other_scenic_cannot_delete():
    from cogni_agent.common.errors import Forbidden
    from cogni_agent.tool_center_service import agent_files as af

    got = await af.save(user_id="u2", scenic_id=OTHER, file_name="theirs.csv", content=b"a\n1\n")
    with pytest.raises(Forbidden):
        await af.delete(file_id=got["id"], user_id="u1", role="scenic_admin", scenic_id=SCENIC)


@run_async
async def test_content_round_trips_through_the_store():
    from cogni_agent.tool_center_service import agent_files as af

    data = _xlsx([["城市", "热度"], ["福建省", 62.12]])
    got = await af.save(user_id="u1", scenic_id=SCENIC, file_name="a.xlsx", content=data)
    row, back = await af.load_content(got["id"])
    assert back == data and row.file_name == "a.xlsx"


@run_async
async def test_uploaded_table_becomes_a_dataset_handle():
    """**整条链路**:上传 → sys_read_file → 句柄 → 统计/画图。

    表格类不变成文本是这条链的关键:三千行的表转文本是四十多万字,塞不进上下文,
    而截断之后模型看到的是半张表,然后基于半张表下结论。
    """
    from cogni_agent.runtime_service.engine import databuf
    from cogni_agent.tool_center_service import agent_files as af
    from cogni_agent.tool_center_service import builtin_tools as bi

    task = "t-upload-chain"
    databuf.clear(task)
    got = await af.save(user_id="u1", scenic_id=SCENIC, file_name="客源.xlsx",
                        content=_xlsx([["城市", "热度"], ["福建省", 62.12],
                                       ["江西省", 8.2], ["上海市", 6.79]]))
    out = await bi.builtin_read_file(file_id=got["id"], _task_id=task)
    assert out["kind"] == "table" and out["dataset"] == "ds_1"
    assert "不要让我把明细贴出来" in out["note"]

    stats = bi.builtin_table_stats(dataset="ds_1", columns="热度", _task_id=task)
    assert stats["columns"][0]["sum"] == 77.11
    chart = bi.builtin_chart(dataset="ds_1", chart_type="bar", label_column="城市",
                             value_columns="热度", _task_id=task)
    assert chart.get("ok") is True
    databuf.clear(task)


@run_async
async def test_reading_a_missing_file_tells_the_model_not_to_guess():
    from cogni_agent.tool_center_service import builtin_tools as bi

    out = await bi.builtin_read_file(file_id="nope", _task_id="t")
    assert "不要猜文件里有什么" in out["error"]
