"""智能体各层共享的核心数据模型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class MessageRole(StrEnum):
    """对话消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    """智能体循环结束的明确原因。"""

    COMPLETED = "completed"
    MAX_ITERATIONS = "max_iterations"
    REPEATED_ACTION = "repeated_action"
    PROVIDER_ERROR = "provider_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型发起的一次结构化工具调用。"""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("工具调用 id 不能为空")
        if not self.name.strip():
            raise ValueError("工具名称不能为空")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("工具参数必须是映射")

    def to_dict(self) -> dict[str, Any]:
        """转换为不依赖具体模型厂商的普通字典。"""

        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }

    def fingerprint(self) -> str:
        """生成稳定指纹，用于识别重复工具调用。"""

        payload = json.dumps(
            {"name": self.name, "arguments": self.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Message:
    """内部统一消息；保留 DeepSeek 推理模型需要的推理内容。"""

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise TypeError("role 必须是 MessageRole")
        if self.role is MessageRole.TOOL:
            if not self.tool_call_id:
                raise ValueError("工具消息必须关联 tool_call_id")
            if self.tool_calls:
                raise ValueError("工具消息不能包含新的工具调用")
        elif self.tool_call_id is not None:
            raise ValueError("只有工具消息可以设置 tool_call_id")

        if self.role is not MessageRole.ASSISTANT:
            if self.tool_calls:
                raise ValueError("只有助手消息可以包含工具调用")
            if self.reasoning_content is not None:
                raise ValueError("只有助手消息可以包含 reasoning_content")

        if self.content is None and not (
            self.role is MessageRole.ASSISTANT and self.tool_calls
        ):
            raise ValueError("只有包含工具调用的助手消息可以省略 content")

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 JSONL 的字典，且不丢失推理内容。"""

        data: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.reasoning_content is not None:
            data["reasoning_content"] = self.reasoning_content
        return data


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果；失败作为数据返回，不让异常击穿智能体循环。"""

    ok: bool
    output: str = ""
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("成功结果不能包含 error")
        if not self.ok and not self.error:
            raise ValueError("失败结果必须说明 error")

    @classmethod
    def success(
        cls, output: str = "", *, metadata: Mapping[str, Any] | None = None
    ) -> ToolResult:
        return cls(ok=True, output=output, metadata=metadata or {})

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        output: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(ok=False, output=output, error=error, metadata=metadata or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """一次可复核的验证记录。"""

    command: tuple[str, ...]
    exit_code: int
    event_index: int
    output_summary: str = ""

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("验证命令不能为空")
        if self.event_index < 0:
            raise ValueError("event_index 不能为负数")


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """智能体提交给完成证据门的结构化完成申请。"""

    summary: str
    changed_files: tuple[str, ...] = ()
    verifications: tuple[VerificationRecord, ...] = ()
    remaining_risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("完成摘要不能为空")


@dataclass(frozen=True, slots=True)
class AgentResult:
    """一次智能体运行的最终结果。"""

    stop_reason: StopReason
    final_text: str
    iterations: int
    completion_request: CompletionRequest | None = None

    def __post_init__(self) -> None:
        if self.iterations < 0:
            raise ValueError("iterations 不能为负数")
        if self.stop_reason is StopReason.COMPLETED and self.completion_request is None:
            raise ValueError("完成状态必须附带通过证据门的完成申请")
        if self.stop_reason is not StopReason.COMPLETED and self.completion_request is not None:
            raise ValueError("未完成状态不能携带完成申请")
