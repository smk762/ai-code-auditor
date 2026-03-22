# Somnus: Audit UI Spec

Implementation tasks for adding the audit dashboard and audit-chat interface to
somnus.  Independent of workstreams A and B in `imogen-migration.md` — can be
built in parallel.

**Prereq:** `audit-api` service running on `http://192.168.1.109:8765`
(start with `docker compose --profile audit-api up -d audit-api` in the
`ai-code-auditor` repo).

**Prereq (chat):** `audit-chat` service running on `http://192.168.1.109:9250`
(start with `docker compose --profile audit-tools up -d audit-chat` in the
`agent-composer` repo).

---

## Env vars

Add to `.env.local.example` and `.env.local`:

```
AUDIT_API_URL=http://192.168.1.109:8765
AUDIT_CHAT_URL=http://192.168.1.109:9250
```

---

## C-1  API proxy — `src/app/api/audit/[...path]/route.ts`

Catch-all proxy to the audit API.  Preserves method, body, and headers.
If `AUDIT_API_KEY` is set, inject as `Authorization: Bearer`.

```typescript
import { NextRequest, NextResponse } from "next/server";

const AUDIT_URL = process.env.AUDIT_API_URL ?? "http://192.168.1.109:8765";
const AUDIT_KEY = process.env.AUDIT_API_KEY ?? "";

async function handler(
  req: NextRequest,
  { params }: { params: { path: string[] } },
) {
  const path = params.path.join("/");
  const target = `${AUDIT_URL}/${path}${req.nextUrl.search}`;

  const headers: HeadersInit = {
    "content-type": req.headers.get("content-type") ?? "application/json",
  };
  if (AUDIT_KEY) headers["Authorization"] = `Bearer ${AUDIT_KEY}`;

  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body:
      req.method !== "GET" && req.method !== "HEAD" ? req.body : undefined,
    // @ts-expect-error Node 18 fetch duplex
    duplex: "half",
  });

  const ct = upstream.headers.get("content-type") ?? "application/json";
  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "content-type": ct },
  });
}

export const GET    = handler;
export const POST   = handler;
export const DELETE = handler;
export const PATCH  = handler;
```

---

## C-2  API proxy — `src/app/api/audit-chat/[...path]/route.ts`

Same pattern, pointing to the `audit-chat` RAG service (port 9250).
The chat service exposes the same HTTP interface as `rag-chat` (port 9150).

```typescript
import { NextRequest, NextResponse } from "next/server";

const CHAT_URL = process.env.AUDIT_CHAT_URL ?? "http://192.168.1.109:9250";

async function handler(
  req: NextRequest,
  { params }: { params: { path: string[] } },
) {
  const path = params.path.join("/");
  const target = `${CHAT_URL}/${path}${req.nextUrl.search}`;

  const upstream = await fetch(target, {
    method: req.method,
    headers: {
      "content-type": req.headers.get("content-type") ?? "application/json",
      ...(req.headers.get("authorization")
        ? { authorization: req.headers.get("authorization")! }
        : {}),
    },
    body:
      req.method !== "GET" && req.method !== "HEAD" ? req.body : undefined,
    // @ts-expect-error Node 18 fetch duplex
    duplex: "half",
  });

  // Pass SSE streams through for streaming responses.
  const ct = upstream.headers.get("content-type") ?? "";
  if (ct.includes("text/event-stream")) {
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
      },
    });
  }

  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "content-type": ct },
  });
}

export const GET    = handler;
export const POST   = handler;
export const DELETE = handler;
export const PATCH  = handler;
```

---

## C-3  Typed API clients

### `src/lib/audit-api.ts`

