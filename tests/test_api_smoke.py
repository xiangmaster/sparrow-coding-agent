"""默认跳过、会产生真实网络请求和少量费用的 DeepSeek 冒烟测试。"""

import pytest

from sparrow_agent.config import ConfigError, read_environment_file
from sparrow_agent.models import Message, MessageRole
from sparrow_agent.provider import DeepSeekProvider, DeepSeekSettings


@pytest.mark.api_smoke
def test_real_deepseek_chat_completion() -> None:
    """验证密钥、网络、模型名称以及基本响应解析。"""

    try:
        project_environment = read_environment_file(".env")
    except ConfigError as exc:
        pytest.fail(f"无法读取项目本地 .env：{exc}")

    try:
        settings = DeepSeekSettings(
            api_key=project_environment.get("DEEPSEEK_API_KEY", ""),
            base_url=project_environment.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ),
            model="deepseek-v4-flash",
            reasoning_effort="low",
            max_tokens=256,
            timeout_seconds=60,
        )
    except ValueError as exc:
        pytest.fail(f"项目本地 .env 配置无效：{exc}")
    response = DeepSeekProvider(settings).complete(
        [
            Message(
                role=MessageRole.USER,
                content="这是连通性测试。请只回复：SPARROW_API_OK",
            )
        ]
    )

    assert response.message.content is not None
    assert "SPARROW_API_OK" in response.message.content
    assert response.message.tool_calls == ()
    assert response.usage.total_tokens > 0
