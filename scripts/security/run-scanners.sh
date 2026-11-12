#!/usr/bin/env bash
# Pinned deterministic scanners for night conductor.
# Versions: semgrep 1.178.0, gitleaks 8.30.1, osv-scanner 2.6.0 (bump in own PR).
set -u
ROOT="${1:-.}"
out="${SECURITY_OUT:-security-out}"
mkdir -p "$out"
: > "$out/exit-codes.txt"
echo "commit=${GITHUB_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}" > "$out/run-meta.txt"
{
  command -v semgrep >/dev/null && semgrep --version
  command -v gitleaks >/dev/null && gitleaks version
  command -v osv-scanner >/dev/null && osv-scanner --version | head -1
} > "$out/tool-versions.txt" 2>/dev/null || true

if command -v semgrep >/dev/null 2>&1; then
  (cd "$ROOT" && semgrep scan --config p/default --metrics=off --error --sarif-output="$out/semgrep.sarif") || true
  echo "semgrep=$?" >> "$out/exit-codes.txt"
else
  echo "semgrep=127" >> "$out/exit-codes.txt"
  echo '{"version":"2.1.0","runs":[]}' > "$out/semgrep.sarif"
fi

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --redact --report-format sarif --report-path "$out/gitleaks.sarif" "$ROOT" || true
  echo "gitleaks=$?" >> "$out/exit-codes.txt"
else
  echo "gitleaks=127" >> "$out/exit-codes.txt"
  echo '{"version":"2.1.0","runs":[]}' > "$out/gitleaks.sarif"
fi

if command -v osv-scanner >/dev/null 2>&1; then
  osv-scanner scan source -r --format sarif --output-file "$out/osv.sarif" "$ROOT" || true
  echo "osv=$?" >> "$out/exit-codes.txt"
else
  echo "osv=127" >> "$out/exit-codes.txt"
  echo '{"version":"2.1.0","runs":[]}' > "$out/osv.sarif"
fi

for f in semgrep gitleaks osv; do
  test -s "$out/$f.sarif" || echo '{"version":"2.1.0","runs":[]}' > "$out/$f.sarif"
done
