# incident-responder-agent

Local-first multi-agent incident diagnosis and remediation system built with LangGraph, a custom Model Context Protocol (MCP) server, and Human-in-the-Loop (HITL) approval gates powered by local Ollama models at zero API cost.

See docs/adr/0001-architecture-and-mcp-transport.md for architectural decisions.

## Setup and Verification

- Copy-Item .env.example .env
- uv sync
- uv run pytest -v
- uv run ruff check .

## Milestone 3 Verification (Human-in-the-Loop Gate)

- Pause before mutation: human_approval calls interrupt() with the proposed tool and arguments, persisting awaiting_approval in SQLite without touching infrastructure.
- Resume with Command(resume=...): Approving executes remediation and marks the incident resolved; rejecting routes to abort_remediation and leaves infrastructure untouched.
- Verified: 11 passed in pytest, All checks passed in ruff.
