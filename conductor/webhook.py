"""Optional status webhooks at wrap-up: mithrandir's companion inbox, plus any extra targets.

The primary target is mithrandir (``AI_AUDIT_MITHRANDIR_WEBHOOK_URL``, unchanged). Extra targets, such as an
argus-scout webhook source, come from ``AI_AUDIT_STATUS_WEBHOOKS``: a JSON list of objects

    [{"name": "scout", "url": "http://<HOST>:<PORT>/api/events/ai-code-auditor",
      "token_env": "AI_AUDIT_SCOUT_WEBHOOK_TOKEN"}]

``token_env`` names the variable holding the bearer token (the token itself never sits in the list). A target
whose ``token_env`` is set but empty is skipped rather than sent without its credentials. Every target gets the
same envelope, and a failing target never stops the others or the wrap-up.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

from conductor.publish import latest_urls, public_base_url

logger = logging.getLogger(__name__)

STATUS_WEBHOOKS_ENV = "AI_AUDIT_STATUS_WEBHOOKS"


def default_webhook_url() -> str:
    """Jarrah mithrandir companion inbox (wired/live)."""
    return "http://192.168.1.109:8400/api/companion/events"


def mithrandir_webhook_url() -> str:
    """Empty AI_AUDIT_MITHRANDIR_WEBHOOK_URL disables; unset uses Jarrah default."""
    if "AI_AUDIT_MITHRANDIR_WEBHOOK_URL" in os.environ:
        return os.environ.get("AI_AUDIT_MITHRANDIR_WEBHOOK_URL", "").strip()
    return default_webhook_url()


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    name: str
    url: str
    token_env: str = ""


def extra_webhook_targets(raw: str | None = None) -> tuple[list[WebhookTarget], list[str]]:
    """Targets from ``AI_AUDIT_STATUS_WEBHOOKS`` and the errors for entries that were skipped.

    Unset or empty means no extra targets. A malformed value or entry is skipped with an error, never fatal.
    """
    if raw is None:
        raw = os.environ.get(STATUS_WEBHOOKS_ENV, "")
    raw = raw.strip()
    if not raw:
        return [], []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], [f"{STATUS_WEBHOOKS_ENV} is not JSON: {exc}"]
    if not isinstance(data, list):
        return [], [f"{STATUS_WEBHOOKS_ENV} must be a JSON list of targets"]
    targets: list[WebhookTarget] = []
    errors: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"{STATUS_WEBHOOKS_ENV}[{index}]: expected an object")
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            errors.append(f"{STATUS_WEBHOOKS_ENV}[{index}]: url must be http(s)")
            continue
        token_env = str(item.get("token_env") or "").strip()
        name = str(item.get("name") or f"target-{index}").strip()
        targets.append(WebhookTarget(name=name, url=url, token_env=token_env))
    return targets, errors


def build_payload(status: dict[str, Any] | None, artifact_url: str | None = None) -> dict[str, Any]:
    """The body every target receives.

    The full LATEST envelope when there is one, plus ``summary`` and ``link`` so a consumer of the argus status
    envelope (argus-scout's webhook source) reads it without a mapping. Mithrandir ignores both.
    """
    json_url, _ = latest_urls()
    if status:
        payload = dict(status)
        payload.setdefault("artifact_url", artifact_url or json_url)
        if payload.get("summary_one_liner"):
            payload.setdefault("summary", payload["summary_one_liner"])
        if payload.get("latest_md_url"):
            payload.setdefault("link", payload["latest_md_url"])
        return payload
    return {
        "type": "auditor_rest",
        "source": "ai-code-auditor",
        "artifact_url": artifact_url or json_url,
        "public_base_url": public_base_url(),
    }


def _post(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None, timeout_s: float) -> dict[str, Any]:
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("status webhook %s HTTP %s: %s", url, resp.status_code, (resp.text or "")[:500])
        return {"sent": ok, "skipped": False, "status_code": resp.status_code, "url": url}
    except requests.RequestException as exc:
        logger.warning("status webhook %s failed: %s", url, exc)
        return {"sent": False, "skipped": False, "error": str(exc), "url": url}


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
    return _post(url, build_payload(status, artifact_url), headers=None, timeout_s=timeout_s)


def notify_extra_targets(
    *,
    status: dict[str, Any] | None = None,
    targets: list[WebhookTarget] | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """POST the envelope to each extra target (``AI_AUDIT_STATUS_WEBHOOKS`` unless ``targets`` is given).

    One result per target, in order, plus one ``{"sent": False, "error": ...}`` per skipped entry. Never raises.
    """
    errors: list[str] = []
    if targets is None:
        targets, errors = extra_webhook_targets()
    results: list[dict[str, Any]] = []
    for error in errors:
        logger.warning("%s", error)
        results.append({"sent": False, "skipped": True, "reason": "bad_target", "error": error})
    if not targets:
        return results
    payload = build_payload(status)
    for target in targets:
        headers = None
        if target.token_env:
            token = os.environ.get(target.token_env, "").strip()
            if not token:
                logger.warning("status webhook %s skipped: %s is unset", target.name, target.token_env)
                results.append(
                    {"name": target.name, "sent": False, "skipped": True, "reason": "token_unset", "url": target.url}
                )
                continue
            headers = {"Authorization": f"Bearer {token}"}
        result = _post(target.url, payload, headers=headers, timeout_s=timeout_s)
        results.append({"name": target.name, **result})
    return results
