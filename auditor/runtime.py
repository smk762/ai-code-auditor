from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def setup_logging(level: str = "INFO", log_file_path: str = "", output_dir: str = "reports") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file_path:
        safe_path = _safe_log_path(log_file_path, output_dir)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(safe_path))
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
    )


def _safe_log_path(log_file_path: str, output_dir: str) -> Path:
    out = Path(output_dir).resolve()
    candidate = Path(log_file_path)
    if not candidate.is_absolute():
        candidate = (out / candidate).resolve()
    if out not in candidate.parents and candidate != out:
        raise ValueError("log_file_path must stay within output_dir for security")
    return candidate


@dataclass(slots=True)
class RunMetadata:
    run_id: str
    started_at: str
    finished_at: str = ""
    status: str = "running"
    scanned_repos: list[str] = field(default_factory=list)
    scanned_files: int = 0
    code_units: int = 0
    findings: int = 0
    violations: int = 0
    stage_timings_ms: dict[str, int] = field(default_factory=dict)
    stage_status: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @staticmethod
    def start(prefix: str, run_id: str = "") -> "RunMetadata":
        now = datetime.now(timezone.utc).isoformat()
        if not run_id:
            run_id = f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        return RunMetadata(run_id=run_id, started_at=now)

    def finish(self, status: str = "success") -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.status = status

    def mark_stage(self, stage: str, elapsed_ms: int, status: str = "success") -> None:
        self.stage_timings_ms[stage] = elapsed_ms
        self.stage_status[stage] = status

    def add_error(self, message: str) -> None:
        self.errors.append(message)


def require_auth(enabled: bool, env_name: str, provided_code: str = "") -> None:
    if not enabled:
        return
    expected = os.getenv(env_name, "")
    if not expected:
        raise ValueError(f"Auth enabled but env var '{env_name}' is not set")
    if provided_code != expected:
        raise PermissionError("Invalid auth code")


def write_run_metadata(metadata: RunMetadata, output_dir: str | Path = "reports") -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run_{metadata.run_id}.json"
    path.write_text(json.dumps(asdict(metadata), indent=2), encoding="utf-8")
    return path
