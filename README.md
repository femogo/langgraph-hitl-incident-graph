# incident-responder-agent

Local-first multi-agent incident diagnosis and remediation system built with LangGraph, a custom Model Context Protocol (MCP) server, and Human-in-the-Loop (HITL) approval gates—powered by local Ollama models at zero API cost.

See docs/adr/0001-architecture-and-mcp-transport.md for architectural decisions.

## Setup & Verification

- Copy-Item .env.example .env
- uv sync
- uv run pytest -v
- uv run ruff check .

## Milestone 4 Verification (Supervisor + Ollama + Live stdio MCP)

- Tool isolation (mcp_tools.py): Dynamically wraps MCP stdio tools into LangChain StructuredTool instances and restricts diagnose_agent strictly to read-only tools (list_services, get_service_status, get_service_logs).
- Supervisor + Structured Proposal + HITL (supervisor.py): diagnose_agent inspects live MCP metrics/logs, remediation_planner outputs a validated RemediationProposal, human_approval pauses via interrupt(), and execute_remediation invokes the approved MCP write tool over stdio.
- Verified: 14 passed in pytest, All checks passed in ruff.

## Guardrails y Auditoría (Hito 5)

Antes de solicitar aprobación humana y nuevamente antes de mutar la infraestructura por MCP (defensa en profundidad), `ActionGuardrails` valida cada acción propuesta contra `GuardrailPolicy`:
- **Allowlists**: restringe los servicios y herramientas de escritura permitidas.
- **Límites de parámetros**: verifica que `scale_service` opere dentro del rango seguro de réplicas (`1..10`).
- **Cooldown & Rate-Limiting**: evita repetir la misma remediación dentro de la ventana de enfriamiento y limita el total de acciones por incidente.

Cada transición (`PROPOSED`, `GUARDRAIL_BLOCKED`, `APPROVED`, `REJECTED`, `EXECUTED`, `EXECUTION_FAILED`) queda registrada en `.incident_audit.jsonl` mediante `AuditLogger`.
