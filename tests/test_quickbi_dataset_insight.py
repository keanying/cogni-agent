# -*- coding: utf-8 -*-
"""解读 · 数据集:指定数据集(或问题上绑的数据集)一样能解读。

解读接口只吃「一段数据文本」,不认数据集 —— 以前选了数据集的解读直接报
「解读需要数据来源」,而数据就在那个数据集里。现在:先按问题从数据集取数,再解读。

另:解读在数据里找不到相关内容时按守卫提示词回「不存在」。现场一条票务问题绑的是舆情看板,
只回了三个字。现在补一句「用的数据里有什么」,APP 侧也据此转交智能体。
"""
from __future__ import annotations

import asyncio
import base64
import inspect

import pytest

from cogni_agent.tool_center_service.quickbi import capabilities as cap
from cogni_agent.tool_center_service.quickbi.client import QuickBIError


class FakeQbi:
    user_id = "u-qbi"

    def __init__(self, answer="昨日售票 1,234 张。"):
        self.payloads = []
        self.answer = answer

    def stream(self, uri, payload):
        self.payloads.append((uri, payload))
        answer = self.answer

        async def gen():
            yield {"type": "trace", "data": "tr-1"}
            yield {"type": "summary", "data": answer}
            yield {"type": "finish", "data": ""}
        return gen()


def _fake_qa(charts, text="共查到 2 个票种"):
    async def run_dataset_qa(qbi, **kw):
        run_dataset_qa.kw = kw
        yield {"type": "meta", "cube_id": kw.get("cube_id")}
        yield {"type": "text", "delta": text, "section": "answer"}
        for c in charts:
            yield {"type": "chart", "chart": c}
        yield {"type": "status", "stage": "finish", "message": ""}
    return run_dataset_qa


CHART = {"title": "昨日票种销售", "fields": [{"name": "票种"}, {"name": "销售票数", "role": "metric"}],
         "rows": [{"票种": "成人票", "销售票数": 800}, {"票种": "儿童票", "销售票数": 434}]}


async def _collect(agen):
    return [e async for e in agen]


def test_dataset_insight_fetches_then_interprets(monkeypatch):
    monkeypatch.setattr(cap, "run_dataset_qa", _fake_qa([CHART]))
    qbi = FakeQbi()
    evs = asyncio.run(_collect(cap.run_dataset_insight(qbi, question="昨日票务销售情况如何?", cube_id="cube-ticket")))

    assert cap.run_dataset_qa.kw["cube_id"] == "cube-ticket"
    uri, payload = qbi.payloads[-1]
    assert uri == cap.URI["interpretation"]
    data = base64.b64decode(payload["stringData"]).decode("utf-8")
    assert "## 昨日票种销售" in data and "| 成人票 | 800 |" in data, "取回的数据要原样交给解读"

    kinds = [e["type"] for e in evs]
    assert kinds.index("chart") < kinds.index("text"), "取回的图先下发"
    answers = [e["delta"] for e in evs if e["type"] == "text"]
    assert answers == ["昨日售票 1,234 张。"], "取数那一步的文字只进思考过程,不混进回答"
    finishes = [e for e in evs if e.get("stage") == "finish"]
    assert len(finishes) == 1, "取数的 finish 不能提前下发(前端会以为整轮结束)"


def test_dataset_with_nothing_found_says_so(monkeypatch):
    monkeypatch.setattr(cap, "run_dataset_qa", _fake_qa([]))
    with pytest.raises(QuickBIError) as ei:
        asyncio.run(_collect(cap.run_dataset_insight(FakeQbi(), question="x", cube_id="c")))
    assert "没有取到数据" in str(ei.value)


def test_prepare_accepts_a_dataset_for_insight():
    """真调一次 _prepare:只给数据集(cube_id),解读要能跑,而不是报缺数据来源。"""
    from cogni_agent.tool_center_service.quickbi import service

    gen, t = asyncio.run(service._prepare(None, FakeQbi(), None, "insight", {"cube_id": "cube-ticket"},
                                          "昨日票务销售情况如何?", "zh_CN", []))
    assert gen.ag_code.co_name == "run_dataset_insight"
    assert t["cube_id"] == "cube-ticket"
    with pytest.raises(QuickBIError) as ei:          # 什么都没给时照旧报,且提示里有数据集这一项
        asyncio.run(service._prepare(None, FakeQbi(), None, "insight", {}, "q", "zh_CN", []))
    assert "数据集" in str(ei.value)


def test_service_routes_dataset_targets_to_dataset_insight():
    from cogni_agent.tool_center_service.quickbi import service

    src = inspect.getsource(service._prepare)
    seg = src[src.index('if capability == "insight":'):src.index('if capability == "report":')]
    assert "cap.run_dataset_insight(" in seg
    assert seg.index("run_dataset_insight") < seg.index("解读需要数据来源"), "有数据集就不能再报缺数据来源"
    # asset kind=dataset 会落到 cube_id 上(问题上绑的数据集资产走的就是这条)
    assert 't.setdefault("cube_id", asset.ref_id)' in src


def test_bare_not_exist_gets_explained():
    md = "## 总评趋势\n| a |\n## 情绪占比4\n| b |\n## 词云8\n"
    qbi = FakeQbi(answer="不存在")
    evs = asyncio.run(_collect(cap.run_interpretation(qbi, question="昨日票务销售情况如何?", markdown=md)))
    text = "".join(e["delta"] for e in evs if e["type"] == "text")
    assert text.startswith("不存在") and cap.NO_RELATED_DATA_MARK in text
    assert "总评趋势、情绪占比4、词云8" in text, "说清用的数据里有什么"

    ok = asyncio.run(_collect(cap.run_interpretation(FakeQbi(), question="q", markdown=md)))
    assert cap.NO_RELATED_DATA_MARK not in "".join(e.get("delta", "") for e in ok if e["type"] == "text")


def test_not_exist_hands_off_in_the_app():
    from cogni_agent.gateway_service import qbi_bridge as qb

    note = cap.no_related_data_note("## 总评趋势\n" * 3)
    assert qb._looks_like_no_data("不存在" + note, [])
    assert qb._looks_like_no_data("不存在", [])
    assert not qb._looks_like_no_data("门票价格并不存在明显问题," * 30, [])
