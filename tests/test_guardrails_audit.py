from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.types import Command

from incident_responder.guardrails import ActionGuardrails, AuditLogger, GuardrailPolicy
from incident_responder.infra import SimulatedInfra
from incident_responder.mcp_tools import open_mcp_stdio_session
from incident_responder.state import (
    initial_incident_state,
    open_async_sqlite_checkpointer,
)
from incident_responder.supervisor import RemediationProposal, build_supervisor_graph


class ScriptedSupervisorLLM(GenericFakeChatModel):
    """Deterministic chat model supporting bind_tools() and with_structured_output()."""

    proposal: RemediationProposal

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        proposal = self.proposal
        return RunnableLambda(lambda _messages: proposal)


def test_guardrails_policy_rules() -> None:
    policy = GuardrailPolicy(
        allowed_services=("payment-api", "auth-service"),
        min_replicas=1,
        max_replicas=8,
        cooldown_seconds=60.0,
        max_actions_per_thread=2,
    )
    guardrails = ActionGuardrails(policy=policy)

    # Disallowed tool
    v_tool = guardrails.validate(
        tool="delete_service", args={"service_name": "payment-api"}, now=100.0
    )
    assert not v_tool.allowed
    assert v_tool.rule == "tool_allowlist"

    # Disallowed service
    v_svc = guardrails.validate(
        tool="restart_service", args={"service_name": "billing-worker"}, now=100.0
    )
    assert not v_svc.allowed
    assert v_svc.rule == "service_allowlist"

    # Replica out of bounds
    v_rep = guardrails.validate(
        tool="scale_service",
        args={"service_name": "payment-api", "replicas": 12},
        now=100.0,
    )
    assert not v_rep.allowed
    assert v_rep.rule == "replica_bounds"

    # Valid action passes and triggers cooldown on repeat
    v_ok = guardrails.validate(
        tool="scale_service",
        args={"service_name": "payment-api", "replicas": 4},
        thread_id="t-1",
        now=100.0,
    )
    assert v_ok.allowed
    guardrails.record_execution(
        tool="scale_service",
        args={"service_name": "payment-api", "replicas": 4},
        thread_id="t-1",
        now=100.0,
    )

    v_cd = guardrails.validate(
        tool="scale_service",
        args={"service_name": "payment-api", "replicas": 5},
        thread_id="t-1",
        now=120.0,
    )
    assert not v_cd.allowed
    assert v_cd.rule == "cooldown"

    # After cooldown expires, second action works; third hits thread rate limit
    v_after_cd = guardrails.validate(
        tool="restart_service",
        args={"service_name": "auth-service"},
        thread_id="t-1",
        now=170.0,
    )
    assert v_after_cd.allowed
    guardrails.record_execution(
        tool="restart_service",
        args={"service_name": "auth-service"},
        thread_id="t-1",
        now=170.0,
    )

    v_rate = guardrails.validate(
        tool="rollback_deployment",
        args={"service_name": "payment-api"},
        thread_id="t-1",
        now=250.0,
    )
    assert not v_rate.allowed
    assert v_rate.rule == "thread_rate_limit"


@pytest.mark.asyncio
async def test_guardrail_blocks_out_of_bounds_proposal_before_hitl(tmp_path: Path) -> None:
    infra_path = tmp_path / "infra.json"
    db_path = tmp_path / "checkpoints.sqlite"
    audit_path = tmp_path / "audit.jsonl"

    SimulatedInfra(state_path=infra_path)
    audit_logger = AuditLogger(audit_path)
    guardrails = ActionGuardrails(policy=GuardrailPolicy(max_replicas=10))

    llm = ScriptedSupervisorLLM(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_service_status",
                            "args": {"service_name": "payment-api"},
                            "id": "tc-1",
                        }
                    ],
                ),
                AIMessage(content="Diagnosis: payment-api CPU spike detected."),
            ]
        ),
        proposal=RemediationProposal(
            tool="scale_service",
            service_name="payment-api",
            replicas=25,
            reason="Scale aggressively to 25 replicas",
        ),
    )

    config = {"configurable": {"thread_id": "guardrail-block-1"}}
    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=llm,
            checkpointer=checkpointer,
            guardrails=guardrails,
            audit_logger=audit_logger,
        )
        result = await graph.ainvoke(
            initial_incident_state(HumanMessage(content="High CPU on payment-api")),
            config=config,
        )

    assert "__interrupt__" not in result
    assert result["resolution_status"] == "aborted"
    assert result["proposed_action"] is None

    events = audit_logger.read_events(thread_id="guardrail-block-1")
    assert [e["event"] for e in events] == ["PROPOSED", "GUARDRAIL_BLOCKED"]
    assert events[1]["details"]["rule"] == "replica_bounds"
    assert events[1]["details"]["stage"] == "pre_approval"

    # Infrastructure untouched
    infra_after = json.loads(infra_path.read_text(encoding="utf-8"))
    assert infra_after["payment-api"]["replicas"] == 2


