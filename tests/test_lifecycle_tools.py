"""目录创建、文件重命名与删除工具的离线测试。"""

from pathlib import Path

import pytest

import sparrow_agent.tools.lifecycle as lifecycle
from sparrow_agent.models import ToolCall
from sparrow_agent.tools import (
    ApplyPatchTool,
    CreateDirectoryTool,
    DeleteFileTool,
    RenameFileTool,
    ToolRegistry,
)
from sparrow_agent.workspace import Workspace


def _registry(tmp_path: Path) -> ToolRegistry:
    workspace = Workspace(tmp_path)
    return ToolRegistry(
        [
            CreateDirectoryTool(workspace),
            ApplyPatchTool(workspace),
            RenameFileTool(workspace),
            DeleteFileTool(workspace),
        ]
    )


def test_create_directory_prepares_nested_parent_for_new_file(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    created = registry.execute(
        ToolCall(id="mkdir", name="create_directory", arguments={"path": "src/pkg"})
    )
    patch = """--- /dev/null
+++ b/src/pkg/__init__.py
@@ -0,0 +1 @@
+VALUE = 1
"""
    written = registry.execute(
        ToolCall(id="write", name="apply_patch", arguments={"patch": patch})
    )

    assert created.ok is True
    assert created.metadata["created_directories"] == ("src/pkg",)
    assert written.ok is True
    assert (tmp_path / "src" / "pkg" / "__init__.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "one/two", "parents": False}, "父目录不存在"),
        ({"path": ".git/hooks"}, "禁止访问"),
        ({"path": "", "parents": True}, "不能为空"),
        ({"path": "new", "parents": 1}, "布尔值"),
    ],
)
def test_create_directory_rejects_invalid_or_sensitive_target(
    tmp_path: Path, arguments: dict, message: str
) -> None:
    result = _registry(tmp_path).execute(
        ToolCall(id="mkdir", name="create_directory", arguments=arguments)
    )

    assert result.ok is False
    assert message in result.error


def test_create_directory_rejects_existing_file_and_directory(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    registry = _registry(tmp_path)

    file_result = registry.execute(
        ToolCall(id="file", name="create_directory", arguments={"path": "file.txt"})
    )
    directory_result = registry.execute(
        ToolCall(
            id="directory",
            name="create_directory",
            arguments={"path": "directory"},
        )
    )

    assert file_result.ok is False and "已经是文件" in file_result.error
    assert directory_result.ok is False and "已经是目录" in directory_result.error


def test_rename_file_preserves_content_mode_and_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "old.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    source.chmod(0o744)
    source_inode = source.stat().st_ino
    registry = _registry(tmp_path)

    result = registry.execute(
        ToolCall(
            id="rename",
            name="rename_file",
            arguments={"source": "old.py", "destination": "new.py"},
        )
    )

    target = tmp_path / "new.py"
    assert result.ok is True
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "print('ok')\n"
    assert target.stat().st_ino == source_inode
    assert target.stat().st_mode & 0o777 == 0o744
    assert result.metadata["changed_files"] == ("old.py", "new.py")

    (tmp_path / "occupied.py").write_text("keep\n", encoding="utf-8")
    overwrite = registry.execute(
        ToolCall(
            id="overwrite",
            name="rename_file",
            arguments={"source": "new.py", "destination": "occupied.py"},
        )
    )
    assert overwrite.ok is False and "目标已经存在" in overwrite.error
    assert (tmp_path / "occupied.py").read_text(encoding="utf-8") == "keep\n"
    assert target.exists()


@pytest.mark.parametrize(
    ("source", "destination", "message"),
    [
        ("same.txt", "same.txt", "相同"),
        ("same.txt", "missing/new.txt", "父目录不存在"),
        (".env", "safe.txt", "禁止访问"),
        ("same.txt", "../outside.txt", "越出工作区"),
    ],
)
def test_rename_file_rejects_invalid_paths(
    tmp_path: Path, source: str, destination: str, message: str
) -> None:
    (tmp_path / "same.txt").write_text("same\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="rename",
            name="rename_file",
            arguments={"source": source, "destination": destination},
        )
    )

    assert result.ok is False
    assert message in result.error


def test_delete_file_removes_only_regular_non_sensitive_file(tmp_path: Path) -> None:
    target = tmp_path / "obsolete.txt"
    target.write_text("remove me\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    registry = _registry(tmp_path)

    deleted = registry.execute(
        ToolCall(id="delete", name="delete_file", arguments={"path": "obsolete.txt"})
    )
    directory = registry.execute(
        ToolCall(id="directory", name="delete_file", arguments={"path": "directory"})
    )
    sensitive = registry.execute(
        ToolCall(id="secret", name="delete_file", arguments={"path": ".env"})
    )

    assert deleted.ok is True
    assert deleted.metadata["changed_files"] == ("obsolete.txt",)
    assert not target.exists()
    assert directory.ok is False and "不是普通文件" in directory.error
    assert sensitive.ok is False and "禁止访问" in sensitive.error


def test_lifecycle_tools_reject_non_string_path(tmp_path: Path) -> None:
    result = _registry(tmp_path).execute(
        ToolCall(id="mkdir", name="create_directory", arguments={"path": 1})
    )

    assert result.ok is False
    assert "必须是字符串" in result.error


def test_rename_helper_handles_target_race_and_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source\n", encoding="utf-8")
    destination.write_text("destination\n", encoding="utf-8")

    with pytest.raises(lifecycle.WorkspacePathError, match="目标已经存在"):
        lifecycle._rename_without_overwrite(source, destination)

    destination.unlink()

    def fail_link(*args, **kwargs):
        raise OSError("link unavailable")

    monkeypatch.setattr(lifecycle.os, "link", fail_link)
    with pytest.raises(lifecycle.WorkspacePathError, match="无法安全重命名"):
        lifecycle._rename_without_overwrite(source, destination)

    assert source.exists()
    assert not destination.exists()


def test_rename_helper_rolls_back_destination_when_source_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_source_unlink(path: Path, *args, **kwargs):
        if path == source:
            raise OSError("source busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)

    with pytest.raises(lifecycle.WorkspacePathError, match="无法删除重命名前"):
        lifecycle._rename_without_overwrite(source, destination)

    assert source.exists()
    assert not destination.exists()
