# Architecture Memory

2026-11-11

Decision:
Night conductor governs the Argus suite (`argus-*`) plus shared deps `agent-composer` and `voluptas`. Scan `dev`. Publish mithrandir status envelopes from Dragon; do not treat the auditor as exclusive morning-broadcast owner.

Reason:
Prior kimini/somnus/gothmog/imogen/vidita/tss/sauron/playact stack is out of scope. Argus consumes agent-composer (RAG/chat) and voluptas (ComfyUI).

Applies to:
`config/ecosystem.yaml`, `config/architecture_rules.yaml`, `ecosystem/ecosystem.md`.
