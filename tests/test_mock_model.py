"""Mock 模型测试:命中测试模型时,所有 LLM 调用返回本地 mock 数据,完全不经过真实 LLM。

与 conftest 的其他引擎测试不同,这里不替换 llm_client 的调用函数,而是验证
client.py 自身的 mock 短路逻辑(provider/模型名命中 → 直接返回模拟数据)。
"""
from __future__ import annotations

from cogni_agent.runtime_service.llm import client
from cogni_agent.runtime_service.llm.client import _is_mock_model


async def _fake_cfg(model_name: str | None):
    """让 _model_cfg 恒解析出 name=test 的模型配置(不连真实 DB)。"""
    name = model_name or "test"
    return name, {"provider": "openai", "base_url": "http://localhost:9999/v1"}


async def _fake_cfg_real(model_name: str | None):
    name = model_name or "qwen2.5-72b"
    return name, {"provider": "dashscope", "base_url": "http://x", "api_key": "k"}


def test_is_mock_model():
    assert _is_mock_model("test", {"provider": "openai"})
    assert _is_mock_model("Test", {"provider": "openai"})
    assert _is_mock_model("  test  ", {"provider": "openai"})
    assert _is_mock_model("anything", {"provider": "mock"})
    assert not _is_mock_model("qwen2.5-72b", {"provider": "dashscope"})


async def test_complete_collected_mock_with_system(monkeypatch):
    """主执行链路(带 system):返回贴合用户问题的 mock 文本,并估算 token。"""
    monkeypatch.setattr(client, "_model_cfg", _fake_cfg)
    result = await client.complete_collected(
        "test",
        [
            {"role": "system", "content": "你是任务分诊员,只输出 JSON。"},
            {"role": "user", "content": "帮我查一下今天景区的客流"},
        ],
    )
    assert result.content
    assert "帮我查一下今天景区的客流" in result.content
    assert result.finish_reason == "stop"
    assert result.prompt_tokens > 0 and result.completion_tokens > 0


async def test_complete_collected_mock_no_system(monkeypatch):
    """推荐追问类调用(无 system):按协议返回 3 行问题列表。"""
    monkeypatch.setattr(client, "_model_cfg", _fake_cfg)
    result = await client.complete_collected(
        "test",
        [{"role": "user", "content": "根据以下对话,生成 3 条用户可能感兴趣的追问。"}],
    )
    lines = [l for l in result.content.strip().split("\n") if l.strip()]
    assert len(lines) == 3


async def test_stream_and_collect_mock_writes_tokens(monkeypatch):
    """流式调用:mock 同样逐个 writer 推送 token,SSE 打字机效果正常。"""
    monkeypatch.setattr(client, "_model_cfg", _fake_cfg)
    deltas: list[dict] = []
    result = await client.stream_and_collect(
        "test",
        [
            {"role": "system", "content": "你是任务执行监督员。"},
            {"role": "user", "content": "目标:查询客流,当前步骤:无"},
        ],
        writer=lambda ev: deltas.append(ev),
    )
    assert result.content
    assert deltas
    # mock 会同时推 reasoning(思考流)和 token(正文)——
    # 推理模型的思考过程是后来加的特性,断言「全是 token」是旧预期。
    # 这里要保证的是:**正文 token 确实推了**,且不出现未知事件类型
    # (出现未知类型说明协议变了,前端会静默丢弃这些事件)。
    kinds = {ev["type"] for ev in deltas}
    assert "token" in kinds, "正文 token 没有推送,SSE 打字机效果会是空的"
    assert kinds <= {"token", "reasoning"}, f"出现未知事件类型:{kinds - {'token', 'reasoning'}}"
    # **正文只由 token 事件拼成**,reasoning 是思考过程、不进最终答复 ——
    # 这正是把两者分成不同事件类型的意义:前端把思考流单独折叠展示,
    # 拼进正文的话用户会看到一段「我需要先确定…」的自言自语。
    assert "".join(ev["delta"] for ev in deltas
                   if ev["type"] == "token") == result.content
    assert result.prompt_tokens > 0 and result.completion_tokens > 0


async def test_non_mock_model_still_calls_llm(monkeypatch):
    """非测试模型不受影响,仍然走真实 litellm 调用。"""
    monkeypatch.setattr(client, "_model_cfg", _fake_cfg_real)
    called: dict = {}

    class _Msg:
        content = "真实回复"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None

    async def _acompletion(**kwargs):
        called["model"] = kwargs.get("model")
        return _Resp()

    monkeypatch.setattr(client.litellm, "acompletion", _acompletion)
    result = await client.complete_collected("qwen2.5-72b", [{"role": "user", "content": "hi"}])
    assert result.content == "真实回复"
    assert called["model"] == "openai/qwen2.5-72b"
