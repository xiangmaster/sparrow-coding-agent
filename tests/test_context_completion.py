"""上下文、证据账本和完成门测试。"""

import json
from pathlib import Path

import pytest

from sparrow_agent.completion import CompletionGate
from sparrow_agent.context import Context
from sparrow_agent.evidence import EvidenceLedger
from sparrow_agent.models import Message, MessageRole, ToolCall, ToolResult
from sparrow_agent.workspace import Workspace


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
    with pytest.raises(ValueError, match="不能为空"):
        context.append_user(" ")


def test_context_restores_and_compacts_multi_turn_user_messages() -> None:
    context = Context(
        "系统规则",
        "第一轮任务",
        max_observation_characters=500,
        max_context_characters=1_000,
        max_summary_characters=300,
    )
    for index in range(3):
        context.append_assistant(
            Message(role=MessageRole.ASSISTANT, content="结论" + "x" * 220)
        )
        context.append_user(f"第 {index + 2} 轮继续检查" + "y" * 100)

    restored = Context.from_messages(
        context.messages,
        max_observation_characters=500,
        max_context_characters=1_000,
        max_summary_characters=300,
    )
    restored.append_assistant(Message(role=MessageRole.ASSISTANT, content="最新结论"))

    messages = restored.messages
    assert messages[0].role is MessageRole.SYSTEM
    assert messages[1].content == "第一轮任务"
    assert "较早历史事实摘要" in (messages[0].content or "")
    visible_text = "\n".join(message.content or "" for message in messages)
    assert "继续检查" in visible_text
    assert messages[-1].content == "最新结论"


def test_context_compacts_old_turns_without_splitting_recent_tool_chain() -> None:
    context = Context(
        "系统规则",
        "用户任务",
        max_observation_characters=500,
        max_context_characters=1_200,
        max_summary_characters=300,
    )
    for index in range(4):
        call = ToolCall(
            id=f"call-{index}",
            name="read_file",
            arguments={"path": f"file-{index}.txt"},
        )
        context.append_assistant(
            Message(
                role=MessageRole.ASSISTANT,
                content=None,
                reasoning_content=f"第 {index} 轮推理" + "r" * 120,
                tool_calls=(call,),
            )
        )
        context.append_tool_result(
            call,
            ToolResult.success(
                "x" * 260,
                metadata={"path": f"file-{index}.txt"},
            ),
        )

    messages = context.messages

    assert messages[0].role is MessageRole.SYSTEM
    assert messages[1].role is MessageRole.USER
    assert context.compacted_turns >= 1
    assert context.estimated_characters <= 1_200
    assert "系统规则" in (messages[0].content or "")
    assert "较早历史事实摘要" in (messages[0].content or "")
    assert "工具 read_file：成功" in (messages[0].content or "")
    assert messages[-2].reasoning_content is not None
    assert "第 3 轮推理" in messages[-2].reasoning_content
    assert messages[-1].tool_call_id == "call-3"

    retained_call_ids = {
        call.id
        for message in messages
        if message.role is MessageRole.ASSISTANT
        for call in message.tool_calls
    }
    retained_result_ids = {
        message.tool_call_id
        for message in messages
        if message.role is MessageRole.TOOL
    }
    assert retained_call_ids == retained_result_ids
    assert "call-0" not in retained_call_ids


def test_context_validates_total_and_summary_budgets() -> None:
    with pytest.raises(ValueError, match="上下文字符预算"):
        Context("系统", "任务", max_context_characters=499)
    with pytest.raises(ValueError, match="摘要字符上限"):
        Context(
            "系统",
            "任务",
            max_context_characters=500,
            max_summary_characters=500,
        )


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


def test_evidence_ledger_uses_snapshot_diff_and_invalidates_same_event_verification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    ledger = EvidenceLedger(baseline_snapshot=workspace.snapshot())

    target.write_text("new\n", encoding="utf-8")
    ledger.record(
        ToolCall(id="format", name="run_command"),
        ToolResult.success(
            "formatted",
            metadata={"command": ["formatter"], "exit_code": 0},
        ),
        workspace.snapshot(),
    )

    assert ledger.changed_files == {"value.txt"}
    assert ledger.last_observed_changes == {"value.txt"}
    assert ledger.verifications_after_last_mutation() == ()

    target.write_text("old\n", encoding="utf-8")
    ledger.record(
        ToolCall(id="restore", name="run_command"),
        ToolResult.success(
            "restored",
            metadata={"command": ["restore"], "exit_code": 0},
        ),
        workspace.snapshot(),
    )

    assert ledger.changed_files == set()
    assert ledger.reported_changed_files == set()


def test_evidence_ledger_observes_change_after_successful_verification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    ledger = EvidenceLedger(baseline_snapshot=workspace.snapshot())

    target.write_text("new\n", encoding="utf-8")
    ledger.record(
        ToolCall(id="edit", name="apply_patch"),
        ToolResult.success("changed", metadata={"changed_files": ["value.txt"]}),
        workspace.snapshot(),
    )
    ledger.record(
        ToolCall(id="verify", name="run_command"),
        ToolResult.success(
            "passed", metadata={"command": ["pytest"], "exit_code": 0}
        ),
        workspace.snapshot(),
    )
    assert len(ledger.verifications_after_last_mutation()) == 1

    (tmp_path / "late.txt").write_text("late\n", encoding="utf-8")
    event_index, changes = ledger.observe_snapshot(workspace.snapshot())

    assert event_index == 3
    assert changes == {"late.txt"}
    assert ledger.changed_files == {"value.txt", "late.txt"}
    assert ledger.verifications_after_last_mutation() == ()


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
