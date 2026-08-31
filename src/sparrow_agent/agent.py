"""Sparrow 的可验证 Agent 主循环。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from sparrow_agent.completion import CompletionGate
from sparrow_agent.context import Context
from sparrow_agent.evidence import EvidenceLedger
from sparrow_agent.models import AgentResult, MessageRole, StopReason, ToolCall, ToolResult
from sparrow_agent.provider import (
    ModelResponse,
    ModelProvider,
    ProviderError,
    ProviderRequestError,
)
from sparrow_agent.recording import EventRecorder, NullRecorder
from sparrow_agent.tools import ToolRegistry
from sparrow_agent.workspace import Workspace

DEFAULT_SYSTEM_PROMPT = """你是 Sparrow，一个在本地工具边界内工作的编程智能体。
先检查项目再修改；每次工具失败后根据观察调整，不要假装操作成功。
小范围修改优先使用 replace_text；新增文件或多文件修改可使用 apply_patch。
修改代码后必须运行合适的验证。只有任务确实完成时，才能调用 request_completion；
自然语言结论不会结束任务，完成申请必须如实列出修改文件、成功验证命令和剩余风险。"""


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Agent 循环、预算、重试和重复动作边界。"""

    max_iterations: int = 20
    repeated_action_limit: int = 3
    max_total_tokens: int = 400_000
    provider_retries: int = 2
    retry_base_seconds: float = 0.5
    max_observation_characters: int = 30_000
    max_context_characters: int = 120_000

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations 必须大于 0")
        if self.repeated_action_limit < 2:
            raise ValueError("repeated_action_limit 必须至少为 2")
        if self.max_total_tokens < 1:
            raise ValueError("max_total_tokens 必须大于 0")
        if self.provider_retries < 0:
            raise ValueError("provider_retries 不能为负数")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds 不能为负数")
        if self.max_observation_characters < 100:
            raise ValueError("max_observation_characters 不能小于 100")
        if self.max_context_characters < 500:
            raise ValueError("max_context_characters 不能小于 500")


