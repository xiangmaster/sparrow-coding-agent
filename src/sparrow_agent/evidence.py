"""从本地工具结果提取可复核的修改与验证证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from sparrow_agent.models import ToolCall, ToolResult, VerificationRecord
from sparrow_agent.workspace import WorkspaceSnapshot

_MAX_OUTPUT_SUMMARY_CHARACTERS = 500


@dataclass(slots=True)
class EvidenceLedger:
    """按事件顺序记录实际发生的修改和命令验证。"""

    baseline_snapshot: WorkspaceSnapshot | None = None
    event_index: int = 0
    reported_changed_files: set[str] = field(default_factory=set)
    last_mutation_index: int | None = None
    verifications: list[VerificationRecord] = field(default_factory=list)
    current_snapshot: WorkspaceSnapshot | None = None
    last_observed_changes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        self.current_snapshot = self.baseline_snapshot

    @property
    def changed_files(self) -> set[str]:
        """返回相对运行起点的真实差异；无快照时兼容工具上报证据。"""

        if self.baseline_snapshot is not None and self.current_snapshot is not None:
            return set(self.baseline_snapshot.changed_paths(self.current_snapshot))
        return set(self.reported_changed_files)

    def record(
        self,
        call: ToolCall,
        result: ToolResult,
        snapshot: WorkspaceSnapshot | None = None,
    ) -> int:
        self.event_index += 1
        current = self.event_index
        observed_changes = self._update_snapshot(snapshot)
        if observed_changes:
            self.last_mutation_index = current
        if result.ok:
            changed_files = _changed_files(result.metadata)
            if changed_files:
                self.reported_changed_files.update(changed_files)
                if self.baseline_snapshot is None:
                    self.last_mutation_index = current

        verification = _verification_record(current, result)
        if verification is not None:
            self.verifications.append(verification)
        return current

    def observe_snapshot(
        self, snapshot: WorkspaceSnapshot
    ) -> tuple[int | None, frozenset[str]]:
        """在完成申请前捕获工具之外发生的变化，并使旧验证失效。"""

        changes = self._update_snapshot(snapshot)
        if not changes:
            return None, changes
        self.event_index += 1
        self.last_mutation_index = self.event_index
        return self.event_index, changes

    def _update_snapshot(
        self, snapshot: WorkspaceSnapshot | None
    ) -> frozenset[str]:
        if snapshot is None or self.current_snapshot is None:
            self.last_observed_changes = frozenset()
            if snapshot is not None:
                self.current_snapshot = snapshot
            return self.last_observed_changes
        changes = self.current_snapshot.changed_paths(snapshot)
        self.current_snapshot = snapshot
        self.last_observed_changes = changes
        return changes

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
