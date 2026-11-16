from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

AuditEventType = Literal[
    "PROPOSED",
    "GUARDRAIL_BLOCKED",
    "APPROVED",
    "REJECTED",
    "EXECUTED",
    "EXECUTION_FAILED",
]

DEFAULT_ALLOWED_SERVICES: tuple[str, ...] = ("payment-api", "auth-service", "inventory-db")
DEFAULT_ALLOWED_WRITE_TOOLS: tuple[str, ...] = (
    "rollback_deployment",
    "restart_service",
    "scale_service",
)


def _extract_service(args: dict[str, Any]) -> str:
    return str(args.get("service_name") or args.get("name") or "").strip()


@dataclass(frozen=True)
class GuardrailPolicy:
    """Deterministic safety policy evaluated before human approval and before execution."""

    allowed_services: tuple[str, ...] = DEFAULT_ALLOWED_SERVICES
    allowed_write_tools: tuple[str, ...] = DEFAULT_ALLOWED_WRITE_TOOLS
    min_replicas: int = 1
    max_replicas: int = 10
    cooldown_seconds: float = 60.0
    max_actions_per_thread: int = 3


@dataclass
class GuardrailVerdict:
    """Result of evaluating a proposed remediation against GuardrailPolicy."""

    allowed: bool
    reason: str
    rule: str | None = None


@dataclass
class ActionGuardrails:
    """Stateful validator enforcing allowlists, parameter bounds, and cooldown/rate limits."""

    policy: GuardrailPolicy = field(default_factory=GuardrailPolicy)
    _last_execution_ts: dict[tuple[str, str], float] = field(default_factory=dict)
    _thread_action_counts: dict[str, int] = field(default_factory=dict)

    def validate(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        thread_id: str = "default",
        now: float | None = None,
    ) -> GuardrailVerdict:
        current_ts = time.monotonic() if now is None else now
        service = _extract_service(args)

        if tool not in self.policy.allowed_write_tools:
            return GuardrailVerdict(
                allowed=False,
                rule="tool_allowlist",
                reason=f"Tool '{tool}' is not in allowed write tools {self.policy.allowed_write_tools}.",
            )

        if service not in self.policy.allowed_services:
            return GuardrailVerdict(
                allowed=False,
                rule="service_allowlist",
                reason=f"Service '{service}' is not in allowed services {self.policy.allowed_services}.",
            )

        if tool == "scale_service":
            raw_replicas = args.get("replicas")
            if isinstance(raw_replicas, bool) or not isinstance(raw_replicas, int):
                return GuardrailVerdict(
                    allowed=False,
                    rule="replica_type",
                    reason=f"Replica count must be an integer, got {raw_replicas!r}.",
                )
            if not (self.policy.min_replicas <= raw_replicas <= self.policy.max_replicas):
                return GuardrailVerdict(
                    allowed=False,
                    rule="replica_bounds",
                    reason=(
                        f"Requested replicas={raw_replicas} outside allowed bounds "
                        f"[{self.policy.min_replicas}, {self.policy.max_replicas}]."
                    ),
                )

        count = self._thread_action_counts.get(thread_id, 0)
        if count >= self.policy.max_actions_per_thread:
            return GuardrailVerdict(
                allowed=False,
                rule="thread_rate_limit",
                reason=(
                    f"Thread '{thread_id}' reached max allowed actions "
                    f"({self.policy.max_actions_per_thread})."
                ),
            )

        key = (service, tool)
        last_ts = self._last_execution_ts.get(key)
        if last_ts is not None and (current_ts - last_ts) < self.policy.cooldown_seconds:
            remaining = round(self.policy.cooldown_seconds - (current_ts - last_ts), 2)
            return GuardrailVerdict(
                allowed=False,
                rule="cooldown",
                reason=(
                    f"Cooldown active for '{tool}' on '{service}' "
                    f"({remaining}s remaining of {self.policy.cooldown_seconds}s)."
                ),
            )

        return GuardrailVerdict(allowed=True, reason="Passed all guardrail checks.")

    def record_execution(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        thread_id: str = "default",
        now: float | None = None,
    ) -> None:
        """Record a completed remediation execution for cooldown and rate-limit tracking."""
        current_ts = time.monotonic() if now is None else now
        service = _extract_service(args)
        self._last_execution_ts[(service, tool)] = current_ts
        self._thread_action_counts[thread_id] = self._thread_action_counts.get(thread_id, 0) + 1


class AuditLogger:
    """Append-only structured JSONL audit trail for incident remediation lifecycle events."""

    def __init__(self, log_path: str | Path = ".incident_audit.jsonl") -> None:
        self.log_path = Path(log_path)

    def log_event(
        self,
        *,
        event: AuditEventType,
        thread_id: str,
        service: str | None,
        tool: str,
        args: dict[str, Any],
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "thread_id": thread_id,
            "service": service,
            "tool": tool,
            "args": args,
            "actor": actor,
            "details": details or {},
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_events(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if thread_id is None or item.get("thread_id") == thread_id:
                records.append(item)
        return records
