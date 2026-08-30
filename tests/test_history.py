"""桌面历史发现、离线摘要与变更预览测试。"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sparrow_agent.history import discover_history, load_history_run
from sparrow_agent.recording import RecordingError, RunRecorder


def _clock(moment: datetime):
    return lambda: moment


def _write_completed_trace(workspace: Path, run_id: str = "history") -> Path:
    moment = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    with RunRecorder(workspace, run_id=run_id, clock=_clock(moment)) as recorder:
        recorder.record("run_started", {"task": "修复边界条件"})
        recorder.record(
            "model_response",
            {"iteration": 1, "usage": {"total_tokens": 42}, "tool_call_count": 1},
        )
        recorder.record(
            "tool_result",
            {
                "tool_name": "replace_text",
                "ok": True,
                "arguments": {
                    "name": "replace_text",
                    "arguments": {
                        "path": "price.py",
                        "old_text": "if total > 99:\n",
                        "new_text": "if total >= 99:\n",
                    },
                },
                "metadata": {"workspace_changed_files": ["price.py"]},
            },
        )
        recorder.record(
            "run_finished",
            {
                "stop_reason": "completed",
                "iterations": 1,
                "final_text": "完成",
                "completion_request": {
                    "summary": "边界条件已修复",
                    "changed_files": ["price.py"],
                    "verifications": [
                        {
                            "command": ["python", "-m", "unittest"],
                            "exit_code": 0,
                            "output_summary": "全部通过",
                        }
                    ],
                },
            },
        )
        return recorder.jsonl_path


def test_discover_history_sorts_limits_and_ignores_untrusted_entries(tmp_path: Path) -> None:
    old = _write_completed_trace(tmp_path, "old")
    new = _write_completed_trace(tmp_path, "new")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    link = tmp_path / ".sparrow" / "runs" / "linked.jsonl"
    link.symlink_to(outside)
    base = datetime.now().timestamp()
    os.utime(old, (base - 20, base - 20))
    os.utime(new, (base, base))

    entries = discover_history(tmp_path, limit=1)

    assert [entry.run_id for entry in entries] == ["new"]
    assert entries[0].task == "修复边界条件"
    assert entries[0].stop_reason == "completed"


def test_load_history_run_builds_offline_summary_and_replace_preview(tmp_path: Path) -> None:
    path = _write_completed_trace(tmp_path)

    run = load_history_run(tmp_path, path)

    assert run.task == "修复边界条件"
    assert run.stop_reason == "completed"
    assert run.iterations == 1
    assert run.total_tokens == 42
    assert run.changed_files == ("price.py",)
    assert "python -m unittest" in run.verification_text
    assert "全部通过" in run.verification_text
    assert "边界条件已修复" in run.gate_text
    assert len(run.previews) == 1
    assert "-if total > 99:" in run.previews[0].text
    assert "+if total >= 99:" in run.previews[0].text


def test_load_history_run_preserves_apply_patch_and_marks_non_reconstructable_delete(
    tmp_path: Path,
) -> None:
    moment = datetime.now(timezone.utc)
    with RunRecorder(tmp_path, run_id="changes", clock=_clock(moment)) as recorder:
        recorder.record("run_started", {"task": "调整文件"})
        recorder.record(
            "tool_result",
            {
                "tool_name": "apply_patch",
                "ok": True,
                "arguments": {
                    "arguments": {
                        "patch": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
                    }
                },
                "metadata": {"workspace_changed_files": ["a.py"]},
            },
        )
        recorder.record(
            "tool_result",
            {
                "tool_name": "delete_file",
                "ok": True,
                "arguments": {"arguments": {"path": "unused.py"}},
                "metadata": {"workspace_changed_files": ["a.py", "unused.py"]},
            },
        )
        recorder.record("run_finished", {"stop_reason": "cancelled", "iterations": 1})
        path = recorder.jsonl_path

    run = load_history_run(tmp_path, path)

    assert run.previews[0].text.startswith("--- a/a.py")
    assert "未保存被删除文件的完整内容" in run.previews[1].text


def test_non_completed_trace_cannot_claim_completion_gate_passed(tmp_path: Path) -> None:
    moment = datetime.now(timezone.utc)
    with RunRecorder(tmp_path, run_id="false-completion", clock=_clock(moment)) as recorder:
        recorder.record("run_started", {"task": "未完成任务"})
        recorder.record(
            "run_finished",
            {
                "stop_reason": "cancelled",
                "iterations": 1,
                "final_text": "用户取消",
                "completion_request": {"summary": "不应被接受"},
            },
        )
        path = recorder.jsonl_path

    run = load_history_run(tmp_path, path)

    assert run.gate_text == "用户取消"
    assert "检查通过" not in run.gate_text


def test_load_history_run_rejects_outside_corrupt_and_symlinked_trace(tmp_path: Path) -> None:
    runs = tmp_path / ".sparrow" / "runs"
    runs.mkdir(parents=True)
    corrupt = runs / "bad.jsonl"
    corrupt.write_text("bad json\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("bad json\n", encoding="utf-8")
    link = runs / "link.jsonl"
    link.symlink_to(outside)

    with pytest.raises(RecordingError, match="有效 JSON"):
        load_history_run(tmp_path, corrupt)
    with pytest.raises(RecordingError, match="越出"):
        load_history_run(tmp_path, outside)
    with pytest.raises(RecordingError, match="符号链接"):
        load_history_run(tmp_path, link)


def test_load_history_run_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    (workspace / ".sparrow").mkdir()
    (workspace / ".sparrow" / "runs").symlink_to(
        outside, target_is_directory=True
    )
    trace = outside / "trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")

    assert discover_history(workspace) == ()
    with pytest.raises(RecordingError, match="目录不能是符号链接"):
        load_history_run(workspace, trace)


def test_discover_history_validates_limit_and_handles_missing_directory(tmp_path: Path) -> None:
    assert discover_history(tmp_path) == ()
    with pytest.raises(ValueError, match="limit"):
        discover_history(tmp_path, limit=0)
