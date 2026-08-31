"""Agent 主循环的确定性离线测试。"""

import json
from pathlib import Path

from sparrow_agent.agent import Agent, AgentSettings
from sparrow_agent.models import Message, MessageRole, StopReason, ToolCall
from sparrow_agent.provider import (
    ModelResponse,
    ProviderError,
    ProviderRequestError,
    ScriptedProvider,
    TokenUsage,
)
from sparrow_agent.recording import MemoryRecorder
from sparrow_agent.tools import (
    ApplyPatchTool,
    DeleteFileTool,
    ReadFileTool,
    ReplaceTextTool,
    RenameFileTool,
    RunCommandTool,
    ToolRegistry,
)
from sparrow_agent.workspace import Workspace


def _tool_response(
    call_id: str,
    name: str,
    arguments,
    *,
    reasoning: str = "执行下一步",
    usage: int = 0,
) -> ModelResponse:
    return ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            reasoning_content=reasoning,
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        ),
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=usage),
    )


def _completion_response(
    changed_files: list[str], commands: list[list[str]]
) -> ModelResponse:
    return _tool_response(
        "complete",
        "request_completion",
        {
            "summary": "已修复并通过验证",
            "changed_files": changed_files,
            "verification_commands": commands,
            "remaining_risks": [],
        },
        reasoning="核对证据后申请完成",
    )


def _real_registry(tmp_path: Path) -> ToolRegistry:
    workspace = Workspace(tmp_path)
    return ToolRegistry(
        [
            ReadFileTool(workspace),
            ReplaceTextTool(workspace),
            ApplyPatchTool(workspace),
            RunCommandTool(workspace),
        ]
    )


def test_agent_runs_full_failure_repair_verify_completion_loop(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        """from pathlib import Path
import sys
value = Path('value.txt').read_text(encoding='utf-8').strip()
print(value)
sys.exit(0 if value == 'final' else 1)
""",
        encoding="utf-8",
    )
    first_patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+wrong
"""
    second_patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-wrong
+final
"""
    command = ["python3", "verify.py"]
    provider = ScriptedProvider(
        [
            _tool_response(
                "read", "read_file", {"path": "value.txt"}, reasoning="先检查现状"
            ),
            _tool_response("patch-1", "apply_patch", {"patch": first_patch}),
            _tool_response("test-1", "run_command", {"command": command}),
            _tool_response("patch-2", "apply_patch", {"patch": second_patch}),
            _tool_response("test-2", "run_command", {"command": command}),
            _completion_response(["value.txt"], [command]),
        ]
    )
    recorder = MemoryRecorder()
    agent = Agent(
        provider,
        _real_registry(tmp_path),
        workspace=Workspace(tmp_path),
        recorder=recorder,
    )

    result = agent.run("把 value.txt 修改为 final，并运行验证。")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.iterations == 6
    assert result.completion_request is not None
    assert result.completion_request.changed_files == ("value.txt",)
    assert [item.exit_code for item in result.completion_request.verifications] == [0]
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "final\n"
    assert agent.last_evidence is not None
    assert [item.exit_code for item in agent.last_evidence.verifications] == [1, 0]

    second_request_messages = provider.requests[1].messages
    assert second_request_messages[2].reasoning_content == "先检查现状"
    first_observation = json.loads(second_request_messages[3].content)
    assert first_observation["ok"] is True
    assert "old" in first_observation["output"]
    assert provider.remaining_responses == 0
    assert provider.requests[0].tools[-1]["function"]["name"] == "request_completion"
    previews = [event for event in recorder.events if event.event == "change_preview"]
    assert len(previews) == 2
    assert previews[-1].data["path"] == "value.txt"
    assert "+final" in previews[-1].data["diff"]


def test_agent_rejects_premature_completion_then_allows_recovery(
    tmp_path: Path,
) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
    command = ["python3", "verify.py"]
    provider = ScriptedProvider(
        [
            _tool_response("patch", "apply_patch", {"patch": patch}),
            _completion_response(["value.txt"], []),
            _tool_response("test", "run_command", {"command": command}),
            _completion_response(["value.txt"], [command]),
        ]
    )

    result = Agent(
        provider, _real_registry(tmp_path), workspace=Workspace(tmp_path)
    ).run("修改并验证")

    assert result.stop_reason is StopReason.COMPLETED
    rejected_observation = json.loads(provider.requests[2].messages[-1].content)
    assert rejected_observation["ok"] is False
    assert "没有运行验证命令" in rejected_observation["error"]


def test_agent_replace_text_participates_in_completion_evidence(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'new\\n'\n",
        encoding="utf-8",
    )
    command = ["python3", "verify.py"]
    provider = ScriptedProvider(
        [
            _tool_response(
                "replace",
                "replace_text",
                {"path": "value.txt", "old_text": "old\n", "new_text": "new\n"},
            ),
            _tool_response("verify", "run_command", {"command": command}),
            _completion_response(["value.txt"], [command]),
        ]
    )

    result = Agent(
        provider, _real_registry(tmp_path), workspace=Workspace(tmp_path)
    ).run("把 old 改为 new 并验证")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.completion_request is not None
    assert result.completion_request.changed_files == ("value.txt",)
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "new\n"


