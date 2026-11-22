# The interpreter validate_patch runs patches under. The test-env check reads this line to make sure
# its report describes the same interpreter (auditor/test_env_readiness.py, #136).
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim
ARG PYTHON_VERSION
ENV AI_AUDIT_VALIDATOR_PYTHON=${PYTHON_VERSION}

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
# argus-iris tenant manifest, the default ARGUS_GPU_TENANT_MANIFEST (auditor/gpu_lease.py)
COPY gpu-tenant.yaml /app/gpu-tenant.yaml

# ruff: fatal-only lint of patched files in validate_patch (per-project test venvs are built at runtime)
# uv: builds a test env on the interpreter a project's requires-python asks for when the image's
# doesn't satisfy it (validator.test_python, #141); its interpreters live in the venv cache.
RUN pip install --no-cache-dir . ruff uv==0.11.9

CMD ["python", "pipelines/nightly_repo_audit.py"]
