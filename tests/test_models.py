"""核心数据模型的离线测试。"""

import pytest

from sparrow_agent.models import (
    AgentResult,
    CompletionRequest,
    Message,
    MessageRole,
    StopReason,
    ToolCall,
    ToolResult,
    VerificationRecord,
)


def test_assistant_message_preserves_reasoning_content_and_tool_calls() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})
    message = Message(
        role=MessageRole.ASSISTANT,
        content=None,
        reasoning_content="需要先读取需求。",
        tool_calls=(call,),
    )

    assert message.to_dict() == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "README.md"},
            }
        ],
        "reasoning_content": "需要先读取需求。",
    }


def test_tool_call_fingerprint_ignores_argument_order_and_call_id() -> None:
    first = ToolCall(id="a", name="edit", arguments={"path": "a.py", "text": "x"})
    second = ToolCall(id="b", name="edit", arguments={"text": "x", "path": "a.py"})

    assert first.fingerprint() == second.fingerprint()


def test_tool_call_preserves_invalid_raw_arguments() -> None:
    call = ToolCall(
        id="bad",
        name="read_file",
        raw_arguments='{"path":',
        argument_error="工具参数不是有效 JSON",
    )

    assert call.to_dict()["raw_arguments"] == '{"path":'
    assert call.to_dict()["argument_error"] == "工具参数不是有效 JSON"

    with pytest.raises(ValueError, match="raw_arguments"):
        ToolCall(id="bad", name="read_file", argument_error="解析失败")


def test_message_role_invariants_reject_invalid_tool_message() -> None:
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=MessageRole.TOOL, content="读取完成")

    with pytest.raises(ValueError, match="助手消息"):
        Message(
            role=MessageRole.USER,
            content="请读取文件",
            reasoning_content="不应出现在用户消息中",
        )

    with pytest.raises(TypeError, match="content"):
        Message(role=MessageRole.USER, content=123)  # type: ignore[arg-type]


def test_tool_result_distinguishes_success_and_failure() -> None:
    success = ToolResult.success("完成", metadata={"elapsed_ms": 3})
    failure = ToolResult.failure("文件不存在")

    assert success.to_dict() == {
        "ok": True,
        "output": "完成",
        "error": None,
        "metadata": {"elapsed_ms": 3},
    }
    assert failure.ok is False
    assert failure.error == "文件不存在"

    with pytest.raises(ValueError, match="必须说明"):
        ToolResult(ok=False)


def test_completed_result_requires_structured_completion_request() -> None:
    verification = VerificationRecord(
        command=("pytest", "-q"),
        exit_code=0,
        event_index=8,
        output_summary="5 passed",
    )
    request = CompletionRequest(
        summary="实现核心数据模型",
        changed_files=("src/sparrow_agent/models.py",),
        verifications=(verification,),
    )
    result = AgentResult(
        stop_reason=StopReason.COMPLETED,
        final_text="任务已完成。",
        iterations=3,
        completion_request=request,
    )

    assert result.completion_request is request

    with pytest.raises(ValueError, match="完成申请"):
        AgentResult(
            stop_reason=StopReason.COMPLETED,
            final_text="任务已完成。",
            iterations=3,
        )


def test_non_completed_result_cannot_claim_completion() -> None:
    request = CompletionRequest(summary="尚未真正完成")

    with pytest.raises(ValueError, match="未完成状态"):
        AgentResult(
            stop_reason=StopReason.MAX_ITERATIONS,
            final_text="达到最大轮数。",
            iterations=20,
            completion_request=request,
        )
