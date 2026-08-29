"""可测试的本地对话上下文。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sparrow_agent.models import Message, MessageRole, ToolCall, ToolResult

_DEFAULT_MAX_OBSERVATION_CHARACTERS = 30_000
_TRUNCATION_SUFFIX = "\n……[Sparrow 已截断过长工具输出]"


class Context:
    """维护消息因果链，并将工具结果转成合法的工具消息。"""

    def __init__(
        self,
        system_prompt: str,
        user_task: str,
        *,
        max_observation_characters: int = _DEFAULT_MAX_OBSERVATION_CHARACTERS,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if not user_task.strip():
            raise ValueError("user_task 不能为空")
        if max_observation_characters < len(_TRUNCATION_SUFFIX) + 1:
            raise ValueError("工具观察字符上限过小")
        self._messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_task),
        ]
        self._max_observation_characters = max_observation_characters

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回不可变消息快照，防止 Provider 修改内部状态。"""

        return tuple(self._messages)

    def append_assistant(self, message: Message) -> None:
        if message.role is not MessageRole.ASSISTANT:
            raise ValueError("只能通过 append_assistant 追加助手消息")
        self._messages.append(message)

    def append_tool_result(self, call: ToolCall, result: ToolResult) -> Message:
        """追加与工具调用 id 成对的、长度受限的 JSON 观察。"""

        payload = result.to_dict()
        output = payload["output"]
        truncated = len(output) > self._max_observation_characters
        if truncated:
            keep = self._max_observation_characters - len(_TRUNCATION_SUFFIX)
            payload["output"] = output[:keep] + _TRUNCATION_SUFFIX
            metadata = dict(payload["metadata"])
            metadata["observation_truncated"] = True
            metadata["original_output_characters"] = len(output)
            payload["metadata"] = metadata
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        message = Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=call.id,
        )
        self._messages.append(message)
        return message

    def append_control_feedback(self, feedback: str) -> None:
        """追加由 Sparrow 控制器生成、并非用户输入的规则反馈。"""

        if not feedback.strip():
            raise ValueError("控制反馈不能为空")
        self._messages.append(
            Message(
                role=MessageRole.SYSTEM,
                content=f"[Sparrow 控制器反馈]\n{feedback}",
            )
        )

    def extend(self, messages: Sequence[Message]) -> None:
        """为轨迹恢复预留的显式批量追加接口。"""

        self._messages.extend(messages)
