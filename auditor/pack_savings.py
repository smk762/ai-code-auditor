"""Pack token savings: how much reading agent packs save a consuming agent, per repo.

Estimated, not telemetered. Tokens are ``chars / CHARS_PER_TOKEN`` (the same heuristic the
bundle budget uses), so the trend is consistent run to run even if absolute counts drift
from a real tokenizer.

For each ecosystem repo:

* **bundle** — ``briefs_markdown`` of the default mission bundle (what packs hand an agent).
* **targeted** — what an agent reads to reach the same specialists without packs, assuming
  it already knows where to look: every file the bundle's in-brief specialists cite, plus the
  repo README and ``ecosystem/ecosystem.md``. This is the conservative baseline.
* **source** — every source file ``scan_repo`` returns: the discovery cost of finding the
  work by crawling.
* **orientation** — CLAUDE.md + AGENTS.md + every ``@import`` in CLAUDE.md that resolves.
  These load in every session, with or without packs. Imports that don't resolve are listed
  under ``broken_imports``.

Plus, over every specialist pack whose cited file exists: the full brief, the compact brief
and the cited file, with the median ratio of file to brief.

``refresh()`` writes ``pack_savings.json`` and appends one summary line to
``pack_savings_history.jsonl`` so the trend survives report overwrites.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from auditor.agent_pack import (
    CHARS_PER_TOKEN,
    DEFAULT_BUNDLE_TOKEN_BUDGET,
    AgentCodingPack,
    build_pack_bundle,
    list_packs,
    load_pack,
)

logger = logging.getLogger(__name__)

REPORT_NAME = "pack_savings.json"
HISTORY_NAME = "pack_savings_history.jsonl"
ORIENTATION_FILES = ("CLAUDE.md", "AGENTS.md")
_IMPORT_RE = re.compile(r"^@(\S+)\s*$", re.MULTILINE)
_MAX_FILE_BYTES = 400_000  # skip generated blobs / bakes; they'd dominate the source count


def tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def file_tokens(path: str | Path) -> int:
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size > _MAX_FILE_BYTES:
            return 0
        return tokens(p.read_text(errors="ignore"))
    except OSError:
        return 0


def cited_file(pack: dict[str, Any], repo_path: Path | None = None) -> Path | None:
    """The pack's cited file; relative paths resolve against the pack's repo path."""
    fp = (pack.get("location") or {}).get("file_path")
    if not fp:
        return None
    path = Path(fp)
    if not path.is_absolute():
        root = (pack.get("repo") or {}).get("path") or repo_path
        if not root:
            return None
        path = Path(root) / path
    return path


def orientation(repo_path: Path) -> tuple[int, list[str]]:
    """Tokens auto-loaded in every session here, and the CLAUDE.md imports that don't resolve."""
    total = sum(file_tokens(repo_path / name) for name in ORIENTATION_FILES)
    claude = repo_path / "CLAUDE.md"
    broken: list[str] = []
    if claude.is_file():
        for ref in _IMPORT_RE.findall(claude.read_text(errors="ignore")):
            target = Path(ref).expanduser()
            target = target if target.is_absolute() else (repo_path / target)
            if target.is_file():
                total += file_tokens(target)
            else:
                broken.append(ref)
    return total, broken


def _ratio(saved_from: int, used: int) -> float | None:
    return round(1 - used / saved_from, 4) if saved_from else None


def measure_repo(
    output_dir: str | Path,
    *,
    name: str,
    repo_path: Path,
    source_files: Iterable[Path],
    ecosystem_md: Path | None,
    token_budget: int = DEFAULT_BUNDLE_TOKEN_BUDGET,
) -> dict[str, Any]:
    bundle = build_pack_bundle(output_dir, repo=name, token_budget=token_budget)
    cited = sorted(
        {str(f) for s in bundle["specialists"] if s.get("in_brief") and (f := cited_file(s, repo_path))}
    )
    targeted = sum(file_tokens(f) for f in cited) + file_tokens(repo_path / "README.md")
    if ecosystem_md is not None:
        targeted += file_tokens(ecosystem_md)
    source = sum(file_tokens(f) for f in source_files)
    orient, broken = orientation(repo_path)
    used = bundle["approx_tokens"]
    return {
        "repo": name,
        "bundle_tokens": used,
        "targeted_tokens": targeted,
        "source_tokens": source,
        "orientation_tokens": orient,
        "broken_imports": broken,
        "specialists_in_bundle": len(bundle["included_ids"]),
        "specialist_candidates": bundle["candidates"],
        "saving_vs_targeted": _ratio(targeted, used),
        "saving_vs_source": _ratio(source, used),
    }


def per_pack_stats(output_dir: str | Path) -> dict[str, Any]:
    rows: list[tuple[int, int, int]] = []
    for entry in list_packs(output_dir, layer="specialist", limit=100_000):
        data = load_pack(output_dir, str(entry.get("id") or ""))
        if not data:
            continue
        fp = cited_file(data)
        cited = file_tokens(fp) if fp else 0
        if not cited:
            continue
        pack = AgentCodingPack.from_dict(data, fallback_id=str(entry.get("id") or ""))
        brief = tokens(pack.to_brief_md())
        if brief:
            rows.append((brief, tokens(pack.to_compact_md()), cited))
    if not rows:
        return {"packs": 0}
    ratios = sorted(c / b for b, _, c in rows)
    q = statistics.quantiles(ratios, n=4) if len(ratios) > 1 else [ratios[0]] * 3
    return {
        "packs": len(rows),
        "median_brief_tokens": int(statistics.median(r[0] for r in rows)),
        "median_compact_tokens": int(statistics.median(r[1] for r in rows)),
        "median_cited_file_tokens": int(statistics.median(r[2] for r in rows)),
        "median_file_to_brief": round(statistics.median(ratios), 2),
        "p25_file_to_brief": round(q[0], 2),
        "p75_file_to_brief": round(q[2], 2),
    }


