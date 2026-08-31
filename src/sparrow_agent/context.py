"""可测试的本地对话上下文。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sparrow_agent.models import Message, MessageRole, ToolCall, ToolResult

_DEFAULT_MAX_OBSERVATION_CHARACTERS = 30_000
_DEFAULT_MAX_CONTEXT_CHARACTERS = 120_000
_DEFAULT_MAX_SUMMARY_CHARACTERS = 6_000
_TRUNCATION_SUFFIX = "\n……[Sparrow 已截断过长工具输出]"
_SUMMARY_HEADER = "[Sparrow 较早历史事实摘要]"


class Context:
    """维护消息因果链，并将工具结果转成合法的工具消息。"""

    def __init__(
        self,
        system_prompt: str,
        user_task: str,
        *,
        max_observation_characters: int = _DEFAULT_MAX_OBSERVATION_CHARACTERS,
        max_context_characters: int = _DEFAULT_MAX_CONTEXT_CHARACTERS,
        max_summary_characters: int | None = None,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if not user_task.strip():
            raise ValueError("user_task 不能为空")
        if max_observation_characters < len(_TRUNCATION_SUFFIX) + 1:
            raise ValueError("工具观察字符上限过小")
        if max_context_characters < 500:
            raise ValueError("上下文字符预算不能小于 500")
        if max_summary_characters is None:
            max_summary_characters = min(
                _DEFAULT_MAX_SUMMARY_CHARACTERS,
                max_context_characters // 4,
            )
        if not 100 <= max_summary_characters < max_context_characters:
            raise ValueError("历史摘要字符上限必须大于等于 100 且小于上下文预算")
        self._messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_task),
        ]
        self._max_observation_characters = max_observation_characters
        self._max_context_characters = max_context_characters
        self._max_summary_characters = max_summary_characters
        self._summary_facts: list[str] = []
        self._compacted_turns = 0

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[Message],
        *,
        max_observation_characters: int = _DEFAULT_MAX_OBSERVATION_CHARACTERS,
        max_context_characters: int = _DEFAULT_MAX_CONTEXT_CHARACTERS,
        max_summary_characters: int | None = None,
    ) -> Context:
        """从已持久化的 Provider 消息恢复可继续追加的上下文。"""

        restored = tuple(messages)
        if len(restored) < 2:
            raise ValueError("恢复上下文至少需要系统消息和首条用户消息")
        if restored[0].role is not MessageRole.SYSTEM:
            raise ValueError("恢复上下文的首条消息必须是系统消息")
        if restored[1].role is not MessageRole.USER:
            raise ValueError("恢复上下文的第二条消息必须是用户消息")
        context = cls(
            restored[0].content or "",
            restored[1].content or "",
            max_observation_characters=max_observation_characters,
            max_context_characters=max_context_characters,
            max_summary_characters=max_summary_characters,
        )
        context._messages = list(restored)
        return context

    @property
    def messages(self) -> tuple[Message, ...]:
        """压缩超预算的较早轮次，并返回不可变消息快照。"""

        self._compact_if_needed()
        return self._assembled_messages()

    @property
    def compacted_turns(self) -> int:
        """返回已被压缩为事实摘要的完整轮次数。"""

        return self._compacted_turns

    @property
    def estimated_characters(self) -> int:
        """返回当前发送给 Provider 的近似序列化字符数。"""

        self._compact_if_needed()
        return _messages_characters(self._assembled_messages())

    @property
    def summary_characters(self) -> int:
        """返回当前历史事实摘要的字符数。"""

        summary = self._summary_message()
        return len(summary.content or "") if summary is not None else 0

    def append_assistant(self, message: Message) -> None:
        if message.role is not MessageRole.ASSISTANT:
            raise ValueError("只能通过 append_assistant 追加助手消息")
        self._messages.append(message)

    def append_user(self, content: str) -> Message:
        """追加下一轮用户输入，使同一上下文可以持续协作。"""

        if not isinstance(content, str) or not content.strip():
            raise ValueError("用户消息不能为空")
        message = Message(role=MessageRole.USER, content=content.strip())
        self._messages.append(message)
        return message

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

    def _assembled_messages(self) -> tuple[Message, ...]:
        anchors = self._messages[:2]
        history = self._messages[2:]
        summary = self._summary_message()
        if summary is None:
            return tuple([*anchors, *history])
        system_content = anchors[0].content or ""
        combined_system = Message(
            role=MessageRole.SYSTEM,
            content=system_content + "\n\n" + (summary.content or ""),
        )
        return tuple([combined_system, anchors[1], *history])

    def _summary_message(self) -> Message | None:
        if not self._summary_facts:
            return None
        content = _SUMMARY_HEADER + "\n" + "\n".join(
            f"- {fact}" for fact in self._summary_facts
        )
        return Message(role=MessageRole.SYSTEM, content=content)

    def _compact_if_needed(self) -> None:
        groups = _history_groups(self._messages[2:])
        if len(groups) <= 1:
            self._fit_summary_to_budget()
            return

        changed = False
        while len(groups) > 1:
            candidate = self._messages[:2] + [item for group in groups for item in group]
            summary = self._summary_message()
            if summary is not None:
                candidate.insert(2, summary)
            if _messages_characters(candidate) <= self._max_context_characters:
                break
            dropped = groups.pop(0)
            self._summary_facts.extend(_summarize_group(dropped))
            self._trim_summary()
            self._compacted_turns += 1
            changed = True

        if changed:
            self._messages = [
                *self._messages[:2],
                *(item for group in groups for item in group),
            ]
        self._fit_summary_to_budget()

    def _trim_summary(self) -> None:
        while len(self._summary_facts) > 1:
            content = _SUMMARY_HEADER + "\n" + "\n".join(
                f"- {fact}" for fact in self._summary_facts
            )
            if len(content) <= self._max_summary_characters:
                return
            self._summary_facts.pop(0)

    def _fit_summary_to_budget(self) -> None:
        while (
            len(self._summary_facts) > 1
            and _messages_characters(self._assembled_messages())
            > self._max_context_characters
        ):
            self._summary_facts.pop(0)
        if (
            self._summary_facts
            and _messages_characters(self._assembled_messages())
            > self._max_context_characters
        ):
            self._summary_facts = [
                f"已压缩 {self._compacted_turns} 个较早轮次；详细事实保留在运行轨迹中"
            ]
        if _messages_characters(self._assembled_messages()) > self._max_context_characters:
            self._summary_facts.clear()


def _history_groups(messages: Sequence[Message]) -> list[list[Message]]:
    """按助手轮次分组，确保工具调用与对应结果不会被拆散。"""

    groups: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        starts_new_group = message.role is MessageRole.USER or (
            message.role is MessageRole.ASSISTANT
            and any(item.role is MessageRole.ASSISTANT for item in current)
        )
        if starts_new_group and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _messages_characters(messages: Sequence[Message]) -> int:
    return sum(
        len(
            json.dumps(
                message.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        for message in messages
    )


def _summarize_group(group: Sequence[Message]) -> list[str]:
    calls = {
        call.id: call
        for message in group
        if message.role is MessageRole.ASSISTANT
        for call in message.tool_calls
    }
    facts: list[str] = []
    for message in group:
        if message.role is MessageRole.USER and message.content:
            facts.append("用户后续要求：" + _short_text(message.content))
        elif message.role is MessageRole.TOOL:
            facts.append(_tool_fact(calls.get(message.tool_call_id or ""), message))
        elif (
            message.role is MessageRole.ASSISTANT
            and not message.tool_calls
            and message.content
        ):
            facts.append("模型曾回复：" + _short_text(message.content))
        elif message.role is MessageRole.SYSTEM and message.content:
            facts.append("控制器反馈：" + _short_text(message.content))
    return facts or ["一个较早轮次已压缩，未产生可提取的工具事实"]


def _tool_fact(call: ToolCall | None, message: Message) -> str:
    name = call.name if call is not None else "未知工具"
    try:
        payload = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        return f"工具 {name} 返回了无法解析的历史观察"
    if not isinstance(payload, dict):
        return f"工具 {name} 返回了非对象历史观察"

    state = "成功" if payload.get("ok") is True else "失败"
    parts = [f"工具 {name}：{state}"]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        _append_metadata_facts(parts, metadata)
    error = payload.get("error")
    if isinstance(error, str) and error:
        parts.append("错误：" + _short_text(error, 160))
    return "；".join(parts)


def _append_metadata_facts(parts: list[str], metadata: dict[str, Any]) -> None:
    path = metadata.get("path")
    if isinstance(path, str) and path:
        parts.append("路径：" + _short_text(path, 120))
    changed = metadata.get("changed_files")
    if isinstance(changed, (list, tuple)):
        files = [item for item in changed if isinstance(item, str) and item]
        if files:
            parts.append("修改：" + "、".join(files[:12]))
    command = metadata.get("command")
    if command and isinstance(command, (list, tuple)) and all(
        isinstance(item, str) for item in command
    ):
        parts.append("命令：" + _short_text(" ".join(command), 180))
    exit_code = metadata.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        parts.append(f"退出码：{exit_code}")


def _short_text(value: str, limit: int = 200) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[:limit] + "……"
