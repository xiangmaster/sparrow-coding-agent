"""工作区安全边界的离线测试。"""

from pathlib import Path

import pytest

from sparrow_agent.workspace import (
    SensitivePathError,
    Workspace,
    WorkspaceBoundaryError,
    WorkspacePathError,
)


def test_workspace_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError, match="不存在"):
        Workspace(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("内容", encoding="utf-8")
    with pytest.raises(WorkspacePathError, match="不是目录"):
        Workspace(file_path)


def test_resolve_accepts_paths_inside_workspace(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("print('ok')", encoding="utf-8")
    workspace = Workspace(tmp_path)

    assert workspace.resolve_file("src/main.py") == source
    assert workspace.relative_path(source) == Path("src/main.py")
    assert workspace.resolve_directory("src") == source.parent


@pytest.mark.parametrize("candidate", ["../outside.txt", "../../tmp/outside.txt"])
def test_resolve_rejects_parent_traversal(tmp_path: Path, candidate: str) -> None:
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceBoundaryError, match="越出工作区"):
        workspace.resolve(candidate, must_exist=False)


def test_resolve_rejects_absolute_path_outside_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceBoundaryError, match="越出工作区"):
        Workspace(workspace_root).resolve_file(outside)


def test_resolve_rejects_symlink_to_file_outside_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace_root / "link.txt").symlink_to(outside)

    with pytest.raises(WorkspaceBoundaryError, match="越出工作区"):
        Workspace(workspace_root).resolve_file("link.txt")


def test_write_rejects_escape_through_symlinked_directory(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    outside_directory = tmp_path / "outside"
    workspace_root.mkdir()
    outside_directory.mkdir()
    (workspace_root / "linked-dir").symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(WorkspaceBoundaryError, match="越出工作区"):
        Workspace(workspace_root).resolve_for_write("linked-dir/new.txt")


def test_resolve_for_write_allows_new_file_under_existing_directory(
    tmp_path: Path,
) -> None:
    target_directory = tmp_path / "src"
    target_directory.mkdir()

    resolved = Workspace(tmp_path).resolve_for_write("src/new.py")

    assert resolved == target_directory / "new.py"
    assert not resolved.exists()


def test_resolve_for_write_requires_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError, match="父目录不存在"):
        Workspace(tmp_path).resolve_for_write("missing/new.py")


def test_resolve_for_write_rejects_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "target").mkdir()

    with pytest.raises(WorkspacePathError, match="不是普通文件"):
        Workspace(tmp_path).resolve_for_write("target")


def test_resolve_converts_invalid_path_to_workspace_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError, match="无法解析"):
        Workspace(tmp_path).resolve("invalid\0path", must_exist=False)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".git/config",
        "config/credentials.json",
        "keys/id_rsa",
    ],
)
def test_resolve_rejects_sensitive_paths(tmp_path: Path, path: str) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")

    with pytest.raises(SensitivePathError, match="禁止访问"):
        Workspace(tmp_path).resolve_file(path)


@pytest.mark.parametrize("filename", [".env.example", ".env.sample", ".env.template"])
def test_resolve_allows_safe_environment_templates(
    tmp_path: Path, filename: str
) -> None:
    target = tmp_path / filename
    target.write_text("API_KEY=replace-me", encoding="utf-8")

    assert Workspace(tmp_path).resolve_file(filename) == target


def test_workspace_snapshot_detects_content_mode_addition_and_deletion(
    tmp_path: Path,
) -> None:
    target = tmp_path / "main.py"
    removed = tmp_path / "old.txt"
    target.write_text("value = 1\n", encoding="utf-8")
    removed.write_text("old\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    before = workspace.snapshot()

    target.write_text("value = 2\n", encoding="utf-8")
    target.chmod(0o744)
    removed.unlink()
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    after = workspace.snapshot()

    assert before.changed_paths(after) == {"main.py", "old.txt", "new.txt"}


def test_workspace_snapshot_excludes_sensitive_dependencies_caches_and_links(
    tmp_path: Path,
) -> None:
    (tmp_path / "visible.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".coverage").write_text("noise\n", encoding="utf-8")
    for directory in (
        ".git",
        ".sparrow",
        ".venv",
        "node_modules",
        "__pycache__",
        "htmlcov",
    ):
        path = tmp_path / directory
        path.mkdir()
        (path / "ignored.txt").write_text("noise\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    snapshot = Workspace(tmp_path).snapshot()

    assert snapshot.files == {"visible.py"}
