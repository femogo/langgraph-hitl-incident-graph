from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import add_messages

ResolutionStatus = Literal[
    "investigating",
    "awaiting_approval",
    "resolved",
    "aborted",
]


class IncidentState(TypedDict):
    """Shared state passed across nodes in the incident-responder graph."""

    messages: Annotated[list[AnyMessage], add_messages]
    incident_summary: str
    target_service: str | None
    proposed_action: dict[str, Any] | None
    resolution_status: ResolutionStatus


def initial_incident_state(user_message: AnyMessage) -> IncidentState:
    """Build a clean initial state for a newly reported incident."""
    return {
        "messages": [user_message],
        "incident_summary": "",
        "target_service": None,
        "proposed_action": None,
        "resolution_status": "investigating",
    }


@contextmanager
def open_sqlite_checkpointer(db_path: str | Path) -> Iterator[SqliteSaver]:
    """Open a synchronous SQLite checkpointer, creating parent dirs if needed."""
    path_str = str(db_path)
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(path_str) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def open_async_sqlite_checkpointer(
    db_path: str | Path,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Open an async SQLite checkpointer (required for async MCP tool calls)."""
    path_str = str(db_path)
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(path_str) as checkpointer:
        yield checkpointer
