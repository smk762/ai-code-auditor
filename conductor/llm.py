"""LiteLLM multi-provider client for conductor / workers."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auditor.spend_ledger import get_ledger

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConductorModels:
    conductor: str
    workers: str
    pr_review: str
    fallbacks: list[str]


def load_conductor_models(path: str | Path = "config/generator.yaml") -> ConductorModels:
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    return ConductorModels(
        conductor=str(
            os.getenv("AI_AUDIT_CONDUCTOR_MODEL")
            or (raw.get("conductor") or {}).get("model")
            or "anthropic/claude-opus-4-5"
        ),
        workers=str(
            os.getenv("AI_AUDIT_WORKER_MODEL")
            or (raw.get("workers") or {}).get("model")
            or "anthropic/claude-opus-4-5"
        ),
        pr_review=str(
            os.getenv("AI_AUDIT_PR_REVIEW_MODEL")
            or (raw.get("pr_review") or {}).get("model")
            or "anthropic/claude-opus-4-5"
        ),
        fallbacks=[str(x) for x in (raw.get("fallbacks") or [])],
    )


def completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float | None = 0.1,
    max_tokens: int = 4096,
    effort: str | None = None,
    use_fallbacks: bool = True,
    lane: str = "conductor",
    note: str = "",
) -> str:
    """Chat completion via LiteLLM when installed; otherwise Anthropic/OpenAI HTTP.

    temperature=None omits the parameter (required for Claude 5.x, which rejects
    sampling params). effort sets Anthropic output_config.effort (low|medium|high|xhigh|max).
    lane is the spend-ledger budget lane; Anthropic calls raise BudgetExceeded once it is spent.
    note labels the ledger row (e.g. mission:task id, repo) for the usage dashboard.
    """
    models = load_conductor_models()
    chosen = model or models.conductor
    is_anthropic = chosen.startswith("anthropic/") or chosen.startswith("claude")
    if is_anthropic:
        # Current Claude models reject sampling params with a 400 ("`temperature` is
        # deprecated for this model"), so never send it on the Anthropic path.
        temperature = None
        get_ledger().check(lane)
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    if effort and chosen.startswith("anthropic/"):
        extra["output_config"] = {"effort": effort}
    try:
        import litellm  # type: ignore

        resp = litellm.completion(
            model=chosen,
            messages=messages,
            max_tokens=max_tokens,
            **extra,
        )
        if is_anthropic:
            _record_litellm_usage(lane, chosen, resp, note)
        return str(resp.choices[0].message.content or "")
    except ImportError:
        logger.debug("litellm not installed; using requests fallback for %s", chosen)
        return _requests_completion(
            chosen, messages, temperature=temperature, max_tokens=max_tokens, effort=effort, lane=lane,
            note=note,
        )
    except Exception as exc:
        if not use_fallbacks:
            raise
        for fb in models.fallbacks:
            try:
                import litellm  # type: ignore

                resp = litellm.completion(
                    model=fb,
                    messages=messages,
                    max_tokens=max_tokens,
                    **({"temperature": temperature} if temperature is not None else {}),
                )
                logger.warning("primary model %s failed (%s); used fallback %s", chosen, exc, fb)
                return str(resp.choices[0].message.content or "")
            except Exception:
                continue
        raise


def _record_litellm_usage(lane: str, model: str, resp: Any, note: str = "") -> None:
    try:
        u = getattr(resp, "usage", None)
        get_ledger().record(
            lane, model,
            int(getattr(u, "prompt_tokens", 0) or 0),
            int(getattr(u, "completion_tokens", 0) or 0),
            note=note,
        )
    except Exception:  # noqa: BLE001
        logger.exception("spend ledger record failed")


def _requests_completion(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None,
    max_tokens: int,
    effort: str | None = None,
    lane: str = "conductor",
    note: str = "",
) -> str:
    import requests

    if model.startswith("anthropic/") or model.startswith("claude"):
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY required")
        name = model.split("/", 1)[-1]
        body: dict[str, Any] = {
            "model": name,
            "max_tokens": max_tokens,
            "messages": [m for m in messages if m.get("role") != "system"],
        }
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if effort:
            body["output_config"] = {"effort": effort}
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=180,
        )
        if not resp.ok:
            # Surface the API's error message (e.g. unknown model, rejected param).
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            get_ledger().record_usage(lane, name, data.get("usage") or {}, note=note)
        except Exception:  # noqa: BLE001 — never fail a paid call over bookkeeping
            logger.exception("spend ledger record failed")
        if data.get("stop_reason") == "refusal":
            raise RuntimeError(f"Anthropic refusal: {data.get('stop_details')}")
        parts = data.get("content") or []
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
        )

    # OpenAI-compatible (openai/, xai/, etc.)
    if model.startswith("openai/") or model.startswith("gpt"):
        key = os.getenv("OPENAI_API_KEY", "")
        base = "https://api.openai.com/v1/chat/completions"
        name = model.split("/", 1)[-1] if "/" in model else model
    elif model.startswith("xai/") or model.startswith("grok"):
        key = os.getenv("XAI_API_KEY", "")
        base = "https://api.x.ai/v1/chat/completions"
        name = model.split("/", 1)[-1]
    else:
        raise RuntimeError(f"unsupported model without litellm: {model}")

    if not key:
        raise RuntimeError("API key missing for provider")
    resp = requests.post(
        base,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": name,
            "messages": messages,
            "max_tokens": max_tokens,
            **({"temperature": temperature} if temperature is not None else {}),
        },
        timeout=120,
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"] or "")
