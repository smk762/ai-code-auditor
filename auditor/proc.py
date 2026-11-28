"""Run a child process so a timeout kills everything it started, not just the child itself.

``subprocess.run(..., timeout=...)`` kills only the direct child: pytest's workers, a test's own
servers or pip's build backends keep running, holding CPU, ports and temp dirs. ``run`` starts the
child in a new session (its own process group) and, on timeout, kills the whole group before
re-raising ``TimeoutExpired``, so callers keep their existing ``except`` clauses."""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

# How long output is still collected after the group was killed. A daemon that left the group
# (setsid, a double fork) keeps the pipes open, and reading them to EOF would wait on it forever.
_DRAIN_TIMEOUT_S = 5.0


def run(cmd: list[str], *, input: str | bytes | None = None, timeout: float | None = None, check: bool = False,
        capture_output: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with subprocess.Popen(cmd, start_new_session=True, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            try:
                out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                out = err = None  # what was read so far is dropped: the pipes are closed unread
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
                proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
        except BaseException:
            kill_group(proc)
            raise
    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


# What a child running code under audit may see of the host env. Everything else (API keys, the
# API token, DATABASE_URL, the RAG ingest URL and its signing secret, GITHUB_TOKEN, AI_AUDIT_*
# service config) stays out: a patch's tests are untrusted code, and a test that finds a live
# service URL in its env calls the real service (a RAG send that blocks for minutes while the GPU
# is busy, or worse, writes into the real collection).
_CHILD_ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TMP", "TEMP",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "PIP_CERT", "NODE_EXTRA_CA_CERTS",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CACHE_DIR",
    "npm_config_cache", "npm_config_registry",
    "GOPATH", "GOCACHE", "GOMODCACHE", "GOFLAGS", "GOPROXY", "CARGO_HOME", "RUSTUP_HOME",
})


def child_env(**extra: str) -> dict[str, str]:
    """A minimal env for a child that runs audited code (tests, installs, checks): the allowlist
    above from the host env, the locale's ``LC_*``, then *extra*."""
    env = {k: v for k, v in os.environ.items() if k in _CHILD_ENV_KEEP or k.startswith("LC_")}
    # The container's uid has no passwd entry, so getpass.getuser() raises unless these name someone
    # (torch's inductor calls it at import: argus-echo's miso tests errored at collection only here).
    for key in ("USER", "LOGNAME"):
        env.setdefault(key, "validate")
    env.update(extra)
    return env
