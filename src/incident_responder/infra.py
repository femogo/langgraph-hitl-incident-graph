from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ServiceStatus = Literal["healthy", "degraded", "down"]

DEFAULT_STATE_PATH = Path(".infra_state.json")


class ServiceRecord(BaseModel):
    name: str
    status: ServiceStatus
    replicas: int = Field(ge=1, le=10)
    cpu_percent: float = Field(ge=0.0, le=100.0)
    memory_percent: float = Field(ge=0.0, le=100.0)
    latency_ms: int = Field(ge=0)
    version: str
    previous_version: str
    logs: list[str]
    updated_at: str


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _initial_services() -> dict[str, ServiceRecord]:
    ts = _now_iso()
    return {
        "auth-service": ServiceRecord(
            name="auth-service",
            status="healthy",
            replicas=2,
            cpu_percent=22.5,
            memory_percent=41.0,
            latency_ms=45,
            version="v1.4.2",
            previous_version="v1.4.1",
            logs=[
                f"{ts} [INFO] Token validation cache hit rate: 98.4%",
                f"{ts} [INFO] Healthcheck OK on 2/2 replicas",
            ],
            updated_at=ts,
        ),
        "payment-api": ServiceRecord(
            name="payment-api",
            status="degraded",
            replicas=2,
            cpu_percent=88.4,
            memory_percent=94.2,
            latency_ms=1850,
            version="v2.3.1",
            previous_version="v2.3.0",
            logs=[
                f"{ts} [WARN] High memory pressure detected on pod payment-api-7f8d9 (94.2%)",
                f"{ts} [ERROR] Upstream timeout after 1800ms: HTTP 502 Bad Gateway on /api/v1/checkout",
                f"{ts} [ERROR] GC overhead limit exceeded following deployment v2.3.1",
            ],
            updated_at=ts,
        ),
        "inventory-db": ServiceRecord(
            name="inventory-db",
            status="healthy",
            replicas=1,
            cpu_percent=35.0,
            memory_percent=60.5,
            latency_ms=12,
            version="v15.4",
            previous_version="v15.3",
            logs=[
                f"{ts} [INFO] Active connections: 42/200",
                f"{ts} [INFO] Checkpoint complete in 18ms",
            ],
            updated_at=ts,
        ),
    }


class SimulatedInfra:
    """Deterministic local infrastructure state persisted to a JSON file."""

    def __init__(self, state_path: Path = DEFAULT_STATE_PATH) -> None:
        self.state_path = state_path
        if not self.state_path.exists():
            self.reset()

    def reset(self) -> dict[str, ServiceRecord]:
        services = deepcopy(_initial_services())
        self._save(services)
        return services

    def _load(self) -> dict[str, ServiceRecord]:
        if not self.state_path.exists():
            return self.reset()
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {name: ServiceRecord.model_validate(data) for name, data in raw.items()}

    def _save(self, services: dict[str, ServiceRecord]) -> None:
        payload = {name: svc.model_dump() for name, svc in services.items()}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _get_or_raise(self, services: dict[str, ServiceRecord], name: str) -> ServiceRecord:
        if name not in services:
            available = ", ".join(sorted(services.keys()))
            raise ValueError(f"Unknown service '{name}'. Available services: {available}")
        return services[name]

    # --- Read-only inspection methods ---

    def list_services(self) -> list[dict[str, object]]:
        services = self._load()
        return [
            {
                "name": svc.name,
                "status": svc.status,
                "replicas": svc.replicas,
                "version": svc.version,
                "latency_ms": svc.latency_ms,
            }
            for svc in services.values()
        ]

    def get_service_status(self, name: str) -> dict[str, object]:
        services = self._load()
        svc = self._get_or_raise(services, name)
        return svc.model_dump(exclude={"logs"})

    def get_service_logs(self, name: str, limit: int = 10) -> dict[str, object]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        services = self._load()
        svc = self._get_or_raise(services, name)
        return {
            "name": svc.name,
            "status": svc.status,
            "logs": svc.logs[-limit:],
        }

    # --- State-mutating remediation methods ---

    def restart_service(self, name: str) -> dict[str, object]:
        services = self._load()
        svc = self._get_or_raise(services, name)
        ts = _now_iso()
        svc.status = "healthy"
        svc.cpu_percent = round(min(svc.cpu_percent, 32.0), 1)
        svc.memory_percent = round(min(svc.memory_percent, 45.0), 1)
        svc.latency_ms = min(svc.latency_ms, 85)
        svc.updated_at = ts
        svc.logs.append(f"{ts} [INFO] Service restarted gracefully; healthchecks passing")
        self._save(services)
        return {
            "action": "restart_service",
            "service": svc.name,
            "status": svc.status,
            "memory_percent": svc.memory_percent,
            "latency_ms": svc.latency_ms,
            "message": f"Restarted '{svc.name}'. Status is now '{svc.status}'.",
        }

    def scale_service(self, name: str, replicas: int) -> dict[str, object]:
        if not 1 <= replicas <= 10:
            raise ValueError("replicas must be between 1 and 10")
        services = self._load()
        svc = self._get_or_raise(services, name)
        ts = _now_iso()
        old_replicas = svc.replicas
        scale_factor = old_replicas / replicas
        svc.replicas = replicas
        svc.cpu_percent = round(max(10.0, min(95.0, svc.cpu_percent * scale_factor)), 1)
        svc.latency_ms = max(15, int(svc.latency_ms * scale_factor))
        if svc.cpu_percent < 75.0 and svc.memory_percent < 85.0:
            svc.status = "healthy"
        svc.updated_at = ts
        svc.logs.append(
            f"{ts} [INFO] Scaled replicas from {old_replicas} to {replicas} (CPU now {svc.cpu_percent}%)"
        )
        self._save(services)
        return {
            "action": "scale_service",
            "service": svc.name,
            "old_replicas": old_replicas,
            "replicas": svc.replicas,
            "cpu_percent": svc.cpu_percent,
            "status": svc.status,
            "message": f"Scaled '{svc.name}' from {old_replicas} to {replicas} replicas.",
        }

    def rollback_deployment(self, name: str) -> dict[str, object]:
        services = self._load()
        svc = self._get_or_raise(services, name)
        ts = _now_iso()
        rolled_back_from = svc.version
        svc.version = svc.previous_version
        svc.status = "healthy"
        svc.cpu_percent = 28.0
        svc.memory_percent = 44.0
        svc.latency_ms = 62
        svc.updated_at = ts
        svc.logs.append(
            f"{ts} [INFO] Rolled back deployment from {rolled_back_from} to {svc.version}; metrics stabilized"
        )
        self._save(services)
        return {
            "action": "rollback_deployment",
            "service": svc.name,
            "rolled_back_from": rolled_back_from,
            "current_version": svc.version,
            "status": svc.status,
            "message": f"Rolled back '{svc.name}' from {rolled_back_from} to {svc.version}.",
        }
