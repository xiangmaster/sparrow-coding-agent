"""真实修改前后差异捕获测试。"""

from pathlib import Path

from sparrow_agent.change_review import capture_change
from sparrow_agent.models import ToolCall, ToolResult
from sparrow_agent.workspace import Workspace


def test_capture_replace_text_uses_real_file_before_and_after(tmp_path: Path) -> None:
    target = tmp_path / "price.py"
    target.write_text("if total > 99:\n    return 0\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    capture = capture_change(
        workspace,
        ToolCall(
            id="replace",
            name="replace_text",
            arguments={"path": "price.py", "old_text": ">", "new_text": ">="},
        ),
    )
    assert capture is not None

    target.write_text("if total >= 99:\n    return 0\n", encoding="utf-8")
    previews = capture.finish(ToolResult.success("完成"))

    assert len(previews) == 1
    assert previews[0].path == "price.py"
    assert "-if total > 99:" in previews[0].diff
    assert "+if total >= 99:" in previews[0].diff
    assert previews[0].added == 1
    assert previews[0].removed == 1


def test_capture_apply_patch_handles_new_file(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    patch = """--- /dev/null
+++ b/new.py
@@ -0,0 +1,1 @@
+print('new')
"""
    capture = capture_change(
        workspace,
        ToolCall(id="patch", name="apply_patch", arguments={"patch": patch}),
    )
    assert capture is not None
    (tmp_path / "new.py").write_text("print('new')\n", encoding="utf-8")

    preview = capture.finish(ToolResult.success("完成"))[0]

    assert preview.path == "new.py"
    assert "--- /dev/null" in preview.diff
    assert "+print('new')" in preview.diff


def test_capture_create_file_shows_all_content_as_addition(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    capture = capture_change(
        workspace,
        ToolCall(
            id="create",
            name="create_file",
            arguments={"path": "new.py", "content": "first\nsecond\n"},
        ),
    )
    assert capture is not None
    (tmp_path / "new.py").write_text("first\nsecond\n", encoding="utf-8")

    preview = capture.finish(ToolResult.success("完成"))[0]

    assert preview.path == "new.py"
    assert "--- /dev/null" in preview.diff
    assert "+first" in preview.diff
    assert preview.added == 2
    assert preview.removed == 0


def test_capture_delete_and_failed_tool(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("old\n", encoding="utf-8")
    capture = capture_change(
        Workspace(tmp_path),
        ToolCall(id="delete", name="delete_file", arguments={"path": "old.txt"}),
    )
    assert capture is not None
    target.unlink()

    assert capture.finish(ToolResult.failure("未删除")) == ()
    preview = capture.finish(ToolResult.success("已删除"))[0]
    assert preview.path == "old.txt"
    assert "-old" in preview.diff
    assert "+++ /dev/null" in preview.diff


def test_capture_ignores_non_mutation_and_sensitive_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    assert capture_change(
        workspace, ToolCall(id="read", name="read_file", arguments={"path": "a.py"})
    ) is None
    capture = capture_change(
        workspace,
        ToolCall(
            id="replace",
            name="replace_text",
            arguments={"path": ".env", "old_text": "a", "new_text": "b"},
        ),
    )
    assert capture is not None
    assert capture.finish(ToolResult.success("不应发生")) == ()
