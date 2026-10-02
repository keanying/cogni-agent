# -*- coding: utf-8 -*-
"""智能体执行详情导出(非 Quick BI 链路)。

存在的理由很直接:出问题时只能靠截图互相猜,而截图里恰恰缺最要紧的三样 ——
**每一步的提示词、工具入参、结果摘要**。现场那次「分诊判对了却一个工具都没调」,
光看界面只知道「它没查数」,看不出是谁没接这一棒。

这组用例钉两件:导出里该有的东西都在;**凭证一个都不能在里面**
(这份东西本来就是要发出去的 —— 泄漏不是「可能被看到」,是「一定被看到」)。
"""
from __future__ import annotations

import asyncio
import functools

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401  注册全部模型
from cogni_agent.common.db import Base

SCENIC = "PFTSCA01018088"
OTHER = "PFTSCB02000052"
SECRET = "deadbeefcafe4f0e8a1b2c3d4e5f6071"   # 与 .env.quickbi 同形状的密钥字面值


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
    yield


async def _seed(task_id="t-1", scenic=SCENIC):
    from cogni_agent.control_plane_service.models import LLMPromptLog, Task

    async with common_db.get_sessionmaker()() as db:
        db.add(Task(
            id=task_id, scenic_id=scenic, user_id="u-1", executor_type="skill", executor_id="we",
            conversation_id="conv-1", source="internal", status="COMPLETED",
            payload={"message": "近一日景区 PFTSCA01018088 的雷达图数据", "_resume": False},
            result={
                "answer": "本次回答与上一问内容相同。",
                "timeline": [
                    {"step": "version_trace", "detail": "we: 显式指定版本 v3"},
                    {"step": "intent_check", "intent": "数据查询", "action": "execute",
                     "reason": "用户请求查询指定景区昨日的雷达图数据",
                     "target_skills": ["we"], "missing": ["period", "start", "end", "rows"],
                     "degraded": False, "wall": "11:36:14"},
                    {"step": "mode_decision", "mode": "L1"},
                    {"step": "tool_call", "tool_name": "radar_query", "kind": "ok",
                     "args": '{"scenic_id": "PFTSCA01018088", "token": "%s"}' % SECRET,
                     "summary": "12 行", "real_duration_ms": 88.2},
                    {"step": "generation"},
                ],
            }))
        db.add(LLMPromptLog(
            task_id=task_id, conversation_id="conv-1", scenic_id=scenic, user_id="u-1",
            step="intent_check", model="deepseek-v4", prompt_tokens=1450,
            system_prompt=f"你是分诊器。内部凭证 xpftkey={SECRET} 不要外传。",
            history='[{"role": "user", "content": "近一日的数据"}]'))
        await db.commit()


@run_async
async def test_export_contains_the_whole_chain():
    """六段都要在:任务 / 执行链 / 工具入参 / 结果摘要 / 提示词 / 回答。"""
    from cogni_agent.control_plane_service import task_debug as td

    await _seed()
    b = await td.task_debug("t-1", user_id="u-root", role="super_admin", scenic_id=None)
    assert b["task"]["status"] == "COMPLETED"
    assert b["task"]["question"].startswith("近一日")
    assert b["captured"]["timeline"] == 5 and b["captured"]["prompts"] == 1

    md = td.task_debug_markdown(b)
    for must in ("意图分诊", "模式判定", "工具调用", "radar_query",
                 "缺失参数", "结果摘要", "每一步发给模型的提示词", "最终回答"):
        assert must in md, f"导出里少了「{must}」:\n{md[:600]}"
    # 判断依据要在 —— 「为什么这么路由」是排障时第一个要看的
    assert "用户请求查询指定景区昨日的雷达图数据" in md


@run_async
async def test_no_credential_ever_reaches_the_export():
    """**凭证一个都不能在里面。**

    这份东西本来就是要发出去的 —— 泄漏不是「可能被看到」,是「一定被看到」。
    脱敏有三道(键名 / 文本里的裸露写法 / 已知凭证字面值),这里三道都踩一遍:
    工具入参里的 token=、提示词正文里的 xpftkey=。
    """
    from cogni_agent.control_plane_service import task_debug as td

    await _seed()
    b = await td.task_debug("t-1", user_id="u-root", role="super_admin", scenic_id=None)
    md = td.task_debug_markdown(b)
    assert SECRET not in md, "密钥出现在导出里了"
    assert SECRET not in str(b), "密钥出现在诊断包里了"
    # 脱敏不能把整段吞掉 —— 吞掉就看不出「这一步到底传了什么」
    assert "scenic_id" in md and "PFTSCA01018088" in md


@run_async
async def test_scenic_role_cannot_export_another_scenics_task():
    """景区角色导不出别家景区的任务 —— 导出里带着问题原文和提示词,越权就是数据泄漏。"""
    from cogni_agent.common.errors import NotFound
    from cogni_agent.control_plane_service import task_debug as td

    await _seed(task_id="t-other", scenic=OTHER)
    with pytest.raises(NotFound):
        await td.task_debug("t-other", user_id="u-yin", role="scenic_admin", scenic_id=SCENIC)
    # 本景区的能导
    await _seed(task_id="t-mine", scenic=SCENIC)
    b = await td.task_debug("t-mine", user_id="u-yin", role="scenic_admin", scenic_id=SCENIC)
    assert b["task"]["id"] == "t-mine"


