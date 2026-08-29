"""基于 OpenAI 兼容 Chat Completions 的 DeepSeek Provider。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import openai
from openai import OpenAI

from sparrow_agent.models import Message, MessageRole, ToolCall
from sparrow_agent.provider.base import (
    ModelResponse,
    ProviderProtocolError,
    ProviderRequestError,
    TokenUsage,
)

_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high", "max"})


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """DeepSeek Provider 配置；密钥不会出现在 repr 中。"""

    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    reasoning_effort: str = "high"
    max_tokens: int = 32_768
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or not self.api_key.strip()
            or self.api_key.strip() == "replace-me"
            or self.api_key != self.api_key.strip()
        ):
            raise ValueError("必须提供有效的 DEEPSEEK_API_KEY")
        parsed_url = urlparse(self.base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ValueError("DEEPSEEK_BASE_URL 必须是有效的 HTTPS 地址")
        if not self.model.strip():
            raise ValueError("模型名称不能为空")
        if self.reasoning_effort not in _ALLOWED_REASONING_EFFORTS:
            allowed = "、".join(sorted(_ALLOWED_REASONING_EFFORTS))
            raise ValueError(f"reasoning_effort 必须是 {allowed} 之一")
        if not 1 <= self.max_tokens <= 384_000:
            raise ValueError("max_tokens 必须在 1 到 384000 之间")
        if not 1 <= self.timeout_seconds <= 600:
            raise ValueError("timeout_seconds 必须在 1 到 600 之间")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> DeepSeekSettings:
        """从环境变量构造配置，不读取或修改进程环境。"""

        values = os.environ if environment is None else environment
        return cls(
            api_key=values.get("DEEPSEEK_API_KEY", ""),
            base_url=values.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=values.get("SPARROW_MODEL", "deepseek-v4-pro"),
            reasoning_effort=values.get("SPARROW_REASONING_EFFORT", "high"),
            max_tokens=_environment_integer(values, "SPARROW_MAX_TOKENS", 32_768),
            timeout_seconds=_environment_float(
                values, "SPARROW_TIMEOUT_SECONDS", 120.0
            ),
        )


class DeepSeekProvider:
    """负责 Sparrow 内部类型与 DeepSeek API 类型之间的双向转换。"""

    def __init__(self, settings: DeepSeekSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=0,
        )

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelResponse:
        if not messages:
            raise ValueError("模型请求至少需要一条消息")
        try:
            api_messages = [_message_to_api(message) for message in messages]
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError(f"Sparrow 消息无法转换为 API 格式：{exc}") from exc
        request: dict[str, Any] = {
            "model": self._settings.model,
            "messages": api_messages,
            "reasoning_effort": self._settings.reasoning_effort,
            "max_tokens": self._settings.max_tokens,
            "extra_body": {"thinking": {"type": "enabled"}},
        }
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        try:
            response = self._client.chat.completions.create(**request)
        except openai.APIError as exc:
            raise ProviderRequestError(
                _safe_api_error_message(exc), retryable=_is_retryable(exc)
            ) from exc
        except Exception as exc:
            raise ProviderRequestError(
                f"模型客户端异常：{type(exc).__name__}: {exc}", retryable=False
            ) from exc
        return _response_from_api(response)


def _message_to_api(message: Message) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.role is MessageRole.ASSISTANT:
        if message.reasoning_content is not None:
            data["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        if call.raw_arguments is not None
                        else json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
    elif message.role is MessageRole.TOOL:
        data["tool_call_id"] = message.tool_call_id
    return data


def _response_from_api(response: Any) -> ModelResponse:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ProviderProtocolError("DeepSeek 响应中没有 choices")
    choice = choices[0]
    api_message = getattr(choice, "message", None)
    if api_message is None:
        raise ProviderProtocolError("DeepSeek 响应中没有 assistant message")

    try:
        tool_calls = tuple(
            _tool_call_from_api(call)
            for call in (getattr(api_message, "tool_calls", None) or ())
        )
    except ProviderProtocolError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProviderProtocolError(f"DeepSeek 工具调用无效：{exc}") from exc
    content = getattr(api_message, "content", None)
    reasoning_content = getattr(api_message, "reasoning_content", None)
    try:
        message = Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderProtocolError(f"DeepSeek assistant message 无效：{exc}") from exc

    return ModelResponse(
        message=message,
        finish_reason=getattr(choice, "finish_reason", None),
        usage=_usage_from_api(getattr(response, "usage", None)),
        model=getattr(response, "model", None),
        response_id=getattr(response, "id", None),
    )


def _tool_call_from_api(api_call: Any) -> ToolCall:
    call_type = getattr(api_call, "type", None)
    if call_type not in (None, "function"):
        raise ProviderProtocolError(f"不支持的 DeepSeek 工具调用类型：{call_type}")
    call_id = getattr(api_call, "id", None)
    function = getattr(api_call, "function", None)
    name = getattr(function, "name", None) if function is not None else None
    raw_arguments = (
        getattr(function, "arguments", None) if function is not None else None
    )
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise ProviderProtocolError("DeepSeek 工具调用缺少 id 或函数名称")
    if not isinstance(raw_arguments, str):
        raise ProviderProtocolError("DeepSeek 工具调用参数不是字符串")

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return ToolCall(
            id=call_id,
            name=name,
            arguments={},
            raw_arguments=raw_arguments,
            argument_error=f"工具参数不是有效 JSON：{exc.msg}",
        )
    if not isinstance(arguments, dict):
        return ToolCall(
            id=call_id,
            name=name,
            arguments={},
            raw_arguments=raw_arguments,
            argument_error="工具参数 JSON 的顶层必须是对象",
        )
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
    )


def _usage_from_api(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    completion_details = getattr(usage, "completion_tokens_details", None)
    return TokenUsage(
        prompt_tokens=_nonnegative_integer(getattr(usage, "prompt_tokens", 0)),
        completion_tokens=_nonnegative_integer(
            getattr(usage, "completion_tokens", 0)
        ),
        reasoning_tokens=_nonnegative_integer(
            getattr(completion_details, "reasoning_tokens", 0)
            if completion_details is not None
            else 0
        ),
    )


def _nonnegative_integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _is_retryable(error: openai.APIError) -> bool:
    if isinstance(error, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
        return True
    status_code = getattr(error, "status_code", None)
    return isinstance(status_code, int) and status_code >= 500


def _safe_api_error_message(error: openai.APIError) -> str:
    status_code = getattr(error, "status_code", None)
    prefix = f"DeepSeek API 请求失败（HTTP {status_code}）" if status_code else "DeepSeek API 请求失败"
    return f"{prefix}：{error}"


def _environment_integer(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    value = environment.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc


def _environment_float(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    value = environment.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字") from exc
