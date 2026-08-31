"""Qt Quick 桌面界面与纯 Python Agent 会话之间的状态桥。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QObject, Property, QThread, QUrl, Signal, Slot

from sparrow_agent.history import ChangePreview, discover_history, load_history_run
from sparrow_agent.models import AgentResult, StopReason
from sparrow_agent.recording import RecordedEvent, RecordingError
from sparrow_agent.session import AgentSession, SessionConfig, SessionEvent

_MAX_DISPLAYED_EVENTS = 500
_TOOL_LABELS = {
    "list_files": "检查文件结构",
    "read_file": "阅读文件",
    "search_text": "搜索代码",
    "create_directory": "创建目录",
    "replace_text": "精确修改",
    "apply_patch": "应用代码补丁",
    "rename_file": "重命名文件",
    "delete_file": "删除文件",
    "run_command": "运行本地验证",
    "request_completion": "提交完成申请",
}
_MUTATION_TOOLS = frozenset(
    {"create_directory", "replace_text", "apply_patch", "rename_file", "delete_file"}
)


class _RunnableSession(Protocol):
    trace_path: Path | None

    def add_listener(self, listener: Callable[[SessionEvent], None]) -> None: ...

    def run(self) -> AgentResult: ...

    def cancel(self) -> bool: ...


class _SessionWorker(QObject):
    event_received = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: _RunnableSession) -> None:
        super().__init__()
        self._session = session
        self._session.add_listener(self.event_received.emit)

    @Slot()
    def run(self) -> None:
        try:
            self.completed.emit(self._session.run())
        except Exception as exc:
            self.failed.emit(str(exc))


class DesktopController(QObject):
    """向 QML 暴露一次运行需要的最小、可观察状态。"""

    workspaceChanged = Signal()
    stateChanged = Signal()
    contentChanged = Signal()
    historyChanged = Signal()
    alert = Signal(str, str, str)

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        config_directory: str | Path | None = None,
        session_factory: Callable[[SessionConfig], _RunnableSession] = AgentSession,
    ) -> None:
        super().__init__()
        self._workspace = Path(workspace or Path.cwd()).resolve()
        self._config_directory = Path(config_directory or Path.cwd()).resolve()
        self._state_key = "idle"
        self._state_text = "就绪"
        self._mode = "home"
        self._task_text = ""
        self._events: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []
        self._history_paths: list[Path] = []
        self._changed_files: list[str] = []
        self._verification_text = "等待验证"
        self._gate_text = "等待运行"
        self._status_text = "描述任务，Sparrow 会用本地证据证明它已经完成。"
        self._trace_path = ""
        self._previews: list[dict[str, str]] = []
        self._iterations = 0
        self._total_tokens = 0
        self._token_budget = 400_000
        self._phase = 0
        self._session_factory = session_factory
        self._session: _RunnableSession | None = None
        self._thread: QThread | None = None
        self._worker: _SessionWorker | None = None
        self.refresh_history()

    @Property(str, notify=workspaceChanged)
    def workspacePath(self) -> str:
        return str(self._workspace)

    @Property(str, notify=workspaceChanged)
    def workspaceName(self) -> str:
        return self._workspace.name or str(self._workspace)

    @Property(bool, notify=workspaceChanged)
    def hasApiConfig(self) -> bool:
        return (self._config_directory / ".env").is_file()

    @Property(str, notify=stateChanged)
    def stateKey(self) -> str:
        return self._state_key

    @Property(str, notify=stateChanged)
    def stateText(self) -> str:
        return self._state_text

    @Property(bool, notify=stateChanged)
    def isBusy(self) -> bool:
        return self._thread is not None

    @Property(str, notify=contentChanged)
    def mode(self) -> str:
        return self._mode

    @Property(str, notify=contentChanged)
    def taskText(self) -> str:
        return self._task_text

    @Property("QVariantList", notify=contentChanged)
    def events(self) -> list[dict[str, Any]]:
        return self._events

    @Property("QVariantList", notify=historyChanged)
    def history(self) -> list[dict[str, Any]]:
        return self._history

    @Property("QStringList", notify=contentChanged)
    def changedFiles(self) -> list[str]:
        return self._changed_files

    @Property(str, notify=contentChanged)
    def verificationText(self) -> str:
        return self._verification_text

    @Property(str, notify=contentChanged)
    def gateText(self) -> str:
        return self._gate_text

    @Property(str, notify=contentChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=contentChanged)
    def tracePath(self) -> str:
        return self._trace_path

    @Property("QVariantList", notify=contentChanged)
    def previews(self) -> list[dict[str, str]]:
        return self._previews

    @Property(int, notify=contentChanged)
    def iterations(self) -> int:
        return self._iterations

    @Property(int, notify=contentChanged)
    def totalTokens(self) -> int:
        return self._total_tokens

    @Property(int, notify=contentChanged)
    def tokenBudget(self) -> int:
        return self._token_budget

    @Property(int, notify=contentChanged)
    def phase(self) -> int:
        return self._phase

    @Slot(str)
    def setWorkspace(self, value: str) -> None:
        if self._thread is not None:
            return
        url = QUrl(value)
        path = Path(url.toLocalFile() if url.isLocalFile() else value)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            self.alert.emit("项目不可用", "选择的目录不存在或无法访问。", "error")
            return
        if not resolved.is_dir():
            self.alert.emit("项目不可用", "请选择一个普通目录。", "error")
            return
        self._workspace = resolved
        self.workspaceChanged.emit()
        self.newTask()
        self.refresh_history()

    @Slot()
    def refresh_history(self) -> None:
        entries = discover_history(self._workspace)
        self._history_paths = [entry.trace_path for entry in entries]
        self._history = [
            {
                "runId": entry.run_id,
                "shortId": entry.run_id[:10],
                "title": entry.task.splitlines()[0][:32],
                "time": entry.modified_at.strftime("%m-%d %H:%M"),
                "stateKey": entry.stop_reason or "unknown",
                "stateText": _short_stop_reason(entry.stop_reason),
                "path": str(entry.trace_path),
            }
            for entry in entries
        ]
        self.historyChanged.emit()

    @Slot(int)
    def loadHistory(self, index: int) -> None:
        if self._thread is not None or not 0 <= index < len(self._history_paths):
            return
        try:
            run = load_history_run(self._workspace, self._history_paths[index])
        except RecordingError as exc:
            self._set_state("error", "轨迹损坏")
            self.alert.emit("无法读取历史记录", str(exc), "error")
            return

        recorded_events = run.events[:_MAX_DISPLAYED_EVENTS]
        self._events = [_present_recorded_event(event) for event in recorded_events]
        hidden = len(run.events) - len(recorded_events)
        if hidden:
            self._events.append(
                {
                    "kind": "info",
                    "title": f"另有 {hidden} 条事件未展开",
                    "detail": "完整事件仍保存在原始 JSONL 轨迹中。",
                    "tone": "muted",
                    "iteration": "",
                }
            )
        self._mode = "history"
        self._task_text = run.task
        self._changed_files = list(run.changed_files)
        self._verification_text = run.verification_text
        self._gate_text = run.gate_text
        self._status_text = (
            f"离线回放 · {run.started_at:%Y-%m-%d %H:%M:%S} · "
            f"{run.iterations} 轮 · {run.total_tokens:,} Tokens"
        )
        self._trace_path = str(run.entry.trace_path)
        self._previews = [_preview_dict(preview) for preview in run.previews]
        self._iterations = run.iterations
        self._total_tokens = run.total_tokens
        self._phase = 4 if run.stop_reason == "completed" else _phase_from_events(run.events)
        self._set_state(run.stop_reason or "unknown", _stop_reason_text(run.stop_reason))
        self.contentChanged.emit()

    @Slot()
    def newTask(self) -> None:
        if self._thread is not None:
            return
        self._mode = "home"
        self._task_text = ""
        self._events = []
        self._changed_files = []
        self._verification_text = "等待验证"
        self._gate_text = "等待运行"
        self._status_text = "描述任务，Sparrow 会用本地证据证明它已经完成。"
        self._trace_path = ""
        self._previews = []
        self._iterations = 0
        self._total_tokens = 0
        self._phase = 0
        self._set_state("idle", "就绪")
        self.contentChanged.emit()

    @Slot(str, str, str, int, int)
    def startTask(
        self,
        task: str,
        model: str,
        reasoning_effort: str,
        max_iterations: int,
        token_budget: int,
    ) -> None:
        if self._thread is not None:
            return
        task = task.strip()
        if not task:
            self.alert.emit("还没有任务", "请先描述希望 Sparrow 完成什么。", "info")
            return
        if not (self._config_directory / ".env").is_file():
            self.alert.emit(
                "缺少本地配置",
                "Sparrow 启动目录没有 .env，请先在 Coding Agent 项目中配置 DeepSeek API Key。",
                "error",
            )
            return
        try:
            config = SessionConfig(
                workspace=self._workspace,
                task=task,
                config_directory=self._config_directory,
                model=model.strip() or None,
                reasoning_effort=reasoning_effort,
                max_iterations=max_iterations,
                max_total_tokens=token_budget,
            )
        except (TypeError, ValueError) as exc:
            self.alert.emit("运行配置无效", str(exc), "error")
            return

        self._session = self._session_factory(config)
        self._thread = QThread(self)
        self._worker = _SessionWorker(self._session)
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

        self._mode = "run"
        self._task_text = task
        self._events = []
        self._changed_files = []
        self._verification_text = "等待 Agent 运行验证命令"
        self._gate_text = "正在收集本地完成证据"
        self._status_text = "正在建立工作区快照……"
        self._trace_path = ""
        self._previews = []
        self._iterations = 0
        self._total_tokens = 0
        self._token_budget = token_budget
        self._phase = 0
        self._set_state("running", "正在运行")
        self.contentChanged.emit()
        self._thread.start()

    @Slot()
    def cancelTask(self) -> None:
        if self._session is not None and self._session.cancel():
            self._set_state("cancelling", "正在安全取消")
            self._status_text = "等待当前模型请求或工具调用到达安全检查点……"
            self.contentChanged.emit()

    @Slot()
    def shutdown(self) -> None:
        if self._session is not None:
            self._session.cancel()

    @Slot(object)
    def _on_event(self, event: SessionEvent) -> None:
        if len(self._events) < _MAX_DISPLAYED_EVENTS:
            self._events.append(_present_session_event(event))
        data = event.data
        if event.event == "run_started":
            self._phase = max(self._phase, 0)
            if self._session is not None and self._session.trace_path is not None:
                self._trace_path = str(self._session.trace_path)
        elif event.event == "model_response":
            self._iterations = max(self._iterations, _plain_int(data.get("iteration")))
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                self._total_tokens += _plain_int(usage.get("total_tokens"))
        elif event.event == "tool_result":
            self._consume_tool_result(data)
        elif event.event == "run_finished":
            self._consume_run_finished(data)
        self._status_text = (
            f"第 {self._iterations} 轮 · "
            f"{self._total_tokens:,} / {self._token_budget:,} Tokens"
        )
        self.contentChanged.emit()

    @Slot(object)
    def _on_completed(self, result: AgentResult) -> None:
        self._iterations = result.iterations
        if result.stop_reason is StopReason.COMPLETED:
            self._phase = 4
            self._set_state("completed", "证据门已放行")
        elif result.stop_reason is StopReason.CANCELLED:
            self._set_state("cancelled", "已安全取消")
        else:
            self._set_state(result.stop_reason.value, _stop_reason_text(result.stop_reason.value))
        self._status_text = (
            f"{_stop_reason_text(result.stop_reason.value)} · {result.iterations} 轮 · "
            f"{self._total_tokens:,} / {self._token_budget:,} Tokens"
        )
        self.contentChanged.emit()
        self.refresh_history()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._set_state("error", "运行失败")
        self._gate_text = message
        self._status_text = "运行失败"
        self.contentChanged.emit()
        self.alert.emit("Sparrow 运行失败", message, "error")

    @Slot()
    def _cleanup_thread(self) -> None:
        self._worker = None
        self._thread = None
        self.stateChanged.emit()

    def _consume_tool_result(self, data: Mapping[str, Any]) -> None:
        tool_name = data.get("tool_name")
        if tool_name in {"list_files", "read_file", "search_text"}:
            self._phase = max(self._phase, 1)
        elif tool_name in _MUTATION_TOOLS:
            self._phase = max(self._phase, 2)
        elif tool_name == "run_command":
            self._phase = max(self._phase, 3)
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            return
        changed = metadata.get("workspace_changed_files")
        if isinstance(changed, (list, tuple)):
            self._changed_files = sorted(
                {str(path) for path in changed if isinstance(path, str)}
            )
        command = metadata.get("command")
        exit_code = metadata.get("exit_code")
        if isinstance(command, (list, tuple)) and isinstance(exit_code, int):
            mark = "✓" if exit_code == 0 else "✕"
            self._verification_text = (
                f"{mark} {' '.join(map(str, command))}\n退出码 {exit_code}"
            )

    def _consume_run_finished(self, data: Mapping[str, Any]) -> None:
        completion = data.get("completion_request")
        if isinstance(completion, Mapping):
            summary = completion.get("summary")
            self._gate_text = (
                f"证据检查通过\n{summary}"
                if isinstance(summary, str) and summary.strip()
                else "证据检查通过"
            )
        else:
            final_text = data.get("final_text")
            self._gate_text = str(final_text) if isinstance(final_text, str) else "未完成"

    def _set_state(self, key: str, text: str) -> None:
        self._state_key = key
        self._state_text = text
        self.stateChanged.emit()


def _present_recorded_event(event: RecordedEvent) -> dict[str, Any]:
    return _present_event(event.event, event.data, event.sequence)


def _present_session_event(event: SessionEvent) -> dict[str, Any]:
    return _present_event(event.event, event.data, event.sequence)


def _present_event(
    event_name: str, data: Mapping[str, Any], sequence: int
) -> dict[str, Any]:
    title = event_name
    detail = ""
    tone = "neutral"
    kind = "event"
    iteration: int | str = data.get("iteration", "")  # type: ignore[assignment]
    if event_name == "run_started":
        title, detail, kind = "建立运行基线", "已记录工作区初始快照", "start"
    elif event_name == "model_response":
        count = data.get("tool_call_count", 0)
        title = f"第 {data.get('iteration', '?')} 轮决策"
        detail = f"模型规划了 {count} 个工具调用"
        kind = "model"
    elif event_name == "tool_result":
        tool_name = str(data.get("tool_name", "未知工具"))
        title = _TOOL_LABELS.get(tool_name, tool_name)
        ok = data.get("ok") is True
        detail = _tool_detail(data)
        tone = "success" if ok else "error"
        kind = "tool"
    elif event_name == "provider_retry":
        title = "模型请求重试"
        detail = f"准备第 {data.get('next_attempt', '?')} 次请求"
        tone, kind = "warning", "retry"
    elif event_name == "provider_error":
        title = "模型请求失败"
        detail = _one_line(data.get("error", "未知错误"))
        tone, kind = "error", "error"
    elif event_name == "context_compacted":
        title = "压缩较早上下文"
        detail = f"保留 {data.get('retained_messages', '?')} 条关键消息"
        tone, kind = "muted", "compact"
    elif event_name == "control_feedback":
        title = "完成条件尚未满足"
        detail = "控制器要求继续工作或提交结构化完成申请"
        tone, kind = "warning", "gate"
    elif event_name == "workspace_changed":
        title = "检测到新的工作区变化"
        detail = _one_line(data.get("changed_since_previous_snapshot", ""))
        tone, kind = "warning", "change"
    elif event_name == "run_finished":
        stop_reason = data.get("stop_reason")
        title = _stop_reason_text(stop_reason if isinstance(stop_reason, str) else None)
        detail = _one_line(data.get("final_text", "运行已经结束"))
        tone = "success" if stop_reason == "completed" else "error"
        kind = "finish"
    return {
        "sequence": sequence,
        "kind": kind,
        "title": title,
        "detail": detail,
        "tone": tone,
        "iteration": str(iteration) if iteration != "" else "",
    }


def _tool_detail(data: Mapping[str, Any]) -> str:
    if data.get("ok") is not True:
        return _one_line(data.get("error", "工具执行失败"))
    call = data.get("arguments")
    arguments = call.get("arguments") if isinstance(call, Mapping) else None
    if isinstance(arguments, Mapping):
        for name in ("path", "source", "destination"):
            value = arguments.get(name)
            if isinstance(value, str):
                return value
        command = arguments.get("command")
        if isinstance(command, list):
            return " ".join(map(str, command))
    output = data.get("output")
    return _one_line(output) or "操作成功"


def _preview_dict(preview: ChangePreview) -> dict[str, str]:
    return {"title": preview.title, "text": preview.text}


def _phase_from_events(events: tuple[RecordedEvent, ...]) -> int:
    phase = 0
    for event in events:
        if event.event != "tool_result":
            continue
        tool_name = event.data.get("tool_name")
        if tool_name in {"list_files", "read_file", "search_text"}:
            phase = max(phase, 1)
        elif tool_name in _MUTATION_TOOLS:
            phase = max(phase, 2)
        elif tool_name == "run_command":
            phase = max(phase, 3)
    return phase


def _stop_reason_text(stop_reason: str | None) -> str:
    return {
        "completed": "证据门已放行",
        "cancelled": "已安全取消",
        "max_iterations": "达到轮次上限",
        "repeated_action": "重复操作终止",
        "provider_error": "模型请求失败",
        "budget_exceeded": "超出预算",
        "error": "运行失败",
    }.get(stop_reason, "状态未知")


def _short_stop_reason(stop_reason: str | None) -> str:
    return {
        "completed": "已完成",
        "cancelled": "已取消",
        "max_iterations": "轮次上限",
        "repeated_action": "重复终止",
        "provider_error": "请求失败",
        "budget_exceeded": "超出预算",
    }.get(stop_reason, "未结束")


def _one_line(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(map(str, value))
    else:
        text = str(value or "")
    return text.replace("\r", " ").replace("\n", " ")[:180]


def _plain_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
