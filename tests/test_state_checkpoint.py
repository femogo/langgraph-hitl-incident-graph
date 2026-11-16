from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from incident_responder.graph import build_incident_graph, run_demo_checkpoint
from incident_responder.state import initial_incident_state, open_sqlite_checkpointer


def test_state_reducer_accumulates_messages() -> None:
    graph = build_incident_graph(checkpointer=None)
    result = graph.invoke(
        initial_incident_state(HumanMessage(content="payment-api is returning 502 errors"))
    )

    assert result["target_service"] == "payment-api"
    assert "payment-api" in result["incident_summary"]
    assert result["resolution_status"] == "investigating"
    assert len(result["messages"]) == 2
    assert isinstance(result["messages"][0], HumanMessage)
    assert isinstance(result["messages"][1], AIMessage)


def test_sqlite_checkpointer_persists_across_restarts(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "inc-2026-001"

    # Turn 1: initial incident report
    state_turn_1 = run_demo_checkpoint(
        db_path=db_path,
        thread_id=thread_id,
        report="Alerts firing on payment-api latency",
    )
    assert len(state_turn_1["messages"]) == 2
    assert state_turn_1["target_service"] == "payment-api"

    # Turn 2: fresh connection & graph instance resuming the exact same thread_id
    state_turn_2 = run_demo_checkpoint(
        db_path=db_path,
        thread_id=thread_id,
        report="Memory usage just crossed 94%",
    )
    assert len(state_turn_2["messages"]) == 4
    assert "Alerts firing on payment-api latency" in str(state_turn_2["messages"][0].content)
    assert "Memory usage just crossed 94%" in str(state_turn_2["messages"][2].content)

    # Verify history snapshots exist in SQLite
    with open_sqlite_checkpointer(db_path) as checkpointer:
        reloaded_graph = build_incident_graph(checkpointer=checkpointer)
        history = list(reloaded_graph.get_state_history({"configurable": {"thread_id": thread_id}}))
        assert len(history) >= 2


def test_sqlite_checkpointer_isolates_threads(tmp_path: Path) -> None:
    db_path = tmp_path / "isolated.sqlite"

    pay_state = run_demo_checkpoint(
        db_path=db_path,
        thread_id="thread-payments",
        report="payment-api high memory",
    )
    auth_state = run_demo_checkpoint(
        db_path=db_path,
        thread_id="thread-auth",
        report="auth-service timeout spike",
    )

    assert pay_state["target_service"] == "payment-api"
    assert auth_state["target_service"] == "auth-service"
    assert len(pay_state["messages"]) == 2
    assert len(auth_state["messages"]) == 2
