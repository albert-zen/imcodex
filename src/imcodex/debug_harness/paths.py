from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class DebugHarnessPaths:
    root: Path
    manifests_dir: Path
    cwd_dir: Path
    data_dir: Path
    run_dir: Path
    cwd_path: Path
    data_path: Path
    observability_run_path: Path
    manifest_path: Path

    @classmethod
    def build(cls, root: Path, run_id: str) -> "DebugHarnessPaths":
        manifests_dir = root / "manifests"
        cwd_dir = root / "cwd"
        data_dir = root / "data"
        run_dir = root / "run"
        return cls(
            root=root,
            manifests_dir=manifests_dir,
            cwd_dir=cwd_dir,
            data_dir=data_dir,
            run_dir=run_dir,
            cwd_path=cwd_dir / run_id,
            data_path=data_dir / run_id,
            observability_run_path=run_dir / run_id,
            manifest_path=manifests_dir / f"{run_id}.json",
        )
