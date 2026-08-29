"""补丁与受限命令工具的离线测试。"""

from pathlib import Path

from sparrow_agent.models import ToolCall
from sparrow_agent.tools import ApplyPatchTool, RunCommandTool, ToolRegistry
from sparrow_agent.workspace import Workspace


def _registry(tmp_path: Path) -> ToolRegistry:
    workspace = Workspace(tmp_path)
    return ToolRegistry([ApplyPatchTool(workspace), RunCommandTool(workspace)])


def test_apply_patch_modifies_existing_file_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("first\nold\nlast\n", encoding="utf-8")
    target.chmod(0o744)
    patch = """--- a/main.py
+++ b/main.py
@@ -1,3 +1,3 @@
 first
-old
+new
 last
"""

    result = _registry(tmp_path).execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": patch})
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "first\nnew\nlast\n"
    assert target.stat().st_mode & 0o777 == 0o744
    assert result.metadata["changed_files"] == ("main.py",)


def test_apply_patch_creates_new_file(tmp_path: Path) -> None:
    patch = """--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
"""

    result = _registry(tmp_path).execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": patch})
    )

    assert result.ok is True
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\nworld\n"


def test_apply_patch_validates_all_files_before_writing(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old one\n", encoding="utf-8")
    second.write_text("actual two\n", encoding="utf-8")
    patch = """--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-old one
+new one
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-wrong context
+new two
"""

    result = _registry(tmp_path).execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": patch})
    )

    assert result.ok is False
    assert "上下文与当前文件不匹配" in result.error
    assert first.read_text(encoding="utf-8") == "old one\n"
    assert second.read_text(encoding="utf-8") == "actual two\n"


def test_apply_patch_rejects_escape_deletion_and_duplicate_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep\n", encoding="utf-8")
    registry = _registry(tmp_path)
    escape = """--- /dev/null
+++ b/../outside.txt
@@ -0,0 +1 @@
+escape
"""
    deletion = """--- a/keep.txt
+++ /dev/null
@@ -1 +0,0 @@
-keep
"""
    duplicate = """--- a/keep.txt
+++ b/keep.txt
@@ -1 +1 @@
-keep
+first
--- a/keep.txt
+++ b/keep.txt
@@ -1 +1 @@
-keep
+second
"""

    escape_result = registry.execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": escape})
    )
    deletion_result = registry.execute(
        ToolCall(id="2", name="apply_patch", arguments={"patch": deletion})
    )
    duplicate_result = registry.execute(
        ToolCall(id="3", name="apply_patch", arguments={"patch": duplicate})
    )

    assert escape_result.ok is False and "越出工作区" in escape_result.error
    assert deletion_result.ok is False and "不支持删除" in deletion_result.error
    assert duplicate_result.ok is False and "重复目标" in duplicate_result.error
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_apply_patch_handles_file_without_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    patch = """--- a/note.txt
+++ b/note.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

    result = _registry(tmp_path).execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": patch})
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new"


def test_apply_patch_rejects_noop_and_inconsistent_new_line_number(
    tmp_path: Path,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old\n", encoding="utf-8")
    registry = _registry(tmp_path)
    noop = """--- a/note.txt
+++ b/note.txt
@@ -1 +1 @@
 old
"""
    inconsistent = """--- a/note.txt
+++ b/note.txt
@@ -1 +2 @@
-old
+new
"""

    noop_result = registry.execute(
        ToolCall(id="1", name="apply_patch", arguments={"patch": noop})
    )
    inconsistent_result = registry.execute(
        ToolCall(id="2", name="apply_patch", arguments={"patch": inconsistent})
    )

    assert noop_result.ok is False and "不包含实际修改" in noop_result.error
    assert inconsistent_result.ok is False and "新文件行号无效" in inconsistent_result.error
    assert target.read_text(encoding="utf-8") == "old\n"


def test_run_command_captures_success_and_uses_requested_cwd(tmp_path: Path) -> None:
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    script = subdirectory / "check.py"
    script.write_text("from pathlib import Path\nprint(Path.cwd().name)\n", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": ["python3", "check.py"], "cwd": "sub"},
        )
    )

    assert result.ok is True
    assert result.output == "sub\n"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["cwd"] == "sub"


def test_run_command_returns_nonzero_exit_as_structured_failure(tmp_path: Path) -> None:
    (tmp_path / "fail.py").write_text(
        "import sys\nprint('failed')\nsys.exit(7)\n", encoding="utf-8"
    )

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": ["python3", "fail.py"]},
        )
    )

    assert result.ok is False
    assert result.error == "命令以退出码 7 结束"
    assert result.output == "failed\n"
    assert result.metadata["exit_code"] == 7


def test_run_command_terminates_timed_out_process_group(tmp_path: Path) -> None:
    (tmp_path / "slow.py").write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(5)\n", encoding="utf-8"
    )

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={
                "command": ["python3", "slow.py"],
                "timeout_seconds": 0.1,
            },
        )
    )

    assert result.ok is False
    assert "已终止进程组" in result.error
    assert result.output == "started\n"
    assert result.metadata["timed_out"] is True
    assert result.metadata["exit_code"] is None


def test_run_command_truncates_output_without_buffering_it_all(tmp_path: Path) -> None:
    (tmp_path / "verbose.py").write_text("print('x' * 1000)\n", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={
                "command": ["python3", "verbose.py"],
                "max_output_bytes": 20,
            },
        )
    )

    assert result.ok is True
    assert result.output == "x" * 20
    assert result.metadata["output_truncated"] is True
    assert result.metadata["output_bytes"] == 1001


def test_run_command_blocks_dangerous_shell_inline_and_git_writes(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    calls = [
        ["rm", "-rf", "target"],
        ["sh", "-c", "echo unsafe"],
        ["python3.13", "-c", "print('unsafe')"],
        ["git", "reset", "--hard"],
    ]

    results = [
        registry.execute(
            ToolCall(id=str(index), name="run_command", arguments={"command": command})
        )
        for index, command in enumerate(calls)
    ]

    assert all(result.ok is False for result in results)
    assert "安全策略禁止" in results[0].error
    assert "安全策略禁止" in results[1].error
    assert "内联代码" in results[2].error
    assert "只读 Git" in results[3].error


def test_run_command_rejects_external_executable_and_cwd(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    registry = _registry(workspace_root)

    executable_result = registry.execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": ["/bin/echo", "hello"]},
        )
    )
    cwd_result = registry.execute(
        ToolCall(
            id="2",
            name="run_command",
            arguments={"command": ["python3", "test.py"], "cwd": ".."},
        )
    )

    assert executable_result.ok is False and "越出工作区" in executable_result.error
    assert cwd_result.ok is False and "越出工作区" in cwd_result.error


def test_run_command_does_not_forward_api_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "should-not-leak")
    (tmp_path / "environment.py").write_text(
        "import os\nprint(os.getenv('DEEPSEEK_API_KEY', 'missing'))\n", encoding="utf-8"
    )

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="run_command",
            arguments={"command": ["python3", "environment.py"]},
        )
    )

    assert result.ok is True
    assert result.output == "missing\n"
    assert "should-not-leak" not in result.output
