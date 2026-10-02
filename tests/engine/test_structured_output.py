"""
v1.1 智能问数 Phase B:结构化输出解析 + 图表模板 + 事件发布顺序测试。

覆盖:
- chart_templates.build_chart: line/bar/pie/radar 模板填充、非法类型/空数据兜底
- structured_output.parse: 结构化块剥离、cards/charts/insight/recommend 拆解、
  无结构化块/非法 JSON 原样兜底
- executor._publish_structured_events: 事件发布顺序 cards → chart → insight → recommend
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cogni_agent.runtime_service.engine import chart_templates
from cogni_agent.runtime_service.engine import structured_output
from cogni_agent.runtime_service.engine.executor import _publish_structured_events


# ── chart_templates: 图表模板 ─────────────────────────────────────────────────

class TestChartTemplates:
    def test_line_template(self):
        spec = {
            "title": "近7天客流", "type": "line",
            "x": ["08-19", "08-20"], "unit": "人",
            "series": [{"name": "在园人数", "data": ["8523", 8000]}],
        }
        option = chart_templates.build_chart(spec)
        assert option is not None
        assert option["xAxis"]["data"] == ["08-19", "08-20"]
        assert option["yAxis"]["name"] == "人"
        assert option["series"][0]["type"] == "line"
        # 字符串数字被清洗为数字
        assert option["series"][0]["data"] == [8523, 8000]

    def test_bar_template(self):
        option = chart_templates.build_chart(
            {"title": "退票", "type": "bar", "x": ["a", "b"],
             "series": [{"name": "x", "data": [1, 2]}]})
        assert option is not None
        assert option["series"][0]["type"] == "bar"

    def test_pie_template(self):
        option = chart_templates.build_chart(
            {"title": "渠道", "type": "pie",
             "data": [{"name": "OTA", "value": 123}, {"name": "直销", "value": 77}]})
        assert option is not None
        assert option["series"][0]["type"] == "pie"
        assert len(option["series"][0]["data"]) == 2

    def test_radar_template(self):
        option = chart_templates.build_chart(
            {"title": "评分", "type": "radar",
             "indicators": [{"name": "交通", "max": 100}],
             "series": [{"name": "本月", "data": [80]}]})
        assert option is not None
        assert option["series"][0]["type"] == "radar"
        assert option["radar"]["indicator"][0]["name"] == "交通"

    def test_unknown_type_returns_none(self):
        assert chart_templates.build_chart({"title": "x", "type": "gauge", "data": []}) is None

    def test_no_series_returns_none(self):
        assert chart_templates.build_chart({"title": "x", "type": "line", "x": ["a"]}) is None

    def test_non_dict_returns_none(self):
        assert chart_templates.build_chart("not-a-dict") is None


# ── structured_output: 结构化块解析 ───────────────────────────────────────────

class TestStructuredOutputParse:
    def test_parse_strips_block_and_extracts(self):
        answer = (
            "今日在园 8523 人。\n"
            "```json\n"
            '{"cards":[{"label":"在园人数","value":"8523","unit":"人","trend":"up"}],'
            '"charts":[{"title":"近7天客流","type":"line","x":["08-19"],'
            '"series":[{"name":"人数","data":[8523]}]}],'
            '"insight":{"level":"warning","content":"午后冲高"},'
            '"recommend":[{"question":"退票高峰几点?","theme":"票务"}]}\n'
            "```"
        )
        r = structured_output.parse(answer)
        assert r.answer == "今日在园 8523 人。"
        assert r.cards == [{"label": "在园人数", "value": "8523", "unit": "人", "trend": "up"}]
        assert len(r.charts) == 1
        assert r.charts[0]["type"] == "line"
        assert "option" in r.charts[0] and "series" in r.charts[0]["option"]
        assert r.insight == {"level": "warning", "content": "午后冲高"}
        assert r.recommends == [{"question": "退票高峰几点?", "theme": "票务"}]

    def test_plain_answer_passthrough(self):
        r = structured_output.parse("普通回答,无结构化块")
        assert r.answer == "普通回答,无结构化块"
        assert not (r.cards or r.charts or r.insight or r.recommends)

    def test_empty_answer(self):
        r = structured_output.parse("")
        assert r.answer == ""

    def test_invalid_json_block_passthrough(self):
        answer = "回答```json\n{not json}\n```尾"
        r = structured_output.parse(answer)
        assert r.answer == answer
        assert not (r.cards or r.charts or r.insight or r.recommends)

    def test_invalid_level_falls_back_to_normal(self):
        answer = '```json\n{"insight":{"level":"critical","content":"x"}}\n```'
        r = structured_output.parse(answer)
        assert r.insight == {"level": "normal", "content": "x"}

    def test_invalid_chart_skipped(self):
        answer = ('```json\n{"charts":[{"title":"坏图","type":"gauge","data":[]},'
                  '{"title":"好图","type":"pie","data":[{"name":"a","value":1}]}]}\n```')
        r = structured_output.parse(answer)
        assert len(r.charts) == 1
        assert r.charts[0]["type"] == "pie"


# ── executor._publish_structured_events: 事件顺序 ─────────────────────────────

class TestPublishStructuredEvents:
    @pytest.mark.asyncio
    async def test_publish_order_and_payloads(self):
        structured = structured_output.StructuredOutput(
            answer="",
            cards=[{"label": "在园人数", "value": "8523", "unit": "人", "trend": "up"}],
            charts=[{"title": "客流", "type": "line", "option": {"series": []}}],
            insight={"level": "warning", "content": "午后冲高"},
            recommends=[{"question": "退票高峰?", "theme": "票务"}],
        )
        published: list[tuple[str, dict]] = []

        async def _fake_publish(task_id: str, event: str, data: dict) -> str:
            published.append((event, data))
            return "1"

        with patch("cogni_agent.runtime_service.engine.executor._publish", new=_fake_publish):
            await _publish_structured_events("task-1", structured)

        events = [e for e, _ in published]
        assert events == ["cards", "chart", "insight", "recommend"]
        assert published[0][1]["items"] == structured.cards
        assert published[1][1] == structured.charts[0]
        assert published[2][1] == structured.insight
        assert published[3][1]["items"] == structured.recommends

    @pytest.mark.asyncio
    async def test_empty_structured_publishes_nothing(self):
        published: list[str] = []

        async def _fake_publish(task_id: str, event: str, data: dict) -> str:
            published.append(event)
            return "1"

        with patch("cogni_agent.runtime_service.engine.executor._publish", new=_fake_publish):
            await _publish_structured_events("task-1", structured_output.StructuredOutput(answer="hi"))
        assert published == []

    @pytest.mark.asyncio
    async def test_publish_timeout_does_not_block(self):
        """单个事件发布超时不阻塞后续事件与整体收尾。"""
        structured = structured_output.StructuredOutput(
            answer="",
            cards=[{"label": "a", "value": "1"}],
            charts=[{"title": "c", "type": "pie", "option": {"series": []}}],
        )

        async def _slow_publish(task_id: str, event: str, data: dict) -> str:
            if event == "cards":
                await asyncio.sleep(5)  # 超时
            return "1"

        with (
            patch("cogni_agent.runtime_service.engine.executor._publish", new=_slow_publish),
            patch("cogni_agent.runtime_service.engine.executor._FINALIZE_STEP_TIMEOUT", 0.01),
        ):
            await _publish_structured_events("task-1", structured)  # 不应抛异常
