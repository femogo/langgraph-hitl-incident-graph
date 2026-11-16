from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from incident_responder.infra import SimulatedInfra


def _extract_tool_payload(result: Any) -> Any:
    """Extract structured or JSON-encoded text payload from an MCP CallToolResult."""
    if getattr(result, "structuredContent", None) is not None:
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    texts = [
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    ]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return [json.loads(t) for t in texts]


def test_infra_lifecycle(tmp_path: Path) -> None:
    state_file = tmp_path / "infra_state.json"
    infra = SimulatedInfra(state_path=state_file)

    services = {s["name"]: s for s in infra.list_services()}
    assert services["payment-api"]["status"] == "degraded"
    assert services["auth-service"]["status"] == "healthy"

    rollback_res = infra.rollback_deployment("payment-api")
    assert rollback_res["status"] == "healthy"
    assert rollback_res["current_version"] == "v2.3.0"

    # Re-instantiate from disk to verify persistence across calls
    reloaded = SimulatedInfra(state_path=state_file)
    status = reloaded.get_service_status("payment-api")
    assert status["status"] == "healthy"
    assert status["version"] == "v2.3.0"


@pytest.mark.asyncio
async def test_mcp_stdio_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "mcp_state.json"
    env = {**os.environ, "INFRA_STATE_PATH": str(state_file)}

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "incident_responder.mcp_server"],
        env=env,
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()

        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}
        assert tool_names == {
            "list_services",
            "get_service_status",
            "get_service_logs",
            "restart_service",
            "scale_service",
            "rollback_deployment",
        }

        # 1. Inspect logs of degraded service
        logs_res = await session.call_tool(
            "get_service_logs", {"service_name": "payment-api", "limit": 2}
        )
        assert not logs_res.is_error
        logs_data = _extract_tool_payload(logs_res)
        assert logs_data["status"] == "degraded"
        assert len(logs_data["logs"]) == 2

        # 2. Scale service and verify state persistence via subsequent read tool
        scale_res = await session.call_tool(
            "scale_service", {"service_name": "payment-api", "replicas": 4}
        )
        assert not scale_res.is_error
        scale_data = _extract_tool_payload(scale_res)
        assert scale_data["replicas"] == 4

        # 3. Restart service to clear memory pressure and restore healthy status
        restart_res = await session.call_tool(
            "restart_service", {"service_name": "payment-api"}
        )
        assert not restart_res.is_error

        status_res = await session.call_tool(
            "get_service_status", {"service_name": "payment-api"}
        )
        status_data = _extract_tool_payload(status_res)
        assert status_data["status"] == "healthy"
        assert status_data["replicas"] == 4


@pytest.mark.asyncio
async def test_mcp_unknown_service_returns_error(tmp_path: Path) -> None:
    state_file = tmp_path / "mcp_err_state.json"
    env = {**os.environ, "INFRA_STATE_PATH": str(state_file)}

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "incident_responder.mcp_server"],
        env=env,
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        res = await session.call_tool(
            "get_service_status", {"service_name": "nonexistent-service"}
        )
        assert res.is_error is True
