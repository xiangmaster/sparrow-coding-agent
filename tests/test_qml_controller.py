"""Qt Quick 控制器的状态投影与离屏界面测试。"""

import os
import time
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl  # noqa: E402

from sparrow_agent.models import (  # noqa: E402
    AgentResult,
    CompletionRequest,
    Message,
    MessageRole,
    StopReason,
    ToolCall,
)
from sparrow_agent.conversation import ConversationConfig, ConversationSession  # noqa: E402
from sparrow_agent.provider import ModelResponse, ScriptedProvider  # noqa: E402
from sparrow_agent.qml_app import (  # noqa: E402
    build_qml_application,
    dispose_qml_application,
)
from sparrow_agent.qml_controller import (  # noqa: E402
    DesktopController,
    _SessionWorker,
    _diff_lines,
    _present_event,
)
from sparrow_agent.recording import RunRecorder  # noqa: E402
from sparrow_agent.session import SessionEvent  # noqa: E402


def _completion_model_response(call_id: str, summary: str) -> ModelResponse:
    return ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=(
                ToolCall(
                    id=call_id,
                    name="request_completion",
                    arguments={
                        "summary": summary,
                        "changed_files": [],
                        "verification_commands": [],
                        "remaining_risks": [],
                    },
                ),
            ),
        ),
        finish_reason="tool_calls",
    )


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


class _MultiTurnSession(_CompletedSession):
    def __init__(self, config=None) -> None:
        super().__init__(config)
        self.messages = []

    def remove_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def run_turn(self, message: str) -> AgentResult:
        self.messages.append(message)
        events = [
            SessionEvent(1, "run_started", {"task": message}),
            SessionEvent(
                2,
                "tool_result",
                {
                    "tool_name": "read_file",
                    "ok": True,
                    "arguments": {"arguments": {"path": "app.py"}},
                    "metadata": {"workspace_changed_files": []},
                },
            ),
            SessionEvent(
                3,
                "run_finished",
                {
                    "stop_reason": "completed",
                    "completion_request": {"summary": f"已处理：{message}"},
                },
            ),
        ]
        for event in events:
            for listener in tuple(self.listeners):
                listener(event)
        return AgentResult(
            stop_reason=StopReason.COMPLETED,
            final_text=f"已处理：{message}",
            iterations=1,
            completion_request=CompletionRequest(summary=f"已处理：{message}"),
        )


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
    controller._on_event(
        SessionEvent(
            sequence=4,
            event="change_preview",
            data={
                "path": "app.py",
                "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                "added": 1,
                "removed": 1,
            },
        )
    )

    assert controller.phase == 3
    assert controller.changedFiles == ["app.py"]
    assert controller.verificationText.startswith("✓ python -m unittest")
    assert controller.previews[0]["summary"] == "+1  -1"
    assert controller.previews[0]["lines"][3]["kind"] == "remove"


