"""受工作区边界保护的只读文件工具。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, Mapping

from sparrow_agent.models import ToolResult
from sparrow_agent.tools.base import ToolSpec
from sparrow_agent.workspace import Workspace, WorkspaceError, WorkspacePathError

_IGNORED_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_MAX_LIST_RESULTS = 1_000
_DEFAULT_LIST_RESULTS = 200
_MAX_READ_BYTES = 256 * 1024
_MAX_READ_LINES = 400
_MAX_READ_CHARACTERS = 50_000
_MAX_SEARCH_FILE_BYTES = 1024 * 1024
_MAX_SEARCH_RESULTS = 500
_DEFAULT_SEARCH_RESULTS = 100
_MAX_SEARCHED_FILES = 10_000
_MAX_MATCH_CHARACTERS = 300


def _string_argument(
    arguments: Mapping[str, Any], name: str, default: str | None = None
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise TypeError(f"参数 {name} 必须是字符串")
    return value


def _boolean_argument(
    arguments: Mapping[str, Any], name: str, default: bool
) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"参数 {name} 必须是布尔值")
    return value


def _integer_argument(
    arguments: Mapping[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"参数 {name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"参数 {name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _iter_files(
    workspace: Workspace,
    directory: Path,
    *,
    include_hidden: bool,
    recursive: bool = True,
) -> Iterator[Path]:
    """以确定性顺序遍历文件，不跟随符号链接。"""

    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            continue
        if not include_hidden and child.name.startswith("."):
            continue
        if child.is_dir():
            if not recursive or child.name in _IGNORED_DIRECTORIES:
                continue
            try:
                safe_directory = workspace.resolve_directory(child)
            except WorkspaceError:
                continue
            yield from _iter_files(
                workspace,
                safe_directory,
                include_hidden=include_hidden,
                recursive=True,
            )
        elif child.is_file():
            try:
                yield workspace.resolve_file(child)
            except WorkspaceError:
                continue


class ListFilesTool:
    """返回受限且确定排序的工作区文件列表。"""

    spec = ToolSpec(
        name="list_files",
        description="列出工作区目录中的文件；默认递归并排除隐藏、缓存和依赖目录。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区相对目录",
                    "default": ".",
                },
                "recursive": {"type": "boolean", "default": True},
                "include_hidden": {"type": "boolean", "default": False},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIST_RESULTS,
                    "default": _DEFAULT_LIST_RESULTS,
                },
            },
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = _string_argument(arguments, "path", ".")
        recursive = _boolean_argument(arguments, "recursive", True)
        include_hidden = _boolean_argument(arguments, "include_hidden", False)
        max_results = _integer_argument(
            arguments,
            "max_results",
            _DEFAULT_LIST_RESULTS,
            minimum=1,
            maximum=_MAX_LIST_RESULTS,
        )
        directory = self._workspace.resolve_directory(path)

        candidates = _iter_files(
            self._workspace,
            directory,
            include_hidden=include_hidden,
            recursive=recursive,
        )

        displayed: list[str] = []
        truncated = False
        for candidate in candidates:
            if len(displayed) == max_results:
                truncated = True
                break
            displayed.append(candidate.relative_to(self._workspace.root).as_posix())

        return ToolResult.success(
            "\n".join(displayed),
            metadata={"count": len(displayed), "truncated": truncated, "path": path},
        )


class ReadFileTool:
    """按行读取大小受限的 UTF-8 文本文件。"""

    spec = ToolSpec(
        name="read_file",
        description="读取工作区内的 UTF-8 文本文件，可指定从 1 开始的行号范围。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对文件路径"},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = _string_argument(arguments, "path")
        start_line = _integer_argument(
            arguments, "start_line", 1, minimum=1, maximum=2**31 - 1
        )
        end_value = arguments.get("end_line")
        has_explicit_end = end_value is not None
        if end_value is None:
            requested_end = start_line + _MAX_READ_LINES - 1
        else:
            if not isinstance(end_value, int) or isinstance(end_value, bool):
                raise TypeError("参数 end_line 必须是整数")
            if end_value < start_line:
                raise ValueError("参数 end_line 不能小于 start_line")
            requested_end = end_value

        file_path = self._workspace.resolve_file(path)
        if file_path.stat().st_size > _MAX_READ_BYTES:
            raise WorkspacePathError(f"文件超过 {_MAX_READ_BYTES} 字节读取上限：{path}")
        data = file_path.read_bytes()
        if b"\0" in data:
            raise WorkspacePathError(f"不支持读取二进制文件：{path}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspacePathError(f"文件不是有效的 UTF-8 文本：{path}") from exc

        lines = text.splitlines()
        total_lines = len(lines)
        if total_lines and start_line > total_lines:
            raise ValueError(f"start_line 超出文件总行数 {total_lines}")

        actual_end = min(requested_end, start_line + _MAX_READ_LINES - 1, total_lines)
        selected = lines[start_line - 1 : actual_end]
        output = "\n".join(
            f"{line_number:>6} | {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )
        character_truncated = len(output) > _MAX_READ_CHARACTERS
        if character_truncated:
            output = output[:_MAX_READ_CHARACTERS]
        if has_explicit_end:
            line_truncated = actual_end < min(requested_end, total_lines)
        else:
            line_truncated = actual_end < total_lines
        truncated = character_truncated or line_truncated

        return ToolResult.success(
            output,
            metadata={
                "path": file_path.relative_to(self._workspace.root).as_posix(),
                "start_line": start_line,
                "end_line": actual_end,
                "total_lines": total_lines,
                "truncated": truncated,
            },
        )


class SearchFilesTool:
    """在工作区文本文件中执行有界的字面量或正则搜索。"""

    spec = ToolSpec(
        name="search_files",
        description="在工作区内搜索文本并返回文件、行号和匹配行；默认按字面量搜索。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索文本或正则表达式"},
                "path": {
                    "type": "string",
                    "description": "工作区相对目录",
                    "default": ".",
                },
                "regex": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "include_hidden": {"type": "boolean", "default": False},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_RESULTS,
                    "default": _DEFAULT_SEARCH_RESULTS,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        query = _string_argument(arguments, "query")
        if not query:
            raise ValueError("参数 query 不能为空")
        if len(query) > 500:
            raise ValueError("参数 query 不能超过 500 个字符")
        path = _string_argument(arguments, "path", ".")
        use_regex = _boolean_argument(arguments, "regex", False)
        case_sensitive = _boolean_argument(arguments, "case_sensitive", True)
        include_hidden = _boolean_argument(arguments, "include_hidden", False)
        max_results = _integer_argument(
            arguments,
            "max_results",
            _DEFAULT_SEARCH_RESULTS,
            minimum=1,
            maximum=_MAX_SEARCH_RESULTS,
        )

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise ValueError(f"正则表达式无效：{exc}") from exc

        directory = self._workspace.resolve_directory(path)
        matches: list[str] = []
        scanned_files = 0
        skipped_files = 0
        truncated = False
        for file_path in _iter_files(
            self._workspace, directory, include_hidden=include_hidden
        ):
            if scanned_files == _MAX_SEARCHED_FILES:
                truncated = True
                break
            scanned_files += 1
            try:
                if file_path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                    skipped_files += 1
                    continue
                data = file_path.read_bytes()
                if b"\0" in data:
                    skipped_files += 1
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                skipped_files += 1
                continue

            relative = file_path.relative_to(self._workspace.root).as_posix()
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) is None:
                    continue
                if len(matches) == max_results:
                    truncated = True
                    break
                compact_line = line[:_MAX_MATCH_CHARACTERS]
                matches.append(f"{relative}:{line_number}: {compact_line}")
            if truncated:
                break

        return ToolResult.success(
            "\n".join(matches),
            metadata={
                "count": len(matches),
                "truncated": truncated,
                "scanned_files": scanned_files,
                "skipped_files": skipped_files,
                "path": path,
            },
        )
