from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from incident_responder.infra import DEFAULT_STATE_PATH, SimulatedInfra

# Route all server logs strictly to stderr so stdout remains clean for JSON-RPC frames.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("incident_responder.mcp_server")


def _get_infra() -> SimulatedInfra:
    custom_path = os.getenv("INFRA_STATE_PATH")
    state_path = Path(custom_path) if custom_path else DEFAULT_STATE_PATH
    return SimulatedInfra(state_path=state_path)


mcp = MCPServer(
    name="incident-responder-infra",
    instructions=(
        "Simulated production infrastructure server. Use read-only tools "
        "(list_services, get_service_status, get_service_logs) to diagnose incidents "
        "before invoking state-mutating remediation tools."
    ),
)


# --- Read-only diagnostic tools ---


@mcp.tool()
def list_services() -> list[dict[str, object]]:
    """List all infrastructure services with their current health status, replicas, and latency."""
    logger.info("Tool invoked: list_services")
    return _get_infra().list_services()


@mcp.tool()
def get_service_status(service_name: str) -> dict[str, object]:
    """Get detailed health metrics (CPU, memory, latency, version) for a specific service."""
    logger.info("Tool invoked: get_service_status(service_name=%s)", service_name)
    return _get_infra().get_service_status(service_name)


@mcp.tool()
def get_service_logs(service_name: str, limit: int = 10) -> dict[str, object]:
    """Fetch recent log entries for a specific service to diagnose errors."""
    logger.info("Tool invoked: get_service_logs(service_name=%s, limit=%s)", service_name, limit)
    return _get_infra().get_service_logs(service_name, limit=limit)


# --- State-mutating remediation tools ---


@mcp.tool()
def restart_service(service_name: str) -> dict[str, object]:
    """Restart a service to clear transient memory/CPU pressure and restore health."""
    logger.info("Tool invoked: restart_service(service_name=%s)", service_name)
    return _get_infra().restart_service(service_name)


@mcp.tool()
def scale_service(service_name: str, replicas: int) -> dict[str, object]:
    """Scale a service horizontally to the target number of replicas (1 to 10)."""
    logger.info("Tool invoked: scale_service(service_name=%s, replicas=%s)", service_name, replicas)
    return _get_infra().scale_service(service_name, replicas=replicas)


@mcp.tool()
def rollback_deployment(service_name: str) -> dict[str, object]:
    """Roll back a service deployment to its previous stable version."""
    logger.info("Tool invoked: rollback_deployment(service_name=%s)", service_name)
    return _get_infra().rollback_deployment(service_name)


def main() -> None:
    logger.info("Starting incident-responder MCP server over stdio")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
