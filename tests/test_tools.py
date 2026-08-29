"""工具协议、注册中心和只读文件工具测试。"""

from pathlib import Path
from typing import Any, Mapping

import pytest

from sparrow_agent.models import ToolCall, ToolResult
from sparrow_agent.tools import (
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    ToolRegistry,
    ToolSpec,
)
from sparrow_agent.workspace import Workspace


class _ExplodingTool:
    spec = ToolSpec(
        name="explode",
        description="用于验证异常隔离。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        raise RuntimeError("boom")


def _registry(tmp_path: Path) -> ToolRegistry:
    workspace = Workspace(tmp_path)
    return ToolRegistry(
        [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            SearchFilesTool(workspace),
        ]
    )


def test_registry_exposes_model_compatible_schemas(tmp_path: Path) -> None:
    schemas = _registry(tmp_path).model_schemas()

    assert [schema["function"]["name"] for schema in schemas] == [
        "list_files",
        "read_file",
        "search_files",
    ]
    assert all(schema["type"] == "function" for schema in schemas)


def test_registry_rejects_duplicate_unknown_and_invalid_calls(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    registry = ToolRegistry([ListFilesTool(workspace)])

    with pytest.raises(ValueError, match="重复"):
        registry.register(ListFilesTool(workspace))

    unknown = registry.execute(ToolCall(id="1", name="missing", arguments={}))
    missing = ToolRegistry([ReadFileTool(workspace)]).execute(
        ToolCall(id="2", name="read_file", arguments={})
    )
    unexpected = registry.execute(
        ToolCall(id="3", name="list_files", arguments={"unknown": True})
    )

    assert unknown.ok is False and "未知工具" in unknown.error
    assert missing.ok is False and "缺少必填参数" in missing.error
    assert unexpected.ok is False and "未声明参数" in unexpected.error


def test_registry_contains_unexpected_tool_exception() -> None:
    result = ToolRegistry([_ExplodingTool()]).execute(
        ToolCall(id="1", name="explode", arguments={})
    )

    assert result.ok is False
    assert result.error == "工具执行异常：RuntimeError: boom"
    assert result.metadata["error_type"] == "RuntimeError"


def test_registry_returns_tool_argument_parse_error_without_execution() -> None:
    call = ToolCall(
        id="bad",
        name="explode",
        raw_arguments="[",
        argument_error="工具参数不是有效 JSON",
    )

    result = ToolRegistry([_ExplodingTool()]).execute(call)

    assert result.ok is False
    assert result.error == "工具参数不是有效 JSON"
    assert result.metadata["error_type"] == "ArgumentParseError"


def test_list_files_is_sorted_recursive_and_excludes_noise(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("x", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(id="1", name="list_files", arguments={})
    )

    assert result.ok is True
    assert result.output.splitlines() == ["src/a.py", "src/b.py"]
    assert result.metadata == {"count": 2, "truncated": False, "path": "."}


def test_list_files_never_reveals_sensitive_hidden_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=replace-me", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("token", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="list_files",
            arguments={"include_hidden": True},
        )
    )

    assert result.output == ".env.example"


def test_list_files_honours_non_recursive_and_result_limit(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.txt").write_text("c", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="list_files",
            arguments={"recursive": False, "max_results": 1},
        )
    )

    assert result.output == "a.txt"
    assert result.metadata["truncated"] is True


def test_read_file_returns_numbered_line_range(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"path": "main.py", "start_line": 2, "end_line": 3},
        )
    )

    assert result.ok is True
    assert result.output == "     2 | 第二行\n     3 | 第三行"
    assert result.metadata["total_lines"] == 3
    assert result.metadata["truncated"] is False


def test_read_file_rejects_binary_sensitive_and_out_of_range_inputs(
    tmp_path: Path,
) -> None:
    (tmp_path / "binary.dat").write_bytes(b"text\0binary")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / "short.txt").write_text("only one line", encoding="utf-8")
    registry = _registry(tmp_path)

    binary = registry.execute(
        ToolCall(id="1", name="read_file", arguments={"path": "binary.dat"})
    )
    sensitive = registry.execute(
        ToolCall(id="2", name="read_file", arguments={"path": ".env"})
    )
    out_of_range = registry.execute(
        ToolCall(
            id="3",
            name="read_file",
            arguments={"path": "short.txt", "start_line": 2},
        )
    )

    assert binary.ok is False and "二进制" in binary.error
    assert sensitive.ok is False and "禁止访问" in sensitive.error
    assert out_of_range.ok is False and "超出文件总行数" in out_of_range.error


def test_search_files_supports_literal_regex_case_and_limits(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Hello world\nhello again\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("HELLO third\n", encoding="utf-8")
    registry = _registry(tmp_path)

    literal = registry.execute(
        ToolCall(
            id="1",
            name="search_files",
            arguments={"query": "hello", "case_sensitive": False, "max_results": 2},
        )
    )
    regex = registry.execute(
        ToolCall(
            id="2",
            name="search_files",
            arguments={"query": "^Hello", "regex": True},
        )
    )

    assert literal.output.splitlines() == [
        "a.txt:1: Hello world",
        "a.txt:2: hello again",
    ]
    assert literal.metadata["truncated"] is True
    assert regex.output == "a.txt:1: Hello world"


def test_search_files_skips_binary_and_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"needle\0binary")
    (tmp_path / ".env").write_text("needle", encoding="utf-8")

    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="search_files",
            arguments={"query": "needle", "include_hidden": True},
        )
    )

    assert result.output == "visible.txt:1: needle"
    assert result.metadata["skipped_files"] == 1


def test_search_files_returns_invalid_regex_as_failure(tmp_path: Path) -> None:
    result = _registry(tmp_path).execute(
        ToolCall(
            id="1",
            name="search_files",
            arguments={"query": "[", "regex": True},
        )
    )

    assert result.ok is False
    assert "正则表达式无效" in result.error