```typescript
const BASE = "/api/audit";

export interface AuditRun {
  run_id:          string;
  status:          "pending" | "running" | "completed" | "failed" | "partial_success";
  started_at:      string | null;
  finished_at:     string | null;
  scanned_repos:   string[];
  scanned_files:   number;
  findings:        number;
  violations:      number;
  errors:          string[];
  duration_ms:     number | null;
  stage_timings_ms: Record<string, number>;
  extra:           Record<string, unknown>;
}

export interface AuditHealth {
  status:        string;
  active_run_id: string | null;
  gpu_present:   boolean;
  vram_free_mb:  number;
  vram_total_mb: number;
  vram_used_mb:  number;
}

export const getHealth   = (): Promise<AuditHealth> =>
  fetch(`${BASE}/audit/health`).then(r => r.json());

export const getRuns     = (limit = 20): Promise<AuditRun[]> =>
  fetch(`${BASE}/audit/runs?limit=${limit}`).then(r => r.json());

export const getRun      = (id: string): Promise<AuditRun> =>
  fetch(`${BASE}/audit/runs/${id}`).then(r => r.json());

export const startRun    = (): Promise<{ run_id: string }> =>
  fetch(`${BASE}/audit/run`, { method: "POST" }).then(r => {
    if (r.status === 409) throw new Error("A run is already active.");
    return r.json();
  });

export const getReport   = (runId: string): Promise<string> =>
  fetch(`${BASE}/audit/reports/${runId}`).then(r => {
    if (!r.ok) throw new Error(`Report unavailable (${r.status})`);
    return r.text();
  });

export const triggerIngest = (): Promise<{ status: string; output: string }> =>
  fetch(`${BASE}/audit/ingest`, { method: "POST" }).then(r => r.json());
```

### `src/lib/audit-chat-api.ts`

The `audit-chat` service is the same `rag-chat` codebase.  Refer to its
OpenAPI docs (`http://192.168.1.109:9250/docs`) for the full schema.
Minimum surface for the chat UI:

```typescript
const BASE = "/api/audit-chat";

export interface Conversation {
  id:         string;
  title:      string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id:         string;
  role:       "user" | "assistant";
  content:    string;
  created_at: string;
}

export interface ChatRequest {
  conversation_id?: string;
  message:          string;
}

// List all past conversations for the current user
export const getConversations = (): Promise<Conversation[]> =>
  fetch(`${BASE}/conversations`).then(r => r.json());

// Get messages for a conversation
export const getMessages = (conversationId: string): Promise<Message[]> =>
  fetch(`${BASE}/conversations/${conversationId}/messages`).then(r => r.json());

// Delete a conversation
export const deleteConversation = (conversationId: string): Promise<void> =>
  fetch(`${BASE}/conversations/${conversationId}`, { method: "DELETE" }).then(() => undefined);

// Send a message (SSE stream — caller reads .body as ReadableStream)
export const sendMessage = (body: ChatRequest): Promise<Response> =>
  fetch(`${BASE}/chat`, {
    method:  "POST",
    body:    JSON.stringify(body),
    headers: { "content-type": "application/json" },
  });
```

---

## C-4  Polling hook — `src/hooks/use-audit-run.ts`

Audits run for hours; SSE is not needed.  This hook polls every 15 seconds
while a run is active and stops automatically when it finishes.

```typescript
import { useEffect, useState } from "react";
import { AuditRun, getRun } from "@/lib/audit-api";

export function useAuditRun(runId: string | null) {
  const [run, setRun] = useState<AuditRun | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const data = await getRun(runId);
        if (!cancelled) {
          setRun(data);
          if (data.status === "pending" || data.status === "running") {
            setTimeout(poll, 15_000);
          }
        }
      } catch {
        if (!cancelled) setTimeout(poll, 30_000); // back off on error
      }
    };

    poll();
    return () => { cancelled = true; };
  }, [runId]);

  return run;
}
```

---

## C-5  SSE hook — `src/hooks/use-audit-chat-stream.ts`

```typescript
import { useState, useCallback } from "react";
import { sendMessage, ChatRequest, Message } from "@/lib/audit-chat-api";

export function useAuditChatStream() {
  const [messages, setMessages]   = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError]         = useState<string | null>(null);

  const send = useCallback(async (req: ChatRequest) => {
    setStreaming(true);
    setError(null);

    // Optimistically append the user turn.
    const userMsg: Message = {
      id:         crypto.randomUUID(),
      role:       "user",
      content:    req.message,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);

    // Placeholder for the assistant turn (filled incrementally).
    const assistantId = crypto.randomUUID();
    setMessages(prev => [
      ...prev,
      { id: assistantId, role: "assistant", content: "", created_at: new Date().toISOString() },
    ]);

    try {
      const resp = await sendMessage(req);
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            if (evt.token || evt.content) {
              const chunk = evt.token ?? evt.content ?? "";
              setMessages(prev =>
                prev.map(m =>
                  m.id === assistantId
                    ? { ...m, content: m.content + chunk }
                    : m,
                ),
              );
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
      setMessages(prev => prev.filter(m => m.id !== assistantId));
    } finally {
      setStreaming(false);
    }
  }, []);

  const reset = useCallback(() => setMessages([]), []);

  return { messages, streaming, error, send, reset };
}
```

