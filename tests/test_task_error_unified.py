"""SSE 错误事件统一规范测试：task_error() / TaskErrorKind / KIND_TO_CODE。

保证所有 error event 走同一套 ErrorCode 数字码（与 ApiResponse 同源），
且 kind 与 code 的映射、默认文案行为稳定。
"""
from cogni_agent.common.schemas import (
    ErrorCode,
    KIND_TO_CODE,
    TaskErrorKind,
    task_error,
)


def test_all_kinds_mapped_to_code():
    """每个 kind 都必须在 KIND_TO_CODE 中有映射，不允许漏登记导致落到 UNKNOWN。"""
    kinds = {
        TaskErrorKind.INTERNAL_ERROR,
        TaskErrorKind.TASK_NOT_FOUND,
        TaskErrorKind.RESOLVE_FAILED,
        TaskErrorKind.RESOLVE_ERROR,
        TaskErrorKind.BUDGET_EXCEEDED,
        TaskErrorKind.GUARDRAIL_TRIPPED,
        TaskErrorKind.OUTPUT_BLOCKED,
        TaskErrorKind.TASK_TIMEOUT,
        TaskErrorKind.UNEXPECTED_ERROR,
        TaskErrorKind.STREAM_ERROR,
        TaskErrorKind.FAILED,
    }
    for kind in kinds:
        assert kind in KIND_TO_CODE, f"kind {kind!r} 未在 KIND_TO_CODE 登记"


def test_kind_to_code_mapping():
    assert KIND_TO_CODE[TaskErrorKind.INTERNAL_ERROR] == ErrorCode.INTERNAL_ERROR
    assert KIND_TO_CODE[TaskErrorKind.TASK_NOT_FOUND] == ErrorCode.TASK_NOT_FOUND
    assert KIND_TO_CODE[TaskErrorKind.RESOLVE_FAILED] == ErrorCode.RESOLVE_FAILED
    assert KIND_TO_CODE[TaskErrorKind.BUDGET_EXCEEDED] == ErrorCode.BUDGET_EXCEEDED
    assert KIND_TO_CODE[TaskErrorKind.GUARDRAIL_TRIPPED] == ErrorCode.GUARDRAIL_TRIPPED
    assert KIND_TO_CODE[TaskErrorKind.OUTPUT_BLOCKED] == ErrorCode.OUTPUT_BLOCKED
    assert KIND_TO_CODE[TaskErrorKind.TASK_TIMEOUT] == ErrorCode.EXECUTION_TIMEOUT
    assert KIND_TO_CODE[TaskErrorKind.UNEXPECTED_ERROR] == ErrorCode.TASK_FAILED
    assert KIND_TO_CODE[TaskErrorKind.FAILED] == ErrorCode.TASK_FAILED


def test_task_error_structure():
    data = task_error(TaskErrorKind.TASK_TIMEOUT)
    assert set(data) == {"kind", "code", "message"}
    assert data["kind"] == TaskErrorKind.TASK_TIMEOUT
    assert data["code"] == ErrorCode.EXECUTION_TIMEOUT
    # 未传 message → 用 ErrorCode 标准文案
    assert data["message"] == ErrorCode.message(ErrorCode.EXECUTION_TIMEOUT)


def test_task_error_custom_message():
    data = task_error(TaskErrorKind.OUTPUT_BLOCKED, message="生成内容未通过安全审核,已拦截")
    assert data["kind"] == TaskErrorKind.OUTPUT_BLOCKED
    assert data["code"] == ErrorCode.OUTPUT_BLOCKED
    assert data["message"] == "生成内容未通过安全审核,已拦截"


def test_task_error_unknown_kind_falls_back_to_unknown():
    data = task_error("no_such_kind")
    assert data["code"] == ErrorCode.UNKNOWN
    assert data["message"] == ErrorCode.message(ErrorCode.UNKNOWN)


def test_error_codes_are_distinct():
    """新增的执行域错误码不应与既有码冲突。"""
    codes = [
        ErrorCode.TASK_FAILED,
        ErrorCode.GUARDRAIL_TRIPPED,
        ErrorCode.EXECUTION_TIMEOUT,
        ErrorCode.LLM_ERROR,
        ErrorCode.RESOLVE_FAILED,
        ErrorCode.BUDGET_EXCEEDED,
        ErrorCode.OUTPUT_BLOCKED,
    ]
    assert len(codes) == len(set(codes))
    # 4xxx 执行域内
    assert all(4000 <= c < 5000 for c in codes)
