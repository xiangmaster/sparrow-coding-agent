"""基于 PySide6 的 Sparrow 原生桌面界面。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sparrow_agent.models import AgentResult, StopReason
from sparrow_agent.session import AgentSession, SessionConfig, SessionEvent


class SessionWorker(QObject):
    """在 Qt 工作线程中同步运行一个纯 Python AgentSession。"""

    event_received = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: AgentSession) -> None:
        super().__init__()
        self._session = session
        self._session.add_listener(self.event_received.emit)

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(self._session.run())
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    """Sparrow 的单工作区、单会话三栏主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self._workspace = Path.cwd()
        self._session: AgentSession | None = None
        self._thread: QThread | None = None
        self._worker: SessionWorker | None = None
        self._total_tokens = 0
        self._iterations = 0
        self.setWindowTitle("Sparrow Agent")
        self.setMinimumSize(1180, 720)
        self.resize(1380, 840)
        self._build_ui()
        self._apply_style()
        self._set_workspace(self._workspace)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 14)
        root_layout.setSpacing(12)
        root_layout.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_timeline_panel())
        splitter.addWidget(self._build_evidence_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 760, 300])
        root_layout.addWidget(splitter, 1)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")
        root_layout.addWidget(self.status_label)
        self.setCentralWidget(root)

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("header")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 10, 16, 10)
        title = QLabel("SPARROW")
        title.setObjectName("appTitle")
        subtitle = QLabel("证据驱动的本地编程智能体")
        subtitle.setObjectName("muted")
        self.workspace_button = QPushButton("选择项目")
        self.workspace_button.clicked.connect(self._choose_workspace)
        self.state_badge = QLabel("就绪")
        self.state_badge.setObjectName("stateBadge")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        layout.addWidget(self.workspace_button)
        layout.addWidget(self.state_badge)
        return frame

    def _build_sidebar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setMinimumWidth(200)
        frame.setMaximumWidth(260)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        new_button = QPushButton("＋  新建任务")
        new_button.setObjectName("primaryButton")
        new_button.clicked.connect(self._reset_task)
        layout.addWidget(new_button)
        layout.addSpacing(12)
        label = QLabel("最近运行")
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        self.history_list = QListWidget()
        self.history_list.setObjectName("historyList")
        layout.addWidget(self.history_list, 1)
        privacy = QLabel("运行轨迹可能包含代码，\n请勿随意分享。")
        privacy.setObjectName("muted")
        layout.addWidget(privacy)
        return frame

    def _build_timeline_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        self.task_title = QLabel("新建编程任务")
        self.task_title.setObjectName("pageTitle")
        layout.addWidget(self.task_title)
        self.timeline = QListWidget()
        self.timeline.setObjectName("timeline")
        self.timeline.setSpacing(6)
        layout.addWidget(self.timeline, 1)
        self.task_editor = QPlainTextEdit()
        self.task_editor.setPlaceholderText(
            "描述要完成的编程任务，例如：检查登录模块并修复失败测试……"
        )
        self.task_editor.setMaximumHeight(116)
        layout.addWidget(self.task_editor)
        action_layout = QHBoxLayout()
        self.start_button = QPushButton("开始运行")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_session)
        self.stop_button = QPushButton("停止任务")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._cancel_session)
        action_layout.addStretch(1)
        action_layout.addWidget(self.stop_button)
        action_layout.addWidget(self.start_button)
        layout.addLayout(action_layout)
        return frame

    def _build_evidence_panel(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setMinimumWidth(280)
        frame.setMaximumWidth(360)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        heading = QLabel("本次运行")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        settings_box = QGroupBox("模型与预算")
        settings_layout = QVBoxLayout(settings_box)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(["deepseek-v4-flash", "deepseek-v4-pro"])
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.addItems(["low", "high", "max"])
        self.iterations_spin = QSpinBox()
        self.iterations_spin.setRange(1, 100)
        self.iterations_spin.setValue(20)
        settings_layout.addWidget(QLabel("模型"))
        settings_layout.addWidget(self.model_combo)
        settings_layout.addWidget(QLabel("推理强度"))
        settings_layout.addWidget(self.reasoning_combo)
        settings_layout.addWidget(QLabel("最大迭代次数"))
        settings_layout.addWidget(self.iterations_spin)
        layout.addWidget(settings_box)

        changes_box = QGroupBox("真实工作区差异")
        changes_layout = QVBoxLayout(changes_box)
        self.changes_label = QLabel("尚无变化")
        self.changes_label.setWordWrap(True)
        changes_layout.addWidget(self.changes_label)
        layout.addWidget(changes_box)

        verification_box = QGroupBox("验证证据")
        verification_layout = QVBoxLayout(verification_box)
        self.verification_label = QLabel("尚无验证")
        self.verification_label.setWordWrap(True)
        verification_layout.addWidget(self.verification_label)
        layout.addWidget(verification_box)

        gate_box = QGroupBox("完成门")
        gate_layout = QVBoxLayout(gate_box)
        self.gate_label = QLabel("等待运行")
        self.gate_label.setWordWrap(True)
        gate_layout.addWidget(self.gate_label)
        layout.addWidget(gate_box)
        layout.addStretch(1)
        return frame

    @Slot()
    def _choose_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 Sparrow 工作区", str(self._workspace)
        )
        if selected:
            self._set_workspace(Path(selected))

    def _set_workspace(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self.workspace_button.setText(self._workspace.name or str(self._workspace))
        self.workspace_button.setToolTip(str(self._workspace))
        self._refresh_history()

    def _refresh_history(self) -> None:
        self.history_list.clear()
        run_directory = self._workspace / ".sparrow" / "runs"
        if not run_directory.is_dir():
            self.history_list.addItem("暂无运行记录")
            return
        traces = sorted(
            run_directory.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:20]
        if not traces:
            self.history_list.addItem("暂无运行记录")
        for trace in traces:
            self.history_list.addItem(trace.stem)

    @Slot()
    def _start_session(self) -> None:
        if self._thread is not None:
            return
        task = self.task_editor.toPlainText().strip()
        if not task:
            QMessageBox.information(self, "缺少任务", "请先描述要完成的编程任务。")
            return
        if not (self._workspace / ".env").is_file():
            QMessageBox.warning(
                self,
                "缺少本地配置",
                "当前项目没有 .env，请先按 .env.example 配置 DeepSeek API Key。",
            )
            return

        config = SessionConfig(
            workspace=self._workspace,
            task=task,
            model=self.model_combo.currentText().strip() or None,
            reasoning_effort=self.reasoning_combo.currentText(),
            max_iterations=self.iterations_spin.value(),
        )
        self._session = AgentSession(config)
        self._thread = QThread(self)
        self._worker = SessionWorker(self._session)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event_received.connect(self._on_event)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._cleanup_thread)
        self._set_running(True)
        self.timeline.clear()
        self.changes_label.setText("尚无变化")
        self.verification_label.setText("尚无验证")
        self.gate_label.setText("正在核对本地证据……")
        self.task_title.setText(task.splitlines()[0][:60])
        self._total_tokens = 0
        self._iterations = 0
        self._thread.start()

    @Slot()
    def _cancel_session(self) -> None:
        if self._session is not None and self._session.cancel():
            self.state_badge.setText("正在取消")
            self.status_label.setText("正在等待当前模型请求或工具调用安全结束……")
            self.stop_button.setEnabled(False)

    @Slot(object)
    def _on_event(self, event: SessionEvent) -> None:
        self.timeline.addItem(_event_text(event))
        self.timeline.scrollToBottom()
        data = event.data
        if event.event == "model_response":
            self._iterations = int(data.get("iteration", self._iterations))
            usage = data.get("usage", {})
            if isinstance(usage, Mapping):
                self._total_tokens += int(usage.get("total_tokens", 0))
        if event.event == "tool_result":
            metadata = data.get("metadata", {})
            if isinstance(metadata, Mapping):
                changed = metadata.get("workspace_changed_files")
                if isinstance(changed, (list, tuple)):
                    self.changes_label.setText(
                        "\n".join(f"• {path}" for path in changed) or "尚无变化"
                    )
                command = metadata.get("command")
                exit_code = metadata.get("exit_code")
                if isinstance(command, (list, tuple)) and isinstance(exit_code, int):
                    mark = "✓" if exit_code == 0 else "✕"
                    self.verification_label.setText(
                        f"{mark} {' '.join(map(str, command))}\n退出码 {exit_code}"
                    )
        if event.event == "run_finished":
            completion = data.get("completion_request")
            self.gate_label.setText(
                "✓ 完成证据检查通过" if completion else "未通过完成证据检查"
            )
        self.status_label.setText(
            f"第 {self._iterations} 轮  ·  {self._total_tokens:,} Tokens"
        )

    @Slot(object)
    def _on_completed(self, result: AgentResult) -> None:
        if result.stop_reason is StopReason.COMPLETED:
            self.state_badge.setText("已完成")
            self.gate_label.setText("✓ 完成证据检查通过")
        elif result.stop_reason is StopReason.CANCELLED:
            self.state_badge.setText("已取消")
            self.gate_label.setText("运行已安全取消")
        else:
            self.state_badge.setText("未完成")
            self.gate_label.setText(result.final_text)
        self.status_label.setText(
            f"{result.stop_reason.value}  ·  {result.iterations} 轮  ·  "
            f"{self._total_tokens:,} Tokens"
        )
        self._set_running(False)
        self._refresh_history()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.state_badge.setText("运行失败")
        self.gate_label.setText(message)
        self.status_label.setText("运行失败")
        self._set_running(False)
        QMessageBox.critical(self, "Sparrow 运行失败", message)

    @Slot()
    def _cleanup_thread(self) -> None:
        self._worker = None
        self._thread = None

    @Slot()
    def _reset_task(self) -> None:
        if self._thread is not None:
            return
        self.task_editor.clear()
        self.timeline.clear()
        self.task_title.setText("新建编程任务")
        self.changes_label.setText("尚无变化")
        self.verification_label.setText("尚无验证")
        self.gate_label.setText("等待运行")
        self.status_label.setText("就绪")
        self.state_badge.setText("就绪")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.task_editor.setReadOnly(running)
        self.workspace_button.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.reasoning_combo.setEnabled(not running)
        self.iterations_spin.setEnabled(not running)
        if running:
            self.state_badge.setText("正在运行")
            self.status_label.setText("正在启动 Agent……")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is None:
            event.accept()
            return
        if self._session is not None:
            self._session.cancel()
        QMessageBox.information(
            self,
            "正在结束任务",
            "已请求安全取消，请等待当前操作结束后再关闭窗口。",
        )
        event.ignore()

    def _apply_style(self) -> None:
        self.setStyleSheet(_STYLE_SHEET)


