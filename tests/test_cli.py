"""命令行装配与退出码的离线集成测试。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

import sparrow_agent.cli as cli
from sparrow_agent.models import Message, MessageRole, ToolCall
from sparrow_agent.provider import ModelResponse, ScriptedProvider
from sparrow_agent.recording import RunRecorder, replay_trace


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
            reasoning_content="这是信息任务，无需修改文件。",
            tool_calls=(
                ToolCall(
                    id="complete",
                    name="request_completion",
                    arguments={
                        "summary": "命令行任务完成",
                        "changed_files": [],
                        "verification_commands": [],
                        "remaining_risks": [],
                    },
                ),
            ),
        ),
        finish_reason="tool_calls",
        model="deepseek-v4-flash",
    )


def test_run_command_assembles_all_components_and_writes_replayable_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    provider = ScriptedProvider([_completion_response()])
    captured_settings = []

    def fake_provider(settings):
        captured_settings.append(settings)
        return provider

    monkeypatch.setattr(cli, "DeepSeekProvider", fake_provider)

    exit_code = cli.main(
        ["run", "解释", "项目状态", "--workspace", str(tmp_path)]
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert "终止原因：completed" in output.out
    assert "命令行任务完成" in output.out
    assert "deepseek-v4-flash（low）" in output.out
    assert "local-test-key" not in output.out + output.err
    assert "[工具] request_completion：成功" in output.err
    assert captured_settings[0].model == "deepseek-v4-flash"
    assert provider.requests[0].messages[1].content == "解释 项目状态"
    assert len(provider.requests[0].tools) == 10
    assert provider.requests[0].tools[3]["function"]["name"] == "create_directory"
    assert provider.requests[0].tools[4]["function"]["name"] == "replace_text"

    traces = list((tmp_path / ".sparrow" / "runs").glob("*.jsonl"))
    assert len(traces) == 1
    assert replay_trace(traces[0]).completed is True


def test_run_command_honours_cli_overrides_and_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    provider = ScriptedProvider([_completion_response()])
    captured_settings = []

    def fake_provider(settings):
        captured_settings.append(settings)
        return provider

    monkeypatch.setattr(cli, "DeepSeekProvider", fake_provider)

    exit_code = cli.main(
        [
            "run",
            "任务",
            "--workspace",
            str(tmp_path),
            "--model",
            "deepseek-v4-pro",
            "--reasoning-effort",
            "max",
            "--no-record",
        ]
    )

    assert exit_code == 0
    assert captured_settings[0].model == "deepseek-v4-pro"
    assert captured_settings[0].reasoning_effort == "max"
    assert not (tmp_path / ".sparrow").exists()


def test_run_command_returns_incomplete_exit_code_for_unapproved_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    plain_response = ModelResponse(
        message=Message(role=MessageRole.ASSISTANT, content="我完成了"),
        finish_reason="stop",
    )
    monkeypatch.setattr(
        cli,
        "DeepSeekProvider",
        lambda settings: ScriptedProvider([plain_response]),
    )

    exit_code = cli.main(
        [
            "run",
            "任务",
            "--workspace",
            str(tmp_path),
            "--max-iterations",
            "1",
            "--no-record",
        ]
    )

    assert exit_code == 3


def test_run_command_requires_project_local_env_even_if_global_key_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "global-key-must-not-be-used")
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        ["run", "任务", "--workspace", str(tmp_path), "--no-record"]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert "配置文件不存在" in output.err
    assert "global-key-must-not-be-used" not in output.err


def test_run_command_uses_sparrow_env_not_target_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_directory = tmp_path / "sparrow"
    workspace = tmp_path / "target"
    config_directory.mkdir()
    workspace.mkdir()
    _write_env(config_directory)
    (workspace / ".env").write_text(
        "DEEPSEEK_API_KEY=target-key-must-not-be-used\n"
        "SPARROW_MODEL=target-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(config_directory)
    captured_settings = []

    def fake_provider(settings):
        captured_settings.append(settings)
        return ScriptedProvider([_completion_response()])

    monkeypatch.setattr(cli, "DeepSeekProvider", fake_provider)

    exit_code = cli.main(
        ["run", "任务", "--workspace", str(workspace), "--no-record"]
    )

    assert exit_code == 0
    assert captured_settings[0].api_key == "local-test-key"
    assert captured_settings[0].model == "deepseek-v4-flash"


def test_run_command_records_cancelled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path)
    monkeypatch.chdir(tmp_path)

    class InterruptingProvider:
        def complete(self, messages, tools=()):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "DeepSeekProvider", lambda settings: InterruptingProvider())

    exit_code = cli.main(["run", "任务", "--workspace", str(tmp_path)])

    assert exit_code == 130
    trace = next((tmp_path / ".sparrow" / "runs").glob("*.jsonl"))
    assert replay_trace(trace).stop_reason == "cancelled"


def test_replay_command_prints_summary_and_optional_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with RunRecorder(
        tmp_path,
        run_id="cli-replay",
        clock=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    ) as recorder:
        recorder.record("run_started", {"task": "任务"})
        recorder.record("run_finished", {"stop_reason": "completed"})

    exit_code = cli.main(["replay", str(recorder.jsonl_path), "--events"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert "终止原因：completed" in output.out
    assert "0001  run_started" in output.out
    assert "0002  run_finished" in output.out


def test_replay_command_returns_configuration_exit_code_for_bad_trace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_trace = tmp_path / "bad.jsonl"
    bad_trace.write_text("invalid\n", encoding="utf-8")

    exit_code = cli.main(["replay", str(bad_trace)])

    output = capsys.readouterr()
    assert exit_code == 2
    assert "不是有效 JSON" in output.err


def test_version_is_available_without_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])

    assert caught.value.code == 0
    assert "Sparrow 0.1.0" in capsys.readouterr().out


def test_run_parser_exposes_context_character_budget() -> None:
    arguments = cli.build_parser().parse_args(
        ["run", "任务", "--context-characters", "8000"]
    )

    assert arguments.context_characters == 8000
