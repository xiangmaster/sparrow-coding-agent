"""工具注册、Schema 汇总与统一失败隔离。"""

from __future__ import annotations

from typing import Any, Iterable

from sparrow_agent.models import ToolCall, ToolResult
from sparrow_agent.tools.base import Tool
from sparrow_agent.workspace import WorkspaceError


class ToolRegistry:
    """按名称调度工具，并保证单次工具失败不会击穿 Agent 循环。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"工具名称重复：{name}")
        self._tools[name] = tool

    def model_schemas(self) -> list[dict[str, Any]]:
        """按注册顺序返回模型可见的工具 Schema。"""

        return [tool.spec.to_model_schema() for tool in self._tools.values()]

    def execute(self, call: ToolCall) -> ToolResult:
        """执行工具调用，并将可预期和意外异常都转换为失败结果。"""

        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.failure(
                f"未知工具：{call.name}", metadata={"tool": call.name}
            )
        try:
            self._validate_arguments(tool, call.arguments)
            return tool.execute(call.arguments)
        except (WorkspaceError, TypeError, ValueError) as exc:
            return ToolResult.failure(
                str(exc), metadata={"tool": call.name, "error_type": type(exc).__name__}
            )
        except Exception as exc:
            return ToolResult.failure(
                f"工具执行异常：{type(exc).__name__}: {exc}",
                metadata={"tool": call.name, "error_type": type(exc).__name__},
            )

    @staticmethod
    def _validate_arguments(tool: Tool, arguments: Any) -> None:
        if not isinstance(arguments, dict) and not hasattr(arguments, "keys"):
            raise TypeError("工具参数必须是映射")
        if any(not isinstance(name, str) for name in arguments):
            raise TypeError("工具参数名称必须是字符串")

        schema = tool.spec.parameters
        required = schema.get("required", ())
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"缺少必填参数：{', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            unexpected = sorted(set(arguments) - allowed)
            if unexpected:
                raise ValueError(f"包含未声明参数：{', '.join(unexpected)}")
