## Repos

| Label | Repo | Role |
|-------|------|------|
| Core API | [kimini](https://github.com/smk762/kimini-api) | FastAPI platform backend — auth, chat, companions, economy, marketplace, generation proxies, WebSocket |
| Familiar UI | [somnus](https://github.com/smk762/somnus) | Frontend/operator app for familiar workflows, HITL review/rating, and training/evaluation operations |
| Image | [imogen](https://github.com/smk762/imogen) | Self-hosted GPU image generation (FLUX.1-dev + SDXL) behind async gateway |
| Video | [vidita](https://github.com/smk762/vidita) | Self-hosted GPU video generation (Wan 2.2) behind async gateway |
| Voice | [tss-stack](https://github.com/smk762/tss-stack) | TTS (XTTS) + STT (Whisper) voice gateway with async job API, presigned audio URLs |
| LoRA | [loraline](https://github.com/smk762/loraline) | LoRA fine-tuning control plane + proxy for image/video/chat/voice jobs |
| Ollama Chat | [agent-composer](https://github.com/smk762/agent-composer) | Ollama-backed chat service (RAG chat endpoint) behind Cloudflare Zero Trust |
| RAG Ingest | [mimiri](https://github.com/smk762/mimiri) | Standalone `rag-ingest` service with signed ingestion API (`/ingest`) for Qdrant upserts via external Ollama embeddings |
| Orchestrator | [gothmog](https://github.com/smk762/gothmog) | LangGraph/LangChain workflow orchestrator — multi-step pipelines across stacks |
| Infra | [test_dbs](https://github.com/smk762/test_dbs) | PostgreSQL, Redis, MinIO, Qdrant — shared data layer |
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
                         │  │  imagegen │ videogen │ voicegen │ loragen    │  │
                         │  ├──────────────────────────────────────────────┤  │
                         │  │  Orchestrate proxy  │  WebSocket (realtime)  │  │
                         │  ├──────────────────────────────────────────────┤  │
                         │  │  TaskIQ workers (high / default / low)       │  │
                         │  └──────────────────────────────────────────────┘  │
                         │                                                    │
                         └──┬──────┬──────┬──────┬──────┬──────┬──────┬───────┘
                            │      │      │      │      │      │      │
            ┌───────────────┘      │      │      │      │      │      └──────────────────┐
            │                ┌─────┘      │      │      └─────┐│                         │
            ▼                ▼            ▼      ▼            ▼▼                         ▼
  ┌─────────────────┐ ┌──────────┐ ┌──────────────┐ ┌────────────────┐ ┌──────────────────┐
  │  GOTHMOG :8030  │ │ IMOGEN   │ │  VIDITA      │ │ TSS-STACK      │ │ AGENT-COMPOSER   │
  │  Orchestrator   │ │ :8003    │ │  :8000       │ │ :9001 gateway  │ │ Ollama chat      │
  │                 │ │ img-gw   │ │  vid-gw      │ │                │ │                  │
  │ LangGraph       │ │          │ │              │ │ TTS (XTTS)     │ │ rag-chat  :9150  │
  │ workflows:      │ │ POST     │ │ POST         │ │ STT (Whisper)  │ │ mimiri    :9050* │
  │ • img_generate  │ │ /images/ │ │ /videos/     │ │                │ │                  │
  │ • img_to_video  │ │ generate │ │ generate     │ │ /voices        │ │ Ollama (remote)  │
  │ • char_create   │ │          │ │              │ │ /tts/jobs      │ │ :11434 @ .138    │
  │ • style_xfer    │ │ GET      │ │ GET          │ │ /stt/jobs      │ │                  │
  │ • batch_gen     │ │ /images/ │ │ /videos/     │ │ /tts/jobs/{id} │ │ Cloudflare       │
  │                 │ │ jobs/{id}│ │ jobs/{id}    │ │ /stt/jobs/{id} │ │ Zero Trust       │
  │ LLM calls:      │ │          │ │              │ │                │ └──────────────────┘
  │ prompt expand,  │ │ GPU:     │ │ GPU:         │ │ Services:      │
  │ style analysis  │ │ flux     │ │ wan22 :8010  │ │ xtts (engine)  │ ┌─────────────────┐
  │                 │ │ :8001    │ │ (internal)   │ │ tts-worker     │ │ LORALINE :8010  │
  │ Tools call ─────┤►│ sdxl     │ │              │ │ whisper-worker │ │ LoRA + Proxy    │
  │ img/vid/voice   │ │ :8002    │ │              │ │ redis, minio   │ │                 │
  └────────┬────────┘ └──────────┘ └──────────────┘ └───────┬────────┘ │ LoRA training   │
           │               ▲             ▲              ▲   │          │ Image proxy ──►─┤─► imogen
           │               │             │              │   │          │ Video proxy ──►─┤─► vidita
           │               │             │              │   │          │ Chat  (Ollama)  │
           └───────────────┴─────────────┴──────────────┘   │          │ Voice (tss) ──►─┤─► tss-stack
             gothmog also calls img/vid/voice stacks        │          │                 │
             directly via LangChain tools                   │          │ POST/GET        │
                                                            │          │ /v1/lora/jobs   │
           kimini voicegen calls tss-stack ─────────────────┘          │ /v1/image/jobs  │
                                                                       │ /v1/video/jobs  │
                                                                       │ /v1/chat/jobs   │
                                                                       └─────────────────┘

  ────────────────────────────────────────────────────────────────────────────
                         SHARED DATA LAYER  (test_dbs)
  ────────────────────────────────────────────────────────────────────────────

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐  ┌──────────────────┐
  │  PostgreSQL :5432│  │  Redis :6379     │  │  MinIO (S3) :9000/:9001  │  │  Qdrant :6333    │
  │                  │  │                  │  │                          │  │                  │
  │  kimini tables   │  │  rate limits     │  │  uploads bucket          │  │  project_docs    │
  │  gothmog.runs    │  │  TaskIQ broker   │  │  generated/images/       │  │  RAG vectors     │
  │  loraline jobs   │  │  WS pub/sub      │  │  generated/videos/       │  │  mimiri upserts  │
  │                  │  │  imgq:*, vidq:*  │  │  generated/audio/        │  │  rag-chat reads  │
  │                  │  │  orcq:*, orcr:*  │  │  lora artifacts          │  │                  │
  │                  │  │  tss job queues  │  │  tts/stt audio artifacts │  │                  │
  └──────────────────┘  └──────────────────┘  └──────────────────────────┘  └──────────────────┘

  ────────────────────────────────────────────────────────────────────────────
                           OBSERVABILITY  (sauron)
  ────────────────────────────────────────────────────────────────────────────

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ SAURON                                                                   │
  │ prometheus:9090  grafana:3001  loki:3100  alertmanager:9093              │
  │ promtail ships Docker logs -> Loki                                       │
  │ Prometheus scrapes primary host services + remote GPU exporters          │
  │ (stack remains healthy if one GPU host is offline)                       │
  └──────────────────────────────────────────────────────────────────────────┘
```

RAG ingestion is served by `mimiri` (`rag-ingest`) on `192.168.1.128:9050`, colocated with Qdrant on `192.168.1.128:6333`, and using Ollama on `192.168.1.138:11434` (`/ui/ingest` only when `INGEST_REQUIRE_ENCRYPTION=0`).

---

## Deployment Topology

### Recommended Host Allocation (homelab test/QA, no failover)

| Host | Profile | Recommended services | Why this fit is optimal |
|------|---------|----------------------|--------------------------|
| `192.168.1.109` | Linux desktop, RTX 3090 24 GB, 64 GB RAM, Ryzen 5 7600 | `imogen`, `vidita`, `loraline` | Best single-job GPU capacity. Keep the heaviest CUDA workloads and LoRA training on the strongest GPU host for predictable latency in test runs. |
| `192.168.1.138` | VM, RTX 4060 8 GB (passthrough), 16 GB RAM, 8 vCPU | `tss-stack`, `agent-composer` (Ollama runtime/chat) | Good fit for moderate GPU services (Whisper/XTTS + small/medium Ollama). Keeps conversational workloads off the control-plane host. |
| `192.168.1.128` | VM, RTX 4060 8 GB (passthrough), 64 GB RAM, 4 vCPU | `kimini`, `somnus`, `gothmog`, `test_dbs` (PostgreSQL/Redis/MinIO/Qdrant), `sauron`, `mimiri` (`rag-ingest`) | Central control/data/observability node. Colocating `somnus` with `kimini` keeps operator UI calls low-latency, and colocating `mimiri` with Qdrant reduces ingest path complexity and cross-host vector write latency. |

### Repo-to-Host Quick Matrix (target state)

| Repo | Primary host |
|------|--------------|
| `kimini-api` | `192.168.1.128` |
| `somnus` | `192.168.1.128` |
| `gothmog` | `192.168.1.128` |
| `test_dbs` | `192.168.1.128` |
| `sauron` | `192.168.1.128` |
| `mimiri` | `192.168.1.128` |
| `imogen` | `192.168.1.109` |
| `vidita` | `192.168.1.109` |
| `loraline` | `192.168.1.109` |
| `tss-stack` | `192.168.1.138` |
| `agent-composer` | `192.168.1.138` |

### Validation Commands (2-3 minute smoke check)

Run these from any host with LAN reachability:

```bash
# Control plane + data + observability (.128)
curl -fsS http://192.168.1.128:8000/health || echo "kimini down"
curl -fsS http://192.168.1.128:8030/health || echo "gothmog down"
curl -fsS http://192.168.1.128:6333/collections >/dev/null || echo "qdrant down"
curl -fsS http://192.168.1.128:9050/health || echo "mimiri down"
curl -fsS http://192.168.1.128:9090/-/healthy || echo "prometheus down"

# GPU generation node (.109)
curl -fsS http://192.168.1.109:8003/health || echo "imogen down"
curl -fsS http://192.168.1.109:8000/videos/health || echo "vidita down"
curl -fsS http://192.168.1.109:8010/health || echo "loraline down"

# Voice/chat node (.138)
curl -fsS http://192.168.1.138:9001/health || echo "tss-stack down"
curl -fsS http://192.168.1.138:9150/health || echo "agent-composer-rag down"
curl -fsS http://192.168.1.138:11434/api/tags >/dev/null || echo "ollama down"

# Optional observability targets (expected to fail if intentionally not deployed)
curl -fsS http://192.168.1.138:9100/metrics >/dev/null || echo "node-host-b exporter down"
curl -fsS http://192.168.1.109:9091/-/ready || echo "pushgateway down"
```

Expected baseline for this homelab profile:

- Core checks (`kimini`, `gothmog`, `qdrant`, `mimiri`, `imogen`, `vidita`, `loraline`, `tss-stack`, `agent-composer-rag`, `ollama`) should pass.
- `node-host-b exporter` and `pushgateway` may fail if not intentionally deployed yet.
- `somnus` should be reachable on its configured frontend route on `192.168.1.128` when the familiar UI is deployed.

### Practical notes (single-job capacity first)

- Keep `kimini` workers (`imagegen`, `videogen`, `voicegen`, `loragen`) on `192.168.1.128`; call GPU services remotely over LAN.
- Run `somnus` on `192.168.1.128` alongside `kimini` for low-latency operator UI/API interactions.
- Use `192.168.1.109` as the primary inference/training target for `imogen`, `vidita`, and `loraline`.
- Use `192.168.1.138` as the default voice/chat node (`tss-stack`, `agent-composer`) with local Ollama.
- No automatic failover needed in this profile; optimize for deterministic single-job behavior and simpler operations.
- Keep Postgres/Redis/MinIO/Qdrant and observability (`sauron`) on `192.168.1.128` to avoid coupling stateful infra with GPU runtime churn.
- Run `mimiri` on `192.168.1.128` with `QDRANT_URL=http://192.168.1.128:6333` and `OLLAMA_URL=http://192.168.1.138:11434`.

---

### Cutover Checklist (status-aware)

Status legend: `[x] done`, `[ ] remaining`, `[~] partial`.

Based on latest probe report from this host:

- [ ] Confirm all ingest callers/scrape targets now point to `mimiri` at `192.168.1.128:9050` after host move.
- [x] Core control-plane/data services are reachable with metrics (`kimini`, `gothmog`, `redis-exporter`, `postgres-exporter`, `node-host-a`, `node-host-c`).
- [x] `tss-stack` and `loraline` are reachable with metrics.
- [x] `mimiri` live placement is aligned to `192.168.1.128:9050`.
- [~] `imogen` and `vidita` are reachable but do not expose `/metrics` (health only).
- [ ] `node-host-b` exporter (`192.168.1.138:9100`) is still unreachable.
- [ ] `pushgateway` scrape target is still unreachable from this vantage point.
- [ ] Ensure `kimini` production env routes image/video/lora calls to `192.168.1.109` and orchestrator calls to `192.168.1.128` (current local `.env` does not yet reflect this ideal).
- [ ] Ensure Prometheus jobs use scrape paths that match reality (`/metrics` where available; health-only checks where metrics are not exposed).

For this homelab profile, no failover setup is required; close the remaining items only to improve observability and endpoint correctness.

---

## Request Flow: Direct Generation

```
Client
  │
  ├─► POST /v1/images/generate ──► Kimini ──► TaskIQ worker ──► Imogen gateway
  │                                                                 │
  │                                               flux/sdxl ◄───────┘
  │                                                  │
  │                                            GPU inference
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
  ├─► POST /v1/voice/stt ──► Kimini ──► TaskIQ worker ──► tss-stack gateway :9001
  │                                                           │
  │                                          whisper-worker ◄─┘──► Whisper model
  │                                                  │
  │                                            MinIO upload (transcript)
  │
  └─► POST /v1/loras ──► Kimini ──► TaskIQ worker ──► LoraLine ──► trainer subprocess
```

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

Domains using this pattern: `imagegen`, `videogen`, `voicegen`, `loragen`

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