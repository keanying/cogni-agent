"""L2 结构化输出 + schema 校验:intent.required_param_gaps 单元测试。

对应 conv_a5c0d4c1e44943509a190c468fa623e0 教训:Triager 判 execute 却漏报
compareStartTime/compareEndTime(既未填 filled 也未报 missing),执行层被迫反问。
本校验在 Triager 输出后按工具 json_schema.required 做必填覆盖检查,拦截漏报。
纯函数测试,不依赖数据库。
"""

from types import SimpleNamespace

import pytest

from cogni_agent.runtime_service.engine import intent


def _tool(name: str, required: list[str] | None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        json_schema={
            "type": "object",
            "properties": {r: {"type": "string"} for r in (required or [])},
            "required": required or [],
        },
    )


def _skill(name: str, tools: list) -> SimpleNamespace:
    return SimpleNamespace(name=name, tools=tools)


# 客流比对汇总:本期时间 + 对比期均为必填(与线上工具声明一致)
TOOL_COMPARE = _tool(
    "客流比对汇总",
    ["supplierId", "startTime", "endTime", "compareStartTime", "compareEndTime"],
)
TOOL_SIMPLE = _tool("客流总览", ["supplierId", "startTime", "endTime"])
SKILL_SCENIC = _skill("数字大脑-客流分析", [TOOL_COMPARE, TOOL_SIMPLE])
SKILL_OTHER = _skill("数字大脑-收入分析", [_tool("营收总览", ["supplierId", "startTime"])])

CHAIN = [SKILL_SCENIC, SKILL_OTHER]


def test_full_filled_passes():
    """filled 覆盖全部必填 -> 校验通过,无 gap。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-客流分析"],
        filled={"supplierId": "PFTSCA01018088", "startTime": "2026-08-01",
                "endTime": "2026-08-31", "compareStartTime": "2026-07-01",
                "compareEndTime": "2026-07-31"},
        missing=[],
    )
    assert gaps == []


def test_gap_detected_when_required_uncovered():
    """复现事故场景:对比期未填也未报缺失 -> 校验拦截出 gap。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-客流分析"],
        filled={"supplierId": "PFTSCA01018088", "startTime": "2026-08-01",
                "endTime": "2026-08-31"},
        missing=[],
    )
    assert set(gaps) == {"compareStartTime", "compareEndTime"}


def test_missing_declared_not_gap():
    """Triager 已声明的 missing 不算 gap(执行层负责补齐/澄清)。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-客流分析"],
        filled={"supplierId": "PFTSCA01018088", "startTime": "2026-08-01",
                "endTime": "2026-08-31"},
        missing=["compareStartTime", "compareEndTime"],
    )
    assert gaps == []


def test_slots_cover_not_gap():
    """会话槽(如 scenicId)已覆盖的参数不算 gap,防止误反问。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-客流分析"],
        filled={"startTime": "2026-08-01", "endTime": "2026-08-31",
                "compareStartTime": "2026-07-01", "compareEndTime": "2026-07-31"},
        missing=[],
        slots={"supplierId": "PFTSCA01018088", "scenic_id": "PFTSCA01018088"},
    )
    assert gaps == []


def test_non_target_skill_ignored():
    """不在 target_skills 里的技能,其工具必填不参与校验。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-收入分析"],
        filled={},
        missing=[],
    )
    assert set(gaps) == {"supplierId", "startTime"}


def test_empty_target_skills_returns_empty():
    """target_skills 为空(全量执行/降级)不校验 -> 恒空。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=[],
        filled={},
        missing=[],
    )
    assert gaps == []


def test_tool_without_required_schema_ok():
    """json_schema 无 required 字段的工具不参与校验。"""
    skill = _skill("无必填技能", [_tool("无必填工具", None)])
    gaps = intent.required_param_gaps(
        skill_chain=[skill],
        target_skills=["无必填技能"],
        filled={},
        missing=[],
    )
    assert gaps == []


def test_gap_deduplicated_across_tools():
    """多个工具共有同一必填参数时,gap 去重。"""
    skill = _skill(
        "多工具技能",
        [_tool("工具A", ["shared", "a"]), _tool("工具B", ["shared", "b"])],
    )
    gaps = intent.required_param_gaps(
        skill_chain=[skill],
        target_skills=["多工具技能"],
        filled={},
        missing=[],
    )
    assert gaps == ["shared", "a", "b"]


def test_schema_gap_merged_into_missing_in_executor_flow():
    """模拟 executor 拦截动作:gap 并入 params_missing,不改变 action。"""
    gaps = intent.required_param_gaps(
        skill_chain=CHAIN,
        target_skills=["数字大脑-客流分析"],
        filled={"supplierId": "PFTSCA01018088", "startTime": "2026-08-01",
                "endTime": "2026-08-31"},
        missing=[],
    )
    params_missing = list(dict.fromkeys([*[], *gaps]))
    assert set(params_missing) == {"compareStartTime", "compareEndTime"}
    # action 保持 execute,由执行层补齐/澄清,不因校验直接改判反问
    assert gaps  # 校验确实拦截到了
