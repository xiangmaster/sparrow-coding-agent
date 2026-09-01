"""多轮对话会话及其本地持久化测试。"""

import json
import os
from pathlib import Path

import pytest

from sparrow_agent.conversation import (
    ConversationConfig,
    ConversationError,
    ConversationSession,
    ConversationState,
    ConversationStore,
    TurnStatus,
)
from sparrow_agent.models import Message, MessageRole, StopReason, ToolCall
from sparrow_agent.provider import ModelResponse, ScriptedProvider, TokenUsage


def _write_env(path: Path) -> None:
    (path / ".env").write_text(
        "DEEPSEEK_API_KEY=test-key\nSPARROW_MODEL=deepseek-v4-flash\n",
        encoding="utf-8",
    )


def _completion(call_id: str, summary: str, *, tokens: int = 0) -> ModelResponse:
    return ModelResponse(
        message=Message(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=(
                ToolCall(
                    id=call_id,
                    name="request_completion",
                    arguments={
                        "summary": summary,
                        "changed_files": [],
                        "verification_commands": [],
                        "remaining_risks": [],
                    },
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=tokens),
    )


def test_conversation_reuses_context_across_multiple_turns(tmp_path: Path) -> None:
    _write_env(tmp_path)
    provider = ScriptedProvider(
        [_completion("done-1", "第一轮完成", tokens=11), _completion("done-2", "第二轮完成", tokens=13)]
    )
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
        provider_factory=lambda settings: provider,
    )

    first = session.run_turn("先解释这个项目")
    second = session.run_turn("继续检查异常处理")

    assert first.stop_reason is StopReason.COMPLETED
    assert second.stop_reason is StopReason.COMPLETED
    assert session.state is ConversationState.IDLE
    assert len(session.thread.turns) == 2
    assert [turn.status for turn in session.thread.turns] == [
        TurnStatus.COMPLETED,
        TurnStatus.COMPLETED,
    ]
    assert [turn.total_tokens for turn in session.thread.turns] == [11, 13]
    second_request = provider.requests[1].messages
    assert [message.content for message in second_request if message.role is MessageRole.USER] == [
        "先解释这个项目",
        "继续检查异常处理",
    ]
    assert any(message.role is MessageRole.TOOL for message in second_request)
    assert any(
        call.id == "done-1"
        for message in second_request
        for call in message.tool_calls
    )
    assert all("thread_id" in event.data and "turn_id" in event.data for event in session.events)


def test_conversation_can_resume_persisted_context(tmp_path: Path) -> None:
    _write_env(tmp_path)
    first_provider = ScriptedProvider([_completion("done-1", "第一轮完成")])
    first_session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
        provider_factory=lambda settings: first_provider,
    )
    first_session.run_turn("检查入口")
    thread_id = first_session.thread.id

    second_provider = ScriptedProvider([_completion("done-2", "第二轮完成")])
    resumed = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
        thread_id=thread_id,
        provider_factory=lambda settings: second_provider,
    )
    resumed.run_turn("基于刚才的结论继续检查测试")

    users = [
        message.content
        for message in second_provider.requests[0].messages
        if message.role is MessageRole.USER
    ]
    assert users == ["检查入口", "基于刚才的结论继续检查测试"]
    loaded = ConversationStore(tmp_path).load(thread_id)
    assert len(loaded.turns) == 2
    assert loaded.turns[-1].assistant_text == "第二轮完成"


def test_conversation_restores_persisted_run_settings(tmp_path: Path) -> None:
    _write_env(tmp_path)
    original = ConversationSession(
        ConversationConfig(
            workspace=tmp_path,
            config_directory=tmp_path,
            model="deepseek-v4-flash",
            reasoning_effort="low",
            max_iterations=7,
            max_total_tokens=123_000,
            max_context_characters=9_000,
            record=False,
        ),
        provider_factory=lambda settings: ScriptedProvider(
            [_completion("done", "完成")]
        ),
    )
    original.run_turn("保存配置")

    resumed = ConversationSession(
        ConversationConfig(
            workspace=tmp_path,
            config_directory=tmp_path,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            max_iterations=20,
            max_total_tokens=400_000,
            max_context_characters=120_000,
            record=False,
        ),
        thread_id=original.thread.id,
        provider_factory=lambda settings: ScriptedProvider([]),
    )

    assert resumed.config.model == "deepseek-v4-flash"
    assert resumed.config.reasoning_effort == "low"
    assert resumed.config.max_iterations == 7
    assert resumed.config.max_total_tokens == 123_000
    assert resumed.config.max_context_characters == 9_000


def test_conversation_store_loads_legacy_thread_without_settings(tmp_path: Path) -> None:
    _write_env(tmp_path)
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
        provider_factory=lambda settings: ScriptedProvider(
            [_completion("done", "完成")]
        ),
    )
    session.run_turn("旧会话")
    path = tmp_path / ".sparrow" / "threads" / f"{session.thread.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("settings")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = ConversationStore(tmp_path).load(session.thread.id)

    assert loaded.model is None
    assert loaded.max_total_tokens is None


