# Sparrow Agent

Sparrow Agent is a small, inspectable coding agent built from first principles.
It asks a language model to choose actions, executes those actions locally, feeds
the results back to the model, and repeats until the programming task is done.

This repository is being developed for the 2026 NJU Software Institute
recommended-admission project assessment.

## Design goals

- **Understandable:** the agent loop and every tool are implemented in this repo.
- **Safe by default:** file operations stay inside the workspace; commands have
  time and output limits.
- **Verifiable:** the agent is encouraged to run tests before declaring success.
- **Auditable:** each model decision, tool call, result, and stop reason is logged.
- **Portable:** the model backend is isolated behind a small provider interface.

The default backend is DeepSeek-V4-Pro, accessed through DeepSeek's
OpenAI-compatible Chat Completions API. DeepSeek-V4-Flash can be selected for
faster, lower-cost development runs without changing the agent implementation.

## Planned workflow

```text
user task
  -> model request with tool schemas
  -> zero or more local tool calls
  -> tool results appended to conversation
  -> repeat until final answer or a hard limit is reached
```

The first release will provide workspace listing, file reading, text search,
patch-based editing, and bounded command execution. It will not use an agent
framework or any API-hosted file/code-execution tool.

## Status

Architecture established; implementation is in progress.

See [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) for the design rationale and
[ROADMAP.md](ROADMAP.md) for the delivery plan.
