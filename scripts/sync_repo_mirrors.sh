#!/usr/bin/env bash
# Sync local mirrors and check out default_branch (dev). Also invoked by rest-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python3 - <<'PY'
from conductor.mirrors import sync_mirrors
results = sync_mirrors()
ok = sum(1 for r in results if r.ok)
print(f"synced {ok}/{len(results)} mirrors")
for r in results:
    status = "ok" if r.ok else "FAIL"
    print(f"  [{status}] {r.name}: {r.action} branch={r.branch or '-'} {r.error}")
raise SystemExit(0 if all(r.ok for r in results) else 1)
PY