@pytest.mark.asyncio
async def test_audit_trail_for_approved_and_rejected_workflows(tmp_path: Path) -> None:
    infra_path = tmp_path / "infra.json"
    db_path = tmp_path / "checkpoints.sqlite"
    audit_path = tmp_path / "audit.jsonl"

    SimulatedInfra(state_path=infra_path)
    audit_logger = AuditLogger(audit_path)
    guardrails = ActionGuardrails()

    # 1. Approved workflow
    llm_approve = ScriptedSupervisorLLM(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_service_logs",
                            "args": {"service_name": "payment-api"},
                            "id": "tc-2",
                        }
                    ],
                ),
                AIMessage(content="Diagnosis: v2.3.1 regression on payment-api."),
            ]
        ),
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="Rollback regression v2.3.1 -> v2.3.0",
        ),
    )

    cfg_approve = {"configurable": {"thread_id": "audit-approve-1"}}
    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=llm_approve,
            checkpointer=checkpointer,
            guardrails=guardrails,
            audit_logger=audit_logger,
        )
        paused = await graph.ainvoke(
            initial_incident_state(HumanMessage(content="payment-api returning 502 errors")),
            config=cfg_approve,
        )
        assert "__interrupt__" in paused

        resumed = await graph.ainvoke(
            Command(resume={"approved": True, "note": "Verified on-call"}),
            config=cfg_approve,
        )
        assert resumed["resolution_status"] == "resolved"

    approved_events = audit_logger.read_events(thread_id="audit-approve-1")
    assert [e["event"] for e in approved_events] == ["PROPOSED", "APPROVED", "EXECUTED"]
    assert approved_events[1]["actor"] == "human"
    assert approved_events[1]["details"]["note"] == "Verified on-call"
    assert approved_events[2]["actor"] == "executor"

    # 2. Rejected workflow
    llm_reject = ScriptedSupervisorLLM(
        messages=iter(
            [
                AIMessage(content="Diagnosis: auth-service memory elevated."),
            ]
        ),
        proposal=RemediationProposal(
            tool="restart_service",
            service_name="auth-service",
            reason="Restart auth-service",
        ),
    )

    cfg_reject = {"configurable": {"thread_id": "audit-reject-1"}}
    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=llm_reject,
            checkpointer=checkpointer,
            guardrails=guardrails,
            audit_logger=audit_logger,
        )
        paused_rej = await graph.ainvoke(
            initial_incident_state(HumanMessage(content="auth-service high memory")),
            config=cfg_reject,
        )
        assert "__interrupt__" in paused_rej

        aborted = await graph.ainvoke(
            Command(resume={"approved": False, "note": "Maintenance window closed"}),
            config=cfg_reject,
        )
        assert aborted["resolution_status"] == "aborted"

    rejected_events = audit_logger.read_events(thread_id="audit-reject-1")
    assert [e["event"] for e in rejected_events] == ["PROPOSED", "REJECTED"]
    assert rejected_events[1]["details"]["note"] == "Maintenance window closed"


@pytest.mark.asyncio
async def test_defense_in_depth_guardrail_blocks_at_execution_if_cooldown_triggered_while_paused(
    tmp_path: Path,
) -> None:
    infra_path = tmp_path / "infra.json"
    db_path = tmp_path / "checkpoints.sqlite"
    audit_path = tmp_path / "audit.jsonl"

    SimulatedInfra(state_path=infra_path)
    audit_logger = AuditLogger(audit_path)
    guardrails = ActionGuardrails(policy=GuardrailPolicy(cooldown_seconds=300.0))

    llm = ScriptedSupervisorLLM(
        messages=iter([AIMessage(content="Diagnosis: payment-api degraded.")]),
        proposal=RemediationProposal(
            tool="restart_service",
            service_name="payment-api",
            reason="Restart payment-api",
        ),
    )

    cfg = {"configurable": {"thread_id": "defense-depth-1"}}
    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=llm,
            checkpointer=checkpointer,
            guardrails=guardrails,
            audit_logger=audit_logger,
        )
        paused = await graph.ainvoke(
            initial_incident_state(HumanMessage(content="payment-api slow")),
            config=cfg,
        )
        assert "__interrupt__" in paused

        # Simulate another thread executing the same remediation while this thread awaited approval
        guardrails.record_execution(
            tool="restart_service",
            args={"service_name": "payment-api"},
            thread_id="other-thread",
        )

        final_state = await graph.ainvoke(
            Command(resume={"approved": True}),
            config=cfg,
        )
        assert final_state["resolution_status"] == "aborted"

    events = audit_logger.read_events(thread_id="defense-depth-1")
    assert [e["event"] for e in events] == ["PROPOSED", "APPROVED", "GUARDRAIL_BLOCKED"]
    assert events[2]["details"]["stage"] == "pre_execution"
    assert events[2]["details"]["rule"] == "cooldown"
