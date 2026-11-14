# ADR 0001: Multi-Agent Architecture, Local MCP Transport, and Persistent State

- **Status:** Accepted
- **Date:** 2026-11-14
- **Milestone:** 0 (Skeleton and ADR)

## Context

We are building `incident-responder-agent`, a local-first multi-agent system capable of diagnosing simulated infrastructure incidents and executing remediation actions through a custom Model Context Protocol (MCP) server. The project runs on Windows 11 with zero cloud API budget, relying exclusively on local LLMs via Ollama.

Because remediation actions modify infrastructure state (restarting services, scaling replicas, rolling back deployments), the system cannot execute write tools blindly. It requires:
1. Strict separation between read-only diagnostics and state-mutating remediation.
2. Mandatory Human-in-the-Loop (HITL) approval before executing write operations.
3. Durable execution state capable of pausing for human input and resuming cleanly without losing context.

## Decision

1. **Multi-Agent Orchestration (LangGraph Supervisor Pattern):** We use LangGraph to coordinate specialized subagents (Diagnostics Agent and Remediation Agent) under a Supervisor, rather than a single monolithic ReAct loop.
2. **Tool Interface (Custom MCP Server over `stdio`):** Infrastructure tools live in an isolated local MCP server communicating over `stdio` (JSON-RPC). All server logging is routed strictly to `stderr` so `stdout` remains clean for protocol frames on Windows.
3. **Persistence and HITL (`SqliteSaver` + `interrupt()`):** Graph state is persisted locally in SQLite (git-ignored) to support thread-based checkpointing and native LangGraph `interrupt()` pauses prior to any write tool execution.
4. **Package Layout and Environment (`src/` + `uv` + Pydantic Settings):** All source code lives under `src/incident_responder/` managed by `uv`, with typed configuration loaded from `.env` via `pydantic-settings`.

## Alternatives Considered

- **Single ReAct Agent with Direct Python Functions:** Simpler initially, but mixes diagnostic reasoning with destructive permissions in one prompt and couples tool execution tightly to the agent process.
- **MCP over HTTP/SSE:** Useful for remote multi-tenant tools, but adds unnecessary port management and network overhead for a single-machine local workflow.
- **In-Memory Checkpointer (`MemorySaver`):** Insufficient for realistic HITL workflows because process restarts wipe pending approvals.

## Consequences

- **Positive:** Clear blast-radius boundaries, deterministic unit testing with mocked LLMs, auditable state transitions, and zero external API costs.
- **Negative / Trade-offs:** Higher initial boilerplate (graph state schemas, MCP subprocess lifecycle on Windows, and checkpointer serialization) compared to a single-agent script.
