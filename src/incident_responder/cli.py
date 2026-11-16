from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from incident_responder import __version__
from incident_responder.guardrails import ActionGuardrails, AuditLogger
from incident_responder.infra import DEFAULT_STATE_PATH, SimulatedInfra
from incident_responder.mcp_tools import open_mcp_stdio_session
from incident_responder.state import (
    initial_incident_state,
    open_async_sqlite_checkpointer,
)
from incident_responder.supervisor import build_supervisor_graph

PromptFn = Callable[[str], str]


def _default_prompt(question: str) -> str:
    return input(question)


def _extract_interrupt_payload(state_values: dict[str, Any]) -> dict[str, Any] | None:
    raw = state_values.get("__interrupt__")
    if not raw:
        return None
    first = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else {"value": value}


def _print_result_summary(result: dict[str, Any], out: TextIO) -> None:
    summary = result.get("incident_summary")
    if summary:
        print(f"\n[Diagnosis]\n{summary}", file=out)

    proposed = result.get("proposed_action")
    if proposed:
        print(
            f"\n[Proposed Remediation]\n"
            f"  Tool:    {proposed.get('tool')}\n"
            f"  Args:    {proposed.get('args')}\n"
            f"  Reason:  {proposed.get('reason')}",
            file=out,
        )

    messages = result.get("messages") or []
    if messages:
        last_content = str(getattr(messages[-1], "content", "")).strip()
        if last_content:
            print(f"\n[Agent Output]\n{last_content}", file=out)

    print(f"\n[Status] {result.get('resolution_status', 'unknown')}", file=out)


async def run_respond_command(
    *,
    report: str,
    thread_id: str,
    db_path: Path,
    infra_state_path: Path,
    audit_log_path: Path,
    non_interactive: bool = False,
    decision: bool | None = None,
    note: str = "",
    llm: BaseChatModel | None = None,
    guardrails: ActionGuardrails | None = None,
    prompt_fn: PromptFn = _default_prompt,
    out: TextIO = sys.stdout,
) -> int:
    config = {"configurable": {"thread_id": thread_id}}
    audit_logger = AuditLogger(audit_log_path)

    print(f"Incident thread: {thread_id}", file=out)
    async with (
        open_mcp_stdio_session(infra_state_path=infra_state_path) as session,
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
            initial_incident_state(HumanMessage(content=report)),
            config=config,
        )

        interrupt_payload = _extract_interrupt_payload(result)
        if interrupt_payload is None:
            _print_result_summary(result, out)
            return 0 if result.get("resolution_status") == "resolved" else 1

        _print_result_summary(result, out)

        if non_interactive and decision is None:
            print(
                f"\n[HITL Paused] Incident awaiting approval in thread '{thread_id}'.\n"
                f"Resume later with:\n"
                f"  incident-responder resume --thread-id {thread_id} --approve",
                file=out,
            )
            return 2

        if decision is None:
            answer = prompt_fn("\nApprove remediation execution? [y/N]: ").strip().lower()
            approved = answer in {"y", "yes", "s", "si", "sí"}
            operator_note = note or prompt_fn("Operator note (optional): ").strip()
        else:
            approved = decision
            operator_note = note

        final_result = await graph.ainvoke(
            Command(resume={"approved": approved, "note": operator_note}),
            config=config,
        )
        _print_result_summary(final_result, out)
        return 0 if final_result.get("resolution_status") == "resolved" else 1


