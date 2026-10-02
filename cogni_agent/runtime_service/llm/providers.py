"""
模型供应商清单 —— 由后端下发,前端「系统设置 · 模型与预算」据此渲染下拉与提示。

放后端的理由和工具模板一样:真正决定"这个 provider 怎么调"的是 client.py 和 litellm,
写在前端就是两份真相。加一个新供应商只需要在这里加一条,前端不用改。

**关于 base_url**:这里给的是官方默认地址。私有化部署 / 专线接入的用户可以在页面上覆盖,
所以字段是 `default_base_url` 而不是写死。
"""
from __future__ import annotations

PROVIDERS = [
    {
        "id": "volcengine",
        "label": "火山引擎(方舟)",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model_name_hint": "填**推理接入点 ID**(形如 ep-20240xxx-xxxxx),不是「doubao-pro-32k」这种模型名",
        "note": "火山方舟通过「推理接入点」调用:在方舟控制台创建接入点后拿到 ep-xxx 的 ID,"
                "把它作为模型名填在这里。API Key 用方舟的 API Key。"
                "如果你所在区域不是华北(cn-beijing),需要相应修改上面的地址。",
        "docs": "https://www.volcengine.com/docs/82379",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/v1",
        "model_name_hint": "如 deepseek-chat / deepseek-reasoner",
        "note": "",
        "docs": "https://platform.deepseek.com/api-docs/",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼(通义)",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_name_hint": "如 qwen-max / qwen2.5-72b-instruct",
        "note": "使用 OpenAI 兼容模式地址(路径里带 compatible-mode),不要填原生 DashScope 地址。",
        "docs": "https://help.aliyun.com/zh/model-studio/",
    },
    {
        "id": "moonshot",
        "label": "月之暗面(Kimi)",
        "default_base_url": "https://api.moonshot.cn/v1",
        "model_name_hint": "如 moonshot-v1-32k",
        "note": "",
        "docs": "https://platform.moonshot.cn/docs",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model_name_hint": "如 glm-4-plus",
        "note": "",
        "docs": "https://open.bigmodel.cn/dev/api",
    },
    {
        "id": "openai",
        "label": "OpenAI 兼容(含自建 vLLM / Ollama)",
        "default_base_url": "http://localhost:8000/v1",
        "model_name_hint": "填该服务实际暴露的模型名",
        "note": "任何提供 OpenAI 兼容接口的服务都可以用这个:vLLM、Ollama、one-api、"
                "以及各家云厂商的兼容端点。自建服务通常不校验 API Key,可随便填一个非空值。",
        "docs": "",
    },
]

_BY_ID = {p["id"]: p for p in PROVIDERS}


def litellm_prefix(provider: str) -> str:
    """provider -> litellm 的模型前缀。

    litellm 靠 `<prefix>/<model>` 决定用哪套协议。国内这些厂商基本都提供 OpenAI 兼容
    接口,统一走 `openai/` 前缀 + 自定义 base_url 是最稳的做法:
    - 不依赖 litellm 是否内置了某个厂商的适配(内置适配的字段映射有时会滞后于厂商更新)
    - 换厂商只是换 base_url,行为可预期
    火山方舟同理:它的 /api/v3 就是 OpenAI 兼容协议。
    """
    return "openai"


def default_base_url(provider: str) -> str | None:
    p = _BY_ID.get(provider)
    return p["default_base_url"] if p else None


def all_providers() -> list[dict]:
    return PROVIDERS


# SDK 会自己拼接具体接口方法路径(/chat/completions 等),base_url 只需要到**版本号**那一层
# (如 .../api/v3、.../v1)。用户从厂商文档里复制的往往是完整接口地址,带着方法后缀 ——
# 填进来会变成 .../v1/chat/completions/chat/completions,报 404,而错误信息
# 完全指不到「你多复制了一截路径」。
#
# **只去掉方法那一截,不能连版本号一起砍** —— 砍掉 /v1 之后地址就彻底错了,
# 那比不处理还糟。所以按方法名匹配,而不是按包含版本号的整段路径匹配。
_METHOD_SUFFIXES = ("/chat/completions", "/completions", "/embeddings", "/messages")


def normalize_base_url(url: str | None) -> tuple[str, str | None]:
    """归一化 base_url,返回 (处理后的地址, 提示信息)。

    提示信息不为空时前端应当展示 —— 静默改掉用户填的值同样不好:
    他下次还会这么填,而且会疑惑「我明明填的是另一个地址」。
    """
    if not url:
        return "", None
    cleaned = url.strip().rstrip("/")
    for suffix in sorted(_METHOD_SUFFIXES, key=len, reverse=True):
        if cleaned.endswith(suffix):
            trimmed = cleaned[: -len(suffix)].rstrip("/")
            if not trimmed or "://" not in trimmed:
                return cleaned, None      # 砍完不是合法地址了,原样保留
            return trimmed, (
                f"已自动去掉地址末尾的 {suffix} —— SDK 会自己拼接这段路径,"
                f"填了会变成重复路径导致 404。实际使用:{trimmed}"
            )
    return cleaned, None