def _event_text(event: SessionEvent) -> str:
    data = event.data
    if event.event == "run_started":
        return "●  已启动任务并建立工作区快照"
    if event.event == "model_response":
        return (
            f"◆  第 {data.get('iteration', '?')} 轮模型响应  ·  "
            f"{data.get('tool_call_count', 0)} 个工具调用"
        )
    if event.event == "tool_result":
        mark = "✓" if data.get("ok") is True else "✕"
        return f"{mark}  {data.get('tool_name', '未知工具')}  ·  " + (
            "成功" if data.get("ok") is True else str(data.get("error", "失败"))
        )
    if event.event == "provider_retry":
        return f"↻  Provider 重试  ·  第 {data.get('next_attempt', '?')} 次尝试"
    if event.event == "context_compacted":
        return (
            f"◇  已压缩 {data.get('newly_compacted_turns', '?')} 个较早轮次，"
            f"保留 {data.get('retained_messages', '?')} 条消息"
        )
    if event.event == "workspace_changed":
        changed = data.get("changed_since_previous_snapshot", ())
        return f"△  完成前发现额外工作区变化：{', '.join(map(str, changed))}"
    if event.event == "control_feedback":
        return "!  控制器要求模型继续工作或提交结构化完成申请"
    if event.event == "run_finished":
        return f"■  运行结束  ·  {data.get('stop_reason', 'unknown')}"
    return f"·  {event.event}"


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or [])
    app.setApplicationName("Sparrow Agent")
    app.setOrganizationName("Sparrow")
    app.setFont(QFont("PingFang SC", 13))
    return app


