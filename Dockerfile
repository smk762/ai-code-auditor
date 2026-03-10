FROM python:3.11-slim

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
COPY cli.py /app/cli.py

RUN pip install --no-cache-dir .

CMD ["python", "pipelines/nightly_repo_audit.py"]
