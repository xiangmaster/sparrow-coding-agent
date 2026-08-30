"""Sparrow 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from sparrow_agent import __version__
from sparrow_agent.agent import Agent, AgentSettings
from sparrow_agent.config import ConfigError, read_environment_file
from sparrow_agent.models import AgentResult, StopReason
from sparrow_agent.provider import DeepSeekProvider, DeepSeekSettings
from sparrow_agent.recording import (
    EventRecorder,
    RecordingError,
    RunRecorder,
    replay_trace,
)
from sparrow_agent.tools import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchFilesTool,
    ToolRegistry,
)
from sparrow_agent.workspace import Workspace, WorkspaceError

_INCOMPLETE_EXIT_CODE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparrow",
        description="带本地工具、证据门和可审计轨迹的轻量编程智能体",
    )
    parser.add_argument("--version", action="version", version=f"Sparrow {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="在指定工作区运行编程任务")
    run_parser.add_argument("task", nargs="+", help="要交给 Sparrow 的自然语言任务")
    run_parser.add_argument(
        "-w", "--workspace", default=".", help="项目工作区，默认是当前目录"
    )
    run_parser.add_argument("--model", help="覆盖项目 .env 中的 SPARROW_MODEL")
    run_parser.add_argument(
        "--reasoning-effort",
        choices=("low", "high", "max"),
        help="覆盖项目 .env 中的推理强度",
    )
    run_parser.add_argument(
        "--max-iterations", type=int, default=20, help="Agent 最大模型迭代次数"
    )
    run_parser.add_argument(
        "--token-budget", type=int, default=200_000, help="单次运行累计 Token 上限"
    )
    run_parser.add_argument(
        "--context-characters",
        type=int,
        default=120_000,
        help="发送给模型的近似消息上下文字符上限",
    )
    run_parser.add_argument(
        "--no-record", action="store_true", help="不写入 .sparrow/runs 运行轨迹"
    )
    run_parser.set_defaults(handler=_run_command)

    replay_parser = subparsers.add_parser("replay", help="无副作用地校验并重放 JSONL 轨迹")
    replay_parser.add_argument("trace", help="要读取的 .jsonl 轨迹文件")
    replay_parser.add_argument(
        "--events", action="store_true", help="额外列出每条事件的序号和类型"
    )
    replay_parser.set_defaults(handler=_replay_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except (ConfigError, WorkspaceError, RecordingError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n运行已由用户取消。", file=sys.stderr)
        return 130


def _run_command(arguments: argparse.Namespace) -> int:
    workspace = Workspace(arguments.workspace)
    environment = read_environment_file(workspace.root / ".env")
    if arguments.model:
        environment["SPARROW_MODEL"] = arguments.model
    if arguments.reasoning_effort:
        environment["SPARROW_REASONING_EFFORT"] = arguments.reasoning_effort
    provider_settings = DeepSeekSettings.from_environment(environment)
    agent_settings = AgentSettings(
        max_iterations=arguments.max_iterations,
        max_total_tokens=arguments.token_budget,
        max_context_characters=arguments.context_characters,
    )
    registry = _build_tool_registry(workspace)
    provider = DeepSeekProvider(provider_settings)
    task = " ".join(arguments.task).strip()

    disk_recorder: RunRecorder | None = None
    console_recorder = _ConsoleRecorder(sys.stderr)
    recorder: EventRecorder = console_recorder
    if not arguments.no_record:
        disk_recorder = RunRecorder(workspace.root)
        recorder = _FanoutRecorder((console_recorder, disk_recorder))

    print(f"工作区：{workspace.root}")
    print(f"模型：{provider_settings.model}（{provider_settings.reasoning_effort}）")
    print("安全边界：受限本地进程，不是操作系统级沙箱。")
    if disk_recorder is not None:
        print(f"运行轨迹：{disk_recorder.jsonl_path}")

    try:
        result = Agent(
            provider,
            registry,
            settings=agent_settings,
            recorder=recorder,
        ).run(task)
    except KeyboardInterrupt:
        recorder.record(
            "run_finished",
            {
                "stop_reason": StopReason.CANCELLED.value,
                "final_text": "用户取消运行",
                "iterations": 0,
                "completion_request": None,
            },
        )
        raise
    finally:
        if disk_recorder is not None:
            disk_recorder.close()

    _print_result(result)
    return 0 if result.stop_reason is StopReason.COMPLETED else _INCOMPLETE_EXIT_CODE


def _replay_command(arguments: argparse.Namespace) -> int:
    summary = replay_trace(arguments.trace)
    print(summary.to_text())
    if arguments.events:
        print("\n事件：")
        for event in summary.events:
            print(f"{event.sequence:04d}  {event.event}")
    return 0


def _build_tool_registry(workspace: Workspace) -> ToolRegistry:
    return ToolRegistry(
        [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            SearchFilesTool(workspace),
            ApplyPatchTool(workspace),
            RunCommandTool(workspace),
        ]
    )


def _print_result(result: AgentResult) -> None:
    print(f"\n终止原因：{result.stop_reason.value}")
    print(result.final_text)
    completion = result.completion_request
    if completion is None:
        return
    if completion.changed_files:
        print("修改文件：")
        for path in completion.changed_files:
            print(f"- {path}")
    if completion.verifications:
        print("验证记录：")
        for record in completion.verifications:
            print(f"- {' '.join(record.command)}（退出码 {record.exit_code}）")
    if completion.remaining_risks:
        print("剩余风险：")
        for risk in completion.remaining_risks:
            print(f"- {risk}")


class _ConsoleRecorder:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        message: str | None = None
        if event == "model_response":
            message = (
                f"[第 {data.get('iteration', '?')} 轮] 模型返回 "
                f"{data.get('tool_call_count', 0)} 个工具调用"
            )
        elif event == "tool_result":
            state = "成功" if data.get("ok") is True else "失败"
            message = f"[工具] {data.get('tool_name', '?')}：{state}"
        elif event == "provider_retry":
            message = f"[重试] Provider 第 {data.get('next_attempt', '?')} 次尝试"
        elif event == "control_feedback":
            message = "[控制器] 模型尚未提交结构化完成申请"
        elif event == "context_compacted":
            message = (
                "[上下文] 已压缩 "
                f"{data.get('newly_compacted_turns', '?')} 个较早轮次，"
                f"保留 {data.get('retained_messages', '?')} 条消息"
            )
        if message is not None:
            print(message, file=self._stream, flush=True)


class _FanoutRecorder:
    def __init__(self, recorders: Sequence[EventRecorder]) -> None:
        self._recorders = tuple(recorders)

    def record(self, event: str, data: Mapping[str, Any]) -> None:
        for recorder in self._recorders:
            recorder.record(event, data)