def test_agent_detects_command_side_effect_and_requires_later_verification(
    tmp_path: Path,
) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "format.py").write_text(
        "from pathlib import Path\nPath('value.txt').write_text('new\\n')\n",
        encoding="utf-8",
    )
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'new\\n'\n",
        encoding="utf-8",
    )
    format_command = ["python3", "format.py"]
    verify_command = ["python3", "verify.py"]
    provider = ScriptedProvider(
        [
            _tool_response("format", "run_command", {"command": format_command}),
            _completion_response(["value.txt"], [format_command]),
            _tool_response("verify", "run_command", {"command": verify_command}),
            _completion_response(["value.txt"], [verify_command]),
        ]
    )
    agent = Agent(
        provider, _real_registry(tmp_path), workspace=Workspace(tmp_path)
    )

    result = agent.run("运行格式化脚本修改文件并验证")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.completion_request is not None
    assert result.completion_request.changed_files == ("value.txt",)
    first_command_observation = json.loads(provider.requests[1].messages[-1].content)
    assert first_command_observation["metadata"]["workspace_changed_files"] == [
        "value.txt"
    ]
    rejected_completion = json.loads(provider.requests[2].messages[-1].content)
    assert "最后一次修改之后没有运行验证" in rejected_completion["error"]


def test_agent_refreshes_snapshot_before_completion_and_records_late_change(
    tmp_path: Path,
) -> None:
    (tmp_path / "value.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text("print('ok')\n", encoding="utf-8")
    command = ["python3", "verify.py"]

    class LateMutatingProvider(ScriptedProvider):
        def complete(self, messages, tools=()):
            if len(self.requests) == 2:
                (tmp_path / "late.txt").write_text("late\n", encoding="utf-8")
            return super().complete(messages, tools)

    provider = LateMutatingProvider(
        [
            _tool_response(
                "replace",
                "replace_text",
                {"path": "value.txt", "old_text": "old\n", "new_text": "new\n"},
            ),
            _tool_response("verify-1", "run_command", {"command": command}),
            _completion_response(["value.txt"], [command]),
            _tool_response("verify-2", "run_command", {"command": command}),
            _completion_response(["late.txt", "value.txt"], [command]),
        ]
    )
    recorder = MemoryRecorder()
    agent = Agent(
        provider,
        _real_registry(tmp_path),
        workspace=Workspace(tmp_path),
        recorder=recorder,
    )

    result = agent.run("修改文件并确保完成前没有额外变化")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.completion_request is not None
    assert set(result.completion_request.changed_files) == {"late.txt", "value.txt"}
    snapshot_events = [
        event for event in recorder.events if event.event == "workspace_changed"
    ]
    assert len(snapshot_events) == 1
    assert snapshot_events[0].data["changed_since_previous_snapshot"] == [
        "late.txt"
    ]
    rejected = json.loads(provider.requests[3].messages[-1].content)
    assert "未声明实际修改文件：late.txt" in rejected["error"]
    assert "最后一次修改之后没有运行验证" in rejected["error"]


def test_agent_snapshot_accepts_rename_and_delete_as_real_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "old.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "obsolete.txt").write_text("remove\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        """from pathlib import Path
assert not Path('old.txt').exists()
assert Path('new.txt').read_text() == 'keep\\n'
assert not Path('obsolete.txt').exists()
""",
        encoding="utf-8",
    )
    workspace = Workspace(tmp_path)
    registry = ToolRegistry(
        [
            RenameFileTool(workspace),
            DeleteFileTool(workspace),
            RunCommandTool(workspace),
        ]
    )
    command = ["python3", "verify.py"]
    changed_files = ["new.txt", "obsolete.txt", "old.txt"]
    provider = ScriptedProvider(
        [
            _tool_response(
                "rename",
                "rename_file",
                {"source": "old.txt", "destination": "new.txt"},
            ),
            _tool_response(
                "delete", "delete_file", {"path": "obsolete.txt"}
            ),
            _tool_response("verify", "run_command", {"command": command}),
            _completion_response(changed_files, [command]),
        ]
    )

    result = Agent(provider, registry, workspace=workspace).run("重命名并删除文件")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.completion_request is not None
    assert result.completion_request.changed_files == tuple(changed_files)


