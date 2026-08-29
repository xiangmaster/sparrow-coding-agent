"""工具协议及模型可见的工具说明。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from sparrow_agent.models import ToolResult


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具的名称、说明与 JSON Schema 参数定义。"""

    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("工具名称不能为空")
        if not self.description.strip():
            raise ValueError("工具说明不能为空")
        if self.parameters.get("type") != "object":
            raise ValueError("工具参数 Schema 的顶层类型必须是 object")

    def to_model_schema(self) -> dict[str, Any]:
        """转换为 DeepSeek/OpenAI 兼容的函数工具 Schema。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class Tool(Protocol):
    """所有本地工具必须遵守的最小协议。"""

    @property
    def spec(self) -> ToolSpec: ...

    def execute(self, arguments: Mapping[str, Any]) -> ToolResult: ...
