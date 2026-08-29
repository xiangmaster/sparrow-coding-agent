"""上下文、证据账本和完成门测试。"""

import json

import pytest

from sparrow_agent.completion import CompletionGate
from sparrow_agent.context import Context
from sparrow_agent.evidence import EvidenceLedger
from sparrow_agent.models import Message, MessageRole, ToolCall, ToolResult


def _completion_call(
    *,
    changed_files: list[str] | None = None,
    commands: list[list[str]] | None = None,
) -> ToolCall:
    return ToolCall(
        id="complete",
        name="request_completion",
        arguments={
            "summary": "任务完成",
            "changed_files": changed_files or [],
            "verification_commands": commands or [],
            "remaining_risks": [],
        },
    )


def test_context_preserves_reasoning_and_pairs_tool_observation() -> None:
    context = Context("系统规则", "用户任务")
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
    assistant = Message(
        role=MessageRole.ASSISTANT,
        content=None,
        reasoning_content="先读取文件",
        tool_calls=(call,),
    )

    context.append_assistant(assistant)
    tool_message = context.append_tool_result(call, ToolResult.success("内容"))

    assert context.messages[2] is assistant
    assert context.messages[2].reasoning_content == "先读取文件"
    assert tool_message.role is MessageRole.TOOL
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content)["output"] == "内容"


def test_context_truncates_only_tool_output_and_keeps_valid_json() -> None:
    context = Context("系统规则", "用户任务", max_observation_characters=100)
    call = ToolCall(id="call-1", name="read_file")

    message = context.append_tool_result(
        call,
        ToolResult.success("x" * 500, metadata={"path": "large.txt"}),
    )
    payload = json.loads(message.content)

    assert len(payload["output"]) == 100
    assert "已截断" in payload["output"]
    assert payload["metadata"]["path"] == "large.txt"
    assert payload["metadata"]["observation_truncated"] is True
    assert payload["metadata"]["original_output_characters"] == 500


def test_context_rejects_wrong_roles_and_empty_feedback() -> None:
    context = Context("系统规则", "用户任务")

    with pytest.raises(ValueError, match="助手消息"):
        context.append_assistant(Message(role=MessageRole.USER, content="错误角色"))
    with pytest.raises(ValueError, match="不能为空"):
        context.append_control_feedback(" ")


def test_evidence_ledger_tracks_mutation_and_post_mutation_verification() -> None:
    ledger = EvidenceLedger()
    patch_call = ToolCall(id="1", name="apply_patch")
    command_call = ToolCall(id="2", name="run_command")
    ledger.record(
        command_call,
        ToolResult.success(
            "old pass",
            metadata={"command": ["pytest", "-q"], "exit_code": 0},
        ),
    )
    ledger.record(
        patch_call,
        ToolResult.success("changed", metadata={"changed_files": ["src/a.py"]}),
    )
    ledger.record(
        command_call,
        ToolResult.failure(
            "failed",
            output="1 failed",
            metadata={"command": ["pytest", "-q"], "exit_code": 1},
        ),
    )

    assert ledger.changed_files == {"src/a.py"}
    assert ledger.last_mutation_index == 2
    assert len(ledger.verifications_after_last_mutation()) == 1
    assert ledger.verifications_after_last_mutation()[0].exit_code == 1


def test_completion_gate_accepts_information_only_task_without_fake_evidence() -> None:
    decision = CompletionGate().evaluate(_completion_call(), EvidenceLedger())

    assert decision.accepted is True
    assert decision.completion_request is not None
    assert decision.completion_request.changed_files == ()
    assert decision.completion_request.verifications == ()


def test_completion_gate_rejects_missing_stale_failed_and_invented_evidence() -> None:
    ledger = EvidenceLedger()
    ledger.record(
        ToolCall(id="patch", name="apply_patch"),
        ToolResult.success("changed", metadata={"changed_files": ["src/a.py"]}),
    )
    missing = CompletionGate().evaluate(
        _completion_call(changed_files=["src/a.py"]), ledger
    )
    ledger.record(
        ToolCall(id="test", name="run_command"),
        ToolResult.failure(
            "failed",
            metadata={"command": ["pytest", "-q"], "exit_code": 1},
        ),
    )
    failed = CompletionGate().evaluate(
        _completion_call(
            changed_files=["src/a.py"], commands=[["pytest", "-q"]]
        ),
        ledger,
    )
    invented = CompletionGate().evaluate(
        _completion_call(
            changed_files=["src/a.py", "src/invented.py"],
            commands=[["other-test"]],
        ),
        ledger,
    )

    assert missing.accepted is False
    assert "最后一次修改之后没有运行验证" in missing.result.error
    assert failed.accepted is False
    assert "最近一次验证失败" in failed.result.error
    assert invented.accepted is False
    assert "没有本地证据" in invented.result.error
    assert "没有对应成功证据" in invented.result.error


def test_completion_gate_accepts_latest_success_after_earlier_failure() -> None:
    ledger = EvidenceLedger()
    ledger.record(
        ToolCall(id="patch", name="apply_patch"),
        ToolResult.success("changed", metadata={"changed_files": ["src/a.py"]}),
    )
    for exit_code in (1, 0):
        result = (
            ToolResult.success(
                "pass", metadata={"command": ["pytest", "-q"], "exit_code": 0}
            )
            if exit_code == 0
            else ToolResult.failure(
                "failed",
                metadata={"command": ["pytest", "-q"], "exit_code": 1},
            )
        )
        ledger.record(ToolCall(id=f"test-{exit_code}", name="run_command"), result)

    decision = CompletionGate().evaluate(
        _completion_call(
            changed_files=["src/a.py"], commands=[["pytest", "-q"]]
        ),
        ledger,
    )

    assert decision.accepted is True
    assert decision.completion_request is not None
    assert [record.exit_code for record in decision.completion_request.verifications] == [
        1,
        0,
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {
            "summary": "done",
            "changed_files": ["../escape.py"],
            "verification_commands": [],
            "remaining_risks": [],
        },
        {
            "summary": "done",
            "changed_files": [],
            "verification_commands": "pytest",
            "remaining_risks": [],
        },
    ],
)
def test_completion_gate_returns_invalid_claim_as_tool_failure(arguments) -> None:
    call = ToolCall(id="complete", name="request_completion", arguments=arguments)

    decision = CompletionGate().evaluate(call, EvidenceLedger())

    assert decision.accepted is False
    assert decision.result.ok is False
