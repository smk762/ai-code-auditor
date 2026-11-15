FROM python:3.11-slim

# git: ecosystem_audit / extraction metrics (subprocess git). wget: compose healthcheck for audit-api.
# graphviz: `dot` binary, used by service_topology renderer.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git wget ca-certificates graphviz \
    && rm -rf /var/lib/apt/lists/*

# node + npm: validate_patch runs TS/JS projects' typecheck, lint, *:check and tests (vitest 4 needs node 20+,
# Debian's nodejs is 18). Copied from the official image instead of apt.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY auditor /app/auditor
COPY conductor /app/conductor
COPY ecosystem /app/ecosystem
COPY patterns /app/patterns
COPY semantic /app/semantic
COPY copilot /app/copilot
COPY pipelines /app/pipelines
COPY qa /app/qa
COPY wiki /app/wiki
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY config /app/config
COPY memory /app/memory
COPY scripts /app/scripts
COPY cli.py /app/cli.py

# ruff: fatal-only lint of patched files in validate_patch (per-project test venvs are built at runtime)
RUN pip install --no-cache-dir . ruff

CMD ["python", "pipelines/nightly_repo_audit.py"]