async def run_resume_command(
    *,
    thread_id: str,
    approved: bool,
    note: str,
    db_path: Path,
    infra_state_path: Path,
    audit_log_path: Path,
    llm: BaseChatModel | None = None,
    guardrails: ActionGuardrails | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    config = {"configurable": {"thread_id": thread_id}}
    audit_logger = AuditLogger(audit_log_path)

    async with (
        open_mcp_stdio_session(infra_state_path=infra_state_path) as session,
        open_async_sqlite_checkpointer(db_path) as checkpointer,
    ):
        graph = await build_supervisor_graph(
            session=session,
            llm=llm,
            checkpointer=checkpointer,
            guardrails=guardrails,
            audit_logger=audit_logger,
        )
        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            print(f"Error: No checkpoint found for thread_id='{thread_id}'.", file=err)
            return 1
        if not snapshot.next:
            status = snapshot.values.get("resolution_status", "unknown")
            print(
                f"Error: Thread '{thread_id}' is not paused at an interrupt (status={status}).",
                file=err,
            )
            return 1

        final_result = await graph.ainvoke(
            Command(resume={"approved": approved, "note": note}),
            config=config,
        )
        _print_result_summary(final_result, out)
        return 0 if final_result.get("resolution_status") == "resolved" else 1


async def run_status_command(
    *,
    thread_id: str | None,
    db_path: Path,
    infra_state_path: Path,
    out: TextIO = sys.stdout,
) -> int:
    infra = SimulatedInfra(state_path=infra_state_path)
    services = infra.list_services()
    print("=== Infrastructure Services ===", file=out)
    for svc in services:
        extras = " | ".join(
            f"{k}={v}"
            for k, v in svc.items()
            if k not in {"name", "status", "replicas", "version"}
        )
        suffix = f" | {extras}" if extras else ""
        print(
            f"- {svc['name']}: status={svc['status']} | replicas={svc['replicas']} "
            f"| version={svc['version']}{suffix}",
            file=out,
        )

    if thread_id:
        print(f"\n=== Checkpoint Thread: {thread_id} ===", file=out)
        if not db_path.exists():
            print("No checkpoint database found.", file=out)
            return 1
        async with (
            open_mcp_stdio_session(infra_state_path=infra_state_path) as session,
            open_async_sqlite_checkpointer(db_path) as checkpointer,
        ):
            graph = await build_supervisor_graph(session=session, checkpointer=checkpointer)
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            if not snapshot or not snapshot.values:
                print(f"Thread '{thread_id}' not found.", file=out)
                return 1
            vals = snapshot.values
            print(f"Resolution status: {vals.get('resolution_status')}", file=out)
            print(f"Target service:    {vals.get('target_service')}", file=out)
            print(f"Pending next node: {list(snapshot.next)}", file=out)
            if vals.get("proposed_action"):
                print(f"Proposed action:   {vals['proposed_action']}", file=out)

    return 0


def run_audit_command(
    *,
    thread_id: str | None,
    audit_log_path: Path,
    out: TextIO = sys.stdout,
) -> int:
    logger = AuditLogger(audit_log_path)
    events = logger.read_events(thread_id=thread_id)
    if not events:
        scope = f" for thread '{thread_id}'" if thread_id else ""
        print(f"No audit events found{scope}.", file=out)
        return 0

    print(f"=== Audit Trail ({len(events)} events) ===", file=out)
    for ev in events:
        print(
            f"[{ev['timestamp']}] {ev['event']} | thread={ev['thread_id']} "
            f"| actor={ev['actor']} | service={ev['service']} | tool={ev['tool']} "
            f"| args={ev['args']} | details={ev['details']}",
            file=out,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident-responder",
        description="Local-first multi-agent incident responder with MCP and HITL approval gates.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(".incident_checkpoints.sqlite"),
        help="Path to the SQLite checkpoint database.",
    )
    parser.add_argument(
        "--infra-state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to the JSON simulated infrastructure state file.",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path(".incident_audit.jsonl"),
        help="Path to the JSONL audit log file.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # respond
    respond_p = subparsers.add_parser(
        "respond",
        help="Investigate an incident report and request HITL approval before remediation.",
    )
    respond_p.add_argument("report", help="Incident description (e.g. 'payment-api returning 500s').")
    respond_p.add_argument(
        "--thread-id",
        default=None,
        help="Conversation/incident thread identifier (auto-generated if omitted).",
    )
    respond_p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt on stdin; pause at HITL gate unless --approve/--reject is provided.",
    )
    decision_group = respond_p.add_mutually_exclusive_group()
    decision_group.add_argument(
        "--approve",
        action="store_true",
        help="Automatically approve the proposed remediation at the HITL gate.",
    )
    decision_group.add_argument(
        "--reject",
        action="store_true",
        help="Automatically reject the proposed remediation at the HITL gate.",
    )
    respond_p.add_argument("--note", default="", help="Optional operator note for audit log.")

    # resume
    resume_p = subparsers.add_parser(
        "resume",
        help="Resume a paused incident thread with human approval or rejection.",
    )
    resume_p.add_argument("--thread-id", required=True, help="Paused incident thread_id.")
    resume_decision = resume_p.add_mutually_exclusive_group(required=True)
    resume_decision.add_argument("--approve", action="store_true", help="Approve remediation.")
    resume_decision.add_argument("--reject", action="store_true", help="Reject remediation.")
    resume_p.add_argument("--note", default="", help="Optional operator note.")

    # status
    status_p = subparsers.add_parser(
        "status",
        help="Inspect simulated infrastructure services and optional thread checkpoint state.",
    )
    status_p.add_argument("--thread-id", default=None, help="Optional thread_id to inspect.")

    # audit
    audit_p = subparsers.add_parser(
        "audit",
        help="Display structured JSONL audit trail events.",
    )
    audit_p.add_argument("--thread-id", default=None, help="Filter audit events by thread_id.")

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    llm: BaseChatModel | None = None,
    guardrails: ActionGuardrails | None = None,
    prompt_fn: PromptFn = _default_prompt,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "respond":
        thread_id = args.thread_id or f"inc-{uuid.uuid4().hex[:8]}"
        decision: bool | None = True if args.approve else (False if args.reject else None)
        return asyncio.run(
            run_respond_command(
                report=args.report,
                thread_id=thread_id,
                db_path=args.db_path,
                infra_state_path=args.infra_state,
                audit_log_path=args.audit_log,
                non_interactive=args.non_interactive,
                decision=decision,
                note=args.note,
                llm=llm,
                guardrails=guardrails,
                prompt_fn=prompt_fn,
                out=out,
            )
        )

    if args.command == "resume":
        return asyncio.run(
            run_resume_command(
                thread_id=args.thread_id,
                approved=bool(args.approve),
                note=args.note,
                db_path=args.db_path,
                infra_state_path=args.infra_state,
                audit_log_path=args.audit_log,
                llm=llm,
                guardrails=guardrails,
                out=out,
                err=err,
            )
        )

    if args.command == "status":
        return asyncio.run(
            run_status_command(
                thread_id=args.thread_id,
                db_path=args.db_path,
                infra_state_path=args.infra_state,
                out=out,
            )
        )

    if args.command == "audit":
        return run_audit_command(
            thread_id=args.thread_id,
            audit_log_path=args.audit_log,
            out=out,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
