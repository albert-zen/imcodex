from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ObservabilityPaths:
    run_root: Path
    runs_dir: Path
    current_dir: Path
    instance_dir: Path
    instance_metadata_path: Path
    current_metadata_path: Path
    log_path: Path
    current_log_path: Path
    events_path: Path
    current_events_path: Path
    raw_protocol_path: Path
    current_raw_protocol_path: Path
    health_path: Path
    current_health_path: Path
    launch_path: Path
    current_launch_path: Path

    @classmethod
    def build(cls, run_root: Path, instance_id: str) -> "ObservabilityPaths":
        runs_dir = run_root / "runs"
        current_dir = run_root / "current"
        instance_dir = runs_dir / instance_id
        return cls(
            run_root=run_root,
            runs_dir=runs_dir,
            current_dir=current_dir,
            instance_dir=instance_dir,
            instance_metadata_path=instance_dir / "instance.json",
            current_metadata_path=current_dir / "instance.json",
            log_path=instance_dir / "bridge.log",
            current_log_path=current_dir / "bridge.log",
            events_path=instance_dir / "events.jsonl",
            current_events_path=current_dir / "events.jsonl",
            raw_protocol_path=instance_dir / "raw-protocol.jsonl",
            current_raw_protocol_path=current_dir / "raw-protocol.jsonl",
            health_path=instance_dir / "health.json",
            current_health_path=current_dir / "health.json",
            launch_path=instance_dir / "launch.json",
            current_launch_path=current_dir / "launch.json",
        )
