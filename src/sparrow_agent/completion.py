"""结构化完成申请与基于本地事实的完成证据门。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from sparrow_agent.evidence import EvidenceLedger
from sparrow_agent.models import CompletionRequest, ToolCall, ToolResult
from sparrow_agent.tools.base import ToolSpec


@dataclass(frozen=True, slots=True)
class CompletionClaim:
    """模型声明的完成内容；尚未得到本地事实认可。"""

    summary: str
    changed_files: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    remaining_risks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    """完成门给出的结果和可继续反馈给模型的工具观察。"""

    result: ToolResult
    completion_request: CompletionRequest | None = None

    @property
    def accepted(self) -> bool:
        return self.completion_request is not None


class CompletionGate:
    """只根据工具证据接受或拒绝模型的完成申请。"""

    spec = ToolSpec(
        name="request_completion",
        description=(
            "当任务确实完成时提交结构化申请。它不会直接结束任务；"
            "Sparrow 会核对实际修改和最后一次修改后的验证证据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "完成内容摘要"},
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本次运行实际修改的工作区相对路径",
                },
                "verification_commands": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "description": "已成功执行的验证命令参数数组",
                },
                "remaining_risks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仍需向用户披露的风险，可为空数组",
                },
            },
            "required": [
                "summary",
                "changed_files",
                "verification_commands",
                "remaining_risks",
            ],
            "additionalProperties": False,
        },
    )

    def evaluate(
        self, call: ToolCall, ledger: EvidenceLedger
    ) -> CompletionDecision:
        try:
            claim = _parse_claim(call)
        except (TypeError, ValueError) as exc:
            return CompletionDecision(ToolResult.failure(str(exc)))

        problems = _evidence_problems(claim, ledger)
        if problems:
            feedback = "完成申请未通过：\n- " + "\n- ".join(problems)
            return CompletionDecision(ToolResult.failure(feedback))

        relevant_verifications = ledger.verifications_after_last_mutation()
        request = CompletionRequest(
            summary=claim.summary,
            changed_files=claim.changed_files,
            verifications=relevant_verifications,
            remaining_risks=claim.remaining_risks,
        )
        return CompletionDecision(
            ToolResult.success(
                "完成证据检查通过",
                metadata={
                    "accepted": True,
                    "changed_files": claim.changed_files,
                    "verification_count": len(relevant_verifications),
                },
            ),
            completion_request=request,
        )


def _parse_claim(call: ToolCall) -> CompletionClaim:
    if call.argument_error is not None:
        raise ValueError(call.argument_error)
    arguments = call.arguments
    allowed = {
        "summary",
        "changed_files",
        "verification_commands",
        "remaining_risks",
    }
    missing = sorted(allowed - set(arguments))
    unexpected = sorted(set(arguments) - allowed)
    if missing:
        raise ValueError(f"完成申请缺少字段：{', '.join(missing)}")
    if unexpected:
        raise ValueError(f"完成申请包含未声明字段：{', '.join(unexpected)}")

    summary = arguments["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise TypeError("完成摘要必须是非空字符串")
    changed_files = _string_tuple(arguments["changed_files"], "changed_files")
    risks = _string_tuple(arguments["remaining_risks"], "remaining_risks")
    commands_value = arguments["verification_commands"]
    if not isinstance(commands_value, list):
        raise TypeError("verification_commands 必须是数组")
    commands: list[tuple[str, ...]] = []
    for command in commands_value:
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise TypeError("每条 verification_commands 都必须是非空字符串数组")
        commands.append(tuple(command))

    normalized_files = tuple(_normalize_claimed_path(path) for path in changed_files)
    if len(set(normalized_files)) != len(normalized_files):
        raise ValueError("changed_files 不能包含重复路径")
    return CompletionClaim(
        summary=summary.strip(),
        changed_files=normalized_files,
        verification_commands=tuple(commands),
        remaining_risks=risks,
    )


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{name} 必须是字符串数组")
    return tuple(value)


def _normalize_claimed_path(value: str) -> str:
    if not value:
        raise ValueError("changed_files 不能包含空路径")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() in ("", "."):
        raise ValueError(f"changed_files 包含非法工作区路径：{value}")
    return path.as_posix()


def _evidence_problems(
    claim: CompletionClaim, ledger: EvidenceLedger
) -> list[str]:
    problems: list[str] = []
    claimed_files = set(claim.changed_files)
    if claimed_files != ledger.changed_files:
        missing = sorted(ledger.changed_files - claimed_files)
        extra = sorted(claimed_files - ledger.changed_files)
        if missing:
            problems.append("未声明实际修改文件：" + "、".join(missing))
        if extra:
            problems.append("声称修改但没有本地证据：" + "、".join(extra))

    relevant = ledger.verifications_after_last_mutation()
    if relevant and relevant[-1].exit_code != 0:
        problems.append("最近一次验证失败，退出码不是 0")

    if ledger.changed_files:
        if not relevant:
            problems.append("最后一次修改之后没有运行验证命令")
        elif not any(record.exit_code == 0 for record in relevant):
            problems.append("最后一次修改之后没有成功验证")
        if not claim.verification_commands:
            problems.append("修改任务必须声明至少一条成功验证命令")

    successful_commands = {
        record.command for record in relevant if record.exit_code == 0
    }
    for command in claim.verification_commands:
        if command not in successful_commands:
            problems.append("声明的验证没有对应成功证据：" + " ".join(command))
    return problems
