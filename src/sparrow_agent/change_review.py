"""在工具修改前后捕获受限文本，并生成可审查的真实统一差异。"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from sparrow_agent.models import ToolCall, ToolResult
from sparrow_agent.workspace import Workspace, WorkspaceError

_MAX_CAPTURE_BYTES = 256 * 1024
_MAX_DIFF_CHARACTERS = 400_000
_PATCH_HEADER = re.compile(r"^(---|\+\+\+)\s+([^\t\r\n]+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ChangePreview:
    path: str
    diff: str
    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class _Target:
    old_path: str | None
    new_path: str | None
    before: str | None


class ChangeCapture:
    """一次修改工具调用的只读前置快照。"""

    def __init__(self, workspace: Workspace, targets: tuple[_Target, ...]) -> None:
        self._workspace = workspace
        self._targets = targets

    def finish(self, result: ToolResult) -> tuple[ChangePreview, ...]:
        if not result.ok:
            return ()
        previews: list[ChangePreview] = []
        for target in self._targets:
            after = _read_text(self._workspace, target.new_path)
            if target.before == after and target.old_path == target.new_path:
                continue
            old_label = f"a/{target.old_path}" if target.old_path else "/dev/null"
            new_label = f"b/{target.new_path}" if target.new_path else "/dev/null"
            lines = tuple(
                difflib.unified_diff(
                    (target.before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=old_label,
                    tofile=new_label,
                )
            )
            if not lines and target.old_path != target.new_path:
                lines = (f"--- {old_label}\n", f"+++ {new_label}\n")
            text = "".join(lines)
            if not text:
                continue
            if len(text) > _MAX_DIFF_CHARACTERS:
                text = text[:_MAX_DIFF_CHARACTERS] + "\n……[差异过长，已截断]"
            added = sum(
                line.startswith("+") and not line.startswith("+++")
                for line in lines
            )
            removed = sum(
                line.startswith("-") and not line.startswith("---")
                for line in lines
            )
            previews.append(
                ChangePreview(
                    path=target.new_path or target.old_path or "未知文件",
                    diff=text.rstrip(),
                    added=added,
                    removed=removed,
                )
            )
        return tuple(previews)


def capture_change(workspace: Workspace | None, call: ToolCall) -> ChangeCapture | None:
    """仅为已知修改工具捕获候选文本，不读取敏感或超大文件。"""

    if workspace is None:
        return None
    pairs = _target_pairs(call)
    if not pairs:
        return None
    targets = tuple(
        _Target(old_path, new_path, _read_text(workspace, old_path))
        for old_path, new_path in pairs
    )
    return ChangeCapture(workspace, targets)


def _target_pairs(call: ToolCall) -> tuple[tuple[str | None, str | None], ...]:
    arguments = call.arguments
    if call.name == "create_file":
        path = arguments.get("path")
        return ((None, path),) if isinstance(path, str) else ()
    if call.name == "replace_text":
        path = arguments.get("path")
        return ((path, path),) if isinstance(path, str) else ()
    if call.name == "rename_file":
        source = arguments.get("source")
        destination = arguments.get("destination")
        if isinstance(source, str) and isinstance(destination, str):
            return ((source, destination),)
    if call.name == "delete_file":
        path = arguments.get("path")
        return ((path, None),) if isinstance(path, str) else ()
    if call.name != "apply_patch":
        return ()
    patch = arguments.get("patch")
    if not isinstance(patch, str):
        return ()
    headers = [match.group(2).strip() for match in _PATCH_HEADER.finditer(patch)]
    pairs: list[tuple[str | None, str | None]] = []
    for index in range(0, len(headers) - 1, 2):
        old_path = _clean_patch_path(headers[index])
        new_path = _clean_patch_path(headers[index + 1])
        if old_path is not None or new_path is not None:
            pairs.append((old_path, new_path))
    return tuple(pairs)


def _clean_patch_path(value: str) -> str | None:
    if value == "/dev/null":
        return None
    return value[2:] if value.startswith(("a/", "b/")) else value


def _read_text(workspace: Workspace, path: str | None) -> str | None:
    if path is None:
        return None
    try:
        target = workspace.resolve_file(path)
        if target.stat().st_size > _MAX_CAPTURE_BYTES:
            return None
        data = target.read_bytes()
    except (OSError, WorkspaceError):
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
