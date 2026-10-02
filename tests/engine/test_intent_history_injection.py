"""v1.1.7 多轮会话历史注入(Triager)的纯函数与数据源单测。

目的:让 Triager 看到上一轮对话内容,避免重复反问(如已说过景区仍再问一次)。
覆盖:
- resolve_history_rounds:未配置=0 / 显式 0-5 / 越界 clamp / 类型非法归 0
- format_history_text:空、多轮正序渲染、超长单条截断、未完成轮标注、总量兜底删最早轮
- _triage_cache_key:history_text 参与缓存键(同 goal 不同历史不命中)
- _SYSTEM_TRIAGE:已含多轮延续判定标准
- conversations.get_recent_turns:按会话取最近 N 轮(排除本轮/空消息轮/未完成标注/时间序)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cogni_agent.control_plane_service import conversations
from cogni_agent.control_plane_service.models import Task
from cogni_agent.runtime_service.engine import intent


# ─────────────── resolve_history_rounds(§4.3:未配置=0,0 是有效值,无框架默认) ───────────────


def test_resolve_missing_is_zero():
    assert intent.resolve_history_rounds(None) == 0
    assert intent.resolve_history_rounds({}) == 0
    assert intent.resolve_history_rounds({"enabled": True}) == 0


def test_resolve_explicit_zero_is_zero():
    # 0 是有效值 = 显式关闭注入,不是「无效值」
    assert intent.resolve_history_rounds({"history_rounds": 0}) == 0


def test_resolve_in_range():
    assert intent.resolve_history_rounds({"history_rounds": 2}) == 2
    assert intent.resolve_history_rounds({"history_rounds": 5}) == 5


def test_resolve_clamp_above_five():
    assert intent.resolve_history_rounds({"history_rounds": 6}) == 5
    assert intent.resolve_history_rounds({"history_rounds": 99}) == 5


def test_resolve_negative_is_zero():
    assert intent.resolve_history_rounds({"history_rounds": -1}) == 0


def test_resolve_illegal_type_is_zero():
    # bool 是 int 子类,必须单独拦截(True 不是合法轮数)
    assert intent.resolve_history_rounds({"history_rounds": True}) == 0
    assert intent.resolve_history_rounds({"history_rounds": False}) == 0
    assert intent.resolve_history_rounds({"history_rounds": "2"}) == 0
    assert intent.resolve_history_rounds({"history_rounds": 2.0}) == 0


# ─────────────── format_history_text(§5.3:单条 300 截断 / 总量兜底删最早轮) ───────────────


def test_format_empty():
    assert intent.format_history_text([]) == ""
    assert intent.format_history_text(None) == ""


def test_format_multi_turn_in_order():
    turns = [
        {"role": "user", "content": "看下7月份的舆情"},
        {"role": "assistant", "content": "请问具体哪一天？"},
        {"role": "user", "content": "每一天的都分析下"},
        {"role": "assistant", "content": "收到"},
    ]
    text = intent.format_history_text(turns)
    assert "用户: 看下7月份的舆情" in text
    assert "助手: 请问具体哪一天？" in text
    assert "用户: 每一天的都分析下" in text
    # 时间正序渲染(因果阅读)
    assert text.index("看下7月份的舆情") < text.index("每一天的都分析下")


def test_format_unfinished_turn_marked():
    # §5.2:未完成标注由数据源层(get_recent_turns)归一化完成,format 层只渲染;
    # 此处输入即归一化后的 turns,断言标注文本原样进入提示词段
    turns = [
        {"role": "user", "content": "查一下今天的门票"},
        {"role": "assistant", "content": "(该轮未完成)"},
    ]
    text = intent.format_history_text(turns)
    assert "用户: 查一下今天的门票" in text
    assert "助手: (该轮未完成)" in text


def test_format_long_message_truncated():
    long_content = "长" * 500
    turns = [{"role": "user", "content": long_content}, {"role": "assistant", "content": "好"}]
    text = intent.format_history_text(turns)
    user_line = text.split("\n")[0]
    # 300(截断) + 1(省略号) + 4(前缀「用户: 」)
    assert len(user_line) == intent._HISTORY_MSG_MAX + 5
    assert user_line.endswith("…")
    assert "助手: 好" in text


def test_format_total_cap_drops_oldest_turn():
    # 正常参数下单条 300 / 轮数上限 5 总量必然 < 4000,兜底不触发;
    # 此处直接构造超长多轮(8 轮 × 每条超长)验证兜底逻辑:
    # 总量超 _HISTORY_TOTAL_MAX 时从最早整轮删除,保留最近轮
    turns: list[dict] = []
    for i in range(8):
        turns.append({"role": "user", "content": f"u{i}" * 500})
        turns.append({"role": "assistant", "content": f"a{i}" * 500})
    text = intent.format_history_text(turns)
    assert len(text) <= intent._HISTORY_TOTAL_MAX
    assert "u0" not in text  # 最早轮被删
    assert "u7" in text  # 最近轮保留


# ─────────────── _triage_cache_key(§7:history 参与键,防「缓存化话题漂移」) ───────────────


def test_cache_key_differs_by_history():
    base = dict(goal="每一天的都分析下", capabilities=[], slots={}, model=None, conversation_id="conv_x")
    k_empty = intent._triage_cache_key(**base, history_text="")
    k_hist = intent._triage_cache_key(**base, history_text="用户: 看下7月份的舆情\n助手: 请问具体哪一天？")
    assert k_empty != k_hist
    # 同 goal + 同历史 → 命中(幂等重发/网络重试)
    k_hist2 = intent._triage_cache_key(**base, history_text="用户: 看下7月份的舆情\n助手: 请问具体哪一天？")
    assert k_hist == k_hist2


# ─────────────── 框架提示词已含多轮延续标准(§6.2/6.3) ───────────────


def test_system_triage_has_multi_turn_rules():
    assert "多轮延续优先" in intent._SYSTEM_TRIAGE
    assert "历史只作上下文" in intent._SYSTEM_TRIAGE


# _SYSTEM_RECHECK 的测试已删除:recheck(反问前二次确认)在 27 号评审时
# 因「不在意图层叠加 LLM 调用」被移除,常量随之不存在。
# 多轮延续规则现在只在 _SYSTEM_TRIAGE 里,由上面的
# test_system_triage_has_multi_turn_rules 覆盖。


# ─────────────── conversations.get_recent_turns(§5:数据源集成,真实测试库) ───────────────


def _mk_task(conv_id: str, message: str | None, answer: str | None, ts: datetime,
             tid: str | None = None) -> Task:
    return Task(
        id=tid,
        conversation_id=conv_id,
        executor_type="agent",
        executor_id="agt_x",
        status="COMPLETED",
        payload={"message": message},
        result={"answer": answer},
        created_at=ts,
    )


@pytest.mark.asyncio
async def test_get_recent_turns_basic(db_session):
    conv = "conv_hist_1"
    now = datetime.now(timezone.utc)
    db_session.add_all([
        _mk_task(conv, "第一轮问题", "第一轮回答", now),
        _mk_task(conv, "第二轮问题", "第二轮回答", now + timedelta(seconds=1)),
        _mk_task(conv, "第三轮问题", "第三轮回答", now + timedelta(seconds=2)),
    ])
    await db_session.commit()

    turns = await conversations.get_recent_turns(conv, limit=2)
    # 最近 2 轮、时间正序、user/assistant 交替
    assert [t["content"] for t in turns] == [
        "第二轮问题", "第二轮回答", "第三轮问题", "第三轮回答",
    ]
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_get_recent_turns_excludes_current_and_system(db_session):
    conv = "conv_hist_2"
    now = datetime.now(timezone.utc)
    db_session.add_all([
        _mk_task(conv, "老问题", "老回答", now),
        _mk_task(conv, "", "系统无消息轮", now + timedelta(seconds=1)),  # 空消息系统任务:跳过
        _mk_task(conv, "本轮问题", "本轮回答", now + timedelta(seconds=2), tid="cur_task_1"),
    ])
    await db_session.commit()

    turns = await conversations.get_recent_turns(conv, exclude_task_id="cur_task_1", limit=3)
    texts = [t["content"] for t in turns]
    assert "本轮问题" not in texts  # 本轮被排除
    assert "系统无消息轮" not in texts  # 空消息轮被跳过(不占轮数)
    assert texts == ["老问题", "老回答"]


@pytest.mark.asyncio
async def test_get_recent_turns_unfinished_marked(db_session):
    conv = "conv_hist_3"
    now = datetime.now(timezone.utc)
    db_session.add_all([
        _mk_task(conv, "中断的问题", None, now),  # 无 answer(失败/中断)
    ])
    await db_session.commit()

    turns = await conversations.get_recent_turns(conv, limit=1)
    assert turns[0] == {"role": "user", "content": "中断的问题"}
    assert turns[1]["content"] == "(该轮未完成)"


@pytest.mark.asyncio
async def test_get_recent_turns_zero_limit_returns_empty(db_session):
    # limit<=0 短路,不进 DB(history_rounds=0 时调用方零成本)
    assert await conversations.get_recent_turns("conv_whatever", limit=0) == []
