"""共享运行会话的状态、事件、记录与取消测试。"""

import threading
from pathlib import Path

import pytest

from sparrow_agent.models import Message, MessageRole, StopReason, ToolCall
from sparrow_agent.provider import ModelResponse, ScriptedProvider
from sparrow_agent.session import AgentSession, SessionConfig, SessionState


def _write_env(workspace: Path) -> None:
    (workspace / ".env").write_text(
        """DEEPSEEK_API_KEY=local-test-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
SPARROW_MODEL=deepseek-v4-flash
SPARROW_REASONING_EFFORT=low
""",
        encoding="utf-8",
    )


def _completion_response() -> ModelResponse:
    return ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=(
                ToolCall(
                    id="complete",
                    name="request_completion",
                    arguments={
                        "summary": "信息任务完成",
                        "changed_files": [],
                        "verification_commands": [],
                        "remaining_risks": [],
                    },
                ),
            ),
        ),
        finish_reason="tool_calls",
    )


def test_session_runs_once_publishes_events_and_keeps_trace_optional(
    tmp_path: Path,
) -> None:
    _write_env(tmp_path)
    provider = ScriptedProvider([_completion_response()])
    received = []
    session = AgentSession(
        SessionConfig(
            workspace=tmp_path,
            task="解释项目",
            config_directory=tmp_path,
            record=False,
        ),
        provider_factory=lambda settings: provider,
    )
    session.add_listener(lambda event: (_ for _ in ()).throw(RuntimeError("界面异常")))
    session.add_listener(received.append)

    result = session.run()

    assert result.stop_reason is StopReason.COMPLETED
    assert session.state is SessionState.COMPLETED
    assert session.result is result
    assert session.error is None
    assert session.trace_path is None
    assert [event.event for event in received] == [
        "run_started",
        "model_response",
        "tool_result",
        "run_finished",
    ]
    assert session.events == tuple(received)
    with pytest.raises(RuntimeError, match="只能运行一次"):
        session.run()
    with pytest.raises(RuntimeError, match="运行前"):
        session.add_listener(received.append)


def test_session_writes_replayable_trace(tmp_path: Path) -> None:
    _write_env(tmp_path)
    session = AgentSession(
        SessionConfig(workspace=tmp_path, task="解释项目", config_directory=tmp_path),
        provider_factory=lambda settings: ScriptedProvider([_completion_response()]),
    )

    session.run()

    assert session.trace_path is not None
    assert session.trace_path.is_file()


def test_session_cooperatively_cancels_blocking_provider(tmp_path: Path) -> None:
    _write_env(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider:
        def complete(self, messages, tools=()):
            started.set()
            assert release.wait(timeout=2)
            return _completion_response()

    session = AgentSession(
        SessionConfig(
            workspace=tmp_path,
            task="等待取消",
            config_directory=tmp_path,
            record=False,
        ),
        provider_factory=lambda settings: BlockingProvider(),
    )
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(session.run()))
    thread.start()
    assert started.wait(timeout=2)

    assert session.cancel() is True
    assert session.state is SessionState.CANCELLING
    assert session.cancel() is False
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcomes[0].stop_reason is StopReason.CANCELLED
    assert session.state is SessionState.CANCELLED


def test_session_marks_configuration_error_as_failed(tmp_path: Path) -> None:
    session = AgentSession(
        SessionConfig(workspace=tmp_path, task="任务", config_directory=tmp_path)
    )

    with pytest.raises(Exception, match="配置文件不存在"):
        session.run()

    assert session.state is SessionState.FAILED
    assert session.error is not None
    assert session.cancel() is False


def test_session_keeps_provider_config_separate_from_target_workspace(
    tmp_path: Path,
) -> None:
    config_directory = tmp_path / "sparrow"
    workspace = tmp_path / "target"
    config_directory.mkdir()
    workspace.mkdir()
    _write_env(config_directory)
    (workspace / ".env").write_text(
        "DEEPSEEK_API_KEY=untrusted-target-key\nSPARROW_MODEL=untrusted-model\n",
        encoding="utf-8",
    )
    captured_settings = []

    def provider_factory(settings):
        captured_settings.append(settings)
        return ScriptedProvider([_completion_response()])

    session = AgentSession(
        SessionConfig(
            workspace=workspace,
            task="解释项目",
            config_directory=config_directory,
            record=False,
        ),
        provider_factory=provider_factory,
    )

    session.run()

    assert captured_settings[0].api_key == "local-test-key"
    assert captured_settings[0].model == "deepseek-v4-flash"


def test_session_config_rejects_empty_task(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="任务不能为空"):
        SessionConfig(workspace=tmp_path, task="  ")


def test_session_config_defaults_to_real_project_token_budget(tmp_path: Path) -> None:
    config = SessionConfig(workspace=tmp_path, task="任务")

    assert config.max_total_tokens == 400_000
