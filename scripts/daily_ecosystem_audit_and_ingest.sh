#!/usr/bin/env bash
# Run full ecosystem audit, then push briefings to mimiri (audit_docs).
# Intended for systemd timer / cron (see contrib/systemd/user).
#
# Requires: docker compose, repo .env with DATABASE_URL, INGEST_SHARED_SECRET, etc.
# Second step reuses the same image/volumes so the graph updated by the audit is visible to ingest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[daily] $(date -Iseconds) starting ecosystem audit …"
docker compose run --rm --no-deps auditor

echo "[daily] $(date -Iseconds) starting RAG ingest …"
docker compose run --rm --no-deps auditor sh -lc 'exec python scripts/ingest_ecosystem_to_rag.py'

echo "[daily] $(date -Iseconds) done."
