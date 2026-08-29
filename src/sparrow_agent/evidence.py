"""从本地工具结果提取可复核的修改与验证证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from sparrow_agent.models import ToolCall, ToolResult, VerificationRecord

_MAX_OUTPUT_SUMMARY_CHARACTERS = 500


@dataclass(slots=True)
class EvidenceLedger:
    """按事件顺序记录实际发生的修改和命令验证。"""

    event_index: int = 0
    changed_files: set[str] = field(default_factory=set)
    last_mutation_index: int | None = None
    verifications: list[VerificationRecord] = field(default_factory=list)

    def record(self, call: ToolCall, result: ToolResult) -> int:
        self.event_index += 1
        current = self.event_index
        if result.ok:
            changed_files = _changed_files(result.metadata)
            if changed_files:
                self.changed_files.update(changed_files)
                self.last_mutation_index = current

        verification = _verification_record(current, result)
        if verification is not None:
            self.verifications.append(verification)
        return current

    def verifications_after_last_mutation(self) -> tuple[VerificationRecord, ...]:
        if self.last_mutation_index is None:
            return tuple(self.verifications)
        return tuple(
            record
            for record in self.verifications
            if record.event_index > self.last_mutation_index
        )


def _changed_files(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    value = metadata.get("changed_files")
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _verification_record(
    event_index: int, result: ToolResult
) -> VerificationRecord | None:
    command = result.metadata.get("command")
    exit_code = result.metadata.get("exit_code")
    if (
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(item, str) or not item for item in command)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
    ):
        return None
    summary = result.output[:_MAX_OUTPUT_SUMMARY_CHARACTERS]
    return VerificationRecord(
        command=tuple(command),
        exit_code=exit_code,
        event_index=event_index,
        output_summary=summary,
    )
