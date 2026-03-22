FROM python:3.11-slim

# git: ecosystem_audit / extraction metrics (subprocess git). wget: compose healthcheck for audit-api.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY auditor /app/auditor
COPY ecosystem /app/ecosystem
COPY patterns /app/patterns
COPY semantic /app/semantic
COPY copilot /app/copilot
COPY pipelines /app/pipelines
COPY qa /app/qa
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY config /app/config
COPY memory /app/memory
COPY scripts /app/scripts
COPY cli.py /app/cli.py

RUN pip install --no-cache-dir .

CMD ["python", "pipelines/nightly_repo_audit.py"]
