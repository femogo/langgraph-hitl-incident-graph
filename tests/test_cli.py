from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from incident_responder.cli import main
from incident_responder.guardrails import AuditLogger
from incident_responder.infra import SimulatedInfra
from incident_responder.supervisor import RemediationProposal


class ScriptedIncidentLLM(BaseChatModel):
    """Deterministic chat model emulating tool-calling diagnosis + structured proposal."""

    proposal: RemediationProposal
    diagnosis_text: str = (
        "Root cause on payment-api: deployment v2.5.0 introduced NullPointerException "
        "in checkout handler causing 18% error rate."
    )
    tool_calls_made: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-incident-llm"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        return RunnableLambda(self._diagnose_step)

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        return RunnableLambda(lambda _messages: self.proposal)

    def _diagnose_step(self, messages: list[BaseMessage]) -> AIMessage:
        if self.tool_calls_made == 0:
            self.tool_calls_made += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-status-1",
                        "name": "get_service_status",
                        "args": {"service_name": self.proposal.service_name},
                    }
                ],
            )
        return AIMessage(content=self.diagnosis_text)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._diagnose_step(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "checkpoints.sqlite"
    infra_path = tmp_path / "infra_state.json"
    audit_path = tmp_path / "audit.jsonl"
    SimulatedInfra(state_path=infra_path)
    return db_path, infra_path, audit_path


def test_cli_respond_interactive_approve_executes_mcp_remediation(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    llm = ScriptedIncidentLLM(
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="Post-deploy 500 spike on v2.5.0",
        )
    )
    answers = iter(["y", "Approved during CLI test"])
    out = io.StringIO()
    err = io.StringIO()

    code = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "respond",
            "payment-api is throwing 500 errors after deploy",
            "--thread-id",
            "cli-inc-approve",
        ],
        llm=llm,
        prompt_fn=lambda _q: next(answers),
        out=out,
        err=err,
    )

    assert code == 0, err.getvalue()
    output_text = out.getvalue()
    assert "[Diagnosis]" in output_text
    assert "[Proposed Remediation]" in output_text
    assert "[Status] resolved" in output_text

    # Verify live infrastructure state was mutated via MCP
    infra = SimulatedInfra(state_path=infra_path)
    svc = infra.get_service_status("payment-api")
    assert svc["status"] == "healthy"
    assert svc["version"] == "v2.3.0"

    # Verify full audit lifecycle
    events = [e["event"] for e in AuditLogger(audit_path).read_events("cli-inc-approve")]
    assert events == ["PROPOSED", "APPROVED", "EXECUTED"]


def test_cli_respond_interactive_reject_leaves_infra_untouched(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    llm = ScriptedIncidentLLM(
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="Post-deploy 500 spike on v2.5.0",
        )
    )
    answers = iter(["n", "Waiting for checkout team"])
    out = io.StringIO()

    code = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "respond",
            "payment-api degraded",
            "--thread-id",
            "cli-inc-reject",
        ],
        llm=llm,
        prompt_fn=lambda _q: next(answers),
        out=out,
    )

    assert code == 1
    assert "[Status] aborted" in out.getvalue()

    svc = SimulatedInfra(state_path=infra_path).get_service_status("payment-api")
    assert svc["status"] == "degraded"
    assert svc["version"] == "v2.3.1"

    events = [e["event"] for e in AuditLogger(audit_path).read_events("cli-inc-reject")]
    assert events == ["PROPOSED", "REJECTED"]


def test_cli_non_interactive_pause_and_resume_across_invocations(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    llm = ScriptedIncidentLLM(
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="Rollback bad release",
        )
    )

    # Step 1: Pause at HITL gate in non-interactive mode (exit code 2)
    out1 = io.StringIO()
    code1 = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "respond",
            "payment-api errors",
            "--thread-id",
            "cli-inc-async",
            "--non-interactive",
        ],
        llm=llm,
        out=out1,
    )
    assert code1 == 2
    assert "[HITL Paused]" in out1.getvalue()
    assert SimulatedInfra(state_path=infra_path).get_service_status("payment-api")["status"] == "degraded"

    # Step 2: Resume from a separate CLI invocation with --approve
    out2 = io.StringIO()
    err2 = io.StringIO()
    code2 = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "resume",
            "--thread-id",
            "cli-inc-async",
            "--approve",
            "--note",
            "Verified metrics, proceed",
        ],
        llm=llm,
        out=out2,
        err=err2,
    )
    assert code2 == 0, err2.getvalue()
    assert "[Status] resolved" in out2.getvalue()

    svc = SimulatedInfra(state_path=infra_path).get_service_status("payment-api")
    assert svc["status"] == "healthy"
    assert svc["version"] == "v2.3.0"


def test_cli_guardrail_blocks_out_of_bounds_proposal(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    llm = ScriptedIncidentLLM(
        proposal=RemediationProposal(
            tool="scale_service",
            service_name="payment-api",
            replicas=25,
            reason="Aggressive scale out",
        )
    )
    out = io.StringIO()

    code = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "respond",
            "payment-api traffic surge",
            "--thread-id",
            "cli-inc-blocked",
            "--approve",
        ],
        llm=llm,
        out=out,
    )

    assert code == 1
    assert "[Guardrail Blocked]" in out.getvalue()
    events = [e["event"] for e in AuditLogger(audit_path).read_events("cli-inc-blocked")]
    assert events == ["PROPOSED", "GUARDRAIL_BLOCKED"]


def test_cli_status_and_audit_subcommands(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    llm = ScriptedIncidentLLM(
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="Rollback bad release",
        )
    )

    main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "respond",
            "payment-api errors",
            "--thread-id",
            "cli-inc-inspect",
            "--approve",
        ],
        llm=llm,
        out=io.StringIO(),
    )

    status_out = io.StringIO()
    status_code = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "status",
            "--thread-id",
            "cli-inc-inspect",
        ],
        out=status_out,
    )
    assert status_code == 0
    assert "payment-api: status=healthy" in status_out.getvalue()
    assert "Resolution status: resolved" in status_out.getvalue()

    audit_out = io.StringIO()
    audit_code = main(
        [
            "--audit-log",
            str(audit_path),
            "audit",
            "--thread-id",
            "cli-inc-inspect",
        ],
        out=audit_out,
    )
    assert audit_code == 0
    assert "PROPOSED" in audit_out.getvalue()
    assert "EXECUTED" in audit_out.getvalue()


def test_cli_resume_missing_thread_returns_error(tmp_path: Path) -> None:
    db_path, infra_path, audit_path = _paths(tmp_path)
    err = io.StringIO()

    code = main(
        [
            "--db-path",
            str(db_path),
            "--infra-state",
            str(infra_path),
            "--audit-log",
            str(audit_path),
            "resume",
            "--thread-id",
            "does-not-exist",
            "--approve",
        ],
        out=io.StringIO(),
        err=err,
    )
    assert code == 1
    assert "No checkpoint found" in err.getvalue()
