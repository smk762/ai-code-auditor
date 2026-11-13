from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from auditor.config import ModelProviderConfig, get_secret_from_env, load_model_provider_config
from auditor.contracts import CodeUnit, Finding
from auditor.llm_cache import ReviewCache, review_key
from auditor.retry import with_retries
from auditor.ecosystem_briefing import build_ecosystem_overview, build_file_briefing

logger = logging.getLogger(__name__)

# Providers whose endpoint speaks POST /v1/chat/completions (OpenAI, DeepSeek, Qwen, vLLM, …).
OPENAI_CHAT_PROVIDERS = {"openai", "openai-compatible", "xai"}
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

AUDIT_INSTRUCTIONS = (
    "Review the code below for security vulnerabilities (auth bypass, injection, path traversal, "
    "hardcoded secrets, IDOR), bugs, cross-service contract violations, and maintainability issues.\n"
    "Return a JSON array. Each element: type, severity (CRITICAL/HIGH/MEDIUM/LOW/INFO), "
    "title, description, line, recommendation.\n"
    "Return [] if no issues. Return only the JSON array."
)

# v2: drops style/maintainability noise, adds a severity rubric and a confidence bar.
# Default since the 2026-11-13 eval (precision 29%→76%, MEDIUM+ recall 11→10 of 13). AI_AUDIT_LLM_PROMPT=v1
# selects the old prompt. Part of the cache key, so switching re-reviews.
AUDIT_INSTRUCTIONS_V2 = """Review the code below as a senior engineer on this codebase. Report only defects that matter:
- security vulnerabilities (auth bypass, injection, path traversal, hardcoded secrets, IDOR, SSRF, unsafe deserialization)
- correctness bugs that will cause wrong results, crashes, data loss, or resource leaks in realistic use
- violations of the cross-service contracts described in the context above

Do NOT report: style, naming, missing docstrings/comments/type hints, refactoring ideas, or generic \
"add validation/logging/error handling" advice. Do not report issues that depend entirely on code you \
cannot see (callers, middleware, config) unless the code shown makes the problem evident. Test code, \
fakes, stubs and fixtures are deliberately simplified: only report a problem there if it makes the \
tests themselves wrong.

Report a finding only if you can point to the line(s) that cause it and you are confident it is real. \
If nothing qualifies, return [] — that is a normal, good outcome.

Severity:
- CRITICAL: exploitable without authentication, or loses/corrupts data in normal operation.
- HIGH: exploitable security flaw, or a bug that breaks a main code path.
- MEDIUM: real bug on an edge path, or a security weakness that needs unusual conditions.
- LOW: minor real defect with limited impact.

Return a JSON array of at most 5 findings, most important first. Each element: type (security|bug|contract), \
severity (CRITICAL/HIGH/MEDIUM/LOW), title, description, line, recommendation. Return only the JSON array."""

PROMPTS = {"v1": AUDIT_INSTRUCTIONS, "v2": AUDIT_INSTRUCTIONS_V2}


