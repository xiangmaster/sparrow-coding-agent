"""可持久化、可连续追加用户消息的多轮 Agent 会话。"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from sparrow_agent.agent import Agent, AgentSettings
from sparrow_agent.context import Context
from sparrow_agent.models import AgentResult, Message, MessageRole, StopReason, ToolCall
from sparrow_agent.provider import DeepSeekProvider, DeepSeekSettings, ModelProvider
from sparrow_agent.recording import EventRecorder, RunRecorder
from sparrow_agent.runtime import FanoutRecorder, build_tool_registry, load_provider_settings
from sparrow_agent.session import SessionEvent
from sparrow_agent.workspace import Workspace

THREAD_SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_THREAD_BYTES = 20 * 1024 * 1024


class ConversationError(RuntimeError):
    """对话无法安全恢复或持久化。"""


class ConversationState(StrEnum):
    """一个可复用对话会话的瞬时执行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class TurnStatus(StrEnum):
    """一轮用户消息的持久化终态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ConversationConfig:
    """同一任务中多轮运行共享的配置。"""

    workspace: str | Path
    model: str | None = None
    reasoning_effort: str | None = None
    max_iterations: int = 20
    max_total_tokens: int = 400_000
    max_context_characters: int = 120_000
    record: bool = True
    config_directory: str | Path = field(default_factory=Path.cwd)

    def agent_settings(self) -> AgentSettings:
        return AgentSettings(
            max_iterations=self.max_iterations,
            max_total_tokens=self.max_total_tokens,
            max_context_characters=self.max_context_characters,
        )


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """一条用户消息及其对应的一次 Agent 运行。"""

    id: str
    user_message: str
    status: TurnStatus
    created_at: str
    completed_at: str | None = None
    stop_reason: str | None = None
    assistant_text: str = ""
    iterations: int = 0
    total_tokens: int = 0
    trace_path: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """面向桌面端的持久化任务聚合。"""

    id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    model: str | None = None
    reasoning_effort: str | None = None
    max_iterations: int | None = None
    max_total_tokens: int | None = None
    max_context_characters: int | None = None
    turns: tuple[ConversationTurn, ...] = ()
    context_messages: tuple[Message, ...] = ()


class ConversationStore:
    """在目标工作区的 `.sparrow/threads` 中原子保存对话。"""

    def __init__(self, workspace: str | Path) -> None:
        try:
            self._root = Path(workspace).resolve(strict=True)
        except OSError as exc:
            raise ConversationError(f"对话工作区不存在：{workspace}") from exc
        if not self._root.is_dir():
            raise ConversationError("对话工作区必须是目录")

    def save(self, thread: ConversationThread) -> Path:
        _validate_id(thread.id, "thread id")
        directory = self._thread_directory(create=True)
        destination = directory / f"{thread.id}.json"
        if destination.is_symlink():
            raise ConversationError("对话记录不能是符号链接")
        payload = json.dumps(
            _thread_to_dict(thread),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > _MAX_THREAD_BYTES:
            raise ConversationError("对话记录超过大小上限")
        temporary = directory / f".{thread.id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ConversationError(f"无法保存对话：{destination}") from exc
        return destination

    def load(self, thread_id: str) -> ConversationThread:
        _validate_id(thread_id, "thread id")
        directory = self._thread_directory(create=False)
        path = directory / f"{thread_id}.json"
        if path.is_symlink():
            raise ConversationError("对话记录不能是符号链接")
        try:
            if path.stat().st_size > _MAX_THREAD_BYTES:
                raise ConversationError("对话记录超过大小上限")
            value = json.loads(path.read_text(encoding="utf-8"))
        except ConversationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationError(f"无法读取对话：{path}") from exc
        return _parse_thread(value, expected_id=thread_id, workspace=self._root)

    def discover(self, *, limit: int = 50) -> tuple[ConversationThread, ...]:
        """按最近更新时间发现可安全读取的任务会话。"""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit 必须是 1 到 100 的整数")
        try:
            directory = self._thread_directory(create=False)
            candidates = sorted(
                (
                    path
                    for path in directory.iterdir()
                    if path.is_file() and not path.is_symlink() and path.suffix == ".json"
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except (ConversationError, OSError):
            return ()
        threads: list[ConversationThread] = []
        for path in candidates:
            if len(threads) >= limit or _ID_PATTERN.fullmatch(path.stem) is None:
                continue
            try:
                threads.append(self.load(path.stem))
            except ConversationError:
                continue
        return tuple(threads)

    def move_to_trash(self, thread_id: str) -> Path:
        """把任务及其逐轮轨迹移入本地回收目录，不直接销毁数据。"""

        thread = self.load(thread_id)
        source = self._thread_directory(create=False) / f"{thread_id}.json"
        trash_root = self._root / ".sparrow" / "trash"
        if trash_root.is_symlink():
            raise ConversationError("回收目录不能是符号链接")
        destination = trash_root / f"thread-{thread_id}-{uuid.uuid4().hex[:8]}"
        try:
            destination.mkdir(parents=True, exist_ok=False)
            os.chmod(trash_root, 0o700)
            os.chmod(destination, 0o700)
            for turn in thread.turns:
                if not turn.trace_path:
                    continue
                trace = (self._root / turn.trace_path).resolve(strict=False)
                run_directory = (self._root / ".sparrow" / "runs").resolve(
                    strict=False
                )
                if not trace.is_relative_to(run_directory) or trace.is_symlink():
                    raise ConversationError("任务轨迹路径无效")
                for item in (trace, trace.with_suffix(".log")):
                    if item.is_file() and not item.is_symlink():
                        item.replace(destination / item.name)
            source.replace(destination / source.name)
        except (OSError, ConversationError) as exc:
            raise ConversationError("无法把任务移入回收目录") from exc
        return destination

    def _thread_directory(self, *, create: bool) -> Path:
        internal = self._root / ".sparrow"
        directory = internal / "threads"
        for candidate in (internal, directory):
            if candidate.is_symlink():
                raise ConversationError("对话目录不能经过符号链接")
        if create:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(internal, 0o700)
                os.chmod(directory, 0o700)
            except OSError as exc:
                raise ConversationError("无法创建对话目录") from exc
        if not directory.is_dir():
            raise ConversationError("对话目录不存在")
        return directory


ProviderFactory = Callable[[DeepSeekSettings], ModelProvider]
ConversationListener = Callable[[SessionEvent], None]


class ConversationSession:
    """复用 Provider 上下文，并让每条用户消息独立经过完成证据门。"""

    def __init__(
        self,
        config: ConversationConfig,
        *,
        thread_id: str | None = None,
        provider_factory: ProviderFactory = DeepSeekProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._workspace = Workspace(config.workspace)
        self._store = ConversationStore(self._workspace.root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._provider_factory = provider_factory
        self._provider: ModelProvider | None = None
        self._context: Context | None = None
        self._state = ConversationState.IDLE
        self._state_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._listeners: list[ConversationListener] = []
        self._events: list[SessionEvent] = []
        self._event_lock = threading.Lock()
        self.trace_path: Path | None = None
        self.error: Exception | None = None
        if thread_id is None:
            now = self._timestamp()
            self.thread = ConversationThread(
                id=uuid.uuid4().hex,
                title="新任务",
                workspace=str(self._workspace.root),
                created_at=now,
                updated_at=now,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
                max_iterations=config.max_iterations,
                max_total_tokens=config.max_total_tokens,
                max_context_characters=config.max_context_characters,
            )
        else:
            self.thread = self._store.load(thread_id)
            self.config = replace(
                config,
                model=(
                    self.thread.model
                    if self.thread.model is not None
                    else config.model
                ),
                reasoning_effort=(
                    self.thread.reasoning_effort
                    if self.thread.reasoning_effort is not None
                    else config.reasoning_effort
                ),
                max_iterations=(
                    self.thread.max_iterations
                    if self.thread.max_iterations is not None
                    else config.max_iterations
                ),
                max_total_tokens=(
                    self.thread.max_total_tokens
                    if self.thread.max_total_tokens is not None
                    else config.max_total_tokens
                ),
                max_context_characters=(
                    self.thread.max_context_characters
                    if self.thread.max_context_characters is not None
                    else config.max_context_characters
                ),
            )
            if self.thread.context_messages:
                self._context = Context.from_messages(
                    self.thread.context_messages,
                    max_context_characters=config.max_context_characters,
                )

    @property
    def state(self) -> ConversationState:
        with self._state_lock:
            return self._state

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        with self._event_lock:
            return tuple(self._events)

    def add_listener(self, listener: ConversationListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: ConversationListener) -> None:
        """移除桌面端某一轮使用的临时事件桥。"""

        try:
            self._listeners.remove(listener)
        except ValueError:
            return

    def cancel(self) -> bool:
        with self._state_lock:
            if self._state is not ConversationState.RUNNING:
                return False
            self._state = ConversationState.CANCELLING
            self._cancel_event.set()
            return True

    def run_turn(self, user_message: str) -> AgentResult:
        """同步执行一轮；上一轮结束后可以再次调用。"""

        user_message = user_message.strip()
        if not user_message:
            raise ValueError("用户消息不能为空")
        with self._state_lock:
            if self._state is not ConversationState.IDLE:
                raise RuntimeError("对话当前正在运行")
            self._state = ConversationState.RUNNING
        self._cancel_event.clear()
        self.error = None
        turn_id = uuid.uuid4().hex
        created_at = self._timestamp()
        turn = ConversationTurn(
            id=turn_id,
            user_message=user_message,
            status=TurnStatus.RUNNING,
            created_at=created_at,
        )
        title = (
            user_message.splitlines()[0][:40]
            if not self.thread.turns
            else self.thread.title
        )
        self.thread = replace(
            self.thread,
            title=title,
            updated_at=created_at,
            turns=(*self.thread.turns, turn),
        )
        self._store.save(self.thread)

        disk_recorder: RunRecorder | None = None
        turn_recorder = _TurnRecorder(self, turn_id)
        recorder: EventRecorder = turn_recorder
        try:
            if self.config.record:
                disk_recorder = RunRecorder(self._workspace.root, run_id=turn_id)
                self.trace_path = disk_recorder.jsonl_path
                recorder = FanoutRecorder((turn_recorder, disk_recorder))
            consumed_tokens = sum(
                item.total_tokens
                for item in self.thread.turns
                if item.id != turn_id
            )
            remaining_tokens = self.config.max_total_tokens - consumed_tokens
            if remaining_tokens <= 0:
                result = AgentResult(
                    stop_reason=StopReason.BUDGET_EXCEEDED,
                    final_text=(
                        f"任务累计 Token 用量 {consumed_tokens} 已达到预算 "
                        f"{self.config.max_total_tokens}"
                    ),
                    iterations=0,
                )
                recorder.record(
                    "run_started",
                    {
                        "task": user_message,
                        "max_iterations": self.config.max_iterations,
                        "max_total_tokens": self.config.max_total_tokens,
                        "remaining_total_tokens": 0,
                        "max_context_characters": self.config.max_context_characters,
                        "snapshot_files": None,
                    },
                )
                recorder.record(
                    "run_finished",
                    {
                        "stop_reason": result.stop_reason.value,
                        "final_text": result.final_text,
                        "iterations": 0,
                        "completion_request": None,
                    },
                )
                self._finish_turn(turn_id, result)
                return result
            settings = replace(
                self.config.agent_settings(),
                max_total_tokens=remaining_tokens,
            )
            agent = Agent(
                self._provider_instance(),
                build_tool_registry(self._workspace),
                settings=settings,
                recorder=recorder,
                workspace=self._workspace,
                is_cancelled=self._cancel_event.is_set,
            )
            result = agent.run(user_message, context=self._context)
            self._context = agent.last_context
            self._finish_turn(turn_id, result)
            return result
        except Exception as exc:
            self.error = exc
            self._fail_turn(turn_id, str(exc))
            raise
        finally:
            if disk_recorder is not None:
                disk_recorder.close()
            with self._state_lock:
                self._state = ConversationState.IDLE

    def _provider_instance(self) -> ModelProvider:
        if self._provider is None:
            settings = load_provider_settings(
                self.config.config_directory,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
            )
            self._provider = self._provider_factory(settings)
        return self._provider

    def _finish_turn(self, turn_id: str, result: AgentResult) -> None:
        status = (
            TurnStatus.COMPLETED
            if result.stop_reason is StopReason.COMPLETED
            else TurnStatus.CANCELLED
            if result.stop_reason is StopReason.CANCELLED
            else TurnStatus.FAILED
        )
        total_tokens = sum(
            _event_tokens(event)
            for event in self.events
            if event.data.get("turn_id") == turn_id
        )
        self._replace_turn(
            turn_id,
            status=status,
            completed_at=self._timestamp(),
            stop_reason=result.stop_reason.value,
            assistant_text=result.final_text,
            iterations=result.iterations,
            total_tokens=total_tokens,
            trace_path=(
                str(self.trace_path.relative_to(self._workspace.root))
                if self.trace_path is not None
                else None
            ),
        )

    def _fail_turn(self, turn_id: str, message: str) -> None:
        self._replace_turn(
            turn_id,
            status=TurnStatus.FAILED,
            completed_at=self._timestamp(),
            assistant_text=message,
        )

    def _replace_turn(self, turn_id: str, **changes: Any) -> None:
        turns = tuple(
            replace(turn, **changes) if turn.id == turn_id else turn
            for turn in self.thread.turns
        )
        messages = self._context.messages if self._context is not None else ()
        self.thread = replace(
            self.thread,
            updated_at=self._timestamp(),
            turns=turns,
            context_messages=messages,
        )
        self._store.save(self.thread)

    def _publish(self, turn_id: str, event: str, data: Mapping[str, Any]) -> None:
        enriched = {**data, "thread_id": self.thread.id, "turn_id": turn_id}
        with self._event_lock:
            item = SessionEvent(len(self._events) + 1, event, enriched)
            self._events.append(item)
        for listener in tuple(self._listeners):
            try:
                listener(item)
            except Exception:
                continue

    def _timestamp(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None:
            raise ConversationError("对话时钟必须返回带时区的时间")
        return moment.astimezone(timezone.utc).isoformat()


class _TurnRecorder:
    def __init__(self, session: ConversationSession, turn_id: str) -> None:
        self._session = session
        self._turn_id = turn_id

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        self._session._publish(self._turn_id, event, data)


def _event_tokens(event: SessionEvent) -> int:
    if event.event != "model_response":
        return 0
    usage = event.data.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    value = usage.get("total_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _thread_to_dict(thread: ConversationThread) -> dict[str, Any]:
    return {
        "schema_version": THREAD_SCHEMA_VERSION,
        "id": thread.id,
        "title": thread.title,
        "workspace": thread.workspace,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "settings": {
            "model": thread.model,
            "reasoning_effort": thread.reasoning_effort,
            "max_iterations": thread.max_iterations,
            "max_total_tokens": thread.max_total_tokens,
            "max_context_characters": thread.max_context_characters,
        },
        "turns": [
            {
                "id": turn.id,
                "user_message": turn.user_message,
                "status": turn.status.value,
                "created_at": turn.created_at,
                "completed_at": turn.completed_at,
                "stop_reason": turn.stop_reason,
                "assistant_text": turn.assistant_text,
                "iterations": turn.iterations,
                "total_tokens": turn.total_tokens,
                "trace_path": turn.trace_path,
            }
            for turn in thread.turns
        ],
        "context_messages": [message.to_dict() for message in thread.context_messages],
    }


def _parse_thread(
    value: Any, *, expected_id: str, workspace: Path
) -> ConversationThread:
    if not isinstance(value, Mapping) or value.get("schema_version") != THREAD_SCHEMA_VERSION:
        raise ConversationError("对话记录版本不受支持")
    if value.get("id") != expected_id:
        raise ConversationError("对话记录 id 与文件名不一致")
    if value.get("workspace") != str(workspace):
        raise ConversationError("对话记录不属于当前工作区")
    try:
        turns = tuple(_parse_turn(item) for item in _sequence(value.get("turns")))
        messages = tuple(
            _parse_message(item) for item in _sequence(value.get("context_messages"))
        )
        settings = value.get("settings", {})
        if not isinstance(settings, Mapping):
            raise ValueError("settings 必须是对象")
        return ConversationThread(
            id=expected_id,
            title=_required_text(value.get("title"), "title"),
            workspace=str(workspace),
            created_at=_required_text(value.get("created_at"), "created_at"),
            updated_at=_required_text(value.get("updated_at"), "updated_at"),
            model=_optional_text(settings.get("model")),
            reasoning_effort=_optional_text(settings.get("reasoning_effort")),
            max_iterations=_optional_positive_int(
                settings.get("max_iterations"), "max_iterations"
            ),
            max_total_tokens=_optional_positive_int(
                settings.get("max_total_tokens"), "max_total_tokens"
            ),
            max_context_characters=_optional_positive_int(
                settings.get("max_context_characters"), "max_context_characters"
            ),
            turns=turns,
            context_messages=messages,
        )
    except (TypeError, ValueError) as exc:
        raise ConversationError(f"对话记录字段无效：{exc}") from exc


def _parse_turn(value: Any) -> ConversationTurn:
    if not isinstance(value, Mapping):
        raise ValueError("turn 必须是对象")
    turn_id = _required_text(value.get("id"), "turn id")
    _validate_id(turn_id, "turn id")
    return ConversationTurn(
        id=turn_id,
        user_message=_required_text(value.get("user_message"), "user_message"),
        status=TurnStatus(_required_text(value.get("status"), "status")),
        created_at=_required_text(value.get("created_at"), "created_at"),
        completed_at=_optional_text(value.get("completed_at")),
        stop_reason=_optional_text(value.get("stop_reason")),
        assistant_text=str(value.get("assistant_text") or ""),
        iterations=_plain_nonnegative_int(value.get("iterations"), "iterations"),
        total_tokens=_plain_nonnegative_int(value.get("total_tokens"), "total_tokens"),
        trace_path=_optional_text(value.get("trace_path")),
    )


def _parse_message(value: Any) -> Message:
    if not isinstance(value, Mapping):
        raise ValueError("message 必须是对象")
    calls = tuple(_parse_tool_call(item) for item in _sequence(value.get("tool_calls", [])))
    return Message(
        role=MessageRole(_required_text(value.get("role"), "role")),
        content=value.get("content"),
        tool_calls=calls,
        tool_call_id=_optional_text(value.get("tool_call_id")),
        reasoning_content=_optional_text(value.get("reasoning_content")),
    )


def _parse_tool_call(value: Any) -> ToolCall:
    if not isinstance(value, Mapping):
        raise ValueError("tool_call 必须是对象")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("tool_call.arguments 必须是对象")
    return ToolCall(
        id=_required_text(value.get("id"), "tool_call.id"),
        name=_required_text(value.get("name"), "tool_call.name"),
        arguments=arguments,
        raw_arguments=_optional_text(value.get("raw_arguments")),
        argument_error=_optional_text(value.get("argument_error")),
    )


def _sequence(value: Any) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError("字段必须是数组")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("可选文本字段类型无效")
    return value


def _plain_nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _validate_id(value: str, name: str) -> None:
    if _ID_PATTERN.fullmatch(value) is None:
        raise ConversationError(f"{name} 格式无效")
