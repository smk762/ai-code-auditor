#!/usr/bin/env python3
"""Push ecosystem architectural knowledge into the mimiri RAG ingest service.

This feeds the agent-composer copilot with structured knowledge about the stack
so it can answer cross-repo questions without hallucinating.  Run this once after
a significant architecture change, or as part of the nightly pipeline.

Usage:
    python scripts/ingest_ecosystem_to_rag.py [--dry-run]

Environment variables:
    MIMIRI_URL              Ingest endpoint (default: http://127.0.0.1:9050/ingest)
    INGEST_SHARED_SECRET    HMAC secret matching mimiri's config
    QDRANT_COLLECTION       Collection to upsert into (default: project_docs)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auditor.env_loader import load_env_file
load_env_file()

import requests

from auditor.ecosystem_briefing import build_ecosystem_overview, build_repo_briefing, _get_repo_roles
from auditor.config import load_ecosystem_config
from ecosystem.api_mapper import discover_api_endpoints
from ecosystem.graph_builder import GraphBuilder
from auditor.repo_resolver import resolve_repo_path

_DEFAULT_MIMIRI_URL = "http://127.0.0.1:9050/ingest"
_DEFAULT_COLLECTION = "project_docs"


def _sign_payload(secret: str, payload: bytes, timestamp: int) -> str:
    msg = f"{timestamp}:".encode() + payload
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _ingest_document(
    url: str,
    secret: str,
    collection: str,
    doc_id: str,
    text: str,
    metadata: dict,
    dry_run: bool,
) -> bool:
    body = {
        "collection": collection,
        "documents": [
            {
                "id": doc_id,
                "text": text,
                "metadata": metadata,
            }
        ],
    }
    payload = json.dumps(body).encode()
    ts = int(time.time())
    sig = _sign_payload(secret, payload, ts)

    if dry_run:
        print(f"  [dry-run] Would POST {len(text)} chars → {doc_id}")
        return True

    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": str(ts),
        "X-Signature": sig,
    }
    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"  ERROR ingesting {doc_id}: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest ecosystem docs into mimiri RAG.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent without posting.")
    args = parser.parse_args()

    cfg = load_ecosystem_config()

    # Config-first: ecosystem.yaml defines the isolated collection and URL.
    # Env vars override if explicitly set.
    url = os.getenv("MIMIRI_URL", cfg.rag_ingest_url)
    secret = os.getenv("INGEST_SHARED_SECRET", "")
    collection = os.getenv("QDRANT_COLLECTION", cfg.rag_collection)

    print(f"Target: {url}  collection: {collection}")

    if not secret and not args.dry_run:
        sys.exit("INGEST_SHARED_SECRET is required (set in env or .env file).")
    graph = GraphBuilder(cfg.graph_db_path)

    ok = 0
    fail = 0

    # 1. Ecosystem overview — one document describing the whole stack
    print("Ingesting ecosystem overview …")
    overview = build_ecosystem_overview(max_chars=8000)
    if _ingest_document(url, secret, collection, "ecosystem::overview", overview,
                        {"type": "architecture", "scope": "ecosystem"}, args.dry_run):
        ok += 1
    else:
        fail += 1

    # 2. Per-repo briefings
    roles = _get_repo_roles()
    for repo in cfg.repos:
        print(f"Ingesting briefing for {repo.name} …")
        repo_path = resolve_repo_path(repo)
        briefing = build_repo_briefing(repo.name, repo_path, graph=graph, max_chars=4000)

        # Append endpoint list if the repo is available
        endpoints: list[str] = []
        if repo_path.exists():
            for py_file in repo_path.rglob("*.py"):
                if any(p in py_file.parts for p in {".git", ".venv", "__pycache__"}):
                    continue
                endpoints.extend(discover_api_endpoints(py_file))

        if endpoints:
            briefing += "\n\nAll discovered endpoints:\n" + "\n".join(f"  {e}" for e in sorted(set(endpoints)))

        meta = {
            "type": "repo_briefing",
            "repo": repo.name,
            "label": roles.get(repo.name, {}).get("label", ""),
        }
        if _ingest_document(url, secret, collection, f"ecosystem::{repo.name}", briefing, meta, args.dry_run):
            ok += 1
        else:
            fail += 1

    graph.close()
    print(f"\nDone. {ok} documents ingested, {fail} failed.")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
