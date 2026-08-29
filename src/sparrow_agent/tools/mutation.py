"""经过完整预验证的统一差异补丁工具。"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sparrow_agent.models import ToolResult
from sparrow_agent.tools.base import ToolSpec
from sparrow_agent.workspace import Workspace, WorkspacePathError

_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)(?:\r?\n)?$"
)
_MAX_PATCH_CHARACTERS = 512_000
_MAX_PATCHED_FILE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FilePatch:
    old_path: str | None
    new_path: str
    hunks: tuple[_Hunk, ...]


@dataclass(frozen=True, slots=True)
class _PreparedWrite:
    path: Path
    content: bytes
    mode: int


class ApplyPatchTool:
    """先验证整份统一差异，再写入所有目标。"""

    spec = ToolSpec(
        name="apply_patch",
        description=(
            "应用标准 unified diff 补丁。支持修改和新增 UTF-8 文本文件；"
            "整份补丁验证通过后才开始写入。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "以 ---、+++ 和 @@ 行组成的 unified diff",
                }
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        patch_text = arguments.get("patch")
        if not isinstance(patch_text, str):
            raise TypeError("参数 patch 必须是字符串")
        if not patch_text.strip():
            raise ValueError("参数 patch 不能为空")
        if len(patch_text) > _MAX_PATCH_CHARACTERS:
            raise ValueError(f"补丁不能超过 {_MAX_PATCH_CHARACTERS} 个字符")

        file_patches = _parse_unified_diff(patch_text)
        prepared = self._prepare_all(file_patches)
        self._write_all(prepared)
        changed_files = tuple(
            item.path.relative_to(self._workspace.root).as_posix() for item in prepared
        )
        return ToolResult.success(
            "已应用补丁：\n" + "\n".join(changed_files),
            metadata={
                "changed_files": changed_files,
                "file_count": len(changed_files),
            },
        )

    def _prepare_all(self, file_patches: tuple[_FilePatch, ...]) -> list[_PreparedWrite]:
        prepared: list[_PreparedWrite] = []
        seen_targets: set[Path] = set()
        for file_patch in file_patches:
            if file_patch.old_path is None:
                target = self._workspace.resolve_for_write(file_patch.new_path)
                if target.exists():
                    raise WorkspacePathError(f"新增目标已经存在：{file_patch.new_path}")
                source = ""
                mode = 0o644
            else:
                old_target = self._workspace.resolve_file(file_patch.old_path)
                new_target = self._workspace.resolve_for_write(file_patch.new_path)
                if old_target != new_target:
                    raise ValueError("首版 apply_patch 不支持重命名文件")
                target = old_target
                if target.stat().st_size > _MAX_PATCHED_FILE_BYTES:
                    raise WorkspacePathError(
                        f"文件超过 {_MAX_PATCHED_FILE_BYTES} 字节补丁上限：{file_patch.old_path}"
                    )
                data = target.read_bytes()
                if b"\0" in data:
                    raise WorkspacePathError(f"不支持修改二进制文件：{file_patch.old_path}")
                try:
                    source = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WorkspacePathError(
                        f"文件不是有效的 UTF-8 文本：{file_patch.old_path}"
                    ) from exc
                mode = target.stat().st_mode & 0o777

            if target in seen_targets:
                raise ValueError(f"补丁包含重复目标：{file_patch.new_path}")
            seen_targets.add(target)
            updated = _apply_hunks(source, file_patch)
            encoded = updated.encode("utf-8")
            if len(encoded) > _MAX_PATCHED_FILE_BYTES:
                raise WorkspacePathError(
                    f"补丁结果超过 {_MAX_PATCHED_FILE_BYTES} 字节上限：{file_patch.new_path}"
                )
            prepared.append(_PreparedWrite(target, encoded, mode))
        return prepared

    @staticmethod
    def _write_all(prepared: list[_PreparedWrite]) -> None:
        staged: list[tuple[Path, Path]] = []
        try:
            for item in prepared:
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=item.path.parent, prefix=".sparrow-patch-", suffix=".tmp"
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(item.content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, item.mode)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                staged.append((temporary, item.path))

            for temporary, target in staged:
                os.replace(temporary, target)
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)


def _parse_unified_diff(patch_text: str) -> tuple[_FilePatch, ...]:
    lines = patch_text.splitlines(keepends=True)
    patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            if lines[index].startswith(("diff ", "index ")) or not lines[index].strip():
                index += 1
                continue
            raise ValueError(f"补丁第 {index + 1} 行不是有效的文件头")

        old_path = _parse_header_path(lines[index], "--- ")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("补丁缺少 +++ 文件头")
        new_path = _parse_header_path(lines[index], "+++ ")
        index += 1
        if new_path is None:
            raise ValueError("首版 apply_patch 不支持删除文件")
        if old_path is None and new_path is None:
            raise ValueError("补丁文件路径无效")

        hunks: list[_Hunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if lines[index].startswith(("diff ", "index ")) or not lines[index].strip():
                index += 1
                continue
            match = _HUNK_HEADER.match(lines[index])
            if match is None:
                raise ValueError(f"补丁第 {index + 1} 行不是有效的 @@ 分块头")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith(("@@ ", "--- ", "diff ", "index ")):
                    break
                if line.startswith("\\ No newline at end of file"):
                    if not hunk_lines:
                        raise ValueError("无换行标记前缺少补丁内容")
                    hunk_lines[-1] = hunk_lines[-1].rstrip("\r\n")
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise ValueError(f"补丁第 {index + 1} 行缺少操作前缀")
                hunk_lines.append(line)
                index += 1

            actual_old = sum(line[0] in " -" for line in hunk_lines)
            actual_new = sum(line[0] in " +" for line in hunk_lines)
            if actual_old != old_count or actual_new != new_count:
                raise ValueError("补丁分块声明的行数与实际内容不一致")
            hunks.append(_Hunk(old_start, old_count, new_start, new_count, tuple(hunk_lines)))

        if not hunks:
            raise ValueError("每个文件补丁至少需要一个 @@ 分块")
        if not any(line[0] in "+-" for hunk in hunks for line in hunk.lines):
            raise ValueError("文件补丁不包含实际修改")
        patches.append(_FilePatch(old_path, new_path, tuple(hunks)))

    if not patches:
        raise ValueError("补丁中没有文件变更")
    return tuple(patches)


def _parse_header_path(line: str, prefix: str) -> str | None:
    value = line[len(prefix) :].rstrip("\r\n").split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise ValueError("补丁文件路径不能为空")
    return value


def _apply_hunks(source: str, file_patch: _FilePatch) -> str:
    source_lines = source.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in file_patch.hunks:
        target_index = 0 if hunk.old_start == 0 else hunk.old_start - 1
        if target_index < cursor or target_index > len(source_lines):
            raise ValueError(f"补丁分块位置无效：{file_patch.new_path}")
        output.extend(source_lines[cursor:target_index])
        cursor = target_index
        expected_new_index = 0 if hunk.new_start == 0 else hunk.new_start - 1
        if expected_new_index != len(output):
            raise ValueError(f"补丁新文件行号无效：{file_patch.new_path}")

        for patch_line in hunk.lines:
            operation = patch_line[0]
            content = patch_line[1:]
            if operation in " -":
                if cursor >= len(source_lines) or source_lines[cursor] != content:
                    raise ValueError(f"补丁上下文与当前文件不匹配：{file_patch.new_path}")
                if operation == " ":
                    output.append(source_lines[cursor])
                cursor += 1
            else:
                output.append(content)
    output.extend(source_lines[cursor:])
    return "".join(output)