def _safe_line(value: Any, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


class LLMClient:
    def __init__(
        self,
        provider_config: ModelProviderConfig | None = None,
        num_gpu_layers: int = -1,
        repo_briefing: str = "",
    ):
        self.provider_config = provider_config or load_model_provider_config()
        self.model = self.provider_config.model
        self.endpoint = self.provider_config.endpoint
        self.timeout_s = self.provider_config.timeout_s
        self.provider = self.provider_config.provider
        self.api_key = get_secret_from_env(self.provider_config.api_key_env)
        # -1 = let Ollama decide (all layers on GPU); ≥0 = explicit layer count for CPU offload
        self.num_gpu_layers = num_gpu_layers
        self._failures = 0
        self._parse_failures = 0
        # Effort only applies to Claude; it is part of the cache key so changing it re-reviews.
        self.effort = os.getenv("AI_AUDIT_LLM_EFFORT", "low") if self.provider == "anthropic" else ""
        self.instructions = PROMPTS.get(os.getenv("AI_AUDIT_LLM_PROMPT", "v2"), AUDIT_INSTRUCTIONS_V2)
        self.cache = ReviewCache.from_env()
        self.cache_hits = 0
        # Cache-only: never call the model (used after a batch prefetch so misses
        # can't turn into full-price synchronous calls).
        self.cache_only = os.getenv("AI_AUDIT_LLM_CACHE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
        self.cache_misses = 0
        self._note = ""  # spend-ledger label for the call in flight (repo name)
        # Pre-built ecosystem context injected into every audit prompt
        self._ecosystem_overview = build_ecosystem_overview()
        self._repo_briefing = repo_briefing

    def generate(self, prompt: str) -> str:
        """Return the model's text, or "" on failure (logged — callers fall back to heuristics)."""
        try:
            if self.provider == "anthropic":
                return self._anthropic_generate(prompt)
            if self.provider in OPENAI_CHAT_PROVIDERS:
                payload: dict[str, Any] = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.provider_config.temperature,
                    "max_tokens": 4096,
                }
            else:  # ollama
                payload = {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "temperature": self.provider_config.temperature,
                }
                # Pass num_gpu only when we want explicit CPU offload control.
                if self.num_gpu_layers >= 0:
                    payload["options"] = {"num_gpu": self.num_gpu_layers}
            headers: dict[str, str] = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            return with_retries(
                lambda: self._do_generate(payload=payload, headers=headers),
                retries=3,
                base_delay_s=1.0,
            )
        except Exception as exc:  # noqa: BLE001 — never let one unit kill the audit run
            self._failures += 1
            if self._failures <= 3 or self._failures % 100 == 0:
                logger.warning(
                    "LLM generate failed (%s/%s, failure #%d) — falling back to heuristics: %s",
                    self.provider, self.model, self._failures, exc,
                )
            return ""

    def _anthropic_generate(self, prompt: str) -> str:
        # Shared multi-provider path (conductor/llm.py). Claude 5.x models reject
        # `temperature`, so it is not sent; effort=low keeps thinking spend small for
        # this per-unit classification-style task.
        from conductor.llm import completion

        return completion(
            [{"role": "user", "content": prompt}],
            model=f"anthropic/{self.model}",
            temperature=None,
            max_tokens=8192,
            effort=self.effort or None,
            use_fallbacks=False,
            lane=os.getenv("AI_AUDIT_SPEND_LANE", "audit"),
            note=self._note,
        )

    def _do_generate(self, payload: dict[str, Any], headers: dict[str, str]) -> str:
        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout_s)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if self.provider in OPENAI_CHAT_PROVIDERS and "choices" in body:
            choices = body.get("choices", [])
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message", {})
                return str(message.get("content", ""))
        return str(body.get("response", body.get("text", "")))

    def health_check(self) -> tuple[bool, str]:
        # Use the short healthcheck timeout — we just want to know if the server responds,
        # not wait for a full model load/generate cycle.
        timeout = self.provider_config.healthcheck_timeout_s
        # For Ollama, hit the lightweight /api/tags endpoint instead of /api/generate
        # so we don't accidentally trigger a model download.
        if self.provider.startswith("ollama"):
            tags_url = self.endpoint.rsplit("/api/", 1)[0] + "/api/tags"
            try:
                resp = requests.get(tags_url, timeout=timeout)
                resp.raise_for_status()
            except requests.RequestException as exc:
                return False, str(exc)
            # Server up is not enough: a missing model 404s every generate call.
            names = {str(m.get("name", "")) for m in (resp.json().get("models") or [])}
            if self.model not in names and f"{self.model}:latest" not in names:
                return False, f"model {self.model!r} not on {tags_url} (have: {sorted(names)})"
            return True, "ok"
        # Cloud providers: don't spend tokens on a probe; just verify a key is configured.
        key_env = self.provider_config.api_key_env or (
            "ANTHROPIC_API_KEY" if self.provider == "anthropic" else ""
        )
        if key_env and not os.getenv(key_env):
            return False, f"{key_env} is not set"
        return True, "ok (key present; endpoint not probed)"

    def build_prompt(self, unit: CodeUnit, repo_path: str = "", instructions: str | None = None) -> str:
        """The per-unit audit prompt (shared by sync, batch, and model-eval paths)."""
        instructions = self.instructions if instructions is None else instructions
        file_ctx = build_file_briefing(
            repo_name=unit.repo,
            file_path=__import__("pathlib").Path(unit.file_path),
            repo_path=__import__("pathlib").Path(repo_path or unit.file_path).parent,
        )
        return (
            f"{self._ecosystem_overview}\n\n"
            f"{self._repo_briefing}\n\n"
            f"---\n{file_ctx}\n"
            f"File: {unit.file_path}\n"
            f"Symbol: {unit.symbol}\n\n"
            f"{instructions}\n\n"
            f"```\n{unit.raw_text}\n```"
        )

    def cache_key(self, unit: CodeUnit) -> str:
        return review_key(
            provider=self.provider, model=self.model, effort=self.effort,
            instructions=self.instructions, unit=unit,
        )

    def analyze_code(self, unit: CodeUnit, repo_path: str = "") -> list[Finding]:
        key = self.cache_key(unit) if self.cache else ""
        if self.cache:
            cached = self.cache.get(key)
            if cached is not None:
                parsed = _parse_findings_json(cached, unit)
                if parsed is not None:
                    self.cache_hits += 1
                    return parsed
        if self.cache_only:
            self.cache_misses += 1
            return []
        self._note = unit.repo
        response = self.generate(self.build_prompt(unit, repo_path))
        if self.cache and response and _parse_findings_json(response, unit) is not None:
            self.cache.put(key, self.model, response)
        return self.findings_from_response(response, unit)

    def findings_from_response(self, response: str, unit: CodeUnit) -> list[Finding]:
        """Parse model output into findings; heuristics only when there is no usable output."""
        if response:
            parsed = _parse_findings_json(response, unit)
            # [] is a valid "no issues" answer — only fall back when parsing failed.
            if parsed is not None:
                return parsed
            self._parse_failures += 1
            if self._parse_failures <= 3:
                logger.warning(
                    "Unparseable LLM output for %s (%s/%s): %.200r",
                    unit.id, self.provider, self.model, response,
                )
        return _heuristic_findings(unit)


