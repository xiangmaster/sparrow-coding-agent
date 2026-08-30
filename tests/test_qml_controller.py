"""Qt Quick 控制器的状态投影与离屏界面测试。"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl  # noqa: E402

from sparrow_agent.models import (  # noqa: E402
    AgentResult,
    CompletionRequest,
    StopReason,
)
from sparrow_agent.qml_app import (  # noqa: E402
    build_qml_application,
    dispose_qml_application,
)
from sparrow_agent.qml_controller import (  # noqa: E402
    DesktopController,
    _SessionWorker,
    _present_event,
)
from sparrow_agent.recording import RunRecorder  # noqa: E402
from sparrow_agent.session import SessionEvent  # noqa: E402


class _CompletedSession:
    def __init__(self, _config=None) -> None:
        self.trace_path = None
        self.listeners = []
        self.cancelled = False

    def add_listener(self, listener) -> None:
        self.listeners.append(listener)

    def run(self) -> AgentResult:
        events = [
            SessionEvent(1, "run_started", {"task": "任务"}),
            SessionEvent(
                2,
                "model_response",
                {"iteration": 1, "tool_call_count": 0, "usage": {"total_tokens": 12}},
            ),
            SessionEvent(
                3,
                "run_finished",
                {
                    "stop_reason": "completed",
                    "completion_request": {"summary": "已验证完成"},
                },
            ),
        ]
        for event in events:
            for listener in self.listeners:
                listener(event)
        return AgentResult(
            stop_reason=StopReason.COMPLETED,
            final_text="已验证完成",
            iterations=1,
            completion_request=CompletionRequest(summary="已验证完成"),
        )

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class _FailingSession(_CompletedSession):
    def run(self) -> AgentResult:
        raise RuntimeError("模拟会话失败")


def _history_trace(workspace: Path) -> Path:
    with RunRecorder(
        workspace,
        run_id="qml-history",
        clock=lambda: datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
    ) as recorder:
        recorder.record("run_started", {"task": "修复价格边界"})
        recorder.record(
            "model_response",
            {"iteration": 1, "tool_call_count": 1, "usage": {"total_tokens": 60}},
        )
        recorder.record(
            "tool_result",
            {
                "tool_name": "replace_text",
                "ok": True,
                "arguments": {
                    "arguments": {
                        "path": "price.py",
                        "old_text": "total > 99\n",
                        "new_text": "total >= 99\n",
                    }
                },
                "metadata": {"workspace_changed_files": ["price.py"]},
            },
        )
        recorder.record(
            "run_finished",
            {
                "stop_reason": "completed",
                "iterations": 1,
                "completion_request": {
                    "summary": "边界已修复",
                    "changed_files": ["price.py"],
                },
            },
        )
        return recorder.jsonl_path


@pytest.mark.gui_smoke
def test_qml_controller_projects_history_into_product_state(tmp_path: Path) -> None:
    _history_trace(tmp_path)
    controller = DesktopController(tmp_path)

    assert controller.history[0]["title"] == "修复价格边界"
    assert controller.history[0]["stateText"] == "已完成"

    controller.loadHistory(0)

    assert controller.mode == "history"
    assert controller.taskText == "修复价格边界"
    assert controller.phase == 4
    assert controller.stateKey == "completed"
    assert controller.changedFiles == ["price.py"]
    assert controller.previews[0]["title"] == "price.py"
    assert [event["title"] for event in controller.events] == [
        "建立运行基线",
        "第 1 轮决策",
        "精确修改",
        "证据门已放行",
    ]


@pytest.mark.gui_smoke
def test_qml_controller_live_events_advance_evidence_phases(tmp_path: Path) -> None:
    controller = DesktopController(tmp_path)
    controller._mode = "run"

    controller._on_event(
        SessionEvent(
            sequence=1,
            event="tool_result",
            data={
                "tool_name": "read_file",
                "ok": True,
                "arguments": {"arguments": {"path": "app.py"}},
                "metadata": {"workspace_changed_files": []},
            },
        )
    )
    controller._on_event(
        SessionEvent(
            sequence=2,
            event="tool_result",
            data={
                "tool_name": "apply_patch",
                "ok": True,
                "metadata": {"workspace_changed_files": ["app.py"]},
            },
        )
    )
    controller._on_event(
        SessionEvent(
            sequence=3,
            event="tool_result",
            data={
                "tool_name": "run_command",
                "ok": True,
                "metadata": {
                    "workspace_changed_files": ["app.py"],
                    "command": ["python", "-m", "unittest"],
                    "exit_code": 0,
                },
            },
        )
    )

    assert controller.phase == 3
    assert controller.changedFiles == ["app.py"]
    assert controller.verificationText.startswith("✓ python -m unittest")


@pytest.mark.gui_smoke
def test_qml_controller_runs_session_in_worker_and_collects_events(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=fake\nDEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    app, engine, _ = build_qml_application([], workspace=tmp_path)
    configs = []

    def factory(config):
        configs.append(config)
        return _CompletedSession(config)

    controller = DesktopController(tmp_path, session_factory=factory)
    controller.startTask("  检查项目  ", "deepseek-v4-flash", "low", 8)
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert controller.isBusy is False
    assert configs[0].task == "检查项目"
    assert configs[0].max_iterations == 8
    assert controller.mode == "run"
    assert controller.stateKey == "completed"
    assert controller.phase == 4
    assert controller.iterations == 1
    assert controller.totalTokens == 12
    assert controller.gateText == "证据检查通过\n已验证完成"
    assert len(controller.events) == 3
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_reports_worker_failure(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=fake\nDEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    app, engine, _ = build_qml_application([], workspace=tmp_path)
    controller = DesktopController(tmp_path, session_factory=_FailingSession)
    alerts = []
    controller.alert.connect(lambda *values: alerts.append(values))

    controller.startTask("触发错误", "deepseek-v4-flash", "low", 4)
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert controller.stateKey == "error"
    assert controller.gateText == "模拟会话失败"
    assert alerts[-1][0] == "Sparrow 运行失败"
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_validates_workspace_task_and_history_errors(tmp_path: Path) -> None:
    controller = DesktopController(tmp_path)
    alerts = []
    controller.alert.connect(lambda *values: alerts.append(values))

    controller.startTask("", "model", "low", 1)
    controller.startTask("任务", "model", "low", 1)
    controller.setWorkspace(str(tmp_path / "missing"))
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    controller.setWorkspace(str(file_path))

    assert [item[0] for item in alerts] == [
        "还没有任务",
        "缺少本地配置",
        "项目不可用",
        "项目不可用",
    ]

    runs = tmp_path / ".sparrow" / "runs"
    runs.mkdir(parents=True)
    (runs / "bad.jsonl").write_text("bad\n", encoding="utf-8")
    controller.refresh_history()
    controller.loadHistory(0)
    assert controller.stateKey == "error"
    assert alerts[-1][0] == "无法读取历史记录"

    other = tmp_path / "other"
    other.mkdir()
    controller.setWorkspace(QUrl.fromLocalFile(str(other)).toString())
    assert controller.workspacePath == str(other)
    assert controller.mode == "home"


@pytest.mark.gui_smoke
def test_qml_controller_cancel_shutdown_and_terminal_states(tmp_path: Path) -> None:
    controller = DesktopController(tmp_path)
    session = _CompletedSession()
    controller._session = session

    controller.cancelTask()
    assert controller.stateKey == "cancelling"
    controller.shutdown()
    assert session.cancelled is True

    controller._on_completed(
        AgentResult(StopReason.CANCELLED, "已取消", iterations=2)
    )
    assert controller.stateKey == "cancelled"
    controller._on_completed(
        AgentResult(StopReason.MAX_ITERATIONS, "超限", iterations=3)
    )
    assert controller.stateKey == "max_iterations"


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("provider_retry", {"next_attempt": 2}, "模型请求重试"),
        ("provider_error", {"error": "失败"}, "模型请求失败"),
        ("context_compacted", {"retained_messages": 5}, "压缩较早上下文"),
        ("control_feedback", {}, "完成条件尚未满足"),
        ("workspace_changed", {"changed_since_previous_snapshot": ["a.py"]}, "检测到新的工作区变化"),
        ("unknown_event", {}, "unknown_event"),
    ],
)
def test_qml_event_presenter_covers_control_events(name, data, expected) -> None:
    assert _present_event(name, data, 1)["title"] == expected


def test_session_worker_emits_failure_without_raising() -> None:
    worker = _SessionWorker(_FailingSession())
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert errors == ["模拟会话失败"]


@pytest.mark.gui_smoke
def test_qml_application_loads_home_and_history_pages(tmp_path: Path) -> None:
    _history_trace(tmp_path)
    app, engine, controller = build_qml_application([], workspace=tmp_path)
    roots = engine.rootObjects()

    assert len(roots) == 1
    root = roots[0]
    assert root.objectName() == "sparrowMainWindow"
    assert root.findChild(QObject, "homePage").property("visible") is True

    controller.loadHistory(0)
    app.processEvents()

    assert root.findChild(QObject, "runPage").property("visible") is True
    assert root.findChild(QObject, "homePage").property("visible") is False
    dispose_qml_application(app, engine)
