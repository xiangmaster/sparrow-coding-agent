"""运行轨迹写入、权限与离线重放测试。"""

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sparrow_agent.agent import Agent
from sparrow_agent.models import Message, MessageRole, ToolCall
from sparrow_agent.provider import ModelResponse, ScriptedProvider
from sparrow_agent.recording import (
    MemoryRecorder,
    RecordingError,
    RunRecorder,
    read_trace,
    replay_trace,
)
from sparrow_agent.tools import ToolRegistry
from sparrow_agent.workspace import SensitivePathError, Workspace


def _fixed_clock() -> datetime:
    return datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _completion_response() -> ModelResponse:
    return ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            reasoning_content="没有文件修改，可以直接提交信息任务。",
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
        model="scripted",
        response_id="response-1",
    )


def test_run_recorder_writes_private_jsonl_and_chinese_log(tmp_path: Path) -> None:
    with RunRecorder(tmp_path, run_id="run-001", clock=_fixed_clock) as recorder:
        recorder.record("run_started", {"task": "第一行\n第二行"})
        recorder.record(
            "tool_result",
            {"tool_name": "read_file", "ok": True, "output": "内容"},
        )
        jsonl_path = recorder.jsonl_path
        log_path = recorder.log_path

    assert stat.S_IMODE(jsonl_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(jsonl_path.parent.stat().st_mode) == 0o700
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    assert json.loads(lines[0])["schema_version"] == 1
    human_log = log_path.read_text(encoding="utf-8")
    assert "开始任务：第一行 第二行" in human_log
    assert "工具 read_file：成功" in human_log


def test_agent_records_complete_auditable_event_order() -> None:
    recorder = MemoryRecorder()
    provider = ScriptedProvider([_completion_response()])

    result = Agent(provider, ToolRegistry(), recorder=recorder).run("解释项目状态")

    assert result.stop_reason.value == "completed"
    assert [event.event for event in recorder.events] == [
        "run_started",
        "model_response",
        "tool_result",
        "run_finished",
    ]
    model_event = recorder.events[1]
    assert model_event.data["message"]["reasoning_content"] == (
        "没有文件修改，可以直接提交信息任务。"
    )
    assert recorder.events[2].data["tool_name"] == "request_completion"
    assert recorder.events[-1].data["stop_reason"] == "completed"


def test_agent_disk_trace_can_be_replayed_after_recorder_closes(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([_completion_response()])
    with RunRecorder(tmp_path, run_id="agent-run", clock=_fixed_clock) as recorder:
        result = Agent(provider, ToolRegistry(), recorder=recorder).run("解释项目状态")
        trace_path = recorder.jsonl_path

    summary = replay_trace(trace_path)

    assert result.stop_reason.value == "completed"
    assert summary.completed is True
    assert summary.model_responses == 1
    assert summary.tool_results == 1
    assert summary.events[-1].data["completion_request"]["summary"] == "信息任务完成"


def test_replay_validates_and_summarizes_without_modifying_trace(tmp_path: Path) -> None:
    with RunRecorder(tmp_path, run_id="replay", clock=_fixed_clock) as recorder:
        recorder.record("run_started", {"task": "任务"})
        recorder.record("model_response", {"iteration": 1, "tool_call_count": 1})
        recorder.record("provider_retry", {"next_attempt": 2})
        recorder.record(
            "context_compacted",
            {"newly_compacted_turns": 2, "retained_messages": 5},
        )
        recorder.record("tool_result", {"tool_name": "read_file", "ok": True})
        recorder.record("run_finished", {"stop_reason": "completed"})
        path = recorder.jsonl_path
    before = path.read_bytes()

    summary = replay_trace(path)

    assert summary.completed is True
    assert summary.model_responses == 1
    assert summary.tool_results == 1
    assert summary.provider_retries == 1
    assert summary.context_compactions == 1
    assert "上下文压缩：1" in summary.to_text()
    assert "终止原因：completed" in summary.to_text()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "line",
    [
        "not json\n",
        '{"schema_version":2,"sequence":1,"timestamp":"t","event":"x","data":{}}\n',
        '{"schema_version":1,"sequence":2,"timestamp":"t","event":"x","data":{}}\n',
        '{"schema_version":1,"sequence":1,"timestamp":"t","event":"Bad Event","data":{}}\n',
        '{"schema_version":1,"sequence":1,"timestamp":"t","event":"x","data":[]}\n',
    ],
)
def test_read_trace_rejects_corrupt_or_unsupported_event(
    tmp_path: Path, line: str
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(line, encoding="utf-8")

    with pytest.raises(RecordingError):
        read_trace(path)


def test_read_trace_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    with pytest.raises(RecordingError, match="为空"):
        read_trace(path)


def test_recorder_rejects_invalid_reused_and_closed_run(tmp_path: Path) -> None:
    with pytest.raises(RecordingError, match="run_id"):
        RunRecorder(tmp_path, run_id="../escape")

    recorder = RunRecorder(tmp_path, run_id="once")
    recorder.close()
    with pytest.raises(RecordingError, match="已经关闭"):
        recorder.record("run_started", {"task": "任务"})
    with pytest.raises(RecordingError, match="无法创建 JSONL"):
        RunRecorder(tmp_path, run_id="once")
    with pytest.raises(RecordingError, match="不存在"):
        RunRecorder(tmp_path / "missing")


def test_recorder_rejects_naive_clock(tmp_path: Path) -> None:
    recorder = RunRecorder(
        tmp_path,
        run_id="naive-clock",
        clock=lambda: datetime(2026, 8, 29, 12, 0),
    )
    try:
        with pytest.raises(RecordingError, match="带时区"):
            recorder.record("run_started", {"task": "任务"})
    finally:
        recorder.close()


def test_recorder_rejects_symlinked_internal_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    workspace_root = tmp_path / "workspace"
    outside.mkdir()
    workspace_root.mkdir()
    (workspace_root / ".sparrow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecordingError, match="符号链接"):
        RunRecorder(workspace_root)


def test_workspace_tools_cannot_read_internal_trace_directory(tmp_path: Path) -> None:
    trace = tmp_path / ".sparrow" / "runs" / "trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"task":"secret"}\n', encoding="utf-8")

    with pytest.raises(SensitivePathError, match="敏感目录"):
        Workspace(tmp_path).resolve_file(".sparrow/runs/trace.jsonl")
