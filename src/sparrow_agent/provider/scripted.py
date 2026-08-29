"""供 Agent 离线测试使用的可脚本化 Provider。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from sparrow_agent.models import Message
from sparrow_agent.provider.base import ModelResponse, ProviderError


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """ScriptedProvider 收到的一次不可变请求快照。"""

    messages: tuple[Message, ...]
    tools: tuple[Mapping[str, Any], ...]


class ScriptedProvider:
    """按顺序返回预设响应或抛出预设异常。"""

    def __init__(
        self, responses: Iterable[ModelResponse | Exception]
    ) -> None:
        self._responses = list(responses)
        self._index = 0
        self.requests: list[RecordedRequest] = []

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelResponse:
        self.requests.append(RecordedRequest(tuple(messages), tuple(tools)))
        if self._index >= len(self._responses):
            raise ProviderError("ScriptedProvider 的预设响应已经用尽")
        response = self._responses[self._index]
        self._index += 1
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def remaining_responses(self) -> int:
        return len(self._responses) - self._index
