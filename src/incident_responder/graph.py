from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from incident_responder.infra import DEFAULT_STATE_PATH, SimulatedInfra
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


def _select_remediation(service_name: str, latest_text: str) -> dict[str, Any]:
    lowered = latest_text.lower()
    if "scale" in lowered or "replicas" in lowered or "traffic" in lowered:
        return {
            "tool": "scale_service",
            "args": {"name": service_name, "replicas": 4},
            "reason": f"High traffic/load reported on {service_name}; scaling to 4 replicas.",
        }
    if "restart" in lowered or "memory" in lowered or "leak" in lowered:
        return {
            "tool": "restart_service",
            "args": {"name": service_name},
            "reason": f"Transient resource pressure on {service_name}; graceful restart recommended.",
        }
    return {
        "tool": "rollback_deployment",
        "args": {"name": service_name},
        "reason": f"Degradation detected on {service_name}; rolling back to previous stable release.",
    }


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


def propose_remediation_node(state: IncidentState) -> dict[str, Any]:
    """Prepare a state-mutating remediation proposal if a target service is identified."""
    target = state.get("target_service")
    if not target:
        return {
            "proposed_action": None,
            "resolution_status": "investigating",
        }

    latest_text = ""
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            latest_text = str(msg.content)
            break

    proposal = _select_remediation(target, latest_text)
    reply = AIMessage(
        content=(
            f"[Proposal] Recommended action '{proposal['tool']}' on '{target}' "
            f"with args {proposal['args']}. Awaiting human operator approval."
        )
    )
    return {
        "messages": [reply],
        "proposed_action": proposal,
        "resolution_status": "awaiting_approval",
    }


def human_approval_node(state: IncidentState) -> dict[str, Any]:
    """Pause execution via interrupt() until an operator approves or rejects proposed_action."""
    proposed = state.get("proposed_action")
    if not proposed:
        return {}

    # No side effects before interrupt(): LangGraph re-executes this node from the top on resume.
    decision = interrupt(
        {
            "type": "remediation_approval",
            "target_service": state.get("target_service"),
            "proposed_action": proposed,
            "instructions": "Resume with Command(resume={'approved': True/False, 'note': '...'})",
        }
    )

    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
        note = str(decision.get("note", "")).strip()
    else:
        approved = bool(decision)
        note = ""

    suffix = f" Note: {note}" if note else ""
    if approved:
        return {
            "messages": [
                AIMessage(content=f"[HITL] Operator APPROVED '{proposed['tool']}'.{suffix}")
            ],
            "resolution_status": "awaiting_approval",
        }

    return {
        "messages": [
            AIMessage(content=f"[HITL] Operator REJECTED '{proposed['tool']}'.{suffix}")
        ],
        "resolution_status": "aborted",
    }


def route_after_approval(
    state: IncidentState,
) -> Literal["execute_remediation", "abort_remediation", "__end__"]:
    """Route approved proposals to execution, rejected ones to abort, or end if none."""
    if not state.get("proposed_action"):
        return "__end__"
    if state.get("resolution_status") == "aborted":
        return "abort_remediation"
    return "execute_remediation"


def _make_execute_remediation_node(infra_state_path: Path):
    def execute_remediation_node(state: IncidentState) -> dict[str, Any]:
        proposed = state.get("proposed_action")
        if not proposed:
            return {"resolution_status": "aborted"}

        infra = SimulatedInfra(state_path=infra_state_path)
        tool_name = str(proposed["tool"])
        args = dict(proposed.get("args", {}))

        if tool_name == "rollback_deployment":
            outcome = infra.rollback_deployment(name=str(args["name"]))
        elif tool_name == "restart_service":
            outcome = infra.restart_service(name=str(args["name"]))
        elif tool_name == "scale_service":
            outcome = infra.scale_service(
                name=str(args["name"]),
                replicas=int(args["replicas"]),
            )
        else:
            raise ValueError(f"Unsupported remediation tool: {tool_name}")

        reply = AIMessage(
            content=f"[Remediation] Executed {tool_name}: {outcome.get('message', outcome)}"
        )
        return {
            "messages": [reply],
            "proposed_action": None,
            "resolution_status": "resolved",
        }

    return execute_remediation_node


def abort_remediation_node(state: IncidentState) -> dict[str, Any]:
    """Close the incident cleanly without mutating infrastructure."""
    proposed = state.get("proposed_action") or {}
    tool_name = proposed.get("tool", "unknown_action")
    reply = AIMessage(
        content=f"[Aborted] Cancelled '{tool_name}'. Infrastructure left untouched."
    )
    return {
        "messages": [reply],
        "proposed_action": None,
        "resolution_status": "aborted",
    }


def build_incident_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    infra_state_path: Path = DEFAULT_STATE_PATH,
) -> CompiledStateGraph[Any]:
    """Compile the stateful HITL incident graph with an optional checkpointer."""
    builder = StateGraph(IncidentState)
    builder.add_node("intake", intake_node)
    builder.add_node("triage", triage_node)
    builder.add_node("propose_remediation", propose_remediation_node)
    builder.add_node("human_approval", human_approval_node)
    builder.add_node("execute_remediation", _make_execute_remediation_node(infra_state_path))
    builder.add_node("abort_remediation", abort_remediation_node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "triage")
    builder.add_edge("triage", "propose_remediation")
    builder.add_edge("propose_remediation", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {
            "execute_remediation": "execute_remediation",
            "abort_remediation": "abort_remediation",
            "__end__": END,
        },
    )
    builder.add_edge("execute_remediation", END)
    builder.add_edge("abort_remediation", END)

    return builder.compile(checkpointer=checkpointer)


def run_demo_checkpoint(
    db_path: str | Path,
    thread_id: str,
    report: str,
    infra_state_path: Path = DEFAULT_STATE_PATH,
) -> IncidentState:
    """Run one turn against a persistent SQLite checkpointer and return the saved state."""
    config = {"configurable": {"thread_id": thread_id}}
    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(
            checkpointer=checkpointer,
            infra_state_path=infra_state_path,
        )
        existing = graph.get_state(config)
        if existing.values:
            result = graph.invoke({"messages": [HumanMessage(content=report)]}, config=config)
        else:
            result = graph.invoke(initial_incident_state(HumanMessage(content=report)), config=config)
        return result  # type: ignore[return-value]
