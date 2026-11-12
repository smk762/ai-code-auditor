"""CLI/pipeline entry for night conductor rest sessions."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.config import load_ecosystem_config
from auditor.runtime import setup_logging
from conductor.session import run_rest_session

logger = logging.getLogger(__name__)


def run(
    *,
    dry_run: bool = False,
    force: bool = False,
    no_webhook: bool = False,
    no_rag: bool = False,
    no_draft_prs: bool = False,
    no_pr_review: bool = False,
) -> dict:
    cfg = load_ecosystem_config()
    setup_logging(level=cfg.log_level, log_file_path=cfg.log_file_path, output_dir=cfg.output_dir)
    logger.info("Starting night conductor rest session (dry_run=%s force=%s)", dry_run, force)
    result = run_rest_session(
        dry_run=dry_run,
        force=force,
        output_dir=cfg.output_dir,
        enable_draft_prs=not no_draft_prs,
        enable_pr_review=not no_pr_review,
        send_webhook=not no_webhook,
        ingest_rag=not no_rag,
    )
    logger.info(
        "Rest session published %s (webhook=%s)",
        result.get("json_path"),
        result.get("webhook"),
    )
    return result


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="Night conductor rest session")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Run even outside rest window")
    p.add_argument("--no-webhook", action="store_true")
    p.add_argument("--no-rag", action="store_true")
    p.add_argument("--no-draft-prs", action="store_true")
    p.add_argument("--no-pr-review", action="store_true")
    args = p.parse_args()
    out = run(
        dry_run=args.dry_run,
        force=args.force,
        no_webhook=args.no_webhook,
        no_rag=args.no_rag,
        no_draft_prs=args.no_draft_prs,
        no_pr_review=args.no_pr_review,
    )
    print(json.dumps({"run_id": out.get("status", {}).get("run_id"), "paths": {"json": out.get("json_path"), "md": out.get("md_path")}, "webhook": out.get("webhook")}, indent=2))
