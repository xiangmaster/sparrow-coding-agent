"""DeepSeek Provider 的离线协议测试。"""

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from sparrow_agent.models import Message, MessageRole, ToolCall
from sparrow_agent.provider import (
    DeepSeekProvider,
    DeepSeekSettings,
    ProviderProtocolError,
    ProviderRequestError,
    ScriptedProvider,
)
from sparrow_agent.provider import ModelResponse, ProviderError


class _FakeCompletions:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.completions = _FakeCompletions(response, error)
        self.chat = SimpleNamespace(completions=self.completions)


def _settings(**overrides: Any) -> DeepSeekSettings:
    values = {"api_key": "test-key", **overrides}
    return DeepSeekSettings(**values)


def _response(
    *,
    content: str | None = "完成",
    reasoning_content: str | None = "思考过程",
    tool_calls: list[Any] | None = None,
) -> Any:
    message = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=35,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=20),
    )
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model="deepseek-v4-pro",
        id="response-1",
    )


def _api_tool_call(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _stream_chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
) -> Any:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model="deepseek-v4-flash",
        id="stream-1",
    )


def test_settings_load_from_environment_without_exposing_api_key() -> None:
    settings = DeepSeekSettings.from_environment(
        {
            "DEEPSEEK_API_KEY": "secret-value",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "SPARROW_MODEL": "deepseek-v4-flash",
            "SPARROW_REASONING_EFFORT": "max",
            "SPARROW_MAX_TOKENS": "4096",
            "SPARROW_TIMEOUT_SECONDS": "45.5",
        }
    )

    assert settings.model == "deepseek-v4-flash"
    assert settings.reasoning_effort == "max"
    assert settings.max_tokens == 4096
    assert settings.timeout_seconds == 45.5
    assert "secret-value" not in repr(settings)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_key": ""}, "API_KEY"),
        ({"api_key": "replace-me"}, "API_KEY"),
        ({"api_key": " key-with-spaces "}, "API_KEY"),
        ({"base_url": "http://api.deepseek.com"}, "HTTPS"),
        ({"base_url": "https://user:pass@api.deepseek.com"}, "HTTPS"),
        ({"reasoning_effort": "medium"}, "reasoning_effort"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
    ],
)
def test_settings_reject_invalid_values(
    overrides: dict[str, Any], message: str
) -> None:
    values = {"api_key": "test-key", **overrides}

    with pytest.raises(ValueError, match=message):
        DeepSeekSettings(**values)


def test_provider_sends_tools_thinking_and_full_reasoning_history() -> None:
    client = _FakeClient(_response())
    provider = DeepSeekProvider(_settings(), client=client)
    previous_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments={"path": "README.md"},
        raw_arguments='{"path":"README.md"}',
    )
    messages = [
        Message(role=MessageRole.SYSTEM, content="你是编程智能体。"),
        Message(role=MessageRole.USER, content="读取 README。"),
        Message(
            role=MessageRole.ASSISTANT,
            content=None,
            reasoning_content="我需要先读取文件。",
            tool_calls=(previous_call,),
        ),
        Message(
            role=MessageRole.TOOL,
            content='{"ok":true,"output":"内容"}',
            tool_call_id="call-1",
        ),
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object"},
            },
        }
    ]

    provider.complete(messages, tools)

    request = client.completions.requests[0]
    assert request["model"] == "deepseek-v4-pro"
    assert request["reasoning_effort"] == "high"
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["tool_choice"] == "auto"
    assert request["tools"] == tools
    assistant = request["messages"][2]
    assert assistant["reasoning_content"] == "我需要先读取文件。"
    assert assistant["tool_calls"][0]["function"]["arguments"] == (
        '{"path":"README.md"}'
    )
    assert request["messages"][3]["tool_call_id"] == "call-1"


def test_provider_parses_response_tool_calls_usage_and_reasoning() -> None:
    api_call = _api_tool_call(
        "call-2", "search_files", '{"query":"入口","path":"src"}'
    )
    client = _FakeClient(
        _response(content=None, reasoning_content="搜索入口。", tool_calls=[api_call])
    )

    response = DeepSeekProvider(_settings(), client=client).complete(
        [Message(role=MessageRole.USER, content="查找入口")]
    )

    assert response.message.role is MessageRole.ASSISTANT
    assert response.message.content is None
    assert response.message.reasoning_content == "搜索入口。"
    assert response.message.tool_calls[0].arguments == {
        "query": "入口",
        "path": "src",
    }
    assert response.message.tool_calls[0].raw_arguments == (
        '{"query":"入口","path":"src"}'
    )
    assert response.finish_reason == "tool_calls"
    assert response.usage.prompt_tokens == 120
    assert response.usage.completion_tokens == 35
    assert response.usage.reasoning_tokens == 20
    assert response.usage.total_tokens == 155
    assert response.model == "deepseek-v4-pro"
    assert response.response_id == "response-1"


