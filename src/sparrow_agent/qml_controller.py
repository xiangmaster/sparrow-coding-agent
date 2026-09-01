"""Qt Quick 桌面界面与纯 Python Agent 会话之间的状态桥。"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QObject, Property, QThread, QUrl, Signal, Slot

from sparrow_agent.conversation import (
    ConversationConfig,
    ConversationError,
    ConversationSession,
    ConversationStore,
    ConversationThread,
)
from sparrow_agent.history import ChangePreview, discover_history, load_history_run
from sparrow_agent.models import AgentResult, StopReason
from sparrow_agent.recording import RecordedEvent, RecordingError
from sparrow_agent.session import SessionEvent

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

    def remove_listener(self, listener: Callable[[SessionEvent], None]) -> None: ...

    def run_turn(self, message: str) -> AgentResult: ...

    def cancel(self) -> bool: ...


class _SessionWorker(QObject):
    event_received = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session: _RunnableSession, message: str) -> None:
        super().__init__()
        self._session = session
        self._message = message
        self._listener = self.event_received.emit
        self._session.add_listener(self._listener)

    @Slot()
    def run(self) -> None:
        try:
            run_turn = getattr(self._session, "run_turn", None)
            result = run_turn(self._message) if run_turn is not None else self._session.run()
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    @Slot()
    def detach(self) -> None:
        remove_listener = getattr(self._session, "remove_listener", None)
        if remove_listener is not None:
            remove_listener(self._listener)


class DesktopController(QObject):
    """向 QML 暴露一次运行需要的最小、可观察状态。"""

    workspaceChanged = Signal()
    stateChanged = Signal()
    contentChanged = Signal()
    historyChanged = Signal()
    alert = Signal(str, str, str)
    toast = Signal(str, str, str)

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        config_directory: str | Path | None = None,
        session_factory: Callable[[ConversationConfig], _RunnableSession] = ConversationSession,
    ) -> None:
        super().__init__()
        self._workspace = Path(workspace or Path.cwd()).resolve()
        self._config_directory = Path(config_directory or Path.cwd()).resolve()
        self._state_key = "idle"
        self._state_text = "就绪"
        self._mode = "home"
        self._task_text = ""
        self._events: list[dict[str, Any]] = []
        self._conversation_messages: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []
        self._history_refs: list[tuple[str, str | Path]] = []
        self._current_thread_id = ""
        self._last_trash_path: Path | None = None
        self._changed_files: list[str] = []
        self._verification_text = "等待验证"
        self._gate_text = "等待运行"
        self._status_text = "描述任务，Sparrow 会用本地证据证明它已经完成。"
        self._trace_path = ""
        self._previews: list[dict[str, Any]] = []
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

    @Property("QVariantList", notify=contentChanged)
    def conversationMessages(self) -> list[dict[str, Any]]:
        return self._conversation_messages

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
    def previews(self) -> list[dict[str, Any]]:
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
        threads = ConversationStore(self._workspace).discover()
        recorded_turn_paths = {
            (self._workspace / turn.trace_path).resolve()
            for thread in threads
            for turn in thread.turns
            if turn.trace_path
        }
        runs = tuple(
            entry
            for entry in discover_history(self._workspace)
            if entry.trace_path.resolve() not in recorded_turn_paths
        )
        self._history_refs = [
            *(("thread", thread.id) for thread in threads),
            *(("run", entry.trace_path) for entry in runs),
        ]
        thread_items = [
            {
                "runId": thread.id,
                "shortId": thread.id[:10],
                "title": thread.title,
                "time": _short_timestamp(thread.updated_at),
                "stateKey": _thread_state(thread),
                "stateText": f"{len(thread.turns)} 轮 · {_short_stop_reason(_thread_state(thread))}",
                "path": thread.id,
                "isCurrent": thread.id == self._current_thread_id,
            }
            for thread in threads
        ]
        run_items = [
            {
                "runId": entry.run_id,
                "shortId": entry.run_id[:10],
                "title": entry.task.splitlines()[0][:32],
                "time": entry.modified_at.strftime("%m-%d %H:%M"),
                "stateKey": entry.stop_reason or "unknown",
                "stateText": _short_stop_reason(entry.stop_reason),
                "path": str(entry.trace_path),
                "isCurrent": False,
            }
            for entry in runs
        ]
        self._history = [*thread_items, *run_items]
        self.historyChanged.emit()

    @Slot(int)
    def loadHistory(self, index: int) -> None:
        if self._thread is not None or not 0 <= index < len(self._history_refs):
            return
        kind, reference = self._history_refs[index]
        if kind == "thread":
            self._load_conversation(str(reference))
            return
        try:
            run = load_history_run(self._workspace, Path(reference))
        except RecordingError as exc:
            self._set_state("error", "轨迹损坏")
            self.alert.emit("无法读取历史记录", str(exc), "error")
            return

        self._session = None
        self._current_thread_id = ""

        recorded_events = run.events[:_MAX_DISPLAYED_EVENTS]
        self._events = [_present_recorded_event(event) for event in recorded_events]
        self._conversation_messages = [
            {"kind": "user", "text": run.task, "title": "你", "tone": "neutral"},
            {
                "kind": "assistant",
                "text": run.gate_text,
                "title": "Sparrow",
                "tone": "success" if run.stop_reason == "completed" else "error",
            },
        ]
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

    @Slot(int)
    def deleteHistory(self, index: int) -> None:
        """从侧栏移除任务，并把本地记录放入可恢复的回收目录。"""

        if self._thread is not None or not 0 <= index < len(self._history_refs):
            return
        kind, reference = self._history_refs[index]
        title = str(self._history[index].get("title", "任务"))
        try:
            if kind == "thread":
                trash_path = ConversationStore(self._workspace).move_to_trash(
                    str(reference)
                )
            else:
                trash_path = _move_run_to_trash(self._workspace, Path(reference))
        except (ConversationError, RecordingError, OSError) as exc:
            self.alert.emit("无法删除任务", str(exc), "error")
            return

        active_thread = getattr(getattr(self._session, "thread", None), "id", None)
        active_trace = Path(self._trace_path) if self._trace_path else None
        should_reset = (
            kind == "thread" and active_thread == str(reference)
        ) or (
            kind == "run"
            and active_trace is not None
            and active_trace == Path(reference)
        )
        if should_reset:
            self.newTask()
        self._last_trash_path = trash_path
        self.refresh_history()
        self.toast.emit("已移入回收目录", f"“{title}”已从任务列表移除", "撤销")

    @Slot()
    def restoreLastDeleted(self) -> None:
        """恢复最近一次从界面删除的任务。"""

        if self._thread is not None or self._last_trash_path is None:
            return
        try:
            _restore_trash(self._workspace, self._last_trash_path)
        except (RecordingError, OSError) as exc:
            self.alert.emit("无法恢复任务", str(exc), "error")
            return
        self._last_trash_path = None
        self.refresh_history()
        self.toast.emit("任务已恢复", "会话和运行记录已返回原位置", "")

    def _load_conversation(self, thread_id: str) -> None:
        try:
            thread = ConversationStore(self._workspace).load(thread_id)
            config = ConversationConfig(
                workspace=self._workspace,
                config_directory=self._config_directory,
                max_total_tokens=self._token_budget,
            )
            self._session = self._session_factory(config, thread_id=thread_id)  # type: ignore[call-arg]
        except (ConversationError, TypeError, ValueError) as exc:
            self._set_state("error", "会话损坏")
            self.alert.emit("无法恢复任务会话", str(exc), "error")
            return

        messages: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        previews: list[dict[str, Any]] = []
        changed_files: set[str] = set()
        verification_text = "尚无验证"
        gate_text = "等待继续对话"
        trace_path = ""
        phase = 0
        for turn in thread.turns:
            messages.append(
                {"kind": "user", "text": turn.user_message, "title": "你", "tone": "neutral"}
            )
            if turn.trace_path:
                try:
                    run = load_history_run(self._workspace, self._workspace / turn.trace_path)
                except RecordingError:
                    run = None
                if run is not None:
                    for event in run.events:
                        if len(events) < _MAX_DISPLAYED_EVENTS:
                            events.append(_present_recorded_event(event))
                        if event.event == "tool_result":
                            presented = _present_recorded_event(event)
                            tool_name = str(event.data.get("tool_name", ""))
                            messages.append(
                                {
                                    "kind": "tool",
                                    "title": presented["title"],
                                    "text": presented["detail"],
                                    "tone": presented["tone"],
                                    **_tool_visual(tool_name),
                                }
                            )
                    previews.extend(_preview_dict(item) for item in run.previews)
                    changed_files.update(run.changed_files)
                    verification_text = run.verification_text
                    gate_text = run.gate_text
                    trace_path = str(run.entry.trace_path)
                    phase = max(phase, _phase_from_events(run.events))
            if turn.assistant_text:
                messages.append(
                    {
                        "kind": "assistant",
                        "text": turn.assistant_text,
                        "title": "Sparrow",
                        "tone": "success" if turn.stop_reason == "completed" else "error",
                    }
                )

        self._mode = "run"
        self._current_thread_id = thread.id
        self._task_text = thread.title
        self._conversation_messages = messages
        self._events = events
        self._previews = previews
        self._changed_files = sorted(changed_files)
        self._verification_text = verification_text
        self._gate_text = gate_text
        self._trace_path = trace_path
        self._iterations = sum(turn.iterations for turn in thread.turns)
        self._total_tokens = sum(turn.total_tokens for turn in thread.turns)
        self._phase = 4 if _thread_state(thread) == "completed" else phase
        state = _thread_state(thread)
        self._set_state(state, _stop_reason_text(state))
        self._status_text = (
            f"已恢复 {len(thread.turns)} 轮对话 · {self._total_tokens:,} Tokens · 可继续发送消息"
        )
        self.contentChanged.emit()
        self.refresh_history()

    @Slot()
    def newTask(self) -> None:
        if self._thread is not None:
            return
        self._mode = "home"
        self._session = None
        self._current_thread_id = ""
        self._task_text = ""
        self._events = []
        self._conversation_messages = []
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
        self.refresh_history()

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
            config = ConversationConfig(
                workspace=self._workspace,
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
        self._current_thread_id = str(
            getattr(getattr(self._session, "thread", None), "id", "")
        )
        self._token_budget = token_budget
        self._begin_turn(task, reset=True)

    @Slot(str)
    def continueTask(self, message: str) -> None:
        """在当前任务中追加一条用户消息并继续运行。"""

        if self._thread is not None:
            return
        message = message.strip()
        if not message:
            self.alert.emit("还没有消息", "请输入希望 Sparrow 继续处理的内容。", "info")
            return
        if self._session is None or self._mode != "run":
            self.alert.emit("无法继续", "请先开始一个新任务。", "error")
            return
        self._begin_turn(message, reset=False)

    def _begin_turn(self, message: str, *, reset: bool) -> None:
        assert self._session is not None
        self._thread = QThread(self)
        self._worker = _SessionWorker(self._session, message)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event_received.connect(self._on_event)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._worker.detach)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._cleanup_thread)

        self._mode = "run"
        if reset:
            self._task_text = message
            self._events = []
            self._conversation_messages = []
            self._changed_files = []
            self._previews = []
            self._total_tokens = 0
        self._conversation_messages.append(
            {"kind": "user", "text": message, "title": "你", "tone": "neutral"}
        )
        self._verification_text = "等待 Agent 运行验证命令"
        self._gate_text = "正在收集本地完成证据"
        self._status_text = "正在建立工作区快照……"
        self._trace_path = ""
        self._iterations = 0
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
            self.refresh_history()
        elif event.event == "model_response":
            self._iterations = max(self._iterations, _plain_int(data.get("iteration")))
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                self._total_tokens += _plain_int(usage.get("total_tokens"))
        elif event.event == "tool_result":
            self._consume_tool_result(data)
            presented = _present_session_event(event)
            tool_name = str(data.get("tool_name", ""))
            self._conversation_messages.append(
                {
                    "kind": "tool",
                    "title": presented["title"],
                    "text": presented["detail"],
                    "tone": presented["tone"],
                    **_tool_visual(tool_name),
                }
            )
        elif event.event == "change_preview":
            path = data.get("path")
            diff = data.get("diff")
            if isinstance(path, str) and isinstance(diff, str) and diff.strip():
                self._previews.append(
                    _preview_payload(
                        path,
                        diff,
                        added=_plain_int(data.get("added")),
                        removed=_plain_int(data.get("removed")),
                    )
                )
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
        self._conversation_messages.append(
            {
                "kind": "assistant",
                "title": "Sparrow",
                "text": result.final_text,
                "tone": "success" if result.stop_reason is StopReason.COMPLETED else "error",
            }
        )
        self.contentChanged.emit()
        self.refresh_history()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._set_state("error", "运行失败")
        self._gate_text = message
        self._status_text = "运行失败"
        self._conversation_messages.append(
            {"kind": "assistant", "title": "Sparrow", "text": message, "tone": "error"}
        )
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


def _tool_visual(tool_name: str) -> dict[str, str]:
    if tool_name in {"list_files", "search_text"}:
        return {"action": "inspect", "icon": "⌕", "actionLabel": "检查"}
    if tool_name == "read_file":
        return {"action": "read", "icon": "≡", "actionLabel": "读取"}
    if tool_name in _MUTATION_TOOLS:
        return {"action": "edit", "icon": "±", "actionLabel": "修改"}
    if tool_name == "run_command":
        return {"action": "command", "icon": ">_", "actionLabel": "命令"}
    if tool_name == "request_completion":
        return {"action": "gate", "icon": "✓", "actionLabel": "审查"}
    return {"action": "other", "icon": "·", "actionLabel": "工具"}


def _preview_dict(preview: ChangePreview) -> dict[str, Any]:
    return _preview_payload(preview.title, preview.text)


_HUNK_RANGE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _preview_payload(
    title: str, text: str, *, added: int | None = None, removed: int | None = None
) -> dict[str, Any]:
    lines = _diff_lines(text)
    if added is None:
        added = sum(item["kind"] == "add" for item in lines)
    if removed is None:
        removed = sum(item["kind"] == "remove" for item in lines)
    visible = [
        item["text"]
        for item in lines
        if item["kind"] in {"add", "remove", "context"}
    ][:10]
    hover = f"+{added}  -{removed}"
    if visible:
        hover += "\n\n" + "\n".join(visible)
    return {
        "title": title,
        "text": text,
        "added": added,
        "removed": removed,
        "summary": f"+{added}  -{removed}",
        "hoverText": hover,
        "lines": lines,
    }


def _diff_lines(text: str) -> list[dict[str, Any]]:
    old_line = 0
    new_line = 0
    result: list[dict[str, Any]] = []
    for raw in text.splitlines():
        kind = "context"
        old_value: int | str = ""
        new_value: int | str = ""
        if raw.startswith(("--- ", "+++ ")):
            kind = "file"
        elif raw.startswith("@@"):
            kind = "hunk"
            match = _HUNK_RANGE.match(raw)
            if match is not None:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
        elif raw.startswith("+"):
            kind = "add"
            new_value = new_line
            new_line += 1
        elif raw.startswith("-"):
            kind = "remove"
            old_value = old_line
            old_line += 1
        elif raw.startswith("\\"):
            kind = "meta"
        else:
            old_value = old_line
            new_value = new_line
            old_line += 1
            new_line += 1
        result.append(
            {
                "kind": kind,
                "oldLine": str(old_value),
                "newLine": str(new_value),
                "text": raw,
            }
        )
    return result


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


def _thread_state(thread: ConversationThread) -> str:
    if not thread.turns:
        return "idle"
    last = thread.turns[-1]
    return last.stop_reason or last.status.value


def _short_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return "时间未知"


def _move_run_to_trash(workspace: Path, trace_path: Path) -> Path:
    run = load_history_run(workspace, trace_path)
    trash_root = workspace / ".sparrow" / "trash"
    if trash_root.is_symlink():
        raise RecordingError("回收目录不能是符号链接")
    destination = trash_root / f"run-{run.entry.run_id}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True, exist_ok=False)
    for item in (run.entry.trace_path, run.entry.trace_path.with_suffix(".log")):
        if item.is_file() and not item.is_symlink():
            item.replace(destination / item.name)
    return destination


def _restore_trash(workspace: Path, trash_path: Path) -> None:
    """把一个界面删除的任务安全地恢复到记录目录。"""

    root = workspace.resolve(strict=True)
    raw_trash_root = root / ".sparrow" / "trash"
    if raw_trash_root.is_symlink():
        raise RecordingError("回收目录不能是符号链接")
    trash_root = raw_trash_root.resolve(strict=True)
    candidate = trash_path.resolve(strict=True)
    if (
        not candidate.is_relative_to(trash_root)
        or candidate.parent != trash_root
        or candidate.is_symlink()
        or not candidate.is_dir()
    ):
        raise RecordingError("回收记录路径无效")

    items = list(candidate.iterdir())
    if not items or any(item.is_symlink() or not item.is_file() for item in items):
        raise RecordingError("回收记录内容无效")
    allowed_suffixes = {".json", ".jsonl", ".log"}
    if any(item.suffix not in allowed_suffixes for item in items):
        raise RecordingError("回收记录包含未知文件")

    thread_directory = root / ".sparrow" / "threads"
    run_directory = root / ".sparrow" / "runs"
    if thread_directory.is_symlink() or run_directory.is_symlink():
        raise RecordingError("记录目录不能是符号链接")
    destinations = [
        (thread_directory if item.suffix == ".json" else run_directory) / item.name
        for item in items
    ]
    if any(
        destination.exists() or destination.is_symlink()
        for destination in destinations
    ):
        raise RecordingError("原位置已存在同名记录，无法自动恢复")

    thread_directory.mkdir(parents=True, exist_ok=True)
    run_directory.mkdir(parents=True, exist_ok=True)
    for item, destination in zip(items, destinations, strict=True):
        item.replace(destination)
    candidate.rmdir()


def _one_line(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(map(str, value))
    else:
        text = str(value or "")
    return text.replace("\r", " ").replace("\n", " ")[:180]


def _plain_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
