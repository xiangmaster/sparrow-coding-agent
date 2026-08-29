"""工作区路径解析与安全边界。"""

from __future__ import annotations

from pathlib import Path
from typing import Final


class WorkspaceError(Exception):
    """工作区操作的基础异常。"""


class WorkspaceBoundaryError(WorkspaceError):
    """目标路径越出了工作区。"""


class SensitivePathError(WorkspaceError):
    """目标属于不应暴露给模型的敏感路径。"""


class WorkspacePathError(WorkspaceError):
    """目标路径不存在或类型不符合要求。"""


_BLOCKED_PARTS: Final[frozenset[str]] = frozenset(
    {".git", ".hg", ".sparrow", ".ssh", ".svn"}
)
_BLOCKED_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
        "service-account.json",
    }
)
_SAFE_ENV_TEMPLATES: Final[frozenset[str]] = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)


class Workspace:
    """将所有文件系统访问限制在一个已存在的目录中。"""

    def __init__(self, root: str | Path) -> None:
        try:
            resolved_root = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError(f"工作区不存在或无法解析：{root}") from exc
        if not resolved_root.is_dir():
            raise WorkspacePathError(f"工作区不是目录：{resolved_root}")
        self._root = resolved_root

    @property
    def root(self) -> Path:
        """工作区的规范化绝对路径。"""

        return self._root

    def resolve(self, path: str | Path, *, must_exist: bool = True) -> Path:
        """解析路径，并拒绝越界、符号链接逃逸和敏感目标。"""

        raw_path = Path(path)
        candidate = raw_path if raw_path.is_absolute() else self._root / raw_path
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise WorkspacePathError(f"路径不存在：{path}") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError(f"路径无法解析：{path}") from exc

        if not resolved.is_relative_to(self._root):
            raise WorkspaceBoundaryError(f"路径越出工作区：{path}")

        relative = resolved.relative_to(self._root)
        self._reject_sensitive(relative)
        return resolved

    def resolve_file(self, path: str | Path) -> Path:
        """解析一个必须存在的普通文件。"""

        resolved = self.resolve(path)
        if not resolved.is_file():
            raise WorkspacePathError(f"目标不是普通文件：{path}")
        return resolved

    def resolve_directory(self, path: str | Path = ".") -> Path:
        """解析一个必须存在的目录，可用于限制命令工作目录。"""

        resolved = self.resolve(path)
        if not resolved.is_dir():
            raise WorkspacePathError(f"目标不是目录：{path}")
        return resolved

    def resolve_for_write(self, path: str | Path) -> Path:
        """解析允许尚不存在的写入目标，并要求其父目录已存在。"""

        resolved = self.resolve(path, must_exist=False)
        parent = resolved.parent
        if not parent.exists():
            raise WorkspacePathError(f"目标父目录不存在：{path}")
        if not parent.is_dir():
            raise WorkspacePathError(f"目标父路径不是目录：{path}")
        if resolved.exists() and not resolved.is_file():
            raise WorkspacePathError(f"写入目标不是普通文件：{path}")
        return resolved

    def relative_path(self, path: str | Path) -> Path:
        """返回安全目标相对于工作区的路径。"""

        return self.resolve(path).relative_to(self._root)

    @staticmethod
    def _reject_sensitive(relative: Path) -> None:
        parts = relative.parts
        if any(part in _BLOCKED_PARTS for part in parts):
            raise SensitivePathError(f"禁止访问敏感目录：{relative}")
        if not parts:
            return

        filename = parts[-1]
        if filename in _BLOCKED_FILENAMES:
            raise SensitivePathError(f"禁止访问凭据文件：{relative}")
        if filename == ".env" or (
            filename.startswith(".env.") and filename not in _SAFE_ENV_TEMPLATES
        ):
            raise SensitivePathError(f"禁止访问环境变量文件：{relative}")
