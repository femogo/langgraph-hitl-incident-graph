# Project Progress — incident-responder-agent

## Completed Milestones
- **Milestone 0 — Skeleton & ADR (2026-11-14):**
  - Initialized local Git repository (main) and Python project with uv (src/ layout).
  - Configured .gitignore and .gitattributes (UTF-8 / LF normalization on Windows 11) ensuring .env, .venv/, and local SQLite/JSON runtime files are never tracked.
  - Built typed environment configuration with pydantic-settings (src/incident_responder/config.py) supporting local Ollama models via .env.
  - Added smoke & configuration override tests (tests/test_smoke.py) and verified linting with ruff.
  - Documented core architectural choices in docs/adr/0001-architecture-and-mcp-transport.md.
- **Milestone 1 — Simulated Infrastructure + stdio MCP Server (2026-11-16):**
  - Installed official MCP Python SDK (mcp==2.3.0) and pytest-asyncio.
  - Built deterministic, JSON-persisted simulated infrastructure (src/incident_responder/infra.py) with 3 microservices (auth-service, degraded payment-api with v2.3.1 memory leak, inventory-db).
  - Implemented custom stdio MCP server using MCPServer (src/incident_responder/mcp_server.py) exposing 3 read-only diagnostic tools and 3 state-mutating remediation tools, routing all logs strictly to stderr.
  - Added end-to-end stdio subprocess integration tests (tests/test_mcp_server.py); all 5 tests passing and ruff clean.

## Key Decisions & Rationale
- **Supervisor Multi-Agent Graph (LangGraph):** Separates read-only diagnostics from state-mutating remediation instead of using a single monolithic ReAct loop.
- **Custom MCP Server over stdio (mcp 2.x MCPServer):** Isolates infrastructure tools in a local subprocess; server logs go strictly to stderr so stdout carries clean JSON-RPC frames on Windows.
- **Persistent Checkpointing + HITL (SqliteSaver + interrupt()):** Enables pausing execution prior to write actions and resuming across process restarts.

## Pinned Dependencies
- Python: 3.11.0 | mcp: 2.3.0 | pydantic: 2.14.3 | pydantic-settings: 2.16.3 | pytest: 9.2.0 | pytest-asyncio: 1.3.0 | ruff: 0.17.5

## Next Step (Milestone 2)
- Install LangGraph + Ollama integration (langgraph, langchain-ollama, langgraph-checkpoint-sqlite) and build the multi-agent Supervisor graph with Human-in-the-Loop interrupt() gates before write tools.
