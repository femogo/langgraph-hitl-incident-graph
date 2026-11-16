# Progress Log — incident-responder-agent

## Completed Milestones
- **Milestone 0 — Skeleton & ADR (2026-11-14):** Initialized local Git repository (main) and Python project with uv (src/ layout), typed settings (src/incident_responder/config.py), smoke tests, and docs/adr/0001-architecture-and-mcp-transport.md.
- **Milestone 1 — Simulated Infra & stdio MCP Server (2026-11-15):** Built stateful JSON-backed SimulatedInfra (infra.py) and custom stdio MCP server (mcp_server.py, mcp==2.3.0) with 3 read-only and 3 write tools; verified in tests/test_mcp_server.py.
- **Milestone 2 — State & SQLite Checkpointer (2026-11-16):** Defined IncidentState (state.py) with add_messages reducer and SqliteSaver / AsyncSqliteSaver context managers; verified in tests/test_state_checkpoint.py.
- **Milestone 3 — Human-in-the-Loop (HITL) Approval Gate (2026-11-16):** Implemented interrupt() and Command(resume=...) approval workflow in graph.py; verified in tests/test_hitl_approval.py (11 passed).
- **Milestone 4 — Multi-Agent Supervisor + Local Ollama + Live MCP Tools (2026-11-16):** Built mcp_tools.py (stdio MCP client + Pydantic input_schema wrapper splitting read_tools and write_tools) and supervisor.py (Supervisor orchestrator, read-only diagnose_agent, structured RemediationProposal planner, HITL interrupt(), and live MCP call_tool execution); verified in tests/test_supervisor_mcp.py (14 passed, ruff clean).

## Pinned Dependencies
- Python: 3.11.0 | mcp: 2.3.0 | langgraph: 1.2.22 | langchain-ollama: 1.1.0 | langgraph-checkpoint-sqlite: 3.1.3 | pydantic: 2.14.3 | pydantic-settings: 2.16.3 | pytest: 9.2.0 | ruff: 0.17.5

## Next Step (Milestone 5 — Guardrails & Audit Trail)
- Add input/action validation guardrails (service allowlists, replica bounds, rate/cooldown checks, and structured audit logging of every proposed, approved, rejected, and executed action).
## Hito 5 — Guardrails y Auditoría (Completado)
- **`src/incident_responder/guardrails.py`**:
  - `GuardrailPolicy` y `ActionGuardrails`: validación determinista de allowlist de servicios (`payment-api`, `auth-service`, `inventory-db`), allowlist de herramientas de escritura (`rollback_deployment`, `restart_service`, `scale_service`), límites de réplicas (`1..10`), ventana de enfriamiento (`cooldown_seconds`) por `(service, tool)` y límite máximo de acciones por hilo (`max_actions_per_thread`).
  - `AuditLogger`: registro estructurado en formato JSON Lines (`.incident_audit.jsonl`) con `timestamp`, `event` (`PROPOSED`, `GUARDRAIL_BLOCKED`, `APPROVED`, `REJECTED`, `EXECUTED`, `EXECUTION_FAILED`), `thread_id`, `service`, `tool`, `args`, `actor` y `details`.
- **`src/incident_responder/supervisor.py`**:
  - Validación de guardrails tras `remediation_planner` (bloqueando acciones fuera de política antes de molestar al operador humano) y defensa en profundidad justo antes de `execute_remediation`.
  - Trazabilidad completa de eventos en cada transición del ciclo de vida (`planner`, `guardrail`, `human`, `executor`).
- **`tests/test_guardrails_audit.py`**: 4 nuevos tests (18 passed en total, `ruff` limpio).
