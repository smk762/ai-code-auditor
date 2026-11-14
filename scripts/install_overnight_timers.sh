#!/usr/bin/env bash
# Idempotent install of night-conductor, Docker ecosystem, merge-poll, batch-review and weekly wiki-refresh user timers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

cp -f "$ROOT/contrib/systemd/user/ai-audit-rest.service" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-rest.timer" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-docker-ecosystem.service" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-docker-ecosystem.timer" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-merge-poll.service" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-merge-poll.timer" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-batch-review.service" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-batch-review.timer" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-wiki-refresh.service" "$UNIT_DIR/"
cp -f "$ROOT/contrib/systemd/user/ai-audit-wiki-refresh.timer" "$UNIT_DIR/"

# Units ship with %h/ai-code-auditor in WorkingDirectory, EnvironmentFile and
# ExecStart. Rewrite every occurrence if this clone lives elsewhere under $HOME.
if [[ "$ROOT" == "$HOME"/* ]]; then
  rel="%h/${ROOT#"$HOME"/}"
  for f in ai-audit-rest.service ai-audit-docker-ecosystem.service ai-audit-merge-poll.service ai-audit-batch-review.service ai-audit-wiki-refresh.service; do
    sed -i "s|%h/ai-code-auditor|$rel|g" "$UNIT_DIR/$f"
  done
fi

systemctl --user daemon-reload
systemctl --user enable --now \
  ai-audit-rest.timer \
  ai-audit-docker-ecosystem.timer \
  ai-audit-merge-poll.timer \
  ai-audit-batch-review.timer \
  ai-audit-wiki-refresh.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

echo "Installed overnight + hourly merge-poll timers:"
systemctl --user list-timers --all | grep ai-audit || true
systemctl --user is-enabled \
  ai-audit-rest.timer \
  ai-audit-docker-ecosystem.timer \
  ai-audit-merge-poll.timer \
  ai-audit-batch-review.timer \
  ai-audit-wiki-refresh.timer
