"""桌面端使用的安全历史发现、离线摘要与变更预览。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from sparrow_agent.recording import RecordedEvent, RecordingError, read_trace

_MAX_HISTORY_ITEMS = 100
_MUTATION_TOOLS = frozenset(
    {"apply_patch", "replace_text", "rename_file", "delete_file"}
)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """左侧历史列表中的轻量记录。"""

    trace_path: Path
    run_id: str
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class ChangePreview:
    """从成功修改事件中还原出的可读变更说明。"""

    title: str
    text: str


@dataclass(frozen=True, slots=True)
class HistoryRun:
    """一份经过完整格式校验、仅供展示的离线运行记录。"""

    entry: HistoryEntry
    events: tuple[RecordedEvent, ...]
    task: str
    started_at: datetime
    stop_reason: str | None
    iterations: int
    total_tokens: int
    changed_files: tuple[str, ...]
    verification_text: str
    gate_text: str
    previews: tuple[ChangePreview, ...]


def discover_history(workspace: str | Path, *, limit: int = 20) -> tuple[HistoryEntry, ...]:
    """发现工作区内的普通 JSONL 轨迹，不跟随符号链接。"""

    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= _MAX_HISTORY_ITEMS
    ):
        raise ValueError(f"limit 必须是 1 到 {_MAX_HISTORY_ITEMS} 的整数")
    root = Path(workspace).resolve()
    run_directory = root / ".sparrow" / "runs"
    if not run_directory.is_dir() or run_directory.is_symlink():
        return ()

    entries: list[HistoryEntry] = []
    try:
        candidates = tuple(run_directory.iterdir())
    except OSError:
        return ()
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file() or path.suffix != ".jsonl":
                continue
            stat_result = path.stat()
        except OSError:
            continue
        entries.append(
            HistoryEntry(
                trace_path=path,
                run_id=path.stem,
                modified_at=datetime.fromtimestamp(stat_result.st_mtime).astimezone(),
            )
        )
    entries.sort(key=lambda item: item.modified_at, reverse=True)
    return tuple(entries[:limit])


def load_history_run(workspace: str | Path, trace_path: str | Path) -> HistoryRun:
    """完整校验并汇总一份轨迹；函数不会执行任何外部操作。"""

    root = Path(workspace).resolve()
    run_directory = root / ".sparrow" / "runs"
    candidate = Path(trace_path)
    if run_directory.is_symlink():
        raise RecordingError("历史轨迹目录不能是符号链接")
    if candidate.is_symlink():
        raise RecordingError("历史轨迹不能是符号链接")
    try:
        resolved = candidate.resolve(strict=True)
        safe_directory = run_directory.resolve(strict=True)
    except OSError as exc:
        raise RecordingError(f"历史轨迹不存在或无法解析：{candidate}") from exc
    if not resolved.is_relative_to(safe_directory):
        raise RecordingError("历史轨迹越出当前工作区")
    if resolved.suffix != ".jsonl" or not resolved.is_file():
        raise RecordingError("历史轨迹必须是 JSONL 普通文件")

    events = read_trace(resolved)
    first = events[0]
    try:
        started_at = datetime.fromisoformat(first.timestamp).astimezone()
    except ValueError as exc:  # read_trace 已校验；保留清晰的领域错误。
        raise RecordingError("历史轨迹开始时间无效") from exc
    task = "未记录任务"
    if first.event == "run_started" and isinstance(first.data.get("task"), str):
        task = str(first.data["task"]).strip() or task

    stop_reason: str | None = None
    iterations = 0
    total_tokens = 0
    changed_files: set[str] = set()
    verification_text = "尚无验证"
    gate_text = "轨迹没有结束事件"
    previews: list[ChangePreview] = []
    for event in events:
        data = event.data
        if event.event == "model_response":
            iterations = max(iterations, _plain_int(data.get("iteration")))
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                total_tokens += _plain_int(usage.get("total_tokens"))
        elif event.event == "tool_result":
            metadata = data.get("metadata")
            if isinstance(metadata, Mapping):
                changed = metadata.get("workspace_changed_files")
                if isinstance(changed, (list, tuple)):
                    changed_files.update(
                        str(path) for path in changed if isinstance(path, str)
                    )
                command = metadata.get("command")
                exit_code = metadata.get("exit_code")
                if isinstance(command, (list, tuple)) and isinstance(exit_code, int):
                    mark = "✓" if exit_code == 0 else "✕"
                    verification_text = (
                        f"{mark} {' '.join(map(str, command))}\n退出码 {exit_code}"
                    )
            preview = _change_preview(event)
            if preview is not None:
                previews.append(preview)
        elif event.event == "run_finished":
            value = data.get("stop_reason")
            stop_reason = value if isinstance(value, str) else None
            iterations = max(iterations, _plain_int(data.get("iterations")))
            completion = data.get("completion_request")
            if stop_reason == "completed" and isinstance(completion, Mapping):
                declared = completion.get("changed_files")
                if isinstance(declared, (list, tuple)):
                    changed_files.update(
                        str(path) for path in declared if isinstance(path, str)
                    )
                verifications = completion.get("verifications")
                if isinstance(verifications, list) and verifications:
                    verification_text = _verification_summary(verifications[-1])
                summary = completion.get("summary")
                gate_text = (
                    f"✓ 完成证据检查通过\n{summary}"
                    if isinstance(summary, str) and summary.strip()
                    else "✓ 完成证据检查通过"
                )
            else:
                final_text = data.get("final_text")
                gate_text = (
                    str(final_text).strip()
                    if isinstance(final_text, str) and final_text.strip()
                    else "未通过完成证据检查"
                )

    entry = HistoryEntry(
        trace_path=resolved,
        run_id=resolved.stem,
        modified_at=datetime.fromtimestamp(resolved.stat().st_mtime).astimezone(),
    )
    return HistoryRun(
        entry=entry,
        events=events,
        task=task,
        started_at=started_at,
        stop_reason=stop_reason,
        iterations=iterations,
        total_tokens=total_tokens,
        changed_files=tuple(sorted(changed_files)),
        verification_text=verification_text,
        gate_text=gate_text,
        previews=tuple(previews),
    )


def _change_preview(event: RecordedEvent) -> ChangePreview | None:
    data = event.data
    if data.get("ok") is not True:
        return None
    tool_name = data.get("tool_name")
    if tool_name not in _MUTATION_TOOLS:
        return None
    call = data.get("arguments")
    arguments = call.get("arguments") if isinstance(call, Mapping) else None
    if not isinstance(arguments, Mapping):
        return None
    if tool_name == "apply_patch":
        patch = arguments.get("patch")
        if isinstance(patch, str) and patch.strip():
            return ChangePreview("统一差异补丁", patch.rstrip())
    if tool_name == "replace_text":
        path = arguments.get("path")
        old = arguments.get("old_text")
        new = arguments.get("new_text")
        if all(isinstance(value, str) for value in (path, old, new)):
            diff = difflib.unified_diff(
                str(old).splitlines(keepends=True),
                str(new).splitlines(keepends=True),
                fromfile=f"a/{path}（替换片段）",
                tofile=f"b/{path}（替换片段）",
            )
            text = "".join(diff).rstrip()
            return ChangePreview(str(path), text or "文本替换未产生可显示的行级差异")
    if tool_name == "rename_file":
        source = arguments.get("source")
        destination = arguments.get("destination")
        if isinstance(source, str) and isinstance(destination, str):
            return ChangePreview(destination, f"重命名：{source} → {destination}")
    if tool_name == "delete_file":
        path = arguments.get("path")
        if isinstance(path, str):
            return ChangePreview(path, f"已删除文件：{path}\n轨迹未保存被删除文件的完整内容。")
    return None


def _verification_summary(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "尚无验证"
    command = value.get("command")
    exit_code = value.get("exit_code")
    if not isinstance(command, (list, tuple)) or not isinstance(exit_code, int):
        return "尚无验证"
    mark = "✓" if exit_code == 0 else "✕"
    summary = value.get("output_summary")
    suffix = f"\n{summary}" if isinstance(summary, str) and summary.strip() else ""
    return f"{mark} {' '.join(map(str, command))}\n退出码 {exit_code}{suffix}"


def _plain_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
