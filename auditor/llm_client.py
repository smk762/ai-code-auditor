from __future__ import annotations

import json
from typing import Any

import requests

from auditor.config import ModelProviderConfig, get_secret_from_env, load_model_provider_config
from auditor.contracts import CodeUnit, Finding
from auditor.retry import with_retries
from auditor.ecosystem_briefing import build_ecosystem_overview, build_file_briefing


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
        # Pre-built ecosystem context injected into every audit prompt
        self._ecosystem_overview = build_ecosystem_overview()
        self._repo_briefing = repo_briefing

    def generate(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "temperature": self.provider_config.temperature,
        }
        # Ollama-specific: pass num_gpu only when we want explicit CPU offload control.
        if self.provider.startswith("ollama") and self.num_gpu_layers >= 0:
            payload["options"] = {"num_gpu": self.num_gpu_layers}
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            return with_retries(
                lambda: self._do_generate(payload=payload, headers=headers),
                retries=3,
                base_delay_s=1.0,
            )
        except requests.RequestException:
            return ""

    def _do_generate(self, payload: dict[str, Any], headers: dict[str, str]) -> str:
        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=self.timeout_s)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if self.provider in {"openai", "openai-compatible"} and "choices" in body:
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
                return True, "ok"
            except requests.RequestException as exc:
                return False, str(exc)
        payload = {"model": self.model, "prompt": "ping", "stream": False}
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return True, "ok"
        except requests.RequestException as exc:
            return False, str(exc)

    def analyze_code(self, unit: CodeUnit, repo_path: str = "") -> list[Finding]:
        file_ctx = build_file_briefing(
            repo_name=unit.repo,
            file_path=__import__("pathlib").Path(unit.file_path),
            repo_path=__import__("pathlib").Path(repo_path or unit.file_path).parent,
        )
        prompt = (
            f"{self._ecosystem_overview}\n\n"
            f"{self._repo_briefing}\n\n"
            f"---\n{file_ctx}\n"
            f"File: {unit.file_path}\n"
            f"Symbol: {unit.symbol}\n\n"
            "Review the code below for security vulnerabilities (auth bypass, injection, path traversal, "
            "hardcoded secrets, IDOR), bugs, cross-service contract violations, and maintainability issues.\n"
            "Return a JSON array. Each element: type, severity (CRITICAL/HIGH/MEDIUM/LOW/INFO), "
            "title, description, line, recommendation.\n"
            "Return [] if no issues. Return only the JSON array.\n\n"
            f"```\n{unit.raw_text}\n```"
        )
        response = self.generate(prompt)
        if response:
            parsed = _parse_findings_json(response, unit)
            if parsed:
                return parsed
        return _heuristic_findings(unit)


def _parse_findings_json(raw: str, unit: CodeUnit) -> list[Finding]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
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
                line=max(int(item.get("line", unit.start_line)), 0),
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