def summarize(repos: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> int:
        return sum(int(r[key]) for r in repos)

    bundle, targeted, orient = total("bundle_tokens"), total("targeted_tokens"), total("orientation_tokens")
    savings = [r["saving_vs_targeted"] for r in repos if r["saving_vs_targeted"] is not None]
    return {
        "repos": len(repos),
        "bundle_tokens": bundle,
        "targeted_tokens": targeted,
        "source_tokens": total("source_tokens"),
        "orientation_tokens": orient,
        "median_saving_vs_targeted": round(statistics.median(savings), 4) if savings else None,
        # Orientation loads either way, so include it on both sides for the session view.
        "session_saving": _ratio(targeted + orient, bundle + orient),
        "saving_vs_source": _ratio(total("source_tokens"), bundle),
        "repos_with_broken_imports": sorted(r["repo"] for r in repos if r["broken_imports"]),
    }


def build_report(
    output_dir: str | Path,
    repos: list[tuple[str, Path, list[Path]]],
    *,
    ecosystem_md: Path | None = None,
    token_budget: int = DEFAULT_BUNDLE_TOKEN_BUDGET,
    now: datetime | None = None,
) -> dict[str, Any]:
    """*repos* is ``(name, repo_path, source_files)`` per repo."""
    rows = []
    for name, path, files in repos:
        try:
            rows.append(
                measure_repo(
                    output_dir, name=name, repo_path=path, source_files=files,
                    ecosystem_md=ecosystem_md, token_budget=token_budget,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad repo shouldn't sink the metric
            logger.warning("pack savings: skip %s: %s", name, exc)
    index = Path(output_dir) / "agent_packs" / "INDEX.json"
    try:
        idx = json.loads(index.read_text())
    except (OSError, ValueError):
        idx = {}
    return {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "method": f"chars/{CHARS_PER_TOKEN}",
        "token_budget": token_budget,
        "packs_updated_at": idx.get("updated_at"),
        "pack_count": idx.get("count"),
        "summary": summarize(rows),
        "per_pack": per_pack_stats(output_dir),
        "repos": rows,
    }


def _out(output_dir: str | Path | None) -> Path:
    return Path(output_dir or os.getenv("AI_AUDIT_OUTPUT_DIR") or "reports")


def report_path(output_dir: str | Path | None = None) -> Path:
    return _out(output_dir) / REPORT_NAME


def history_path(output_dir: str | Path | None = None) -> Path:
    return _out(output_dir) / HISTORY_NAME


def _ecosystem_repos() -> list[tuple[str, Path, list[Path]]]:
    from auditor.config import load_ecosystem_config
    from auditor.repo_resolver import resolve_repo_path
    from auditor.repo_scanner import scan_repo

    out = []
    for repo in load_ecosystem_config().repos:
        if not repo.enabled:
            continue
        try:
            out.append((repo.name, resolve_repo_path(repo), scan_repo(repo)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("pack savings: cannot scan %s: %s", repo.name, exc)
    return out


def refresh(
    *,
    output_dir: str | Path | None = None,
    repos: list[tuple[str, Path, list[Path]]] | None = None,
    ecosystem_md: Path | None = None,
) -> dict[str, Any]:
    """Measure, write the report and append the summary to the history file."""
    out = _out(output_dir)
    if ecosystem_md is None:
        default = Path(__file__).resolve().parents[1] / "ecosystem" / "ecosystem.md"
        ecosystem_md = default if default.is_file() else None
    report = build_report(out, repos if repos is not None else _ecosystem_repos(), ecosystem_md=ecosystem_md)
    out.mkdir(parents=True, exist_ok=True)
    report_path(out).write_text(json.dumps(report, indent=2))
    line = {
        "generated_at": report["generated_at"],
        "packs_updated_at": report["packs_updated_at"],
        "pack_count": report["pack_count"],
        **{k: v for k, v in report["summary"].items() if k != "repos_with_broken_imports"},
        "median_file_to_brief": report["per_pack"].get("median_file_to_brief"),
    }
    with history_path(out).open("a") as fh:
        fh.write(json.dumps(line) + "\n")
    return report


def refresh_quietly(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Pipeline tail: never fail the caller over the metric."""
    if not pack_savings_enabled():
        return {"skipped": "AI_AUDIT_PACK_SAVINGS=0"}
    try:
        s = refresh(output_dir=output_dir)["summary"]
        return {"session_saving": s["session_saving"], "median_saving_vs_targeted": s["median_saving_vs_targeted"]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pack savings refresh failed: %s", exc)
        return {"error": str(exc)[:300]}


def load_report(output_dir: str | Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(report_path(output_dir).read_text())
    except (OSError, ValueError):
        return None


def load_history(output_dir: str | Path | None = None, *, limit: int = 365) -> list[dict[str, Any]]:
    try:
        lines = history_path(output_dir).read_text().splitlines()
    except OSError:
        return []
    rows = []
    for raw in lines[-limit:]:
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows


def pack_savings_enabled() -> bool:
    return os.getenv("AI_AUDIT_PACK_SAVINGS", "1").strip().lower() not in {"0", "false", "no", "off"}