---

## C-6  Route pages — `src/app/audit/`

### Route map

| Route | Component type | Purpose |
|-------|---------------|---------|
| `audit/page.tsx` | Client | Dashboard |
| `audit/runs/page.tsx` | Client | Run history table |
| `audit/runs/[runId]/page.tsx` | Client | Run detail |
| `audit/reports/[runId]/page.tsx` | Client | Report viewer |
| `audit/chat/page.tsx` | Client | Audit-chat (full UI with history) |

---

### `audit/page.tsx` — Dashboard

**Data:** `getHealth()` on mount + 30s interval; `getRuns(5)` on mount.

**Layout:**

```
┌─────────────────────────────────────────────┐
│  GPU Status         VRAM gauge (used/total)  │
│  Active run: <run_id> [running 00:42:17]     │
│  — or — No active run   [Start Audit ▶]      │
├─────────────────────────────────────────────┤
│  Recent runs (last 5)                        │
│  run_id  started  duration  repos  findings  │
│  ...                                         │
└─────────────────────────────────────────────┘
```

**"Start Audit" button:**
- Calls `startRun()`, stores returned `run_id` in state.
- Disabled while a run is active or `vram_free_mb < 6000`.
- On 409, show toast: "A run is already active".

**Active run card:**
- Mounts `useAuditRun(activeRunId)` once a run is started or one is seen in health.
- Shows spinner + elapsed timer while `status === "running"`.
- Shows success/error badge on completion.

**VRAM gauge:**
- Simple `<progress>` or thin bar: `vram_used_mb / vram_total_mb`.
- Amber if `vram_free_mb < 8000`, red if `< 6000`.

---

### `audit/runs/page.tsx` — Run History

**Data:** `getRuns(50)`.

**Table columns:**

| Column | Notes |
|--------|-------|
| Run ID | Monospace, truncated to last 20 chars; full value in `title` tooltip |
| Started | `toLocaleString()` |
| Duration | From `duration_ms`; format `HHh MMm` |
| Repos | `scanned_repos.length` |
| Files | `scanned_files` |
| Findings | `findings` |
| Violations | `violations` with red badge if `> 0` |
| Status | Coloured badge: pending/running/completed/failed/partial_success |

Click any row → `audit/runs/[runId]`.

---

### `audit/runs/[runId]/page.tsx` — Run Detail

**Data:** `useAuditRun(runId)` (live-polls while running).

**Layout:**

```
Status badge   Run ID   Started → Finished   Duration

Stage timings (horizontal bar chart)
  scan_and_extract         ████████░░░░  4m 12s
  graph_and_embedding      ██░░░░░░░░░░    48s
  pattern_mining           ███░░░░░░░░░  1m 05s
  governance_and_reports   █░░░░░░░░░░░    22s

Scanned repos: [pill list]
Findings: 12   Violations: 3   Files: 847

Errors: (collapsible accordion — hidden if empty)
  • repo-name: error text

Extra (GPU info, collapsible):
  offload_mode: partial   num_layers: 48   vram_free: 8528 MB

[View Report]  [Trigger Ingest]
```

**"View Report"** → navigate to `audit/reports/[runId]`.
**"Trigger Ingest"** → calls `triggerIngest()`, shows toast on success/failure.
Both buttons visible only when `status === "completed"` or `"partial_success"`.

---

### `audit/reports/[runId]/page.tsx` — Report Viewer

**Data:** `getReport(runId)` — returns combined markdown text.

Render with a Markdown component (e.g. `react-markdown` + `remark-gfm`).

**Header actions:**
- "Download .md" — `Blob` + `URL.createObjectURL` download of the raw text.
- "← Back to run" — link to `audit/runs/[runId]`.

**Error states:**
- `409` (run not yet complete) → "Report not available yet — run is still in progress."
- `410` (overwritten) → "Reports for this run have been overwritten by a later run. If S3 archiving is configured, retrieve from `ai-audit/{runId}/`."

