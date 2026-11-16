# incident-responder-agent

Local-first multi-agent incident diagnosis and remediation system built with **LangGraph**, a custom **Model Context Protocol (MCP)** server, and **Human-in-the-Loop (HITL)** approval gates—powered by local **Ollama** models at zero API cost.

See [ADR 0001](docs/adr/0001-architecture-and-mcp-transport.md) for architectural decisions.

## Setup & Verification

`powershell
Copy-Item .env.example .env
uv sync
uv run pytest -v
uv run ruff check .
`

## Milestone 1: Simulated Infrastructure & Custom MCP Server (stdio)

- **Simulated Infrastructure (src/incident_responder/infra.py):** Models uth-service, payment-api (active memory leak on 2.3.1), and inventory-db with JSON state persistence.
- **MCP Server (src/incident_responder/mcp_server.py):** Exposes 6 tools over stdio using MCPServer (mcp==2.3.0):
  - *Read-only:* list_services, get_service_status, get_service_logs
  - *Remediation (mutating):* 
estart_service, scale_service, 
ollback_deployment
