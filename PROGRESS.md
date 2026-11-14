# Project Progress — incident-responder-agent

## Completed Milestones
- **Milestone 0 — Skeleton & ADR (2026-11-14):**
  - Initialized local Git repository (main) and Python project with uv (src/ layout).
  - Configured .gitignore and .gitattributes (UTF-8 / LF normalization on Windows 11) ensuring .env, .venv/, and local SQLite files are never tracked.
  - Built typed environment configuration with pydantic-settings (src/incident_responder/config.py) supporting local Ollama models via .env.
  - Added smoke & configuration override tests (tests/test_smoke.py) and verified linting with ruff.
  - Documented core architectural choices in docs/adr/0001-architecture-and-mcp-transport.md.

## Key Decisions & Rationale
- **Supervisor Multi-Agent Graph (LangGraph):** Separates read-only diagnostics from state-mutating remediation.
- **Custom MCP Server over stdio:** Isolates infrastructure tools in a local subprocess; server logs go strictly to stderr so stdout carries clean JSON-RPC frames.
- **Persistent Checkpointing + HITL (SqliteSaver + interrupt()):** Enables pausing execution prior to write actions and resuming across process restarts.

## Pinned Dependencies
- Python: 3.11.0 | pydantic: 2.14.3 | pydantic-settings: 2.16.3 | pytest: 9.2.0 | ruff: 0.17.5

## Next Step (Milestone 1)
- Install official mcp SDK (uv add mcp) and build the simulated infrastructure state + stdio MCP server.
