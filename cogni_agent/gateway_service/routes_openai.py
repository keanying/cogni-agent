"""
G-API-01/05/06/07:开放平台 OpenAI 兼容协议入口。

POST /v1/chat/completions - 请求体必填扩展字段 agent_id;
GET  /v1/models           - 列出当前 org 可调用的 agent_id(映射为 model 字段)。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cogni_agent.common.errors import ZhilvError
from cogni_agent.control_plane_service.queue import create_task, get_task
from cogni_agent.gateway_service.deps import CurrentApiKey, get_current_api_key
from cogni_agent.runtime_service import streaming

router = APIRouter(prefix="/v1", tags=["开放平台 · OpenAI 兼容"])

_POLL_INTERVAL = 0.4
_MAX_WAIT_SECONDS = 300


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    agent_id: str            # 平台自定义扩展字段,置于 body 顶层(G-API-06)
    messages: list[Message]
    stream: bool = False
    user: str | None = None  # 末端用户标识,用于成本归属


def _latest_user_message(messages: list[Message]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return messages[-1].content if messages else ""


@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, key: CurrentApiKey = Depends(get_current_api_key)):
    """
    G-API-05 鉴权链路(简化版,生产实现需补全):
    1. api_key 校验(已由 get_current_api_key 完成)
    2. 校验 org 是否在该 agent 的 allowed_orgs 内
    3. 校验 key 对该 agent 是否有调用授权
    4. 通过则建任务,转发进 control-plane,与「对话」页走同一条 runtime_service.engine.executor 执行链路
    """
    payload = {
        "message": _latest_user_message(req.messages),
        "user_id": req.user or key.key_id,
        "role": "scenic_operator",
        "overrides": None,
        "override_mode": None,
    }
    task = await create_task(
        scenic_id=key.scenic_id, source="openapi", payload=payload,
        executor_type="agent", executor_id=req.agent_id,
        user_id=req.user or key.key_id,
    )
    task_id = task["id"]
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"

    if req.stream:
        async def event_gen():
            async for _event_id, event_name, data in streaming.subscribe(task_id):
                if event_name == "token":
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": req.agent_id,
                        "choices": [{"index": 0, "delta": {"content": data.get("delta", "")}, "finish_reason": None}],
                    }
                    yield {"event": "message", "data": json.dumps(chunk, ensure_ascii=False)}
                elif event_name in ("done", "error", "cancelled"):
                    finish_reason = "stop" if event_name == "done" else "content_filter" if event_name == "cancelled" else "error"
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": req.agent_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    }
                    yield {"event": "message", "data": json.dumps(chunk, ensure_ascii=False)}
                    yield {"event": "message", "data": "[DONE]"}
                    return
        return EventSourceResponse(event_gen())

    # 非流式:与「对话」页的 /chat/result/{task_id} 是同一种"等任务跑完再一次性拿结果"的用法,
    # 这里做成阻塞轮询,方便开放平台调用方按标准 OpenAI SDK 的同步用法直接拿到完整响应。
    waited = 0.0
    result_row = None
    while waited < _MAX_WAIT_SECONDS:
        result_row = await get_task(task_id, scenic_id=key.scenic_id)
        if result_row["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    if result_row is None or result_row["status"] not in ("COMPLETED",):
        reason = (result_row or {}).get("result") or {"error": "任务执行超时"}
        raise ZhilvError(f"任务未成功完成: {reason}")

    result = result_row.get("result") or {}
    usage = result.get("usage") or {}
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.agent_id,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": result.get("answer", "")}, "finish_reason": "stop"},
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
        },
    }


@router.get("/models")
async def list_models(key: CurrentApiKey = Depends(get_current_api_key)):
    """列出该 api_key 可调用的 agent_id,映射为 OpenAI 的 model 字段。"""
    return {"object": "list", "data": [{"id": aid, "object": "model"} for aid in key.allowed_agent_ids]}
