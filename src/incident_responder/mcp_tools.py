from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field, create_model

READ_TOOL_NAMES = frozenset({"list_services", "get_service_status", "get_service_logs"})
WRITE_TOOL_NAMES = frozenset({"restart_service", "scale_service", "rollback_deployment"})


def extract_tool_text(result: Any) -> str:
    """Extract plain text / JSON payload from an MCP CallToolResult."""
    chunks: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            chunks.append(str(text))
    if chunks:
        return "\n".join(chunks)
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured)
    return ""


async def call_mcp_tool(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Invoke a tool on an active MCP ClientSession and raise on tool error."""
    result = await session.call_tool(tool_name, arguments=arguments or {})
    payload = extract_tool_text(result)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool '{tool_name}' failed: {payload}")
    return payload


def _build_args_schema(tool_name: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """Convert an MCP JSON Schema dictionary into a Pydantic BaseModel for LangChain."""
    properties = input_schema.get("properties", {}) or {}
    required = set(input_schema.get("required", []) or [])
    fields: dict[str, Any] = {}

    type_map: dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }

    for prop_name, prop_meta in properties.items():
        json_type = prop_meta.get("type", "string")
        py_type = type_map.get(json_type, Any)
        description = prop_meta.get("description", "")
        if prop_name in required:
            fields[prop_name] = (py_type, Field(..., description=description))
        else:
            default_val = prop_meta.get("default", None)
            fields[prop_name] = (py_type | None, Field(default=default_val, description=description))

    model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Input"
    return create_model(model_name, **fields)


def _wrap_mcp_tool(
    session: ClientSession,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
) -> StructuredTool:
    """Wrap a single MCP server tool as an async LangChain StructuredTool."""
    args_schema = _build_args_schema(tool_name, input_schema)

    async def _invoke(**kwargs: Any) -> str:
        clean_args = {k: v for k, v in kwargs.items() if v is not None}
        return await call_mcp_tool(session, tool_name, clean_args)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=tool_name,
        description=description or f"MCP tool {tool_name}",
        args_schema=args_schema,
    )


async def load_mcp_tool_groups(
    session: ClientSession,
) -> tuple[list[StructuredTool], list[StructuredTool]]:
    """Discover tools from an initialized MCP session and split into (read_tools, write_tools)."""
    listed = await session.list_tools()
    read_tools: list[StructuredTool] = []
    write_tools: list[StructuredTool] = []

    for tool in listed.tools:
        lc_tool = _wrap_mcp_tool(
            session=session,
            tool_name=tool.name,
            description=tool.description or "",
            input_schema=tool.input_schema or {},
        )
        if tool.name in READ_TOOL_NAMES:
            read_tools.append(lc_tool)
        elif tool.name in WRITE_TOOL_NAMES:
            write_tools.append(lc_tool)

    return read_tools, write_tools


@asynccontextmanager
async def open_mcp_stdio_session(
    infra_state_path: Path | None = None,
) -> AsyncIterator[ClientSession]:
    """Spawn the local incident-responder MCP server over stdio and yield an initialized session."""
    env = os.environ.copy()
    if infra_state_path is not None:
        env["INFRA_STATE_PATH"] = str(infra_state_path)

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
        yield session
