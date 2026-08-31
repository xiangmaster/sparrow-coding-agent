"""PySide6 安装后执行的离屏桌面窗口冒烟测试。"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sparrow_agent.desktop import MainWindow, create_application  # noqa: E402
from sparrow_agent.recording import RunRecorder  # noqa: E402


@pytest.mark.gui_smoke
def test_desktop_window_builds_three_panels_and_initial_controls() -> None:
    app = create_application([])
    window = MainWindow()
    window.show()
    app.processEvents()

    assert window.minimumSize().width() == 1180
    assert window.minimumSize().height() == 720
    assert window.start_button.isEnabled() is True
    assert window.stop_button.isEnabled() is False
    assert window.token_budget_spin.value() == 400_000
    assert window.timeline.count() == 0
    assert window.gate_label.text() == "等待运行"

    window.close()
    app.processEvents()


@pytest.mark.gui_smoke
def test_desktop_loads_history_without_starting_a_session(tmp_path: Path) -> None:
    with RunRecorder(
        tmp_path,
        run_id="desktop-history",
        clock=lambda: datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    ) as recorder:
        recorder.record("run_started", {"task": "离线查看这次任务"})
        recorder.record(
            "tool_result",
            {
                "tool_name": "replace_text",
                "ok": True,
                "arguments": {
                    "arguments": {
                        "path": "app.py",
                        "old_text": "old\n",
                        "new_text": "new\n",
                    }
                },
                "metadata": {"workspace_changed_files": ["app.py"]},
            },
        )
        recorder.record(
            "run_finished",
            {
                "stop_reason": "completed",
                "iterations": 1,
                "completion_request": {"summary": "已完成", "changed_files": []},
            },
        )

    app = create_application([])
    window = MainWindow()
    window._set_workspace(tmp_path)
    window.history_list.setCurrentRow(0)
    app.processEvents()

    assert window._session is None
    assert window.timeline.count() == 3
    assert "历史回放" in window.task_title.text()
    assert window.task_editor.toPlainText() == "离线查看这次任务"
    assert window.task_editor.isReadOnly() is True
    assert window.start_button.isEnabled() is False
    assert window.state_badge.text() == "已完成"
    assert window.preview_combo.currentText() == "app.py"
    assert window.preview_button.isEnabled() is True

    window._reset_task()
    assert window.task_editor.isReadOnly() is False
    assert window.start_button.isEnabled() is True
    window.close()
    app.processEvents()
