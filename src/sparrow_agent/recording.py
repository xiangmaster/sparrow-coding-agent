"""版本化运行轨迹、中文日志和不执行副作用的离线重放。"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

TRACE_SCHEMA_VERSION = 1
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_TRACE_BYTES = 50 * 1024 * 1024
_MAX_EVENT_LINE_BYTES = 2 * 1024 * 1024


class RecordingError(RuntimeError):
    """轨迹无法安全写入或读取。"""


class EventRecorder(Protocol):
    """Agent 依赖的最小事件记录接口。"""

    def record(self, event: str, data: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """一条通过格式校验的轨迹事件。"""

    schema_version: int
    sequence: int
    timestamp: str
    event: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event": self.event,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    """离线轨迹的可检查摘要。"""

    events: tuple[RecordedEvent, ...]
    model_responses: int
    tool_results: int
    provider_retries: int
    context_compactions: int
    stop_reason: str | None

    @property
    def completed(self) -> bool:
        return self.stop_reason == "completed"

    def to_text(self) -> str:
        lines = [
            f"事件总数：{len(self.events)}",
            f"模型响应：{self.model_responses}",
            f"工具结果：{self.tool_results}",
            f"Provider 重试：{self.provider_retries}",
            f"上下文压缩：{self.context_compactions}",
            f"终止原因：{self.stop_reason or '未知'}",
        ]
        return "\n".join(lines)


class NullRecorder:
    """默认空记录器，避免让持久化逻辑污染 Agent 分支。"""

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        return None


class MemoryRecorder:
    """测试使用的内存事件记录器。"""

    def __init__(self) -> None:
        self.events: list[RecordedEvent] = []

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        safe_data = _json_round_trip(data)
        self.events.append(
            RecordedEvent(
                schema_version=TRACE_SCHEMA_VERSION,
                sequence=len(self.events) + 1,
                timestamp="1970-01-01T00:00:00+00:00",
                event=_validate_event_name(event),
                data=safe_data,
            )
        )


class RunRecorder:
    """把事件同步写入权限受限的 JSONL 与中文摘要日志。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        run_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            root = Path(workspace_root).resolve(strict=True)
        except OSError as exc:
            raise RecordingError(f"轨迹工作区不存在或无法解析：{workspace_root}") from exc
        if not root.is_dir():
            raise RecordingError(f"轨迹工作区不是目录：{root}")
        self.run_id = run_id or uuid.uuid4().hex
        if _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise RecordingError("run_id 只能包含字母、数字、下划线和连字符")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sequence = 0
        self._closed = False

        internal_directory = _prepare_trace_directory(root, root / ".sparrow")
        run_directory = _prepare_trace_directory(root, internal_directory / "runs")
        self.jsonl_path = run_directory / f"{self.run_id}.jsonl"
        self.log_path = run_directory / f"{self.run_id}.log"
        self._jsonl_stream, self._log_stream = self._open_output_files()

    def _open_output_files(self) -> tuple[TextIO, TextIO]:
        try:
            jsonl_stream = self.jsonl_path.open("x", encoding="utf-8")
            os.chmod(self.jsonl_path, 0o600)
        except OSError as exc:
            raise RecordingError(f"无法创建 JSONL 轨迹：{self.jsonl_path}") from exc
        try:
            log_stream = self.log_path.open("x", encoding="utf-8")
            os.chmod(self.log_path, 0o600)
        except OSError as exc:
            jsonl_stream.close()
            self.jsonl_path.unlink(missing_ok=True)
            raise RecordingError(f"无法创建中文运行日志：{self.log_path}") from exc
        return jsonl_stream, log_stream

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        if self._closed:
            raise RecordingError("记录器已经关闭")
        safe_event = _validate_event_name(event)
        safe_data = _json_round_trip(data)
        self._sequence += 1
        moment = self._clock()
        if moment.tzinfo is None:
            self._sequence -= 1
            raise RecordingError("记录器时钟必须返回带时区的时间")
        timestamp = moment.astimezone(timezone.utc).isoformat()
        recorded = RecordedEvent(
            schema_version=TRACE_SCHEMA_VERSION,
            sequence=self._sequence,
            timestamp=timestamp,
            event=safe_event,
            data=safe_data,
        )
        line = json.dumps(
            recorded.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(line.encode("utf-8")) > _MAX_EVENT_LINE_BYTES:
            self._sequence -= 1
            raise RecordingError("单条轨迹事件超过大小上限")
        self._jsonl_stream.write(line + "\n")
        self._jsonl_stream.flush()
        self._log_stream.write(_human_event_line(recorded) + "\n")
        self._log_stream.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._jsonl_stream.close()
        self._log_stream.close()
        self._closed = True

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def replay_trace(path: str | Path) -> ReplaySummary:
    """校验并汇总 JSONL；不会调用 Provider，也不会执行任何工具。"""

    events = read_trace(path)
    stop_reason: str | None = None
    for event in events:
        if event.event == "run_finished":
            value = event.data.get("stop_reason")
            stop_reason = value if isinstance(value, str) else None
    return ReplaySummary(
        events=events,
        model_responses=sum(event.event == "model_response" for event in events),
        tool_results=sum(event.event == "tool_result" for event in events),
        provider_retries=sum(event.event == "provider_retry" for event in events),
        context_compactions=sum(
            event.event == "context_compacted" for event in events
        ),
        stop_reason=stop_reason,
    )


def read_trace(path: str | Path) -> tuple[RecordedEvent, ...]:
    trace_path = Path(path)
    try:
        if trace_path.stat().st_size > _MAX_TRACE_BYTES:
            raise RecordingError("轨迹文件超过大小上限")
        stream = trace_path.open("rb")
    except OSError as exc:
        raise RecordingError(f"轨迹文件无法读取：{trace_path}") from exc

    events: list[RecordedEvent] = []
    with stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if len(raw_line) > _MAX_EVENT_LINE_BYTES:
                raise RecordingError(f"轨迹第 {line_number} 行超过大小上限")
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecordingError(f"轨迹第 {line_number} 行不是有效 JSON") from exc
            events.append(_parse_recorded_event(value, line_number))
    if not events:
        raise RecordingError("轨迹文件为空")
    return tuple(events)


def _parse_recorded_event(value: Any, line_number: int) -> RecordedEvent:
    if not isinstance(value, dict):
        raise RecordingError(f"轨迹第 {line_number} 行顶层必须是对象")
    if value.get("schema_version") != TRACE_SCHEMA_VERSION:
        raise RecordingError(f"轨迹第 {line_number} 行版本不受支持")
    expected_sequence = line_number
    if value.get("sequence") != expected_sequence:
        raise RecordingError(f"轨迹第 {line_number} 行序号不连续")
    timestamp = value.get("timestamp")
    event = value.get("event")
    data = value.get("data")
    if not isinstance(timestamp, str) or not timestamp:
        raise RecordingError(f"轨迹第 {line_number} 行缺少时间戳")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise RecordingError(f"轨迹第 {line_number} 行时间戳无效") from exc
    if parsed_timestamp.tzinfo is None:
        raise RecordingError(f"轨迹第 {line_number} 行时间戳缺少时区")
    if not isinstance(event, str):
        raise RecordingError(f"轨迹第 {line_number} 行事件名称无效")
    _validate_event_name(event)
    if not isinstance(data, dict):
        raise RecordingError(f"轨迹第 {line_number} 行 data 必须是对象")
    return RecordedEvent(
        schema_version=TRACE_SCHEMA_VERSION,
        sequence=expected_sequence,
        timestamp=timestamp,
        event=event,
        data=data,
    )


def _validate_event_name(event: str) -> str:
    if not isinstance(event, str) or _EVENT_NAME_PATTERN.fullmatch(event) is None:
        raise RecordingError(f"事件名称无效：{event}")
    return event


def _prepare_trace_directory(root: Path, directory: Path) -> Path:
    if directory.is_symlink():
        raise RecordingError(f"轨迹目录不能是符号链接：{directory}")
    try:
        directory.mkdir(exist_ok=True, mode=0o700)
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise RecordingError(f"无法创建轨迹目录：{directory}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise RecordingError(f"轨迹目录越出工作区或不是目录：{directory}")
    os.chmod(resolved, 0o700)
    return resolved


def _json_round_trip(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RecordingError("事件 data 必须是映射")
    try:
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        value = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise RecordingError("事件 data 无法序列化为 JSON") from exc
    if not isinstance(value, dict):
        raise RecordingError("事件 data 序列化后必须是对象")
    return value


def _human_event_line(event: RecordedEvent) -> str:
    data = event.data
    if event.event == "run_started":
        detail = f"开始任务：{_one_line(data.get('task', ''))}"
    elif event.event == "model_response":
        detail = (
            f"第 {data.get('iteration', '?')} 轮模型响应，"
            f"工具调用 {data.get('tool_call_count', 0)} 个"
        )
    elif event.event == "tool_result":
        state = "成功" if data.get("ok") is True else "失败"
        detail = f"工具 {data.get('tool_name', '?')}：{state}"
    elif event.event == "provider_retry":
        detail = f"Provider 请求失败，准备第 {data.get('next_attempt', '?')} 次尝试"
    elif event.event == "control_feedback":
        detail = f"控制器反馈：{_one_line(data.get('feedback', ''))}"
    elif event.event == "context_compacted":
        detail = (
            f"上下文压缩较早轮次 {data.get('newly_compacted_turns', '?')} 个，"
            f"保留消息 {data.get('retained_messages', '?')} 条"
        )
    elif event.event == "run_finished":
        detail = f"运行结束：{data.get('stop_reason', 'unknown')}"
    else:
        detail = event.event
    return f"[{event.sequence:04d}] {event.timestamp} {detail}"


def _one_line(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:200]
