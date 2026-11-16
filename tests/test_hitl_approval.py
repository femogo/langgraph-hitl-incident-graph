from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from incident_responder.graph import build_incident_graph
from incident_responder.infra import SimulatedInfra
from incident_responder.state import initial_incident_state, open_sqlite_checkpointer


def test_hitl_pauses_before_mutating_infra(tmp_path: Path) -> None:
    db_path = tmp_path / "hitl.sqlite"
    infra_path = tmp_path / "infra.json"
    infra = SimulatedInfra(state_path=infra_path)
    assert infra.get_service_status("payment-api")["status"] == "degraded"

    config = {"configurable": {"thread_id": "inc-hitl-pause"}}
    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(checkpointer=checkpointer, infra_state_path=infra_path)
        result = graph.invoke(
            initial_incident_state(HumanMessage(content="payment-api is throwing 502 errors")),
            config=config,
        )

        assert "__interrupt__" in result
        interrupt_payload = result["__interrupt__"][0].value
        assert interrupt_payload["type"] == "remediation_approval"
        assert interrupt_payload["proposed_action"]["tool"] == "rollback_deployment"

        snapshot = graph.get_state(config)
        assert snapshot.values["resolution_status"] == "awaiting_approval"
        assert snapshot.next == ("human_approval",)

    # Infrastructure must remain untouched while awaiting approval
    assert infra.get_service_status("payment-api")["status"] == "degraded"
    assert infra.get_service_status("payment-api")["version"] == "v2.3.1"


def test_hitl_resume_approved_executes_remediation_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "hitl.sqlite"
    infra_path = tmp_path / "infra.json"
    infra = SimulatedInfra(state_path=infra_path)
    config = {"configurable": {"thread_id": "inc-hitl-approve"}}

    # Step 1: Run until interrupt() and close SQLite connection
    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(checkpointer=checkpointer, infra_state_path=infra_path)
        paused = graph.invoke(
            initial_incident_state(HumanMessage(content="payment-api is degraded after release")),
            config=config,
        )
        assert "__interrupt__" in paused

    # Step 2: Re-open SQLite in a fresh graph instance and resume with approval
    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(checkpointer=checkpointer, infra_state_path=infra_path)
        final = graph.invoke(
            Command(resume={"approved": True, "note": "Approved by on-call SRE"}),
            config=config,
        )

        assert "__interrupt__" not in final
        assert final["resolution_status"] == "resolved"
        assert final["proposed_action"] is None
        assert any("Operator APPROVED" in str(m.content) for m in final["messages"])

    service = infra.get_service_status("payment-api")
    assert service["status"] == "healthy"
    assert service["version"] == "v2.3.0"


def test_hitl_resume_rejected_aborts_without_touching_infra(tmp_path: Path) -> None:
    db_path = tmp_path / "hitl.sqlite"
    infra_path = tmp_path / "infra.json"
    infra = SimulatedInfra(state_path=infra_path)
    config = {"configurable": {"thread_id": "inc-hitl-reject"}}

    with open_sqlite_checkpointer(db_path) as checkpointer:
        graph = build_incident_graph(checkpointer=checkpointer, infra_state_path=infra_path)
        paused = graph.invoke(
            initial_incident_state(HumanMessage(content="Restart payment-api due to memory leak")),
            config=config,
        )
        assert "__interrupt__" in paused

        final = graph.invoke(
            Command(resume={"approved": False, "note": "Hold until DB backup finishes"}),
            config=config,
        )

        assert "__interrupt__" not in final
        assert final["resolution_status"] == "aborted"
        assert final["proposed_action"] is None
        assert any("Operator REJECTED" in str(m.content) for m in final["messages"])
        assert any("Infrastructure left untouched" in str(m.content) for m in final["messages"])

    service = infra.get_service_status("payment-api")
    assert service["status"] == "degraded"
    assert service["version"] == "v2.3.1"
