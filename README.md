# incident-responder-agent

Local-first multi-agent incident diagnosis and remediation system built with **LangGraph**, a custom **Model Context Protocol (MCP)** server, and **Human-in-the-Loop (HITL)** approval gates—powered by local **Ollama** models at zero API cost.

See [ADR 0001](docs/adr/0001-architecture-and-mcp-transport.md) for architectural decisions.

## Setup & Verification (Milestone 0)

`powershell
Copy-Item .env.example .env
uv sync
uv run pytest -v
uv run ruff check .
`
