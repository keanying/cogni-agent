"""管理端「系统功能说明」只读 API(系统设置 → 系统功能说明页面)。

数据源 = cogni_agent/feature_docs/**/*.md(仓库内 tracked,随镜像发布,支持
子目录分域);每次请求实时读文件——改文档重新部署即生效,无缓存无版本表。
纯只读,不写任何库表;鉴权与管理后台一致(登录即可读,内容不含敏感信息)。

目录约定(2026-09-11):子目录 = 功能域,与代码架构(cogni_agent 包)对应,
如 scheduling/(调度与预热)。文档多了靠目录树定位,不再只靠文件名猜;
返回项带 directory 字段(相对路径目录,根下为空串),前端按目录分组渲染。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from cogni_agent.gateway_service.deps import CurrentUser, get_current_user
from fastapi import Depends

router = APIRouter(prefix="/admin/feature-docs", tags=["feature-docs"])

# cogni_agent/feature_docs(gateway_service 上一级的包目录,随镜像发布)
_DOCS_DIR = Path(__file__).resolve().parent.parent / "feature_docs"

# frontmatter 解析:开头 --- 到 --- 之间的 `key: value` 行(值可含中文/逗号)
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^([a-z_]+):\s*(.*)$")

# doc_id 允许子目录段(如 调度与预热/prewarm):段字符 = 字母/数字/_/-(\w 含中文,
# 目录名用中文),斜杠分隔;详情路由用 :path 转换器接住多段
_DOC_ID_RE = re.compile(r"\A[\w\-]+(?:/[\w\-]+)*\Z")


def _parse_doc(path: Path, doc_id: str) -> dict:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = _FM_RE.match(text)
    content = text[m.end():] if m else text
    if m:
        for line in m.group(1).splitlines():
            kv = _KV_RE.match(line.strip())
            if kv:
                meta[kv.group(1)] = kv.group(2).strip()
    return {
        "id": doc_id,
        "title": meta.get("title") or path.stem,
        "category": meta.get("category") or "未分类",
        "summary": meta.get("summary") or "",
        "related": [s.strip() for s in meta.get("related", "").split(",") if s.strip()],
        "updated_at": path.stat().st_mtime,
        "content": content.lstrip("\n"),
    }


def _list_raw() -> list[dict]:
    if not _DOCS_DIR.is_dir():
        return []
    docs: list[dict] = []
    for p in sorted(_DOCS_DIR.rglob("*.md")):
        rel = p.relative_to(_DOCS_DIR).with_suffix("")
        docs.append({
            **{k: v for k, v in _parse_doc(p, str(rel).replace("\\", "/")).items()
               if k != "content"},
            "directory": str(rel.parent).replace("\\", "/") if str(rel.parent) != "." else "",
        })
    docs.sort(key=lambda d: (d["directory"], d["title"]))
    return docs


@router.get("")
async def list_feature_docs(user: CurrentUser = Depends(get_current_user)):
    """文档列表(不含正文):搜索/目录分组在前端做(量小,一次性拉全量索引)。"""
    return {"items": _list_raw()}


@router.get("/{doc_id:path}")
async def get_feature_doc(doc_id: str, user: CurrentUser = Depends(get_current_user)):
    """单篇详情(含正文 markdown)。doc_id 可含子目录段(调度与预热/prewarm,
    :path 转换器接住斜杠——否则多段 id 匹配不到路由,页面点击无响应)。"""
    if not _DOC_ID_RE.fullmatch(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    path = (_DOCS_DIR / f"{doc_id}.md").resolve()
    if not path.is_relative_to(_DOCS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="文档不存在")   # 防路径穿越
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文档不存在")
    doc = _parse_doc(path, doc_id)
    doc["directory"] = doc_id.rsplit("/", 1)[0] if "/" in doc_id else ""
    return doc