def test_provider_streams_visible_text_and_reassembles_tool_call() -> None:
    first_call = SimpleNamespace(
        index=0,
        id="call-stream",
        function=SimpleNamespace(
            name="request_completion",
            arguments='{"summary":"完成",',
        ),
    )
    second_call = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(
            name=None,
            arguments=(
                '"changed_files":[],"verification_commands":[],'
                '"remaining_risks":[]}'
            ),
        ),
    )
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=6,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    chunks = [
        _stream_chunk(content="我先说明", reasoning_content="分析", tool_calls=[first_call]),
        _stream_chunk(content="完成结果。", tool_calls=[second_call], finish_reason="tool_calls"),
        SimpleNamespace(
            choices=[], usage=usage, model="deepseek-v4-flash", id="stream-1"
        ),
    ]
    client = _FakeClient(chunks)
    deltas: list[str] = []

    response = DeepSeekProvider(_settings(), client=client).complete_stream(
        [Message(role=MessageRole.USER, content="完成任务")],
        on_text_delta=deltas.append,
    )

    assert deltas == ["我先说明", "完成结果。"]
    assert response.message.content == "我先说明完成结果。"
    assert response.message.reasoning_content == "分析"
    assert response.message.tool_calls[0].name == "request_completion"
    assert response.message.tool_calls[0].arguments["summary"] == "完成"
    assert response.usage.total_tokens == 16
    request = client.completions.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    ("raw_arguments", "expected_error"),
    [
        ('{"path":', "不是有效 JSON"),
        ('["README.md"]', "顶层必须是对象"),
    ],
)
def test_provider_preserves_malformed_tool_arguments_as_structured_error(
    raw_arguments: str, expected_error: str
) -> None:
    api_call = _api_tool_call("call-bad", "read_file", raw_arguments)
    client = _FakeClient(
        _response(content=None, reasoning_content="读取。", tool_calls=[api_call])
    )

    response = DeepSeekProvider(_settings(), client=client).complete(
        [Message(role=MessageRole.USER, content="读取文件")]
    )
    call = response.message.tool_calls[0]

    assert call.arguments == {}
    assert call.raw_arguments == raw_arguments
    assert expected_error in call.argument_error


def test_provider_omits_tool_fields_when_no_tools_are_supplied() -> None:
    client = _FakeClient(_response())

    DeepSeekProvider(_settings(), client=client).complete(
        [Message(role=MessageRole.USER, content="你好")]
    )

    request = client.completions.requests[0]
    assert "tools" not in request
    assert "tool_choice" not in request


def test_provider_wraps_non_json_internal_tool_arguments_as_protocol_error() -> None:
    invalid_call = ToolCall(
        id="call-invalid",
        name="read_file",
        arguments={"paths": {"README.md"}},
    )
    message = Message(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=(invalid_call,),
    )

    with pytest.raises(ProviderProtocolError, match="无法转换"):
        DeepSeekProvider(_settings(), client=_FakeClient(_response())).complete([message])


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(message=None)], usage=None),
        _response(content=None, reasoning_content="只有推理", tool_calls=None),
    ],
)
def test_provider_rejects_invalid_response_shape(response: Any) -> None:
    provider = DeepSeekProvider(_settings(), client=_FakeClient(response))

    with pytest.raises(ProviderProtocolError):
        provider.complete([Message(role=MessageRole.USER, content="任务")])


def test_provider_rejects_unsupported_or_incomplete_tool_call() -> None:
    unsupported = SimpleNamespace(
        id="call-1",
        type="custom",
        function=SimpleNamespace(name="read_file", arguments="{}"),
    )
    missing_name = SimpleNamespace(
        id="call-2",
        type="function",
        function=SimpleNamespace(name=None, arguments="{}"),
    )

    for call in (unsupported, missing_name):
        provider = DeepSeekProvider(
            _settings(),
            client=_FakeClient(
                _response(content=None, reasoning_content="调用工具", tool_calls=[call])
            ),
        )
        with pytest.raises(ProviderProtocolError):
            provider.complete([Message(role=MessageRole.USER, content="任务")])


def test_provider_wraps_unexpected_client_error_without_retry() -> None:
    provider = DeepSeekProvider(
        _settings(), client=_FakeClient(error=RuntimeError("offline"))
    )

    with pytest.raises(ProviderRequestError) as caught:
        provider.complete([Message(role=MessageRole.USER, content="任务")])

    assert caught.value.retryable is False
    assert "RuntimeError: offline" in str(caught.value)


def test_provider_marks_connection_error_as_retryable() -> None:
    error = openai.APIConnectionError(
        message="connection failed",
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    provider = DeepSeekProvider(_settings(), client=_FakeClient(error=error))

    with pytest.raises(ProviderRequestError) as caught:
        provider.complete([Message(role=MessageRole.USER, content="任务")])

    assert caught.value.retryable is True
    assert "DeepSeek API 请求失败" in str(caught.value)


def test_environment_numeric_errors_are_explicit() -> None:
    with pytest.raises(ValueError, match="SPARROW_MAX_TOKENS"):
        DeepSeekSettings.from_environment(
            {"DEEPSEEK_API_KEY": "key", "SPARROW_MAX_TOKENS": "many"}
        )
    with pytest.raises(ValueError, match="SPARROW_TIMEOUT_SECONDS"):
        DeepSeekSettings.from_environment(
            {"DEEPSEEK_API_KEY": "key", "SPARROW_TIMEOUT_SECONDS": "slow"}
        )


def test_scripted_provider_returns_sequence_and_records_request_snapshots() -> None:
    first = ModelResponse(
        message=Message(role=MessageRole.ASSISTANT, content="第一步"),
        finish_reason="stop",
    )
    expected_error = ProviderError("预设失败")
    provider = ScriptedProvider([first, expected_error])
    messages = [Message(role=MessageRole.USER, content="任务")]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    assert provider.complete(messages, tools) is first
    messages.append(Message(role=MessageRole.USER, content="后来追加"))
    assert len(provider.requests[0].messages) == 1
    assert provider.remaining_responses == 1

    with pytest.raises(ProviderError, match="预设失败"):
        provider.complete(messages, tools)
    assert provider.remaining_responses == 0

    with pytest.raises(ProviderError, match="已经用尽"):
        provider.complete(messages, tools)