@run_async
async def test_empty_timeline_says_so_instead_of_rendering_nothing():
    """时间线为空时要**说出来**。

    渲染成一片空白的话,人会以为是导出坏了,而真相是任务在进入执行图之前就失败了 ——
    这本身就是最有价值的那条线索。
    """
    from cogni_agent.control_plane_service import task_debug as td
    from cogni_agent.control_plane_service.models import Task

    async with common_db.get_sessionmaker()() as db:
        db.add(Task(id="t-empty", scenic_id=SCENIC, user_id="u-1", executor_type="skill",
                    executor_id="we", status="FAILED", payload={"message": "问一句"},
                    result={"error": "resolve_failed: Skill 未指定版本"}))
        await db.commit()

    b = await td.task_debug("t-empty", user_id="u-root", role="super_admin", scenic_id=None)
    md = td.task_debug_markdown(b)
    assert "时间线为空" in md
    assert "resolve_failed" in md, "错误必须出现在导出里"


def test_masking_covers_key_suffixed_header_names():
    """`xpftkey=` 这种形状要盖住。

    原先脱敏的正则只枚举了 apikey|api_key,于是 **xpftkey** 漏在外面 ——
    而那正是数据平台接口真实用的 header 名(见 1.17 那份接口文档)。
    凭证的命名各家各样(appKey / xpftkey / bizKey),枚举不过来,按后缀认才靠得住。
    """
    from cogni_agent.tool_center_service.quickbi.trace import mask

    for text in (f"xpftkey={SECRET}", f"appKey: {SECRET}", f'{{"token":"{SECRET}"}}',
                 f"accessTicket={SECRET}", f"Authorization: Bearer {SECRET}"):
        assert SECRET not in str(mask(text)), f"没盖住:{text[:30]}"

    # 不能把正常内容也盖掉 —— 盖过头就看不出这一步到底传了什么
    keep = "景区 PFTSCA01018088 的雷达图数据,monkey_count=42"
    assert "PFTSCA01018088" in str(mask(keep))


@run_async
async def test_export_never_drops_unknown_timeline_fields():
    """时间线里**不认识的字段也要打出来**。

    原先渲染用的是白名单 —— 于是 `resolve_provenance` 那一行是空的,
    解析期告警(「绑定的工具已不存在,已跳过」这类)一个字都看不到。
    而诊断导出最要紧的线索,往往就在你没预料到的那个字段里。
    """
    from cogni_agent.control_plane_service import task_debug as td
    from cogni_agent.control_plane_service.models import Task

    async with common_db.get_sessionmaker()() as db:
        db.add(Task(id="t-diag", scenic_id=SCENIC, user_id="u-1", executor_type="skill",
                    executor_id="we", status="COMPLETED", payload={"message": "问一句"},
                    result={"answer": "答", "timeline": [
                        {"step": "resolve_provenance",
                         "provenance": {"we": "显式指定版本 v3"}},
                        {"step": "resolve_diagnostic", "level": "warn", "code": "tool_missing",
                         "message": "Skill「we」绑定的工具 t-9 已不存在,已跳过", "ref": "t-9"},
                    ]}))
        await db.commit()

    b = await td.task_debug("t-diag", user_id="u-root", role="super_admin", scenic_id=None)
    md = td.task_debug_markdown(b)
    assert "显式指定版本 v3" in md, "版本溯源的内容丢了"
    assert "已不存在,已跳过" in md, "解析期告警丢了 —— 这正是「为什么没调工具」的答案"
    assert "tool_missing" in md
    assert "level" in md, "连没预料到的字段也要打出来"


@run_async
async def test_self_check_calls_out_a_toolless_run():
    """只剩系统内置工具 = 一个业务工具都没挂上 —— 导出要**直接说出来**。

    现场那次就是这样:分诊判对了(数据查询 → we),能力清单里却只有 6 个 sys_* 工具。
    没有取数工具,模型只能凭上下文说话,答得再像也不是查出来的。
    这条线索原先埋在分诊提示词的正文里,得逐字读才看得见。
    """
    from cogni_agent.control_plane_service import task_debug as td
    from cogni_agent.control_plane_service.models import LLMPromptLog, Task

    history = (
        '[{"role": "user", "content": "今天是 2026-09-20。\\n\\n用户请求:\\n近一日的数据\\n\\n'
        '当前智能体的能力清单:\\n- we: 专注于 we 的智能助手\\n'
        '  可用工具:sys_now, sys_date_range, sys_table_query\\n\\n'
        '已收集的上下文参数(APP端日期选择/历史会话):\\n{}"}]'
    )
    async with common_db.get_sessionmaker()() as db:
        db.add(Task(id="t-noTool", scenic_id=SCENIC, user_id="u-1", executor_type="skill",
                    executor_id="we", status="COMPLETED", payload={"message": "近一日的数据"},
                    result={"answer": "与上一问相同", "timeline": [
                        {"step": "intent_check", "intent": "数据查询", "action": "execute"},
                        {"step": "generation"},
                    ]}))
        db.add(LLMPromptLog(task_id="t-noTool", step="intent_check", model="m",
                            system_prompt="你是分诊器", history=history))
        await db.commit()

    b = await td.task_debug("t-noTool", user_id="u-root", role="super_admin", scenic_id=None)
    assert b["capabilities"]["skills"] == ["we"]
    assert b["capabilities"]["tools"] == ["sys_date_range", "sys_now", "sys_table_query"]

    titles = [c["title"] for c in b["checks"]]
    assert "本次没有任何业务工具可用" in titles, b["checks"]
    assert "整轮没有调用任何工具" in titles

    md = td.task_debug_markdown(b)
    assert "值得注意" in md and "本次可用的技能与工具" in md
    assert "一个都没挂上" in md
