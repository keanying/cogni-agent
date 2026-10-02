#!/usr/bin/env python
"""
【已废弃】本脚本最后一次有效期为 0043 之前(2026-08 大版本合并):其 _seed 以纯 ORM
直插 Tool/Skill/Agent,携带的 status/suggested_mode/knowledge_sources/constraints 等字段
先后被 0043/0045/0050 迁移删除,0051(Tool 内容列删列、纯版本快照化)后工具内容同样
只能落在版本快照里 —— 直接 ORM 构造已不可能在当前 schema 运行。
链路校验职责已移交:管理后台试运行工具、governance evals 评测、create_p0_group_skills
等脚本 + 部署后健康检查。保留本文件仅为记录历史意图,勿再运行(_seed 会快速失败)。

手动集成校验脚本——不依赖真实模型 API Key,用一个内置的假 OpenAI 兼容服务器
(严格遵循 OpenAI streaming chunk 协议)跑通整条链路,验证:

  1. 真实 Postgres 任务队列(create_task -> claim_one 语义)
  2. 真实 Postgres LangGraph Checkpointer(AsyncPostgresSaver)
  3. 真实 Redis(或内存回退)SSE 事件流,含真实逐 token 流式
  4. 真实 HTTP 工具沙箱执行
  5. 真实 HITL 中断/审批/跨进程恢复(核心难点,单元测试用 InMemorySaver 无法暴露的
     序列化/反序列化问题,只有连真实 Postgres 才能验证到)

跑之前需要:
  1. `.env` 指向一个真实可写的 Postgres + Redis(建表:`python scripts/init_db.py`)
  2. `uv sync`(或至少装好 fastapi/uvicorn/asyncpg/langgraph 等核心依赖)

用法:
  uv run python scripts/manual_e2e_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import configure_logging
from cogni_agent.config import get_settings
from cogni_agent.control_plane_service.queue import create_task, get_task
from cogni_agent.governance_event_bus_service.approvals import Approval, resolve_approval
from cogni_agent.runtime_service import streaming
from cogni_agent.runtime_service.engine.executor import execute_task
from cogni_agent.tool_center_service.agent.models import Agent
from cogni_agent.tool_center_service.skill.models import Prompt, Skill
from cogni_agent.tool_center_service.tool.models import Tool
from sqlalchemy import select

FAKE_MODEL_NAME = "manual-check-fake-model"
FAKE_MODEL_PORT = 8199
FAKE_TOOL_PORT = 8198


def _install_fake_model():
    """把假模型注册进内存态配置,不需要改 config.yaml/.env。"""
    settings = get_settings()
    settings.raw["runtime"]["models"].append(
        {"name": FAKE_MODEL_NAME, "provider": "openai", "api_key": "x", "base_url": f"http://127.0.0.1:{FAKE_MODEL_PORT}/v1"}
    )


def _start_fake_model_server():
    """
    极简 OpenAI 兼容假模型,通用适配任意场景(不区分风险等级):
    - 若还没见过 role=tool 的消息,读取本次请求携带的 tools 定义,调用其中第一个工具
      (每个校验用 Skill 只绑定一个工具,这样足够确定地驱动流程,不需要为每种场景单独
      起一个假模型实例/端口)。
    - 已经看到工具结果 -> 给出一句通用的完成回答。
    真实模拟 OpenAI streaming chunk 协议(含 tool_calls 增量与 usage 统计)。
    """

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            messages = body.get("messages", [])
            tools = body.get("tools") or []
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            created = int(time.time())

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(payload):
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

            if not has_tool_result and tools:
                tool_name = tools[0]["function"]["name"]
                send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": FAKE_MODEL_NAME,
                      "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                          {"index": 0, "id": "call_1", "type": "function", "function": {"name": tool_name, "arguments": "{}"}}
                      ]}, "finish_reason": None}]})
                send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": FAKE_MODEL_NAME,
                      "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                      "usage": {"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36}})
            else:
                final_text = "好的,已根据工具返回结果为您处理完成。"
                for ch in final_text:
                    send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": FAKE_MODEL_NAME,
                          "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}]})
                send({"id": cid, "object": "chat.completion.chunk", "created": created, "model": FAKE_MODEL_NAME,
                      "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 40, "completion_tokens": len(final_text), "total_tokens": 40 + len(final_text)}})
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", FAKE_MODEL_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def _start_fake_tool_backend():
    """假业务后端,供 HTTP 类型 Tool 真实调用(sandbox/local.py 走真实 httpx 请求)。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._respond({"city": "厦门", "weather": "晴", "temp_c": 29})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._respond({"ok": True})

        def _respond(self, payload):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", FAKE_TOOL_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def _seed(risk_level: str) -> tuple[str, str]:
    raise NotImplementedError(
        "manual_e2e_check 已废弃(0043/0045/0050/0051 删列后 ORM seed 无法运行,见文件头说明)。"
        "请改用管理后台/能力生成脚本建资源 + governance evals 校验链路。"
    )
    tool_id, skill_id, agent_id = f"chk_tool_{risk_level}", f"chk_skill_{risk_level}", f"chk_agent_{risk_level}"
    tool_name = "cancel_booking" if risk_level == "high" else "get_scenic_weather"
    async with get_sessionmaker()() as db:
        for existing_id, model in ((tool_id, Tool), (skill_id, Skill), (agent_id, Agent)):
            row = await db.get(model, existing_id)
            if row:
                await db.delete(row)
        # 还要按**名字**清理一次:工具名在租户范围内唯一,别的调试脚本可能用不同的 id
        # 建过同名工具,只按 id 清理会在插入时撞上 uq_tc_tools_scope_name。
        # 这个脚本必须能反复跑,清理不彻底比报错更烦人。
        stale = (await db.execute(select(Tool).where(Tool.name == tool_name))).scalars().all()
        for row in stale:
            await db.delete(row)
        # Prompt 没有以 skill_id 为主键,重复跑脚本时需要显式清理,否则会残留多条
        # is_current=True 的历史记录。(resolver 现在会取最新一条并告警,不再直接抛
        # MultipleResultsFound,但残留数据仍然会让校验结果变得不可预测。)
        old_prompts = (await db.execute(select(Prompt).where(Prompt.skill_id == skill_id))).scalars().all()
        for p in old_prompts:
            await db.delete(p)
        await db.commit()

        method = "POST" if risk_level == "high" else "GET"
        db.add(Tool(
            id=tool_id, name=tool_name, description="手动校验脚本用工具", type="http",
            type_config={"method": method, "url": f"http://127.0.0.1:{FAKE_TOOL_PORT}/x",
                         "params_schema": {"type": "object", "properties": {}}, "headers": {}},
            risk_level=risk_level, status="PUBLISHED", active=True,
            owner_user_id="checker", scenic_id=None, visibility="public",
        ))
        db.add(Skill(
            id=skill_id, name=f"手动校验 Skill({risk_level})", tool_ids=[tool_id], suggested_mode="L2",
            knowledge_sources=[], constraints={}, owner_user_id="checker", scenic_id="demo_scenic", visibility="public",
            status="PUBLISHED",
        ))
        db.add(Prompt(skill_id=skill_id, version="v1", content="你负责处理用户请求。", is_current=True))
        db.add(Agent(
            id=agent_id, name=f"手动校验 Agent({risk_level})", bound_skill_ids=[skill_id], visibility="public",
            status="PUBLISHED", active=True, owner_user_id="checker", scenic_id=None,
        ))
        await db.commit()
    return skill_id, agent_id


