from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class InstanceContext:
    instance_id: str
    pid: int
    started_at: str
    service_name: str
    cwd: str
    git_branch: str
    git_commit: str
    python_version: str
    http_host: str
    http_port: int
    app_server_url: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
