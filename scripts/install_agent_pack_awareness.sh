#!/usr/bin/env bash
# Install agent-pack awareness (AGENTS.md section + .cursor/rules/agent-coding-packs.mdc)
# into local checkouts listed in config/ecosystem.yaml. Local files only; no git commits.
#
#   bash scripts/install_agent_pack_awareness.sh [--dry-run] [--repo NAME] [--api-base URL] [--force]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY=python3; fi
fi
exec "$PY" scripts/install_agent_pack_awareness.py "$@"
