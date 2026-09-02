"""CLI 与桌面端共用的 Sparrow 运行时装配。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sparrow_agent.config import read_environment_file
from sparrow_agent.provider import DeepSeekSettings
from sparrow_agent.recording import EventRecorder
from sparrow_agent.tools import (
    ApplyPatchTool,
    CreateDirectoryTool,
    CreateFileTool,
    DeleteFileTool,
    ListFilesTool,
    ReadFileTool,
    RenameFileTool,
    ReplaceTextTool,
    RunCommandTool,
    SearchFilesTool,
    ToolRegistry,
)
from sparrow_agent.workspace import Workspace


def load_provider_settings(
    config_directory: str | Path,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> DeepSeekSettings:
    """从 Sparrow 自身配置目录加载 Provider 配置并应用显式覆盖。"""

    environment = read_environment_file(Path(config_directory).resolve() / ".env")
    if model:
        environment["SPARROW_MODEL"] = model
    if reasoning_effort:
        environment["SPARROW_REASONING_EFFORT"] = reasoning_effort
    return DeepSeekSettings.from_environment(environment)


def build_tool_registry(workspace: Workspace) -> ToolRegistry:
    """按稳定顺序注册 CLI 与 GUI 共用的十个本地工具。"""

    return ToolRegistry(
        [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            SearchFilesTool(workspace),
            CreateDirectoryTool(workspace),
            CreateFileTool(workspace),
            ReplaceTextTool(workspace),
            ApplyPatchTool(workspace),
            RenameFileTool(workspace),
            DeleteFileTool(workspace),
            RunCommandTool(workspace),
        ]
    )


class FanoutRecorder:
    """将同一结构化事件同步发送给多个记录器。"""

    def __init__(self, recorders: Sequence[EventRecorder]) -> None:
        self._recorders = tuple(recorders)

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        for recorder in self._recorders:
            recorder.record(event, data)
