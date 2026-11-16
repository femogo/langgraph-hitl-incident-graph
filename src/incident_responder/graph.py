from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incident_responder.state import (
    IncidentState,
    initial_incident_state,
    open_sqlite_checkpointer,
)

KNOWN_SERVICES = ("payment-api", "auth-service", "inventory-db")


def _detect_service(text: str) -> str | None:
    lowered = text.lower()
    for service in KNOWN_SERVICES:
        if service in lowered:
            return service
    return None


def intake_node(state: IncidentState) -> dict[str, Any]:
    """Extract target service and build an initial incident summary from user input."""
    latest_text = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            latest_text = str(msg.content)
            break

    detected = _detect_service(latest_text) or state.get("target_service")
    summary = (
        f"Incident reported for {detected}: {latest_text}"
        if detected
        else f"Unclassified incident: {latest_text}"
    )
    return {
        "target_service": detected,
        "incident_summary": summary,
    }


def triage_node(state: IncidentState) -> dict[str, Any]:
    """Append a structured triage acknowledgment using the add_messages reducer."""
    target = state.get("target_service") or "unknown-service"
    reply = AIMessage(
        content=f"[Triage] Recorded incident for '{target}'. Ready for diagnostic inspection."
    )
    return {
        "messages": [reply],
        "resolution_status": "investigating",
    }


def build_incident_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any]:
    """Compile the Milestone 2 stateful incident graph with an optional checkpointer."""
    builder = StateGraph(IncidentState)
    builder.add_node("intake", intake_node)
    builder.add_node("triage", triage_node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "triage")
    builder.add_edge("triage", END)

    return builder.compile(checkpointer=checkpointer)


def run_demo_checkpoint(db_path: str | Path, thread_id: str, report: str) -> IncidentState:
    """Run one turn against a persistent SQLite checkpointer and return the saved state."""
    config = {"configurable": {"thread_id": thread_id}}
    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(checkpointer=checkpointer)
        existing = graph.get_state(config)
        if existing.values:
            result = graph.invoke({"messages": [HumanMessage(content=report)]}, config=config)
        else:
            result = graph.invoke(initial_incident_state(HumanMessage(content=report)), config=config)
        return result  # type: ignore[return-value]
