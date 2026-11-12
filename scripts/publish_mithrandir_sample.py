#!/usr/bin/env python3
"""Publish a sample LATEST envelope so Jarrah can smoke-poll before a full rest-run."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.env_loader import load_env_file

load_env_file()

from conductor.schemas import FindingsSummary, MithrandirStatus, RestWindowInfo
from conductor.wrapup import wrap_up


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default="auditor:sample-1")
    p.add_argument("--no-webhook", action="store_true")
    p.add_argument("--no-rag", action="store_true")
    args = p.parse_args()

    status = MithrandirStatus(
        run_id=args.run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        ended_reason="completed",
        summary_one_liner="Sample rest publish: clean completed, no decisions.",
        speakable=True,
        latest_url="http://192.168.1.198:8765/reports/mithrandir/LATEST.json",
        latest_md_url="http://192.168.1.198:8765/reports/mithrandir/LATEST.md",
        window=RestWindowInfo(start="00:30", end="06:00"),
        repos=["argus-lens", "argus-cull"],
        scanner_counts={"semgrep": 0, "gitleaks": 0, "osv": 0},
        findings=FindingsSummary(),
        draft_prs=[],
        decisions_needed=[],
        worker_stats={"tasks": 0},
        next_rest_window="tomorrow 00:30",
    )
    result = wrap_up(
        status,
        send_webhook=not args.no_webhook,
        ingest_rag=not args.no_rag,
    )
    print(result.get("json_path"))
    print(result.get("webhook"))


if __name__ == "__main__":
    main()
