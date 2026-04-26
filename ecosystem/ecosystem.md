## Repos

| Label | Repo | Role |
|-------|------|------|
| Core API | [kimini](https://github.com/smk762/kimini-api) | FastAPI platform backend — auth, chat, companions, economy, marketplace, generation proxies, WebSocket |
| Familiar UI | [somnus](https://github.com/smk762/somnus) | Frontend/operator app for familiar workflows, HITL review/rating, and training/evaluation operations |
| Image | [imogen](https://github.com/smk762/imogen) | Self-hosted GPU image generation (FLUX.1-dev + SDXL) + LoRA training and post-processing (absorbed from loraline/rubedo) |
| Video | [vidita](https://github.com/smk762/vidita) | Self-hosted GPU video generation (Wan 2.2) behind async gateway |
| Voice | [tss-stack](https://github.com/smk762/tss-stack) | TTS (XTTS) + STT (Whisper) voice gateway with async job API, presigned audio URLs |
| Ollama Chat | [agent-composer](https://github.com/smk762/agent-composer) | Ollama-backed chat + RAG ingest service, ModernBERT classifier sidecar, behind Cloudflare Zero Trust |
| Orchestrator | [gothmog](https://github.com/smk762/gothmog) | LangGraph/LangChain workflow orchestrator — multi-step pipelines across stacks |
| ComfyUI | [voluptas](https://github.com/smk762/voluptas) | ComfyUI inference node — docker-compose wrapper with GPU passthrough, workflow API at :8188 |
| Observability | [sauron](https://github.com/smk762/sauron) | Prometheus + Grafana + Loki + Promtail + Alertmanager for cross-stack metrics, logs, and alerting |

---

## Ecosystem Flow Diagram

```
                              ┌─────────────────────────────────────────────┐
                              │             CLIENTS / FRONTENDS             │
                              │  (somnus UI, web app, mobile, SDK, admin)   │
                              └────────────────────┬────────────────────────┘
                                                   │
                                        HTTP / WebSocket
                                                   │
                         ┌─────────────────────────▼──────────────────────────┐
                         │                                                    │
                         │            KIMINI  —  Core API  :8000              │
                         │         (fastapi-prod-skeleton)                    │
                         │                                                    │
                         │  ┌──────────┬──────────┬──────────┬─────────────┐  │
                         │  │ Auth     │ Chat     │ Economy  │ Marketplace │  │
                         │  │ Users    │ Convos   │ Gems     │ Creators    │  │
                         │  │ Personas │ Stories  │ Subs     │ Discover    │  │
                         │  ├──────────┴──────────┴──────────┴─────────────┤  │
                         │  │           Generation Domains                 │  │
                         │  │  imagegen │ videogen │ voicegen              │  │
                         │  ├──────────────────────────────────────────────┤  │
                         │  │  Orchestrate proxy  │  WebSocket (realtime)  │  │
                         │  ├──────────────────────────────────────────────┤  │
                         │  │  TaskIQ workers (high / default / low)       │  │
                         │  └──────────────────────────────────────────────┘  │
                         │                                                    │
                         └──┬──────┬──────┬──────┬──────┬──────┬─────────────┘
                            │      │      │      │      │      │
            ┌───────────────┘      │      │      │      │      └──────────────────┐
            │                ┌─────┘      │      └─────┐│                         │
            ▼                ▼            ▼            ▼▼                         ▼
  ┌─────────────────┐ ┌──────────┐ ┌──────────────┐ ┌────────────────┐ ┌──────────────────┐
  │  GOTHMOG :8030  │ │ IMOGEN   │ │  VIDITA      │ │ TSS-STACK      │ │ AGENT-COMPOSER   │
  │  Orchestrator   │ │ :8003    │ │  :8000       │ │ :9001 gateway  │ │ Ollama chat      │
  │                 │ │ img-gw   │ │  vid-gw      │ │                │ │                  │
  │ LangGraph       │ │          │ │              │ │ TTS (XTTS)     │ │ rag-chat  :9150  │
  │ workflows:      │ │ POST     │ │ POST         │ │ STT (Whisper)  │ │ rag-ingest :9050 │
  │ • img_generate  │ │ /images/ │ │ /videos/     │ │                │ │                  │
  │ • img_to_video  │ │ generate │ │ generate     │ │ /voices        │ │ Ollama :11434    │
  │ • char_create   │ │          │ │              │ │ /tts/jobs      │ │ (container-local)│
  │ • style_xfer    │ │ GET      │ │ GET          │ │ /stt/jobs      │ │                  │
  │ • batch_gen     │ │ /images/ │ │ /videos/     │ │ /tts/jobs/{id} │ │ Cloudflare       │
  │                 │ │ jobs/{id}│ │ jobs/{id}    │ │ /stt/jobs/{id} │ │ Zero Trust       │
  │ LLM calls:      │ │          │ │              │ │                │ └──────────────────┘
  │ prompt expand,  │ │ GPU:     │ │ GPU:         │ │ Services:      │
  │ style analysis  │ │ flux     │ │ wan22 :8010  │ │ xtts (engine)  │ ┌──────────────────┐
  │                 │ │ :8001    │ │ (internal)   │ │ tts-worker     │ │ VOLUPTAS :8188   │
  │ Tools call ─────┤►│ sdxl     │ │              │ │ whisper-worker │ │ ComfyUI          │
  │ img/vid/voice   │ │ :8002    │ │              │ │ redis, minio   │ │                  │
  └────────┬────────┘ │          │ │              │ │                │ │ somnus → direct  │
           │          │ LoRA /   │ │              │ │                │ │ WS + proxy       │
           │          │ train    │ │              │ │                │ └──────────────────┘
           │          │ :8004    │ │              │ │                │
           │          └──────────┘ └──────────────┘ └───────┬────────┘
           │               ▲             ▲              ▲   │
           │               │             │              │   │
           └───────────────┴─────────────┴──────────────┘   │
             gothmog calls img/vid/voice stacks              │
             directly via LangChain tools                    │
           kimini voicegen calls tss-stack ──────────────────┘

  ────────────────────────────────────────────────────────────────────────────
                         SHARED DATA LAYER
  ────────────────────────────────────────────────────────────────────────────

  ┌──────────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  ┌──────────────────┐
  │ PostgreSQL :5433     │  │ Redis :6380      │  │ MinIO (S3) :9000/:9001   │  │ Qdrant :6333     │
  │ NAS (.121)           │  │ NAS (.121)       │  │ NAS (.121)               │  │ .198 (agent-     │
  │                      │  │                  │  │                          │  │  composer)       │
  │  kimini tables       │  │  rate limits     │  │  uploads bucket          │  │  project_docs    │
  │  gothmog.runs        │  │  TaskIQ broker   │  │  generated/images/       │  │  RAG vectors     │
  │                      │  │  WS pub/sub      │  │  generated/videos/       │  │  code_audit      │
  │                      │  │  imgq:*, vidq:*  │  │  generated/audio/        │  │  audit_docs      │
  │                      │  │  orcq:*, orcr:*  │  │  comfyui/workflows/      │  │                  │
  │                      │  │  tss job queues  │  │  tts/stt audio artifacts │  │                  │
  └──────────────────────┘  └──────────────────┘  └──────────────────────────┘  └──────────────────┘

  ────────────────────────────────────────────────────────────────────────────
                           OBSERVABILITY  (sauron)
  ────────────────────────────────────────────────────────────────────────────

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ SAURON  (migrating to .198)                                              │
  │ prometheus:9090  grafana:3001  loki:3100  alertmanager:9093              │
  │ promtail ships Docker logs -> Loki                                       │
  │ Prometheus scrapes primary host services + remote GPU exporters          │
  │ (stack remains healthy if one GPU host is offline)                       │
  └──────────────────────────────────────────────────────────────────────────┘
```

RAG ingestion is served by `rag-ingest` on `192.168.1.198:9050` (inside `agent-composer`), colocated with Qdrant on `192.168.1.198:6333`, using Ollama container-locally at `http://ollama:11434`.

---

## Deployment Topology

### Host Allocation

| Host | Hardware | Services |
|------|----------|----------|
| `192.168.1.109` | Linux desktop, RTX 3090 24 GB, 64 GB RAM, Ryzen 5 7600 | `kimini-api`, `imogen` (+ LoRA/post-process absorbed), `vidita`, `voluptas` |
| `192.168.1.86` | — | `gothmog`, `somnus` |
| `192.168.1.198` | VM, RTX 4060 8 GB (passthrough), 16 GB RAM, 8 vCPU | `agent-composer` (Ollama + Qdrant + rag-ingest + ModernBERT), `tss-stack` *(pending)*, `sauron` *(pending)* |
| `192.168.1.121` | DS923+ NAS | PostgreSQL (`:5433`), Redis (`:6380`), MinIO (`:9000`) |

### Repo-to-Host Quick Matrix

| Repo | Host |
|------|------|
| `kimini-api` | `192.168.1.109` |
| `imogen` | `192.168.1.109` |
| `vidita` | `192.168.1.109` |
| `voluptas` | `192.168.1.109` |
| `gothmog` | `192.168.1.86` |
| `somnus` | `192.168.1.86` |
| `agent-composer` (+ Qdrant + rag-ingest) | `192.168.1.198` |
| `tss-stack` | `192.168.1.198` *(pending migration)* |
| `sauron` | `192.168.1.198` *(pending migration)* |
| PostgreSQL / Redis / MinIO | `192.168.1.121` (NAS) |
| ~~`mimiri`~~ | retired — `rag-ingest` lives in `agent-composer` |
| ~~`loraline`~~ | retired — absorbed into `imogen` |
| ~~`rubedo`~~ | retired — absorbed into `imogen` |
| ~~`test_dbs`~~ | retired — data layer split: NAS (.121) + Qdrant in agent-composer |

### Validation Commands (2-3 minute smoke check)

```bash
# NAS data layer (.121)
docker run --rm -e PGPASSWORD=testpass postgres:16 \
  psql -h 192.168.1.121 -p 5433 -U testuser -d ai_audit -c "SELECT 1" >/dev/null || echo "postgres (NAS) down"
redis-cli -h 192.168.1.121 -p 6380 -a testpass ping || echo "redis (NAS) down"
curl -fsS http://192.168.1.121:9000/minio/health/live || echo "minio (NAS) down"

# GPU + core API node (.109)
curl -fsS http://192.168.1.109:8000/health || echo "kimini down"
curl -fsS http://192.168.1.109:8003/health || echo "imogen down"
curl -fsS http://192.168.1.109:8000/videos/health || echo "vidita down"
curl -fsS http://192.168.1.109:8188/system_stats || echo "voluptas (comfyui) down"

# Orchestrator / UI node (.86)
curl -fsS http://192.168.1.86:8030/health || echo "gothmog down"
# somnus: check on its configured frontend port

# Voice/chat/RAG node (.198)
curl -fsS http://192.168.1.198:9001/health || echo "tss-stack down"
curl -fsS http://192.168.1.198:9150/health || echo "agent-composer-rag down"
curl -fsS http://192.168.1.198:9050/health || echo "rag-ingest down"
curl -fsS http://192.168.1.198:6333/collections >/dev/null || echo "qdrant down"
curl -fsS http://192.168.1.198:11434/api/tags >/dev/null || echo "ollama down"

# Optional observability (expected to fail until sauron migrated to .138)
curl -fsS http://192.168.1.198:9090/-/healthy || echo "prometheus down"
curl -fsS http://192.168.1.198:9100/metrics >/dev/null || echo "node exporter down"
curl -fsS http://192.168.1.109:9091/-/ready || echo "pushgateway down"
```

Expected baseline:
- Core checks (`kimini`, `gothmog`, `imogen`, `vidita`, `voluptas`, `agent-composer-rag`, `rag-ingest`, `qdrant`, `ollama`, NAS data layer) should pass.
- `tss-stack` and `sauron` expected to fail until migration to `.198` is complete.

### Practical notes

- `kimini-api`, `imogen`, `vidita`, `voluptas` run on `.109` (RTX 3090 — heaviest GPU workloads).
- `gothmog` and `somnus` run on `.86` (orchestration + operator UI).
- `agent-composer` with local Ollama, Qdrant, and `rag-ingest` runs on `.198`.
- `tss-stack` and `sauron` will migrate to `.198` when ready.
- PostgreSQL (`:5433`), Redis (`:6380`), and MinIO (`:9000`) run on the DS923+ NAS (`.121`) — stateful infra decoupled from compute nodes.
- Qdrant is container-local inside `agent-composer` on `.198`; `rag-ingest` uses `QDRANT_URL=http://qdrant:6333`.
- No automatic failover; optimize for deterministic single-job behavior.

---

### Cutover Checklist

Status legend: `[x] done`, `[ ] remaining`, `[~] partial`.

- [x] `.128` decommissioned.
- [x] `rag-ingest` and Qdrant colocated in `agent-composer` on `192.168.1.198` — `mimiri` retired.
- [x] `loraline` and `rubedo` functionality absorbed into `imogen`.
- [x] Stateful data layer (PostgreSQL, Redis) moved to NAS (`.121`, non-standard ports).
- [x] `gothmog` and `somnus` running on `192.168.1.86`.
- [x] `kimini-api` running on `192.168.1.109`.
- [~] `imogen` and `vidita` reachable but do not expose `/metrics` (health only).
- [ ] `tss-stack` migration to `.138` not yet complete.
- [ ] `sauron` migration to `.138` not yet complete.
- [ ] Update Prometheus scrape config in `sauron` — Qdrant target `.138:6333`; gothmog target `.86:8030`; Redis port `6380`.
- [ ] Confirm all ingest callers/scrape targets point to `192.168.1.198:9050`.
- [ ] `node exporter` (`.138:9100`) unreachable until sauron is deployed.
- [ ] Ensure `kimini` env routes image/video calls to `.109` and orchestrator calls to `.86:8030`.

---

## Request Flow: Direct Generation

```
Client
  │
  ├─► POST /v1/images/generate ──► Kimini ──► TaskIQ worker ──► Imogen gateway
  │                                                                 │
  │                                               flux/sdxl ◄───────┘
  │                                                  │
  │                                            GPU inference + post-process
  │                                                  │
  │                                            MinIO upload
  │                                                  │
  │   poll GET /v1/images/{job_id} ◄── Kimini ◄──── Redis job status
  │   or subscribe WS job:{job_id}
  │
  ├─► POST /v1/videos/generate ──► Kimini ──► TaskIQ worker ──► Vidita gateway
  │                                                                 │
  │                                                wan22 ◄──────────┘
  │                                                  │
  │                                            GPU inference → MinIO
  │
  ├─► POST /v1/voice/tts ──► Kimini ──► TaskIQ worker ──► tss-stack gateway :9001
  │                                                           │
  │                                              tts-worker ◄─┘──► xtts engine
  │                                                  │
  │                                            MinIO upload (presigned URL)
  │
  └─► POST /v1/voice/stt ──► Kimini ──► TaskIQ worker ──► tss-stack gateway :9001
                                                           │
                                         whisper-worker ◄─┘──► Whisper model
                                                  │
                                            MinIO upload (transcript)
```

---

## Request Flow: ComfyUI (somnus → voluptas direct)

```
Operator (browser)
  │
  │  WebSocket ws://NEXT_PUBLIC_COMFYUI_WS_URL/ws?clientId=X
  │  ───────────────────────────────────────────────────────► Voluptas :8188 (direct, LAN-only)
  │                                                            (WEB_ENABLE_AUTH=false)
  │
  ├─► POST /api/comfyui/prompt ──► somnus (server) ──proxy──► Voluptas :8188
  │         (submit workflow)                                   │
  │                                                             │  GPU inference (ComfyUI)
  │                                                             │
  │  WS message: executing / progress ◄──────────────────────────┘
  │  WS message: node=null → run complete
  │
  ├─► GET /api/comfyui/history/{prompt_id} ──proxy──► Voluptas /history/{id}
  │
  ├─► GET /api/comfyui/view?filename=...&type=output ──proxy──► Voluptas /view
  │
  └─► GET/POST/PUT/DELETE /api/comfyui-workflows[/{id}]
            (workflow CRUD — reads/writes MinIO uploads/comfyui/workflows/{id}.json)
            No Kimini in this path — somnus owns the workflow library directly.
```

Note: `COMFYUI_API_URL` (server-side proxy target) and `NEXT_PUBLIC_COMFYUI_WS_URL` (browser WS) are
separate env vars. The HTTP proxy uses the server-side var; the WebSocket connects directly from the
browser (no server-side WS proxy needed since ComfyUI is LAN-gated).

---

## Request Flow: Orchestrated Pipeline

```
Client
  │
  POST /v1/orchestrate/run { workflow: "character_create", input: {...} }
  │
  ▼
Kimini ──proxy──► Gothmog
                    │
                    ▼
              LangGraph executes workflow DAG:
              ┌──────────────────────────────────────────────────────────┐
              │                                                          │
              │  1. expand_character  ──► LLM (Ollama / OpenAI / etc.)   │
              │         │                                                │
              │         ▼                                                │
              │  2. generate_portraits ──► Imogen (3× txt2img)           │
              │         │                                                │
              │         ▼                                                │
              │  3. moderate_portraits ──► (placeholder / NudeNet)       │
              │         │                                                │
              │         ▼                                                │
              │  4. generate_video_intro ──► Vidita (img2vid)            │
              │                                                          │
              └──────────────────────────────────────────────────────────┘
                    │
                    ▼
              Result stored in Redis + Postgres
              │
              ▼
Client polls GET /v1/orchestrate/runs/{run_id}
  or streams via POST /v1/orchestrate/run/stream (SSE)
```

---

## Async Job Pattern (universal)

Every generation domain follows the same contract:

```
  Client                Kimini                  Backend Gateway
    │                     │                          │
    │  POST /v1/{domain}  │                          │
    │────────────────────►│  enqueue (TaskIQ/Redis)  │
    │  ◄── 202 {job_id}   │                          │
    │                     │  worker picks up job     │
    │                     │─────────────────────────►│  POST /{domain}/generate
    │                     │  ◄── 202 {backend_id}    │
    │                     │                          │  GPU inference...
    │                     │  poll loop               │
    │                     │─────────────────────────►│  GET /{domain}/jobs/{id}
    │                     │  ◄── {status, result}    │
    │                     │                          │
    │  poll or WS sub     │  update job in DB        │
    │────────────────────►│                          │
    │  ◄── {completed}    │                          │
```

Domains using this pattern: `imagegen`, `videogen`, `voicegen`

---

## Observability Flow (sauron)

```
Runtime services + infra exporters
  │
  ├─ metrics endpoints (/metrics) ───────────────► Prometheus (:9090)
  │                                                │
  │                                                ├─ alert rules ─► Alertmanager (:9093)
  │                                                └─ datasource ──► Grafana (:3001)
  │
  └─ Docker container logs ─► Promtail ───────────► Loki (:3100) ──► Grafana Explore

Remote host metrics scrapes are best-effort/opportunistic. In this homelab profile, degraded visibility is acceptable when a non-critical exporter is down.
```
