# Deployment

How to run the auditor every night against several repos. To try it on one repo, use the
[README quick start](../README.md#quick-start) instead.

The checked-in `config/` describes the author's deployment. Before running your own, replace:

- `config/ecosystem.yaml`: the repos to scan
- `config/repo_mirrors.yaml`: where their git mirrors live and which branch to follow
- `config/architecture_rules.yaml`: allowed calls and service roles between your repos
- `ecosystem/ecosystem.md`: the role table and flow. Scan prompts include it, so leaving the author's text in place sends
  it to your model provider.
- the bind mounts in `docker-compose.yml`

## Environment

```bash
cp .env.example .env      # replace every <PLACEHOLDER>
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # extras: semantic (FAISS), conductor (LiteLLM), rules (the pinned semgrep)
alembic upgrade head
```

| Service | Needed for | Without it |
|---|---|---|
| Postgres (`DATABASE_URL`) | run history and findings | SQLite when unset |
| Redis (`REDIS_URL`) | one pipeline run at a time | continues with a warning, unless `REDIS_LOCK_REQUIRED=true` |
| A model provider (`AI_AUDIT_LLM_*`) | model review of code units | fallback pattern rules and static tools; files are recorded as unreviewed |
| `GITHUB_TOKEN` | private mirrors, PR intel, merge polling, agent PR metrics | those steps are skipped or use a cache |
| A RAG service (`AI_AUDIT_RAG_INGEST_URL`, `RAG_CHAT_URL`) | `search`, `repair`, sending packs and wiki text | set `AI_AUDIT_PACK_RAG_INGEST=0` |

`.env.example` sets `REDIS_LOCK_REQUIRED=true`.

## Repositories

Each entry under `repos` in `config/ecosystem.yaml` needs a `name` and a `path`. `subpath` limits the scan to one folder.

```yaml
repos:
  # A clone on disk: the checked-out commit is scanned and `branch` is not applied.
  - name: my-service
    path: /repos/my-service
    provider: local
    subpath: ""

  # A remote: cloned into local_cache_path. access_token_env names the variable holding a token.
  - name: my-other-service
    path: https://github.com/org/my-other-service.git
    branch: main
    provider: remote
    access_token_env: GITHUB_TOKEN
    local_cache_path: .cache/repos
```

Patch validation, the structure scan and the seeded evals read the mirrors in `config/repo_mirrors.yaml`, from
`origin/<branch>`. `ai-audit rest-run` syncs mirrors first. `bash scripts/sync_repo_mirrors.sh` does it by hand. A mirror
with a unique local commit is never reset.

## Docker

```bash
docker compose up -d --build audit-api        # HTTP API and web UI on port 8765
docker compose run --rm --no-deps auditor     # one ecosystem pass, then exit
```

- Paths in `config/ecosystem.yaml` must exist inside the container. Bind-mount the mirrors at the same absolute paths,
  read-only.
- `DATABASE_URL` and `REDIS_URL` must be reachable from the container, so not `127.0.0.1` unless it uses host networking.
- The base service reserves an NVIDIA GPU. Remove the `deploy` block on a host without one.
- Set `AI_AUDIT_API_TOKEN` (`openssl rand -hex 32`). Read-only pages need no authentication, so keep the port on loopback
  (`AUDIT_API_BIND_IP=127.0.0.1`) or a trusted network.

## Nightly timers

```bash
bash scripts/install_overnight_timers.sh      # systemd user timers
```

Units are in [contrib/systemd/user](../contrib/systemd/user). Set `AI_AUDIT_REST_TZ` to the host's timezone; the window
is in `config/rest_windows.yaml`. `ai-audit rest-run --force` runs outside it, and `--dry-run` stubs the workers.

## Status reports and webhooks

At wrap-up the rest session writes a status envelope, served at `GET /reports/mithrandir/LATEST.json`, and posts it to
`AI_AUDIT_MITHRANDIR_WEBHOOK_URL` when set. `AI_AUDIT_STATUS_WEBHOOKS` adds targets as a JSON list of
`{"name", "url", "token_env"}`. `token_env` names the variable with that target's bearer token; when it is empty the
target is skipped rather than sent without a token.

Merge-triggered refreshes are in [the webhook notes](../contrib/github/merge-refresh-webhook.md), and the repair loop in
[the repair quick start](repair-quickstart.md).