---

### `audit/chat/page.tsx` — Audit Chat

Full-page chat interface backed by the `audit-chat` service (port 9250),
isolated `audit_docs` RAG collection, `qwen2.5-coder:32b` model.  Supports
resuming past conversations.

**Data sources:**
- `getConversations()` on mount → sidebar conversation list.
- `getMessages(conversationId)` when a conversation is selected → load history.
- `useAuditChatStream()` for sending messages and streaming responses.

**Layout:**

```
┌──────────────────┬──────────────────────────────────────┐
│  Conversations   │  Chat area                           │
│                  │                                      │
│  [+ New]         │  [message bubbles, user right /      │
│                  │   assistant left]                    │
│  Today           │                                      │
│  • Audit Q re… ← │  [thinking spinner while streaming]  │
│  • imogen deps   │                                      │
│                  │  ──────────────────────────────────  │
│  Yesterday       │  [textarea]         [Send ▶]         │
│  • gothmog API   │                                      │
│  • tss-stack     │                                      │
└──────────────────┴──────────────────────────────────────┘
```

**Behaviour:**

- **Sidebar** — lists conversations sorted by `updated_at` desc, grouped
  Today / Yesterday / Older.  Active conversation highlighted.
  "New" button clears the chat area and unsets `conversationId` (next send
  creates a new conversation).  Trash icon on hover → `deleteConversation()`.

- **Selecting a conversation** — calls `getMessages(id)`, populates chat area.
  Sets `conversationId` in state so `sendMessage` sends
  `{ conversation_id: id, message }` to continue the thread.

- **Sending a message** — calls `send({ conversation_id, message })`.
  Assistant reply streams token by token from the SSE response.  After
  streaming completes, reload `getConversations()` to update sidebar titles
  (the service derives the title from the first user message).

- **Conversation title** — use the first 60 chars of the first user message.

- **Empty state** — when no conversation is selected, show a centered prompt
  with example questions:
  - "What APIs does imogen expose?"
  - "Which repos depend on gothmog?"
  - "What findings were in the last audit?"

- **Error handling** — if the `audit-chat` service is unreachable, show a
  warning banner: "Audit chat is offline — start it with
  `docker compose --profile audit-tools up -d audit-chat`."

---

## C-7  Nav integration

Add two entries to the existing nav:

```
Audit   →  /audit          (dashboard icon)
  └ Chat  →  /audit/chat   (chat icon, nested or separate)
```

---

## C-8  Affected files summary

| File | Action |
|------|--------|
| `src/app/api/audit/[...path]/route.ts` | **Create** — proxy to audit API `:8765` |
| `src/app/api/audit-chat/[...path]/route.ts` | **Create** — proxy to audit-chat `:9250` |
| `src/lib/audit-api.ts` | **Create** — typed audit API client |
| `src/lib/audit-chat-api.ts` | **Create** — typed audit-chat client |
| `src/hooks/use-audit-run.ts` | **Create** — polling hook for run status |
| `src/hooks/use-audit-chat-stream.ts` | **Create** — SSE streaming hook for chat |
| `src/app/audit/page.tsx` | **Create** — dashboard |
| `src/app/audit/runs/page.tsx` | **Create** — run history table |
| `src/app/audit/runs/[runId]/page.tsx` | **Create** — run detail |
| `src/app/audit/reports/[runId]/page.tsx` | **Create** — report viewer |
| `src/app/audit/chat/page.tsx` | **Create** — audit chat with history |
| nav component | **Modify** — add Audit + Chat nav entries |
| `.env.local.example` | **Modify** — add `AUDIT_API_URL`, `AUDIT_CHAT_URL` |

---

## C-9  Not in scope (follow-up tasks)

- **Findings browser** — `GET /audit/findings` endpoint backed by PostgreSQL
  `findings` table; filter by repo, severity, finding_type.  Requires a
  new endpoint in `audit_api.py`.
- **Ecosystem graph visualisation** — D3 or vis.js force graph of repo
  dependencies from the graph DB.
- **Architecture violations diff** — compare violations between run N and
  run N-1.
- **Audit-chat pinning** — pin key answers as "notes" for later reference.
