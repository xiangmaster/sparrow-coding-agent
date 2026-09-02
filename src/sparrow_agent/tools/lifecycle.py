"""受工作区边界保护的目录创建、文件创建、重命名与删除工具。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from sparrow_agent.models import ToolResult
from sparrow_agent.tools.base import ToolSpec
from sparrow_agent.workspace import Workspace, WorkspacePathError

_MAX_CREATED_FILE_BYTES = 1024 * 1024


def _path_argument(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise TypeError(f"参数 {name} 必须是字符串")
    if not value.strip():
        raise ValueError(f"参数 {name} 不能为空")
    return value


class CreateDirectoryTool:
    """创建后续新增文件所需的工作区目录。"""

    spec = ToolSpec(
        name="create_directory",
        description=(
            "在工作区内创建目录，默认同时创建缺失的父目录。"
            "适合在 apply_patch 新增文件前准备目录；不会覆盖已有文件或目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对目录路径"},
                "parents": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否同时创建缺失的父目录",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = _path_argument(arguments, "path")
        parents = arguments.get("parents", True)
        if not isinstance(parents, bool):
            raise TypeError("参数 parents 必须是布尔值")
        target = self._workspace.resolve(path, must_exist=False)
        if target.exists():
            kind = "目录" if target.is_dir() else "文件"
            raise WorkspacePathError(f"创建目标已经是{kind}：{path}")
        if not parents and not target.parent.is_dir():
            raise WorkspacePathError(f"目标父目录不存在：{path}")

        target.mkdir(parents=parents, exist_ok=False)
        relative = target.relative_to(self._workspace.root).as_posix()
        return ToolResult.success(
            f"已创建目录 {relative}",
            metadata={"created_directories": (relative,)},
        )


class CreateFileTool:
    """以不覆盖目标的方式创建一个 UTF-8 文本文件。"""

    spec = ToolSpec(
        name="create_file",
        description=(
            "在工作区内新建 UTF-8 文本文件并一次写入完整内容，适合新增代码、测试或"
            "文档。目标父目录必须存在，目标文件必须不存在；绝不覆盖已有文件。"
            "新增文件优先使用本工具，无需手写 unified diff 行号，也不要先运行 touch。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对文件路径"},
                "content": {
                    "type": "string",
                    "description": "要写入文件的完整 UTF-8 文本内容",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = _path_argument(arguments, "path")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise TypeError("参数 content 必须是字符串")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_CREATED_FILE_BYTES:
            raise WorkspacePathError(
                f"文件内容超过 {_MAX_CREATED_FILE_BYTES} 字节创建上限：{path}"
            )

        target = self._workspace.resolve_for_write(path)
        if target.exists():
            raise WorkspacePathError(f"创建目标已经存在：{path}")

        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise WorkspacePathError(f"创建目标已经存在：{path}") from exc
        except OSError as exc:
            if created:
                target.unlink(missing_ok=True)
            raise WorkspacePathError(f"无法安全创建文件：{path}：{exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        relative = target.relative_to(self._workspace.root).as_posix()
        return ToolResult.success(
            f"已创建文件 {relative}",
            metadata={
                "changed_files": (relative,),
                "created_file": relative,
                "result_bytes": len(encoded),
            },
        )


class RenameFileTool:
    """以不覆盖目标的方式重命名一个普通文件。"""

    spec = ToolSpec(
        name="rename_file",
        description=(
            "在工作区内重命名普通文件。目标父目录必须存在，目标路径必须不存在；"
            "操作不会覆盖任何现有文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "现有工作区相对文件路径"},
                "destination": {
                    "type": "string",
                    "description": "新的工作区相对文件路径",
                },
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        source_value = _path_argument(arguments, "source")
        destination_value = _path_argument(arguments, "destination")
        source = self._workspace.resolve_file(source_value)
        destination = self._workspace.resolve_for_write(destination_value)
        if source == destination:
            raise ValueError("源文件与目标文件相同，不会产生修改")
        if destination.exists():
            raise WorkspacePathError(f"重命名目标已经存在：{destination_value}")

        _rename_without_overwrite(source, destination)
        source_relative = source.relative_to(self._workspace.root).as_posix()
        destination_relative = destination.relative_to(self._workspace.root).as_posix()
        return ToolResult.success(
            f"已将 {source_relative} 重命名为 {destination_relative}",
            metadata={
                "changed_files": (source_relative, destination_relative),
                "renamed_from": source_relative,
                "renamed_to": destination_relative,
            },
        )


class DeleteFileTool:
    """永久删除一个受工作区边界保护的普通文件。"""

    spec = ToolSpec(
        name="delete_file",
        description=(
            "永久删除工作区内的一个普通文件。不会删除目录，也不能访问凭据或版本控制"
            "内部路径；仅在任务明确需要删除文件时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的工作区相对文件路径"}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = _path_argument(arguments, "path")
        target = self._workspace.resolve_file(path)
        relative = target.relative_to(self._workspace.root).as_posix()
        target.unlink()
        return ToolResult.success(
            f"已删除文件 {relative}",
            metadata={"changed_files": (relative,), "deleted_file": relative},
        )


def _rename_without_overwrite(source: Path, destination: Path) -> None:
    """用独占硬链接加删除实现不覆盖目标的同文件系统重命名。"""

    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise WorkspacePathError(f"重命名目标已经存在：{destination}") from exc
    except OSError as exc:
        raise WorkspacePathError(f"无法安全重命名文件：{exc}") from exc
    try:
        source.unlink()
    except OSError as exc:
        try:
            destination.unlink()
        except OSError:
            pass
        raise WorkspacePathError(f"无法删除重命名前的源路径：{source}") from exc