def _extract_json_array(raw: str) -> Any:
    """Parse a JSON array from model output, tolerating <think> blocks and ``` fences."""
    text = _THINK_RE.sub("", raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _parse_findings_json(raw: str, unit: CodeUnit) -> list[Finding] | None:
    """Findings from model output; [] for a clean unit, None if the output isn't a JSON array."""
    payload = _extract_json_array(raw)
    if not isinstance(payload, list):
        return None
    findings: list[Finding] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "LOW")).upper()
        findings.append(
            Finding(
                id=f"llm:{unit.id}:{idx}",
                type=str(item.get("type", "code_quality")),
                severity=severity if severity in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"} else "LOW",
                repo=unit.repo,
                file_path=unit.file_path,
                line=_safe_line(item.get("line"), unit.start_line),
                title=str(item.get("title", "LLM finding")),
                description=str(item.get("description", "")),
                evidence=unit.symbol,
                recommendation=str(item.get("recommendation", "Review and remediate.")),
                source="llm",
            )
        )
    return findings


def _heuristic_findings(unit: CodeUnit) -> list[Finding]:
    text = unit.raw_text.lower()
    findings: list[Finding] = []
    rules = [
        ("hardcoded_secret", "HIGH", "Potential hardcoded secret", "password" in text or "secret" in text),
        ("todo_tech_debt", "LOW", "TODO marker found", "todo" in text or "fixme" in text),
        ("debug_print", "LOW", "Debug print statement present", "print(" in text),
    ]
    for idx, (kind, severity, title, matched) in enumerate(rules):
        if not matched:
            continue
        findings.append(
            Finding(
                id=f"heuristic:{unit.id}:{idx}",
                type="security" if kind == "hardcoded_secret" else "code_quality",
                severity=severity,
                repo=unit.repo,
                file_path=unit.file_path,
                line=unit.start_line,
                title=title,
                description=f"Matched heuristic rule: {kind}",
                evidence=unit.symbol,
                recommendation="Review this code path and apply project standards.",
                source="heuristic",
            )
        )
    return findings
