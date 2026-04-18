from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class DebugRunManifest:
    run_id: str
    pid: int
    port: int
    purpose: str | None
    cwd: str
    data_dir: str
    run_dir: str
    started_at: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DebugRunManifest":
        return cls(
            run_id=str(payload["run_id"]),
            pid=int(payload["pid"]),
            port=int(payload["port"]),
            purpose=str(payload["purpose"]) if payload.get("purpose") is not None else None,
            cwd=str(payload["cwd"]),
            data_dir=str(payload["data_dir"]),
            run_dir=str(payload["run_dir"]),
            started_at=str(payload["started_at"]),
            status=str(payload["status"]),
        )