def test_agent_compacts_context_and_records_auditable_event(tmp_path: Path) -> None:
    for index in range(4):
        (tmp_path / f"file-{index}.txt").write_text(
            f"value-{index}\n" + "x" * 240,
            encoding="utf-8",
        )
    provider = ScriptedProvider(
        [
            _tool_response(
                f"read-{index}",
                "read_file",
                {"path": f"file-{index}.txt"},
                reasoning=f"读取第 {index} 个文件" + "r" * 220,
            )
            for index in range(4)
        ]
        + [_completion_response([], [])]
    )
    recorder = MemoryRecorder()
    agent = Agent(
        provider,
        _real_registry(tmp_path),
        settings=AgentSettings(max_context_characters=1_200),
        recorder=recorder,
    )

    result = agent.run("依次检查四个文件并总结")

    assert result.stop_reason is StopReason.COMPLETED
    events = [event for event in recorder.events if event.event == "context_compacted"]
    assert events
    assert events[-1].data["total_compacted_turns"] >= 1
    assert events[-1].data["estimated_characters"] <= 1_200
    final_request = provider.requests[-1].messages
    assert any(
        message.role is MessageRole.SYSTEM
        and message.content is not None
        and "较早历史事实摘要" in message.content
        for message in final_request
    )

    retained_call_ids = {
        call.id
        for message in final_request
        if message.role is MessageRole.ASSISTANT
        for call in message.tool_calls
    }
    retained_result_ids = {
        message.tool_call_id
        for message in final_request
        if message.role is MessageRole.TOOL
    }
    assert retained_call_ids == retained_result_ids


def test_agent_natural_language_answer_does_not_bypass_completion_gate() -> None:
    plain = ModelResponse(
        message=Message(role=MessageRole.ASSISTANT, content="我已经完成了。"),
        finish_reason="stop",
    )
    provider = ScriptedProvider([plain, plain])
    agent = Agent(
        provider,
        ToolRegistry(),
        settings=AgentSettings(max_iterations=2),
    )

    result = agent.run("任务")

    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert result.completion_request is None
    assert "Sparrow 控制器反馈" in provider.requests[1].messages[-1].content


def test_agent_breaks_repeated_identical_action() -> None:
    repeated = _tool_response("same", "unknown_tool", {"value": 1})
    provider = ScriptedProvider([repeated, repeated, repeated])
    agent = Agent(
        provider,
        ToolRegistry(),
        settings=AgentSettings(max_iterations=5, repeated_action_limit=3),
    )

    result = agent.run("任务")

    assert result.stop_reason is StopReason.REPEATED_ACTION
    assert result.iterations == 3


def test_agent_retries_retryable_provider_error_without_new_iteration() -> None:
    response = _completion_response([], [])
    provider = ScriptedProvider(
        [ProviderRequestError("稍后重试", retryable=True), response]
    )
    delays: list[float] = []
    agent = Agent(
        provider,
        ToolRegistry(),
        settings=AgentSettings(provider_retries=1, retry_base_seconds=0.25),
        sleeper=delays.append,
    )

    result = agent.run("信息任务")

    assert result.stop_reason is StopReason.COMPLETED
    assert result.iterations == 1
    assert delays == [0.25]
    assert len(provider.requests) == 2


def test_agent_stops_on_terminal_provider_error() -> None:
    provider = ScriptedProvider([ProviderError("服务不可用")])

    result = Agent(provider, ToolRegistry()).run("任务")

    assert result.stop_reason is StopReason.PROVIDER_ERROR
    assert "服务不可用" in result.final_text


def test_agent_honours_cancellation_before_first_provider_request() -> None:
    provider = ScriptedProvider([_completion_response([], [])])
    agent = Agent(provider, ToolRegistry(), is_cancelled=lambda: True)

    result = agent.run("任务")

    assert result.stop_reason is StopReason.CANCELLED
    assert result.iterations == 0
    assert provider.requests == []


def test_agent_honours_cancellation_after_provider_returns() -> None:
    provider = ScriptedProvider([_completion_response([], [])])
    checks = iter((False, False, True))
    agent = Agent(
        provider,
        ToolRegistry(),
        is_cancelled=lambda: next(checks),
    )

    result = agent.run("任务")

    assert result.stop_reason is StopReason.CANCELLED
    assert result.iterations == 1
    assert len(provider.requests) == 1


def test_agent_stops_before_tools_when_token_budget_is_exceeded() -> None:
    response = _tool_response(
        "unknown", "unknown_tool", {}, usage=11
    )
    provider = ScriptedProvider([response])
    agent = Agent(
        provider,
        ToolRegistry(),
        settings=AgentSettings(max_total_tokens=10),
    )

    result = agent.run("任务")

    assert result.stop_reason is StopReason.BUDGET_EXCEEDED
    assert "11" in result.final_text


def test_agent_rejects_completion_mixed_with_other_tool_call() -> None:
    mixed = ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=(
                ToolCall(id="one", name="request_completion", arguments={}),
                ToolCall(id="two", name="missing", arguments={}),
            ),
        ),
        finish_reason="tool_calls",
    )
    provider = ScriptedProvider([mixed])
    agent = Agent(
        provider,
        ToolRegistry(),
        settings=AgentSettings(max_iterations=1),
    )

    result = agent.run("任务")

    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert agent.last_context is not None
    observations = agent.last_context.messages[-2:]
    assert all("不能与其他工具调用" in message.content for message in observations)
