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
from sparrow_agent.tools import (
    ApplyPatchTool,
    ReadFileTool,
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
    agent = Agent(provider, _real_registry(tmp_path))

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

    result = Agent(provider, _real_registry(tmp_path)).run("修改并验证")

    assert result.stop_reason is StopReason.COMPLETED
    rejected_observation = json.loads(provider.requests[2].messages[-1].content)
    assert rejected_observation["ok"] is False
    assert "没有运行验证命令" in rejected_observation["error"]


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