async def check_normal_flow():
    print("\n[1/2] 校验普通流程:真实 HTTP 工具调用 + 真实逐 token SSE 流式 …")
    _, agent_id = await _seed("low")
    task = await create_task(
        scenic_id="demo_scenic", source="internal",
        payload={"message": "厦门天气怎么样", "user_id": "checker", "role": "scenic_operator", "overrides": {"model": FAKE_MODEL_NAME}},
        executor_type="agent", executor_id=agent_id,
    )
    task_id = task["id"]

    events = []

    async def consume():
        async for eid, ev, data in streaming.subscribe(task_id, timeout_seconds=30):
            events.append((ev, data))

    consumer = asyncio.create_task(consume())
    await execute_task(task_id)
    await consumer

    row = await get_task(task_id, scenic_id="demo_scenic")
    assert row["status"] == "COMPLETED", f"期望 COMPLETED,实际 {row['status']}: {row.get('result')}"
    assert any(ev == "token" for ev, _ in events), "未收到任何逐 token 流式事件"
    timeline_steps = [d.get("step") for ev, d in events if ev == "timeline"]
    assert "tool_call" in timeline_steps, f"未观察到 tool_call 时间线节点,实际:{timeline_steps}"
    print(f"    ✓ 任务 {task_id[:8]} 正确完成,收到 {len(events)} 条 SSE 事件,最终回答:{row['result']['answer']}")


async def check_hitl_flow():
    print("\n[2/2] 校验 HITL 流程:高风险工具中断 -> 真实 Postgres 断点恢复 …")
    _, agent_id = await _seed("high")
    task = await create_task(
        scenic_id="demo_scenic", source="internal",
        payload={"message": "帮我取消预订", "user_id": "checker", "role": "scenic_operator", "overrides": {"model": FAKE_MODEL_NAME}},
        executor_type="agent", executor_id=agent_id,
    )
    task_id = task["id"]

    await execute_task(task_id)
    row = await get_task(task_id, scenic_id="demo_scenic")
    assert row["status"] == "WAITING_HUMAN", f"期望 WAITING_HUMAN,实际 {row['status']}"

    async with get_sessionmaker()() as db:
        approval = (await db.execute(select(Approval).where(Approval.related_task_id == task_id))).scalars().first()
    assert approval is not None, "未生成审批记录"
    print(f"    ✓ 任务在高风险工具调用前正确暂停,生成审批记录 {approval.id[:8]}")

    await resolve_approval(approval.id, approved=True, opinion="手动校验脚本自动通过", resolved_by="checker")
    row2 = await get_task(task_id, scenic_id="demo_scenic")
    assert row2["status"] == "QUEUED", f"期望审批通过后重新 QUEUED,实际 {row2['status']}"

    # 模拟 worker 认领循环重新调用 execute_task(task_id)
    await execute_task(task_id)
    row3 = await get_task(task_id, scenic_id="demo_scenic")
    assert row3["status"] == "COMPLETED", f"期望恢复后 COMPLETED,实际 {row3['status']}: {row3.get('result')}"
    print(f"    ✓ 审批通过后从真实 Postgres 检查点正确恢复执行,最终回答:{row3['result']['answer']}")


async def main():
    configure_logging()
    _install_fake_model()
    _start_fake_model_server()
    _start_fake_tool_backend()
    await asyncio.sleep(1.0)  # 假服务器启动缓冲;偶发环境较慢时 0.5s 不够会导致首次连接失败

    await check_normal_flow()
    await check_hitl_flow()
    print("\n=== 手动集成校验全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