class Agent:
    """编排 Provider、工具、上下文和完成门，不隐藏任何循环状态。"""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        settings: AgentSettings | None = None,
        completion_gate: CompletionGate | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        sleeper: Callable[[float], None] = time.sleep,
        recorder: EventRecorder | None = None,
        workspace: Workspace | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._settings = settings or AgentSettings()
        self._completion_gate = completion_gate or CompletionGate()
        self._system_prompt = system_prompt
        self._sleeper = sleeper
        self._recorder = recorder or NullRecorder()
        self._workspace = workspace
        self._is_cancelled = is_cancelled or (lambda: False)
        schemas = tools.model_schemas()
        names = {schema["function"]["name"] for schema in schemas}
        if self._completion_gate.spec.name in names:
            raise ValueError("ToolRegistry 不能注册保留工具 request_completion")
        self._model_tools = tuple(
            [*schemas, self._completion_gate.spec.to_model_schema()]
        )
        self.last_context: Context | None = None
        self.last_evidence: EvidenceLedger | None = None
        self._reported_compacted_turns = 0

    def run(self, task: str, *, context: Context | None = None) -> AgentResult:
        """执行一轮任务；传入上下文时把任务作为新的用户消息继续对话。"""

        task = task.strip()
        if not task:
            raise ValueError("任务不能为空")
        if context is None:
            context = Context(
                self._system_prompt,
                task,
                max_observation_characters=self._settings.max_observation_characters,
                max_context_characters=self._settings.max_context_characters,
            )
        else:
            context.append_user(task)
        baseline_snapshot = (
            self._workspace.snapshot() if self._workspace is not None else None
        )
        evidence = EvidenceLedger(baseline_snapshot=baseline_snapshot)
        self.last_context = context
        self.last_evidence = evidence
        self._reported_compacted_turns = 0
        total_tokens = 0
        last_action_signature: str | None = None
        repeated_actions = 0
        last_visible_text = ""
        self._recorder.record(
            "run_started",
            {
                "task": task,
                "max_iterations": self._settings.max_iterations,
                "max_total_tokens": self._settings.max_total_tokens,
                "max_context_characters": self._settings.max_context_characters,
                "snapshot_files": (
                    len(baseline_snapshot.entries)
                    if baseline_snapshot is not None
                    else None
                ),
            },
        )
        if self._is_cancelled():
            return self._finish(_cancelled_result(0))

        for iteration in range(1, self._settings.max_iterations + 1):
            if self._is_cancelled():
                return self._finish(_cancelled_result(iteration - 1))
            response_or_error = self._request_model(context, iteration)
            if self._is_cancelled():
                return self._finish(_cancelled_result(iteration))
            if isinstance(response_or_error, ProviderError):
                return self._finish(
                    AgentResult(
                        stop_reason=StopReason.PROVIDER_ERROR,
                        final_text=str(response_or_error),
                        iterations=iteration,
                    )
                )
            response = response_or_error
            context.append_assistant(response.message)
            self._recorder.record(
                "model_response",
                {
                    "iteration": iteration,
                    "finish_reason": response.finish_reason,
                    "model": response.model,
                    "response_id": response.response_id,
                    "message": response.message.to_dict(),
                    "tool_call_count": len(response.message.tool_calls),
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "reasoning_tokens": response.usage.reasoning_tokens,
                        "total_tokens": response.usage.total_tokens,
                    },
                },
            )
            if response.message.content:
                last_visible_text = response.message.content
            total_tokens += response.usage.total_tokens
            if total_tokens > self._settings.max_total_tokens:
                return self._finish(
                    AgentResult(
                        stop_reason=StopReason.BUDGET_EXCEEDED,
                        final_text=(
                            f"累计 Token 用量 {total_tokens} 超过预算 "
                            f"{self._settings.max_total_tokens}"
                        ),
                        iterations=iteration,
                    )
                )

            calls = response.message.tool_calls
            if not calls:
                feedback = (
                    "你返回了自然语言答案，但尚未提交 request_completion。"
                    "如果任务已完成，请提交结构化完成申请；否则继续使用工具。"
                )
                context.append_control_feedback(feedback)
                self._recorder.record(
                    "control_feedback", {"iteration": iteration, "feedback": feedback}
                )
                continue

            structural_error = _tool_call_structure_error(calls)
            if structural_error is not None:
                for call in calls:
                    result = ToolResult.failure(structural_error)
                    context.append_tool_result(call, result)
                    event_index = evidence.record(call, result)
                    self._record_tool_result(iteration, event_index, call, result)
                    (
                        last_action_signature,
                        repeated_actions,
                        repeated,
                    ) = _update_repetition(
                        call,
                        result,
                        last_action_signature,
                        repeated_actions,
                        self._settings.repeated_action_limit,
                    )
                    if repeated:
                        return self._finish(_repeated_result(iteration))
                continue

            call = calls[0] if len(calls) == 1 else None
            if call is not None and call.name == self._completion_gate.spec.name:
                if self._workspace is not None:
                    snapshot_index, snapshot_changes = evidence.observe_snapshot(
                        self._workspace.snapshot()
                    )
                    if snapshot_changes:
                        self._recorder.record(
                            "workspace_changed",
                            {
                                "iteration": iteration,
                                "event_index": snapshot_index,
                                "changed_since_previous_snapshot": sorted(
                                    snapshot_changes
                                ),
                                "changed_since_run_start": sorted(
                                    evidence.changed_files
                                ),
                                "source": "completion_refresh",
                            },
                        )
                decision = self._completion_gate.evaluate(call, evidence)
                context.append_tool_result(call, decision.result)
                event_index = evidence.record(call, decision.result)
                self._record_tool_result(
                    iteration, event_index, call, decision.result
                )
                if decision.accepted:
                    assert decision.completion_request is not None
                    return self._finish(
                        AgentResult(
                            stop_reason=StopReason.COMPLETED,
                            final_text=decision.completion_request.summary,
                            iterations=iteration,
                            completion_request=decision.completion_request,
                        )
                    )
                (
                    last_action_signature,
                    repeated_actions,
                    repeated,
                ) = _update_repetition(
                    call,
                    decision.result,
                    last_action_signature,
                    repeated_actions,
                    self._settings.repeated_action_limit,
                )
                if repeated:
                    return self._finish(_repeated_result(iteration))
                continue

            for tool_call in calls:
                if self._is_cancelled():
                    return self._finish(_cancelled_result(iteration))
                result = self._tools.execute(tool_call)
                snapshot = (
                    self._workspace.snapshot()
                    if self._workspace is not None
                    else None
                )
                event_index = evidence.record(tool_call, result, snapshot)
                if snapshot is not None:
                    result = _with_workspace_evidence(result, evidence)
                context.append_tool_result(tool_call, result)
                self._record_tool_result(
                    iteration, event_index, tool_call, result
                )
                (
                    last_action_signature,
                    repeated_actions,
                    repeated,
                ) = _update_repetition(
                    tool_call,
                    result,
                    last_action_signature,
                    repeated_actions,
                    self._settings.repeated_action_limit,
                )
                if repeated:
                    return self._finish(_repeated_result(iteration))

        final_text = "达到最大迭代次数，任务未通过完成证据检查"
        if last_visible_text:
            final_text += f"。最后一次模型输出：{last_visible_text}"
        return self._finish(
            AgentResult(
                stop_reason=StopReason.MAX_ITERATIONS,
                final_text=final_text,
                iterations=self._settings.max_iterations,
            )
        )

    def _request_model(
        self, context: Context, iteration: int
    ) -> ModelResponse | ProviderError:
        messages = context.messages
        if context.compacted_turns > self._reported_compacted_turns:
            newly_compacted = (
                context.compacted_turns - self._reported_compacted_turns
            )
            self._recorder.record(
                "context_compacted",
                {
                    "iteration": iteration,
                    "newly_compacted_turns": newly_compacted,
                    "total_compacted_turns": context.compacted_turns,
                    "retained_messages": len(messages),
                    "estimated_characters": context.estimated_characters,
                    "summary_characters": context.summary_characters,
                    "max_context_characters": self._settings.max_context_characters,
                },
            )
            self._reported_compacted_turns = context.compacted_turns
        for attempt in range(self._settings.provider_retries + 1):
            try:
                return self._provider.complete(messages, self._model_tools)
            except ProviderRequestError as exc:
                if not exc.retryable or attempt == self._settings.provider_retries:
                    self._recorder.record(
                        "provider_error",
                        {
                            "iteration": iteration,
                            "attempt": attempt + 1,
                            "retryable": exc.retryable,
                            "error": str(exc),
                        },
                    )
                    return exc
                delay = self._settings.retry_base_seconds * (2**attempt)
                self._recorder.record(
                    "provider_retry",
                    {
                        "iteration": iteration,
                        "attempt": attempt + 1,
                        "next_attempt": attempt + 2,
                        "delay_seconds": delay,
                        "error": str(exc),
                    },
                )
                self._sleeper(delay)
            except ProviderError as exc:
                self._recorder.record(
                    "provider_error",
                    {
                        "iteration": iteration,
                        "attempt": attempt + 1,
                        "retryable": False,
                        "error": str(exc),
                    },
                )
                return exc
        raise AssertionError("Provider 重试循环不应到达此处")

    def _record_tool_result(
        self,
        iteration: int,
        event_index: int,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        self._recorder.record(
            "tool_result",
            {
                "iteration": iteration,
                "event_index": event_index,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "arguments": call.to_dict(),
                **result.to_dict(),
            },
        )

    def _finish(self, result: AgentResult) -> AgentResult:
        completion = result.completion_request
        completion_data = None
        if completion is not None:
            completion_data = {
                "summary": completion.summary,
                "changed_files": completion.changed_files,
                "verifications": [
                    {
                        "command": record.command,
                        "exit_code": record.exit_code,
                        "event_index": record.event_index,
                        "output_summary": record.output_summary,
                    }
                    for record in completion.verifications
                ],
                "remaining_risks": completion.remaining_risks,
            }
        self._recorder.record(
            "run_finished",
            {
                "stop_reason": result.stop_reason.value,
                "final_text": result.final_text,
                "iterations": result.iterations,
                "completion_request": completion_data,
            },
        )
        return result


def _with_workspace_evidence(
    result: ToolResult, evidence: EvidenceLedger
) -> ToolResult:
    metadata = {
        **result.metadata,
        "workspace_changed_files": tuple(sorted(evidence.changed_files)),
        "changed_since_previous_snapshot": tuple(
            sorted(evidence.last_observed_changes)
        ),
    }
    return ToolResult(
        ok=result.ok,
        output=result.output,
        error=result.error,
        metadata=metadata,
    )


def _tool_call_structure_error(calls: tuple[ToolCall, ...]) -> str | None:
    ids = [call.id for call in calls]
    if len(ids) != len(set(ids)):
        return "同一助手消息中的 tool_call id 必须唯一"
    completion_count = sum(call.name == "request_completion" for call in calls)
    if completion_count and len(calls) != 1:
        return "request_completion 不能与其他工具调用混在同一助手消息中"
    return None


def _update_repetition(
    call: ToolCall,
    result: ToolResult,
    previous_signature: str | None,
    previous_count: int,
    limit: int,
) -> tuple[str, int, bool]:
    payload = json.dumps(
        {"call": call.fingerprint(), "result": result.to_dict()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    count = previous_count + 1 if signature == previous_signature else 1
    return signature, count, count >= limit


def _repeated_result(iteration: int) -> AgentResult:
    return AgentResult(
        stop_reason=StopReason.REPEATED_ACTION,
        final_text="检测到连续重复且结果相同的工具调用，已触发熔断",
        iterations=iteration,
    )


def _cancelled_result(iteration: int) -> AgentResult:
    return AgentResult(
        stop_reason=StopReason.CANCELLED,
        final_text="运行已收到取消请求，并在安全检查点停止",
        iterations=iteration,
    )
