# -*- coding: utf-8 -*-
"""「跑成功了,但用户什么也没看到」的三个来源。

这一组都不报错 —— 任务是 COMPLETED、运行记录是绿的、耗时十几秒像是真干了活,
而用户那边是一张空白卡片,或者一句「还没有收到您的具体问题」。运营看着一条成功记录,
完全不知道该查什么,只能截图来问。

三个来源各自独立,但表现一模一样:

1. **ReAct 循环不把本轮消息发给模型** —— 工具调用成功、返回了数据,下一轮生成却
   既看不到用户问的话、也看不到工具返回,只能照着系统提示词里的上下文参数瞎答;
2. **小Q 的最终答复被归进了思考过程** —— 推理和答复是两段独立文本,中间隔着别的事件,
   而文本拼接器一路攒下来,把答复也当成了推理;
3. **一个字都没产出却记成 success** —— 没有回答、没有图表、没有报告,状态却是成功。
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ══════════════════════════════════════════════════════════════════════
# 一、ReAct 循环:本轮的问题与工具结果必须发给模型
# ══════════════════════════════════════════════════════════════════════

# graphs.py 顶层要 langchain_core,本环境装不了 —— 所以这几条按**源码文本**检查。
# 检查文本看着笨,但它守的正是这次出事的那一行:它被删掉之后没有任何报错,
# 整条 ReAct 链静默失效(工具照样调、数据照样取、然后结果被丢掉),
# 而任务状态一路 COMPLETED。这种东西必须有人盯着。
_GRAPHS = Path(__file__).resolve().parent.parent / "cogni_agent" / "runtime_service" / "engine" / "graphs.py"
_SRC = _GRAPHS.read_text(encoding="utf-8")


def test_agent_node_sends_the_current_turn_to_the_model():
    """**这是「工具成功了却答非所问」的根因。**

    `outgoing` 曾经只有 系统提示词 + 注入的历史轮次,而历史是**别的轮次**的纯文本
    (get_recent_turns 明确 exclude 了本任务)。于是本轮的一切都没发出去:
    模型看不到用户问什么,更看不到工具返回了什么 —— 而收尾提示词还写着
    「请基于已获取的数据与工具执行结果作答」,那些结果根本不在上下文里。
    """
    assert "_current_turn_messages(state)" in _SRC, \
        "agent_node 没把本轮消息发给模型 —— 工具结果会被丢掉,而任务照样 COMPLETED"
    # 必须在 outgoing 的装配段里,不能只是定义了没用
    seg = _SRC[_SRC.index("outgoing: list[dict] = [{\"role\": \"system\""):]
    assert "_current_turn_messages(state)" in seg[:2000], "本轮消息没有被 extend 进 outgoing"


def test_current_turn_starts_at_the_last_human_message_inclusive():
    """切片起点必须**含**最后一条 HumanMessage。

    取成「它之后」的话,上下文里就没有用户问的那句话了 ——
    正是现场那句「我看到带了景区和日期,但还没有收到您的具体问题」的由来。
    """
    body = _SRC[_SRC.index("def _current_turn_messages"):]
    body = body[:body.index("\ndef ", 10)]
    assert "msgs[start:]" in body, "要从 HumanMessage 本身开始切,不是从它之后"
    assert "list(msgs)" in body, "一条 HumanMessage 都没有时应回退为全部,而不是空上下文"


def test_old_turns_are_still_injected_as_plain_text():
    """反向保证:不能因为这次修复,把旧轮的工具结果原文又翻回上下文里。

    那是另一个坑(旧轮工具原文会教模型不重新取数),当初改成按轮注入文本就是为了它。
    两个目的要同时成立:旧轮只给文本、本轮给完整消息。
    """
    assert "exclude_task_id=state.get(\"task_id\")" in _SRC, \
        "注入历史时必须排除本任务,否则本轮会被当成历史重复注入一遍"


# ══════════════════════════════════════════════════════════════════════
# 二、小Q 的最终答复不能被归进思考过程
# ══════════════════════════════════════════════════════════════════════

def _split(events):
    from cogni_agent.tool_center_service.quickbi.events import AnswerSplitter

    sp = AnswerSplitter()
    out = []
    for e in events:
        out += sp.feed(e)
    out += sp.flush()
    answer = "".join(o.get("delta", "") for o in out
                     if o.get("type") == "text" and (o.get("section") or "answer") == "answer")
    reasoning = "".join(o.get("delta", "") for o in out if o.get("type") == "reasoning")
    return answer, reasoning


_NL2SQL = ("问题选中数据集：九龙潭-OTA-评级雷达图分数数据\n"
           "提及字段及维值：景区ID(PFTSCA01018088)\n"
           "判定0：查询近一日的雷达图各评分数据；范围=昨日。\n"
           "判定4：字段均为 number，直接用。")
_REPLY = "该问题与小Q问数的数据集无关，请重新提问"


def test_reply_after_the_reasoning_block_is_an_answer_not_reasoning():
    """现场那一次:推理和答复是**两段独立文本**,中间隔着一个 feedback 事件。

    拼接器一路攒下来,开头被认成 NL2SQL 前言(strong=True)之后,
    后面那句答复被继续当成同一段推理 —— 整段归档进「思考过程」,
    回答栏空着。而运行状态是 success,没有任何报错。
    """
    answer, reasoning = _split([
        {"type": "text", "delta": _NL2SQL},
        {"type": "feedback", "data": "[]"},
        {"type": "text", "delta": _REPLY},
    ])
    assert _REPLY in answer, "小Q 要给用户看的那句话被吞进了思考过程"
    assert "判定0" in reasoning, "推理仍然要留在推理里,不能一起倒进回答"
    assert "判定0" not in answer


def test_any_non_text_event_ends_the_reasoning_block():
    """不只是 feedback —— sql、chart 这些插进来同样意味着那段推理讲完了。"""
    for sep in ({"type": "sql", "sql": "SELECT 1"}, {"type": "chart", "title": "x"},
                {"type": "feedback", "data": "[]"}):
        answer, _ = _split([{"type": "text", "delta": _NL2SQL}, sep,
                            {"type": "text", "delta": _REPLY}])
        assert _REPLY in answer, f"{sep['type']} 之后的文本应按回答处理"


def test_uninterrupted_reasoning_is_still_reasoning():
    """反向保证:中间没有别的事件时,NL2SQL 前言整段仍然算推理 ——
    不能为了修上面那条,把本来就该藏起来的过程一股脑倒给用户。"""
    answer, reasoning = _split([{"type": "text", "delta": _NL2SQL}])
    assert "判定0" in reasoning
    assert answer.strip() == ""


def test_normal_answers_are_untouched():
    """绝大多数问数回答没有思维链特征,必须原样直通。"""
    answer, reasoning = _split([
        {"type": "text", "delta": "昨日综合服务分 4.6 分，"},
        {"type": "chart", "title": "雷达图"},
        {"type": "text", "delta": "环比上升 0.2 分。"},
    ])
    assert answer == "昨日综合服务分 4.6 分，环比上升 0.2 分。"
    assert reasoning == ""


def test_boundary_marker_still_works_within_one_block():
    """原有的「开始输出」分界逻辑不能被这次改动带坏。"""
    answer, reasoning = _split([
        {"type": "text", "delta": "思考过程：先看字段。开始输出：\n结论是 4.6 分。"},
    ])
    assert "结论是 4.6 分。" in answer
    assert "先看字段" in reasoning


# ══════════════════════════════════════════════════════════════════════
# 三、零产出不能记成成功
# ══════════════════════════════════════════════════════════════════════

def test_empty_result_is_reported_as_a_failure_with_a_next_step():
    """没有回答、没有图表、没有报告,却记成 success —— 最难查的一种「成功」。

    运行记录是绿的、耗时十几秒、没有任何报错,而用户那边是空白卡片。
    运营看着一条成功记录,不知道该查什么,只能来截图。
    所以这种情况要判失败,并且**说清下一步**,而不是只说一句「无结果」。
    """
    import inspect

    from cogni_agent.tool_center_service.quickbi import service as qsvc

    src = inspect.getsource(qsvc.run_stream)
    assert "EMPTY_RESULT" in src, "零产出必须判失败,不能留一条绿色的空记录"
    assert "换一份目标数据" in src, "报错要给下一步动作,不能只说「没有结果」"
    # 报告类必须排除在外:它本来就是创建即返回,正文等后台轮询收回来
    assert 'capability != "report"' in src, "报告类是创建即返回,不能被这条判成失败"
