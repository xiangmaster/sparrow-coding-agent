"""不经过 Shell 的受限本地命令执行工具。"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from sparrow_agent.models import ToolResult
from sparrow_agent.tools.base import ToolSpec
from sparrow_agent.workspace import Workspace, WorkspacePathError

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 60.0
_DEFAULT_OUTPUT_BYTES = 30_000
_MAX_OUTPUT_BYTES = 100_000
_MAX_ARGUMENTS = 64
_MAX_ARGUMENT_CHARACTERS = 32_768
_BLOCKED_EXECUTABLES = frozenset(
    {
        "bash",
        "chmod",
        "chown",
        "curl",
        "dd",
        "fish",
        "kill",
        "mkfs",
        "mount",
        "mv",
        "nc",
        "netcat",
        "pkill",
        "powershell",
        "reboot",
        "rm",
        "rmdir",
        "scp",
        "sh",
        "shutdown",
        "ssh",
        "su",
        "sudo",
        "umount",
        "wget",
        "zsh",
    }
)
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
)
_INLINE_CODE_FLAGS = {
    "node": frozenset({"-e", "--eval"}),
    "perl": frozenset({"-e"}),
    "ruby": frozenset({"-e"}),
}


class RunCommandTool:
    """在工作区内运行参数数组命令，并限制权限、时间和输出。"""

    spec = ToolSpec(
        name="run_command",
        description=(
            "在工作区内执行不经过 Shell 的命令参数数组，适合运行测试、静态检查和构建。"
            "危险命令、内联代码和写入型 Git 子命令会被拒绝。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "可执行文件及其参数，不是 Shell 字符串",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作区相对工作目录",
                    "default": ".",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": _MAX_TIMEOUT_SECONDS,
                    "default": _DEFAULT_TIMEOUT_SECONDS,
                },
                "max_output_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_OUTPUT_BYTES,
                    "default": _DEFAULT_OUTPUT_BYTES,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        command = _validate_command(arguments.get("command"))
        cwd_value = arguments.get("cwd", ".")
        if not isinstance(cwd_value, str):
            raise TypeError("参数 cwd 必须是字符串")
        cwd = self._workspace.resolve_directory(cwd_value)
        timeout = _number_argument(
            arguments,
            "timeout_seconds",
            _DEFAULT_TIMEOUT_SECONDS,
            minimum=0.1,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        max_output = _integer_argument(
            arguments,
            "max_output_bytes",
            _DEFAULT_OUTPUT_BYTES,
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
        )
        command = self._resolve_executable(command)
        _enforce_command_policy(command)

        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        with tempfile.TemporaryFile(mode="w+b") as captured:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=_sanitized_environment(self._workspace.root),
                stdin=subprocess.DEVNULL,
                stdout=captured,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

            captured.flush()
            output_size = captured.tell()
            captured.seek(0)
            output = captured.read(max_output).decode("utf-8", errors="replace")

        duration_ms = round((time.monotonic() - started) * 1000)
        metadata = {
            "command": command,
            "cwd": cwd.relative_to(self._workspace.root).as_posix(),
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_truncated": output_size > max_output,
            "output_bytes": output_size,
        }
        if timed_out:
            return ToolResult.failure(
                f"命令执行超过 {timeout:g} 秒，已终止进程组",
                output=output,
                metadata=metadata,
            )
        if exit_code != 0:
            return ToolResult.failure(
                f"命令以退出码 {exit_code} 结束",
                output=output,
                metadata=metadata,
            )
        return ToolResult.success(output, metadata=metadata)

    def _resolve_executable(self, command: list[str]) -> list[str]:
        executable = Path(command[0])
        if executable.is_absolute() or len(executable.parts) > 1:
            resolved = self._workspace.resolve_file(executable)
            if not os.access(resolved, os.X_OK):
                raise WorkspacePathError(f"命令文件不可执行：{command[0]}")
            command[0] = str(resolved)
        return command


def _validate_command(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("参数 command 必须是字符串数组")
    if not value:
        raise ValueError("参数 command 不能为空")
    if len(value) > _MAX_ARGUMENTS:
        raise ValueError(f"命令参数不能超过 {_MAX_ARGUMENTS} 个")
    if any(not isinstance(part, str) for part in value):
        raise TypeError("参数 command 的每一项都必须是字符串")
    if any(not part or "\0" in part for part in value):
        raise ValueError("命令参数不能为空且不能包含空字节")
    if sum(len(part) for part in value) > _MAX_ARGUMENT_CHARACTERS:
        raise ValueError(f"命令参数总长度不能超过 {_MAX_ARGUMENT_CHARACTERS} 个字符")
    return list(value)


def _enforce_command_policy(command: list[str]) -> None:
    executable = Path(command[0]).name.lower()
    if executable in _BLOCKED_EXECUTABLES:
        raise ValueError(f"命令被安全策略禁止：{executable}")
    if executable == "git":
        if len(command) < 2 or command[1] not in _READ_ONLY_GIT_SUBCOMMANDS:
            raise ValueError("仅允许只读 Git 子命令")
    inline_flags = (
        frozenset({"-c"})
        if executable.startswith("python")
        else _INLINE_CODE_FLAGS.get(executable)
    )
    if inline_flags and len(command) > 1 and command[1] in inline_flags:
        raise ValueError(f"禁止通过 {executable} 执行内联代码")


def _number_argument(
    arguments: Mapping[str, Any],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = arguments.get(name, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"参数 {name} 必须是数字")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"参数 {name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return number


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


def _sanitized_environment(workspace_root: Path) -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    virtual_environment_bin = workspace_root / ".venv" / "bin"
    if virtual_environment_bin.is_dir():
        inherited_path = environment.get("PATH", "")
        environment["PATH"] = f"{virtual_environment_bin}{os.pathsep}{inherited_path}"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment
