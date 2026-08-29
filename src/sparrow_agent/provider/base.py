"""与具体模型厂商无关的 Provider 契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from sparrow_agent.models import Message


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次模型请求的标准化 Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Provider 返回给 Agent 的统一响应。"""

    message: Message
    finish_reason: str | None
    usage: TokenUsage = TokenUsage()
    model: str | None = None
    response_id: str | None = None


class ProviderError(RuntimeError):
    """模型接入层异常。"""


class ProviderProtocolError(ProviderError):
    """服务响应不满足 Sparrow 依赖的协议。"""


class ProviderRequestError(ProviderError):
    """模型请求失败，并标明是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelProvider(Protocol):
    """Agent 依赖的最小模型接口。"""

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelResponse: ...
