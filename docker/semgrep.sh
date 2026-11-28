#!/bin/sh
# semgrep in the image (Dockerfile): a writable home when HOME isn't one, and no network calls of its own.
if [ ! -w "${HOME:-/}" ]; then
    HOME="${TMPDIR:-/tmp}/semgrep-home-$(id -u)"
    mkdir -p "$HOME"
    export HOME
fi
export SEMGREP_SEND_METRICS=off SEMGREP_ENABLE_VERSION_CHECK=0
exec /opt/semgrep/bin/semgrep "$@"
