# Technical Design

## 1. Scope

Sparrow is a single-user command-line coding agent. Given a task and a workspace,
it can inspect files, edit code, run commands, observe results, and continue until
it can justify that the task is complete.

The project deliberately avoids GUI work, multi-agent orchestration, retrieval
systems, and server-hosted execution. The assessment values ownership of the
agent mechanics, so a reliable and explainable core is more important than a
large feature list.

## 2. Technology choices

| Area | Choice | Rationale |
| --- | --- | --- |
| Language | Python 3.11+ | Fast iteration, clear subprocess and filesystem APIs, easy review |
| CLI | `argparse` + standard library | Keeps the control flow visible and dependencies small |
| Model client | Official `openai` Python package | Vendor-supported transport; agent logic remains local |
| Model protocol | Chat Completions tool calling for v1 | Typed calls and broad compatibility with OpenAI-compatible gateways |
| Data model | `dataclasses` and typed dictionaries | Avoids hiding state transitions behind a framework |
| Testing | `pytest` | Concise unit and integration tests |
| Packaging | `pyproject.toml`, `src/` layout | Reproducible installation and clean imports |

The first provider calls `client.chat.completions.create(...)`. This endpoint is
chosen for the initial release because compatible gateways commonly implement
its tool-calling message format. The provider module is intentionally narrow: it
translates Sparrow's internal messages and tool definitions to the API format,
then translates the response back. The rest of the code does not depend on a
provider-specific response type, so a Responses API provider can be added later
without changing the agent loop or local tools.

Only custom function definitions are sent to the model. Sparrow does not enable
provider-hosted file search, code execution, shell, or patch tools; every action
against the workspace is dispatched and executed by code in this repository.

## 3. Components

```text
CLI
 `- loads configuration and task
 `- constructs Agent

Agent
 |- owns the iteration loop and limits
 |- sends conversation to ModelProvider
 |- validates and dispatches tool calls
 |- appends normalized results to Context
 `- decides and records why execution stopped

Context
 |- stores messages and tool observations
 |- estimates size and truncates oversized observations
 `- preserves the task, system rules, and recent causal chain

ToolRegistry
 |- exposes JSON schemas to the model
 |- validates names and arguments
 `- maps calls to local implementations

Workspace / Tools
 |- list and read files
 |- search text
 |- apply a patch
 `- run a bounded subprocess

RunLogger
 `- writes a human-readable and JSONL execution trace
```

## 4. Agent loop

1. Initialize the conversation with behavioral rules and the user's task.
2. Ask the model for either tool calls or a final response.
3. Reject malformed, unknown, or invalid tool calls as structured tool errors.
4. Execute valid calls locally and append bounded observations.
5. Continue until the model returns a final answer.
6. Stop defensively on maximum iterations, repeated identical calls, user
   interruption, unrecoverable provider error, or budget exhaustion.

The model's final answer is not by itself proof of completion. The system prompt
asks it to inspect relevant changes and run suitable tests. The trace records
whether verification actually occurred so the user can judge the result.

## 5. Initial tools

### `list_files`

Returns a bounded workspace tree. Hidden and ignored directories are excluded by
default to reduce noise and accidental secret exposure.

### `read_file`

Reads a UTF-8 text file with optional line bounds. Binary files and oversized
reads return explicit errors instead of silently consuming context.

### `search_files`

Searches text in the workspace and returns file, line number, and matched text.
Results are capped deterministically.

### `apply_patch`

Applies a unified diff after validating every affected path. Patch-based editing
makes changes compact, inspectable, and less likely to overwrite unrelated code.

### `run_command`

Runs a command in the workspace with a timeout, output cap, and captured exit
code. The initial implementation uses an argument list rather than a shell where
possible, avoiding implicit expansion and command substitution.

## 6. Safety boundaries

- Resolve every filesystem path and require it to remain below the workspace.
- Reject symlink escapes after canonical path resolution.
- Never read `.env`, common credential files, or VCS internals through tools.
- Bound file size, search count, subprocess duration, and captured output.
- Surface failures to the model as data; do not crash the whole loop.
- Require confirmation for a small set of destructive command patterns.
- Keep API credentials only in environment variables or ignored local files.

The project does not claim to be a secure sandbox. It is a guarded local agent,
and that limitation will be stated explicitly in the CLI and documentation.

## 7. Context management

The conversation is held locally so its behavior is testable and independent of
server-side conversation storage. Each observation has a character cap. When the
estimated context exceeds a threshold, Sparrow retains:

1. system rules and the original task;
2. a compact factual summary of older completed steps;
3. recent model messages, tool calls, and matching tool results.

Tool-call/result pairs are never separated. This preserves protocol validity and
the causal information needed for the next decision.

## 8. Error handling and termination

Provider errors are classified as retryable or terminal. Retryable failures use
bounded exponential backoff. Tool errors become structured observations. The
loop always ends with one explicit stop reason:

- `completed`
- `max_iterations`
- `repeated_action`
- `provider_error`
- `budget_exceeded`
- `cancelled`

This makes termination behavior easy to test and explain in the interview.

## 9. Testing strategy

- Unit tests for path containment, schemas, output truncation, and stop rules.
- Fake-provider tests for deterministic multi-step agent loops.
- Integration tests in temporary workspaces for file and command tools.
- A recorded demo fixture containing a real bug and an automated test suite.

No test needs a paid API call except an explicitly marked smoke test.

## 10. Deferred features

Streaming output, multiple providers, automatic context summarization, token-cost
accounting, and richer command approval policies come after the end-to-end loop.
They will only be added when the core remains stable and demonstrable.
