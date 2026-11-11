"""Optional mithrandir companion webhook at wrap-up."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from conductor.publish import latest_urls, public_base_url

logger = logging.getLogger(__name__)


def default_webhook_url() -> str:
    """Jarrah mithrandir companion inbox (wired/live)."""
    return "http://192.168.1.109:8400/api/companion/events"


def mithrandir_webhook_url() -> str:
    """Empty AI_AUDIT_MITHRANDIR_WEBHOOK_URL disables; unset uses Jarrah default."""
    if "AI_AUDIT_MITHRANDIR_WEBHOOK_URL" in os.environ:
        return os.environ.get("AI_AUDIT_MITHRANDIR_WEBHOOK_URL", "").strip()
    return default_webhook_url()


def notify_mithrandir(
    *,
    artifact_url: str | None = None,
    webhook_url: str | None = None,
    status: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """POST status-event envelope (or thin pointer). Non-fatal on failure.

    Prefer sending the full LATEST envelope so mithrandir can merge without
    an immediate GET. Poll-only still works if webhook unset/fails.
    """
    url = (webhook_url if webhook_url is not None else mithrandir_webhook_url()).strip()
    if not url:
        return {"sent": False, "skipped": True, "reason": "webhook_unset"}

    json_url, _ = latest_urls()
    if status:
        # Full envelope — mithrandir merges among energy/thermal/auditor sources.
        payload = dict(status)
        payload.setdefault("artifact_url", artifact_url or json_url)
    else:
        payload = {
            "type": "auditor_rest",
            "source": "ai-code-auditor",
            "artifact_url": artifact_url or json_url,
            "public_base_url": public_base_url(),
        }
    try:
        resp = requests.post(url, json=payload, timeout=timeout_s)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning(
                "mithrandir webhook HTTP %s: %s",
                resp.status_code,
                (resp.text or "")[:500],
            )
        return {
            "sent": ok,
            "skipped": False,
            "status_code": resp.status_code,
            "url": url,
        }
    except requests.RequestException as exc:
        logger.warning("mithrandir webhook failed: %s", exc)
        return {"sent": False, "skipped": False, "error": str(exc), "url": url}