def test_conversation_token_budget_is_cumulative_across_turns(tmp_path: Path) -> None:
    _write_env(tmp_path)
    provider = ScriptedProvider([_completion("done", "第一轮", tokens=11)])
    session = ConversationSession(
        ConversationConfig(
            workspace=tmp_path,
            config_directory=tmp_path,
            max_total_tokens=11,
            record=False,
        ),
        provider_factory=lambda settings: provider,
    )

    first = session.run_turn("第一轮")
    second = session.run_turn("继续执行")

    assert first.stop_reason is StopReason.COMPLETED
    assert second.stop_reason is StopReason.BUDGET_EXCEEDED
    assert len(provider.requests) == 1
    assert [turn.total_tokens for turn in session.thread.turns] == [11, 0]


def test_conversation_persists_trace_reference_per_turn(tmp_path: Path) -> None:
    _write_env(tmp_path)
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path),
        provider_factory=lambda settings: ScriptedProvider(
            [_completion("done", "已完成")]
        ),
    )

    session.run_turn("记录这一轮")

    turn = session.thread.turns[0]
    assert turn.trace_path == f".sparrow/runs/{turn.id}.jsonl"
    assert (tmp_path / turn.trace_path).is_file()
    saved = json.loads(
        (tmp_path / ".sparrow" / "threads" / f"{session.thread.id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["schema_version"] == 1
    assert saved["settings"]["max_total_tokens"] == 400_000
    assert saved["turns"][0]["trace_path"] == turn.trace_path


def test_conversation_marks_failed_turn_and_allows_retry(tmp_path: Path) -> None:
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False)
    )

    with pytest.raises(Exception, match="配置文件不存在"):
        session.run_turn("第一次会失败")

    assert session.state is ConversationState.IDLE
    assert session.thread.turns[0].status is TurnStatus.FAILED
    _write_env(tmp_path)
    session._provider_factory = lambda settings: ScriptedProvider(
        [_completion("done", "重试成功")]
    )
    result = session.run_turn("配置后重试")
    assert result.stop_reason is StopReason.COMPLETED


def test_conversation_store_rejects_invalid_id_and_workspace(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path)
    with pytest.raises(ConversationError, match="格式无效"):
        store.load("../escape")

    other = tmp_path / "other"
    other.mkdir()
    _write_env(tmp_path)
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
        provider_factory=lambda settings: ScriptedProvider(
            [_completion("done", "完成")]
        ),
    )
    session.run_turn("创建对话")
    source = tmp_path / ".sparrow" / "threads" / f"{session.thread.id}.json"
    target_dir = other / ".sparrow" / "threads"
    target_dir.mkdir(parents=True)
    target = target_dir / source.name
    target.write_bytes(source.read_bytes())

    with pytest.raises(ConversationError, match="不属于当前工作区"):
        ConversationStore(other).load(session.thread.id)


def test_conversation_store_discovers_recent_valid_threads(tmp_path: Path) -> None:
    _write_env(tmp_path)
    sessions = []
    for index in range(2):
        session = ConversationSession(
            ConversationConfig(workspace=tmp_path, config_directory=tmp_path, record=False),
            provider_factory=lambda settings, index=index: ScriptedProvider(
                [_completion(f"done-{index}", f"完成 {index}")]
            ),
        )
        session.run_turn(f"任务 {index}")
        sessions.append(session)
    first_path = (
        tmp_path / ".sparrow" / "threads" / f"{sessions[0].thread.id}.json"
    )
    os.utime(first_path, (1, 1))
    (first_path.parent / "broken.json").write_text("bad", encoding="utf-8")

    discovered = ConversationStore(tmp_path).discover()

    assert [thread.title for thread in discovered] == ["任务 1", "任务 0"]
    with pytest.raises(ValueError, match="limit"):
        ConversationStore(tmp_path).discover(limit=0)


def test_conversation_store_moves_thread_and_traces_to_trash(tmp_path: Path) -> None:
    _write_env(tmp_path)
    session = ConversationSession(
        ConversationConfig(workspace=tmp_path, config_directory=tmp_path),
        provider_factory=lambda settings: ScriptedProvider(
            [_completion("done", "完成")]
        ),
    )
    session.run_turn("可删除任务")
    thread_path = tmp_path / ".sparrow" / "threads" / f"{session.thread.id}.json"
    trace_path = tmp_path / session.thread.turns[0].trace_path

    trash = ConversationStore(tmp_path).move_to_trash(session.thread.id)

    assert not thread_path.exists()
    assert not trace_path.exists()
    assert (trash / thread_path.name).is_file()
    assert (trash / trace_path.name).is_file()
    assert ConversationStore(tmp_path).discover() == ()
