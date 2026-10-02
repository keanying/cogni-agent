"""
M6-06/07:嵌入模型统一入口——文本走 litellm.aembedding()(复用 runtime_service 已经
配置好的模型 Provider 体系,不重新发明一套模型路由),图片走 CLIP(可选依赖
open-clip-torch,未安装时给出清晰报错而不是让调用方以为是别的原因失败)。

embedding 维度必须和 kb_chunks.embedding 列的 Vector(EMBED_DIM) 一致(见
multi2vec_clip_knowledge/store.py::EMBED_DIM = 768)。不同 provider 的向量维度不同
(如 OpenAI text-embedding-3-small 是 1536,bge-base 是 768),因此这里做一次统一
的维度对齐:模型输出维度 > 目标维度时截断,< 目标维度时补零。这不是最优做法,但避免了
"换一个嵌入模型就要连带改表结构/迁移历史数据"的强耦合;真实生产环境建议 EMBED_DIM
直接对齐所选模型的原生维度。
"""
from __future__ import annotations

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.knowledge_service.multi2vec_clip_knowledge.store import EMBED_DIM

log = get_logger("knowledge_service.embeddings")


def _align_dim(vec: list[float]) -> list[float]:
    if len(vec) == EMBED_DIM:
        return vec
    if len(vec) > EMBED_DIM:
        return vec[:EMBED_DIM]
    return vec + [0.0] * (EMBED_DIM - len(vec))


async def resolve_embedding_model() -> tuple[str, dict] | None:
    """解析嵌入模型配置。

    与对话模型走**同一套模型注册表**(「系统设置 · 模型与预算」+ config.yaml),
    不再单独维护一份 —— 否则用户得在两个地方各配一次,而且很容易只配了一个就以为好了。

    优先级:
      1. 页面上 `model_type='embedding'` 且「设为默认」的模型(每种类型各有一个默认)
      2. config.yaml 的 knowledge.embedding_model(需在 runtime.models 里登记)

    返回 None 表示没有可用的嵌入模型,由调用方给出可操作的报错。
    """
    from cogni_agent.runtime_service.llm.client import _db_model_configs, _static_model_configs

    # 1) 页面上嵌入类型的默认模型
    try:
        from sqlalchemy import select

        from cogni_agent.common.db import get_sessionmaker
        from cogni_agent.governance_event_bus_service.settings import LlmModelConfig

        async with get_sessionmaker()() as db:
            row = (await db.execute(
                select(LlmModelConfig).where(LlmModelConfig.model_type == "embedding",
                                             LlmModelConfig.is_default.is_(True),
                                             LlmModelConfig.active.is_(True))
            )).scalars().first()
        if row is not None:
            db_models = await _db_model_configs()
            cfg = db_models.get(row.id)  # db 清单以不可变的 id 为 key,展示名可改
            if cfg:
                return row.id, cfg
    except Exception:  # noqa: BLE001 - 库不可用时回落到文件配置
        log.debug("查询嵌入模型配置失败,回落到 config.yaml")

    # 2) config.yaml:knowledge.embedding_model 可能填展示名或供应商 ID,按别名匹配 db 清单
    name = get_settings().get("knowledge", "embedding_model")
    if not name:
        return None
    for k, v in (await _db_model_configs()).items():
        if v.get("name") == name or (v.get("model_id") and v["model_id"] == name):
            return k, v
    cfg = _static_model_configs().get(name)
    if cfg:
        return name, cfg
    for k, v in _static_model_configs().items():  # 静态清单的 key 即 name,按 model_id 兜底
        if v.get("model_id") == name:
            return k, v
    return None


class EmbeddingNotConfigured(RuntimeError):
    """没有可用的嵌入模型。单独一个类型,便于上层给出「去哪配」的引导而不是笼统报错。"""


async def embed_text(text: str) -> list[float]:
    """生成文本嵌入。

    没有可用嵌入模型时抛 EmbeddingNotConfigured 并说明**具体去哪配**,
    而不是静默返回全零向量 —— 全零向量会让检索"能跑但永远召回不到东西",
    这种失败比直接报错难查得多。
    """
    resolved = await resolve_embedding_model()
    if resolved is None:
        raise EmbeddingNotConfigured(
            "尚未配置嵌入模型,无法把文档转成向量。\n"
            "请到「系统设置 · 模型与预算」的「嵌入模型」清单添加一个**支持 embedding 接口**的模型"
            "(如 bge-m3、text-embedding-3-small、doubao-embedding),填好 API Key 后"
            "点「设为默认」。\n"
            "注意:对话模型和嵌入模型是两类不同的接口,通常需要分别配置。"
        )
    model_name, model_cfg = resolved

    import litellm

    # 用供应商模型 ID(model_id)调模型;展示名只用于列表展示与选择
    model_id = model_cfg.get("model_id") or model_name
    resp = await litellm.aembedding(
        model=f"openai/{model_id}",     # 统一走 OpenAI 兼容协议,与对话模型一致
        input=[text],
        api_key=model_cfg.get("api_key"),
        base_url=model_cfg.get("base_url"),
    )
    vec = resp.data[0]["embedding"]
    return _align_dim(vec)


async def embed_image(file_path: str) -> list[float]:
    """CLIP 图像嵌入(可选依赖 open-clip-torch,对应 pyproject.toml 的 clip extra)。"""
    try:
        import open_clip
        import torch
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "图片嵌入需要安装可选依赖组(`uv sync --extra clip`):open-clip-torch / torch / pillow"
        ) from exc

    model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
    model.eval()
    image = preprocess(Image.open(file_path).convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        features = model.encode_image(image)
        features = features / features.norm(dim=-1, keepdim=True)
    return _align_dim(features[0].tolist())
