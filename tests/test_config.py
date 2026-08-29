"""项目本地环境文件读取测试。"""

import os
from pathlib import Path

import pytest

from sparrow_agent.config import ConfigError, read_environment_file


def test_read_environment_file_supports_comments_quotes_and_export(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """# 本地配置
DEEPSEEK_API_KEY=local-key
export SPARROW_MODEL="deepseek-v4-flash"
SPARROW_REASONING_EFFORT='low'
EMPTY=
VALUE=kept # 行尾注释
""",
        encoding="utf-8",
    )

    values = read_environment_file(env_file)

    assert values == {
        "DEEPSEEK_API_KEY": "local-key",
        "SPARROW_MODEL": "deepseek-v4-flash",
        "SPARROW_REASONING_EFFORT": "low",
        "EMPTY": "",
        "VALUE": "kept",
    }


def test_read_environment_file_does_not_modify_process_environment(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SPARROW_LOCAL_ONLY=value\n", encoding="utf-8")
    os.environ.pop("SPARROW_LOCAL_ONLY", None)

    values = read_environment_file(env_file)

    assert values["SPARROW_LOCAL_ONLY"] == "value"
    assert "SPARROW_LOCAL_ONLY" not in os.environ


@pytest.mark.parametrize(
    "content",
    [
        "INVALID LINE\n",
        "1INVALID=value\n",
        "DUPLICATE=one\nDUPLICATE=two\n",
        'UNFINISHED="value\n',
    ],
)
def test_read_environment_file_rejects_invalid_syntax(
    tmp_path: Path, content: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError):
        read_environment_file(env_file)


def test_read_environment_file_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        read_environment_file(tmp_path / ".env")
