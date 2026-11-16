from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langgraph.types import Command

from incident_responder.infra import SimulatedInfra
from incident_responder.mcp_tools import load_mcp_tool_groups, open_mcp_stdio_session
from incident_responder.state import (
    initial_incident_state,
    open_async_sqlite_checkpointer,
)
from incident_responder.supervisor import (
    RemediationProposal,
    build_supervisor_graph,
)


class FakeSREChatModel(BaseChatModel):
    """Deterministic chat model emulating ChatOllama tool-calling and structured output."""

    proposal: RemediationProposal

    @property
    def _llm_type(self) -> str:
        return "fake-sre-ollama"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_messages:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_service_status",
                        "args": {"service_name": self.proposal.service_name},
                        "id": "call_status_1",
                        "type": "tool_call",
                    },
                    {
                        "name": "get_service_logs",
                        "args": {"service_name": self.proposal.service_name, "limit": 5},
                        "id": "call_logs_1",
                        "type": "tool_call",
                    },
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=msg)])

        summary = AIMessage(
            content=(
                f"Root cause identified on {self.proposal.service_name}: "
                "memory at 94.2%, latency 1850ms, and HTTP 502 errors after deployment v2.3.1."
            )
        )
        return ChatResult(generations=[ChatGeneration(message=summary)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return RunnableLambda(lambda _messages: self.proposal)


@pytest.mark.asyncio
async def test_mcp_stdio_session_separates_read_and_write_tools(tmp_path: Path) -> None:
    infra_path = tmp_path / "infra.json"
    SimulatedInfra(state_path=infra_path).reset()

    async with open_mcp_stdio_session(infra_state_path=infra_path) as session:
        read_tools, write_tools = await load_mcp_tool_groups(session)

    assert {t.name for t in read_tools} == {
        "list_services",
        "get_service_status",
        "get_service_logs",
    }
    assert {t.name for t in write_tools} == {
        "restart_service",
        "scale_service",
        "rollback_deployment",
    }


@pytest.mark.asyncio
async def test_supervisor_diagnoses_via_mcp_pauses_and_executes_on_approval(
    tmp_path: Path,
) -> None:
    infra_path = tmp_path / "infra.json"
    db_path = tmp_path / "checkpoints.sqlite"
    infra = SimulatedInfra(state_path=infra_path)
    infra.reset()

    fake_llm = FakeSREChatModel(
        proposal=RemediationProposal(
            tool="rollback_deployment",
            service_name="payment-api",
            reason="GC overhead and 502 errors started right after deploying v2.3.1.",
        )
    )
    config = {"configurable": {"thread_id": "mcp-approve-thread"}}

    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=fake_llm,
            checkpointer=checkpointer,
        )
        paused = await graph.ainvoke(
            initial_incident_state(
                AIMessage(content="Checkout is returning 502 errors on payment-api")
            ),
            config=config,
        )

        # Graph must pause at human_approval before mutating MCP infrastructure
        assert paused["resolution_status"] == "awaiting_approval"
        assert paused["target_service"] == "payment-api"
        assert paused["proposed_action"]["tool"] == "rollback_deployment"
        assert infra.get_service_status("payment-api")["status"] == "degraded"
        assert infra.get_service_status("payment-api")["version"] == "v2.3.1"

        # Verify diagnostic subagent actually invoked live MCP read tools
        tool_msgs = [m for m in paused["messages"] if isinstance(m, ToolMessage)]
        assert {m.name for m in tool_msgs} == {"get_service_status", "get_service_logs"}

        # Resume with operator approval -> executes rollback_deployment via stdio MCP
        resumed = await graph.ainvoke(
            Command(resume={"approved": True, "note": "Approved rollback in prod"}),
            config=config,
        )

    assert resumed["resolution_status"] == "resolved"
    assert resumed["proposed_action"] is None
    post_status = infra.get_service_status("payment-api")
    assert post_status["status"] == "healthy"
    assert post_status["version"] == "v2.3.0"


@pytest.mark.asyncio
async def test_supervisor_aborts_without_mutating_mcp_on_rejection(tmp_path: Path) -> None:
    infra_path = tmp_path / "infra.json"
    db_path = tmp_path / "checkpoints.sqlite"
    infra = SimulatedInfra(state_path=infra_path)
    infra.reset()

    fake_llm = FakeSREChatModel(
        proposal=RemediationProposal(
            tool="restart_service",
            service_name="payment-api",
            reason="High memory pressure detected on payment-api.",
        )
    )
    config = {"configurable": {"thread_id": "mcp-reject-thread"}}

    async with (
        open_mcp_stdio_session(infra_state_path=infra_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=fake_llm,
            checkpointer=checkpointer,
        )
        await graph.ainvoke(
            initial_incident_state(AIMessage(content="Investigate payment-api latency")),
            config=config,
        )
        aborted = await graph.ainvoke(
            Command(resume={"approved": False, "note": "Hold during peak checkout window"}),
            config=config,
        )

    assert aborted["resolution_status"] == "aborted"
    assert aborted["proposed_action"] is None
    still_degraded = infra.get_service_status("payment-api")
    assert still_degraded["status"] == "degraded"
    assert still_degraded["version"] == "v2.3.1"
