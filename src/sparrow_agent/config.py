"""Sparrow 本地配置文件读取，不修改全局进程环境。"""

from __future__ import annotations

import re
from pathlib import Path

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENV_FILE_BYTES = 64 * 1024


class ConfigError(ValueError):
    """本地配置文件缺失或格式无效。"""


def read_environment_file(path: str | Path) -> dict[str, str]:
    """读取简单的 KEY=VALUE 文件，不执行变量展开或 Shell 语法。"""

    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"Sparrow 配置文件不存在：{file_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Sparrow 配置文件无法读取：{file_path}") from exc
    if len(data) > _MAX_ENV_FILE_BYTES:
        raise ConfigError(f"Sparrow 配置文件超过 {_MAX_ENV_FILE_BYTES} 字节")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Sparrow 配置文件必须是 UTF-8 文本") from exc

    values: dict[str, str] = {}
    for line_number, original_line in enumerate(text.splitlines(), start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ConfigError(f"Sparrow 配置第 {line_number} 行格式无效")
        if name in values:
            raise ConfigError(f"Sparrow 配置第 {line_number} 行重复定义 {name}")
        values[name] = _parse_value(raw_value.strip(), line_number)
    return values


def _parse_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value[0] in "\"'":
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ConfigError(f"Sparrow 配置第 {line_number} 行引号不完整")
        return value[1:-1]
    comment_index = value.find(" #")
    return value[:comment_index].rstrip() if comment_index >= 0 else value