@pytest.mark.gui_smoke
def test_qml_controller_runs_session_in_worker_and_collects_events(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=fake\nDEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    app, engine, _ = build_qml_application(
        [], workspace=tmp_path, config_directory=tmp_path
    )
    configs = []

    def factory(config):
        configs.append(config)
        return _CompletedSession(config)

    controller = DesktopController(
        tmp_path, config_directory=tmp_path, session_factory=factory
    )
    controller.startTask("  检查项目  ", "deepseek-v4-flash", "low", 8, 450_000)
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert controller.isBusy is False
    assert configs[0].workspace == tmp_path
    assert configs[0].max_iterations == 8
    assert configs[0].max_total_tokens == 450_000
    assert controller.mode == "run"
    assert controller.stateKey == "completed"
    assert controller.phase == 4
    assert controller.iterations == 1
    assert controller.totalTokens == 12
    assert controller.gateText == "证据检查通过\n已验证完成"
    assert len(controller.events) == 3
    assert [message["kind"] for message in controller.conversationMessages] == [
        "user",
        "assistant",
    ]
    assert controller.conversationMessages[0]["text"] == "检查项目"
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_reports_worker_failure(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=fake\nDEEPSEEK_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    app, engine, _ = build_qml_application(
        [], workspace=tmp_path, config_directory=tmp_path
    )
    controller = DesktopController(
        tmp_path, config_directory=tmp_path, session_factory=_FailingSession
    )
    alerts = []
    controller.alert.connect(lambda *values: alerts.append(values))

    controller.startTask("触发错误", "deepseek-v4-flash", "low", 4, 400_000)
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert controller.stateKey == "error"
    assert controller.gateText == "模拟会话失败"
    assert alerts[-1][0] == "Sparrow 运行失败"
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_continues_the_same_conversation_session(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=fake\n", encoding="utf-8")
    app, engine, _ = build_qml_application(
        [], workspace=tmp_path, config_directory=tmp_path
    )
    sessions = []

    def factory(config):
        session = _MultiTurnSession(config)
        sessions.append(session)
        return session

    controller = DesktopController(
        tmp_path, config_directory=tmp_path, session_factory=factory
    )
    controller.startTask("检查项目", "model", "low", 4, 400_000)
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    controller.continueTask("继续检查测试")
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert len(sessions) == 1
    assert sessions[0].messages == ["检查项目", "继续检查测试"]
    assert [item["kind"] for item in controller.conversationMessages] == [
        "user",
        "tool",
        "assistant",
        "user",
        "tool",
        "assistant",
    ]
    assert controller.conversationMessages[-1]["text"] == "已处理：继续检查测试"
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_restores_thread_and_continues_after_restart(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=fake\n", encoding="utf-8")
    first = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path),
        provider_factory=lambda settings: ScriptedProvider(
            [
                _completion_model_response("first-complete", "第一轮完成"),
            ]
        ),
    )
    first.run_turn("先检查项目")
    followup_provider = ScriptedProvider(
        [_completion_model_response("second-complete", "第二轮完成")]
    )

    def factory(config, *, thread_id=None):
        return ConversationSession(
            config,
            thread_id=thread_id,
            provider_factory=lambda settings: followup_provider,
        )

    app, engine, _ = build_qml_application(
        [], workspace=tmp_path, config_directory=tmp_path
    )
    controller = DesktopController(
        tmp_path, config_directory=tmp_path, session_factory=factory
    )
    assert controller.history[0]["title"] == "先检查项目"

    controller.loadHistory(0)
    assert controller.mode == "run"
    assert [item["kind"] for item in controller.conversationMessages] == [
        "user",
        "tool",
        "assistant",
    ]
    controller.continueTask("继续检查测试")
    deadline = time.monotonic() + 2
    while controller.isBusy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert len(controller._session.thread.turns) == 2
    users = [
        message.content
        for message in followup_provider.requests[0].messages
        if message.role.value == "user"
    ]
    assert users == ["先检查项目", "继续检查测试"]
    dispose_qml_application(app, engine)


@pytest.mark.gui_smoke
def test_qml_controller_validates_workspace_task_and_history_errors(tmp_path: Path) -> None:
    controller = DesktopController(tmp_path, config_directory=tmp_path)
    alerts = []
    controller.alert.connect(lambda *values: alerts.append(values))

    controller.startTask("", "model", "low", 1, 400_000)
    controller.startTask("任务", "model", "low", 1, 400_000)
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
def test_qml_api_config_does_not_follow_selected_workspace(tmp_path: Path) -> None:
    config_directory = tmp_path / "sparrow"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    config_directory.mkdir()
    first_workspace.mkdir()
    second_workspace.mkdir()
    (config_directory / ".env").write_text("DEEPSEEK_API_KEY=fake\n", encoding="utf-8")
    (second_workspace / ".env").write_text(
        "DEEPSEEK_API_KEY=untrusted-target-key\n", encoding="utf-8"
    )
    controller = DesktopController(
        first_workspace, config_directory=config_directory
    )

    assert controller.hasApiConfig is True
    controller.setWorkspace(str(second_workspace))
    assert controller.hasApiConfig is True


@pytest.mark.gui_smoke
def test_qml_controller_cancel_shutdown_and_terminal_states(tmp_path: Path) -> None:
    controller = DesktopController(tmp_path, config_directory=tmp_path)
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


def test_qml_diff_lines_expose_line_numbers_and_visual_kinds() -> None:
    lines = _diff_lines(
        "--- a/app.py\n+++ b/app.py\n@@ -2,2 +2,2 @@\n-old\n+new\n same\n"
    )

    assert [item["kind"] for item in lines] == [
        "file",
        "file",
        "hunk",
        "remove",
        "add",
        "context",
    ]
    assert lines[3]["oldLine"] == "2"
    assert lines[4]["newLine"] == "2"
    assert lines[5]["oldLine"] == "3"
    assert lines[5]["newLine"] == "3"


def test_session_worker_emits_failure_without_raising() -> None:
    worker = _SessionWorker(_FailingSession(), "任务")
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert errors == ["模拟会话失败"]


@pytest.mark.gui_smoke
def test_qml_application_loads_home_and_history_pages(tmp_path: Path) -> None:
    _history_trace(tmp_path)
    app, engine, controller = build_qml_application(
        [], workspace=tmp_path, config_directory=tmp_path
    )
    roots = engine.rootObjects()

    assert len(roots) == 1
    root = roots[0]
    assert root.objectName() == "sparrowMainWindow"
    assert root.findChild(QObject, "homePage").property("visible") is True
    assert root.findChild(QObject, "tokenBudgetInput").property("value") == 400

    controller.loadHistory(0)
    app.processEvents()

    assert root.findChild(QObject, "runPage").property("visible") is True
    assert root.findChild(QObject, "homePage").property("visible") is False
    assert root.findChild(QObject, "conversationList") is not None
    assert root.findChild(QObject, "conversationComposer") is not None
    assert root.findChild(QObject, "continueTaskButton") is not None
    dispose_qml_application(app, engine)


def test_qml_brand_assets_are_packaged() -> None:
    asset_dir = files("sparrow_agent").joinpath("qml", "assets")

    assert asset_dir.joinpath("logo.png").is_file()
    assert asset_dir.joinpath("logo-mark.svg").is_file()
