"""不依赖 GUI 框架的单次 Agent 运行会话。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from sparrow_agent.agent import Agent, AgentSettings
from sparrow_agent.models import AgentResult, StopReason
from sparrow_agent.provider import DeepSeekProvider, DeepSeekSettings, ModelProvider
from sparrow_agent.recording import EventRecorder, RunRecorder
from sparrow_agent.runtime import FanoutRecorder, build_tool_registry, load_provider_settings
from sparrow_agent.workspace import Workspace


class SessionState(str, Enum):
    """桌面端可观察的单次运行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """一次运行所需的用户配置。"""

    workspace: str | Path
    task: str
    model: str | None = None
    reasoning_effort: str | None = None
    max_iterations: int = 20
    max_total_tokens: int = 400_000
    max_context_characters: int = 120_000
    record: bool = True
    config_directory: str | Path = field(default_factory=Path.cwd)

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("任务不能为空")

    def agent_settings(self) -> AgentSettings:
        return AgentSettings(
            max_iterations=self.max_iterations,
            max_total_tokens=self.max_total_tokens,
            max_context_characters=self.max_context_characters,
        )


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """供界面按顺序消费的一条结构化事件。"""

    sequence: int
    event: str
    data: Mapping[str, Any]


ProviderFactory = Callable[[DeepSeekSettings], ModelProvider]
SessionListener = Callable[[SessionEvent], None]


class AgentSession:
    """装配并运行一次 Agent，提供线程安全状态、事件与协作式取消。"""

    def __init__(
        self,
        config: SessionConfig,
        *,
        provider_factory: ProviderFactory = DeepSeekProvider,
    ) -> None:
        self.config = config
        self._provider_factory = provider_factory
        self._state = SessionState.IDLE
        self._state_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._events: list[SessionEvent] = []
        self._event_lock = threading.Lock()
        self._listeners: list[SessionListener] = []
        self.result: AgentResult | None = None
        self.error: Exception | None = None
        self.trace_path: Path | None = None

    @property
    def state(self) -> SessionState:
        with self._state_lock:
            return self._state

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        with self._event_lock:
            return tuple(self._events)

    def add_listener(self, listener: SessionListener) -> None:
        """在运行前注册事件监听器；回调会在执行 Agent 的线程中调用。"""

        with self._state_lock:
            if self._state is not SessionState.IDLE:
                raise RuntimeError("只能在会话运行前添加监听器")
            self._listeners.append(listener)

    def cancel(self) -> bool:
        """请求在下一个安全检查点停止；返回是否接受了本次请求。"""

        with self._state_lock:
            if self._state is not SessionState.RUNNING:
                return False
            self._state = SessionState.CANCELLING
            self._cancel_event.set()
            return True

    def run(self) -> AgentResult:
        """同步执行会话；桌面端应当从 `QThread` 调用此方法。"""

        with self._state_lock:
            if self._state is not SessionState.IDLE:
                raise RuntimeError("AgentSession 只能运行一次")
            self._state = SessionState.RUNNING

        disk_recorder: RunRecorder | None = None
        try:
            workspace = Workspace(self.config.workspace)
            provider_settings = load_provider_settings(
                self.config.config_directory,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
            )
            provider = self._provider_factory(provider_settings)
            session_recorder = _SessionRecorder(self)
            recorder: EventRecorder = session_recorder
            if self.config.record:
                disk_recorder = RunRecorder(workspace.root)
                self.trace_path = disk_recorder.jsonl_path
                recorder = FanoutRecorder((session_recorder, disk_recorder))

            result = Agent(
                provider,
                build_tool_registry(workspace),
                settings=self.config.agent_settings(),
                recorder=recorder,
                workspace=workspace,
                is_cancelled=self._cancel_event.is_set,
            ).run(self.config.task.strip())
            self.result = result
            self._set_terminal_state(result)
            return result
        except Exception as exc:
            self.error = exc
            with self._state_lock:
                self._state = SessionState.FAILED
            raise
        finally:
            if disk_recorder is not None:
                disk_recorder.close()

    def _set_terminal_state(self, result: AgentResult) -> None:
        if result.stop_reason is StopReason.COMPLETED:
            state = SessionState.COMPLETED
        elif result.stop_reason is StopReason.CANCELLED:
            state = SessionState.CANCELLED
        else:
            state = SessionState.FAILED
        with self._state_lock:
            self._state = state

    def _publish(self, event: str, data: Mapping[str, Any]) -> None:
        with self._event_lock:
            session_event = SessionEvent(
                sequence=len(self._events) + 1,
                event=event,
                data=dict(data),
            )
            self._events.append(session_event)
        for listener in tuple(self._listeners):
            try:
                listener(session_event)
            except Exception:
                continue


class _SessionRecorder:
    def __init__(self, session: AgentSession) -> None:
        self._session = session

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        self._session._publish(event, data)