def run_desktop() -> int:
    app = create_application(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


_STYLE_SHEET = """
QWidget {
    background: #f4f6f8;
    color: #17212b;
    font-size: 13px;
}
QLabel {
    background: transparent;
}
QFrame#header, QFrame#panel {
    background: #ffffff;
    border: 1px solid #e2e7ec;
    border-radius: 12px;
}
QLabel#appTitle {
    color: #0f766e;
    font-size: 18px;
    font-weight: 800;
    letter-spacing: 2px;
}
QLabel#pageTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#sectionTitle {
    font-size: 14px;
    font-weight: 700;
}
QLabel#muted, QLabel#statusLabel {
    color: #667582;
}
QLabel#stateBadge {
    color: #0f766e;
    background: #e6f5f2;
    border-radius: 9px;
    padding: 6px 10px;
    font-weight: 700;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #cfd8df;
    border-radius: 8px;
    padding: 8px 13px;
}
QPushButton:hover { background: #f0f5f5; }
QPushButton:disabled { color: #9aa5ad; background: #edf0f2; }
QPushButton#primaryButton {
    color: #ffffff;
    background: #0f766e;
    border-color: #0f766e;
    font-weight: 700;
}
QPushButton#primaryButton:hover { background: #115e59; }
QPushButton#dangerButton { color: #b42318; border-color: #f1b5b0; }
QPlainTextEdit, QListWidget, QComboBox, QSpinBox {
    background: #fbfcfd;
    border: 1px solid #dbe2e7;
    border-radius: 8px;
    padding: 7px;
    selection-background-color: #b7ded9;
}
QListWidget#timeline::item {
    background: #f8fafb;
    border: 1px solid #e5eaee;
    border-radius: 8px;
    padding: 11px;
    margin: 2px;
}
QListWidget#historyList::item { padding: 8px; }
QGroupBox {
    background: #fbfcfd;
    border: 1px solid #e0e6ea;
    border-radius: 9px;
    margin-top: 10px;
    padding: 12px 9px 9px 9px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QSplitter::handle { background: transparent; width: 10px; }
"""
