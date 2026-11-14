"""Build the wiki for one or more repos: notes → drafts → check-and-fix → citation pass → files.

Output, per repo, under ``reports/wiki/<repo>/``: one markdown file per page plus
``INDEX.json`` with the source SHA, the page tree and, per page, the content hash of every file
it cites (so a later rebuild can redo only pages whose cited files changed). A page whose
check-and-fix pass fails keeps its previous version rather than shipping an unchecked draft.
``reports/wiki/INDEX.json`` lists every built repo.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auditor.repo_resolver import resolve_scan_root
from auditor.spend_ledger import BudgetExceeded, get_ledger
from wiki import citations, notes as notes_mod, pages as pages_mod
from wiki.batch import LANE, Request, Runner, StageStats, estimate_usd, request_id
from wiki.findings import build_findings_page
from wiki.sources import structure_block

logger = logging.getLogger(__name__)

# Output tokens (incl. thinking) per request, for pricing before submitting. From the eval:
# notes ~600-900; Opus pages and check-and-fix 3-6k at medium effort. Deliberately high.
EST_OUTPUT = {"note": 900, "draft": 7000, "verify": 7000}
EST_NOTE_CHARS = 1200  # a note's share of the page context, for dry-run estimates
FINDINGS_SLUG = "findings"


def wiki_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / "wiki"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(path: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


@dataclass
class RepoResult:
    repo: str
    pages_written: list[str] = field(default_factory=list)
    pages_failed: dict[str, str] = field(default_factory=dict)
    dropped_claims: int = 0
    notes_written: int = 0
    notes_cached: int = 0
    notes_failed: int = 0
    oversize: list[str] = field(default_factory=list)
    skipped: str = ""


@dataclass
class BuildReport:
    repos: dict[str, RepoResult] = field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    estimate_usd: float = 0.0
    cost_usd: float = 0.0
    stopped: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"repos": {k: asdict(v) for k, v in self.repos.items()}, "stages": self.stages,
                "estimate_usd": round(self.estimate_usd, 4), "cost_usd": round(self.cost_usd, 4),
                "stopped": self.stopped, "dry_run": self.dry_run,
                "wiki_lane_left_usd": round(get_ledger().remaining(LANE), 2)}


def _stage(report: BuildReport, name: str, st: StageStats) -> None:
    report.stages[name] = {"requested": st.requested, "reused": st.reused, "submitted": st.submitted,
                           "est_usd": round(st.est_usd, 4), "cost_usd": round(st.cost_usd, 4),
                           "failed": st.failed, "batch_ids": st.batch_ids}
    report.cost_usd += st.cost_usd


def load_index(output_dir: str | Path, repo: str) -> dict[str, Any] | None:
    try:
        return json.loads((wiki_dir(output_dir) / repo / "INDEX.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_top_index(output_dir: str | Path) -> None:
    root = wiki_dir(output_dir)
    repos = {}
    for idx_path in sorted(root.glob("*/INDEX.json")):
        if idx_path.parent.name.startswith("_"):
            continue
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        repos[idx["repo"]] = {"built_at": idx.get("built_at"), "source_sha": idx.get("source_sha"),
                              "branch": idx.get("branch"), "pages": len(idx.get("pages") or [])}
    (root / "INDEX.json").write_text(json.dumps({"updated_at": _now(), "repos": repos}, indent=2), encoding="utf-8")


def build(repo_names: list[str] | None, output_dir: str | Path, *, mode: str = "batch",
          dry_run: bool = False, runner: Runner | None = None, public: bool = False,
          denylist_path: str | Path | None = None, visibility=None) -> BuildReport:
    """Build the internal wiki, or with ``public=True`` the public variant of each public argus
    repo: same notes, but every model input scrubbed (wiki/public.py), publication rules added
    to the prompts, output under reports/wiki/_public/ and no findings page."""
    from auditor.config import load_ecosystem_config

    out = Path(output_dir)
    enabled = {r.name: r for r in load_ecosystem_config().repos if r.enabled}
    unknown = sorted(set(repo_names or []) - set(enabled))
    if unknown:
        raise ValueError(f"unknown or disabled repos: {', '.join(unknown)}")
    repos = [enabled[n] for n in (repo_names or sorted(enabled))]
    report = BuildReport(dry_run=dry_run)
    scrub = lambda s: s  # noqa: E731
    eco_text = None
    if public:
        from wiki.public import public_inputs

        scrub, eco_text, vis = public_inputs(denylist_path, visibility)
        for r in repos:
            if not r.name.startswith("argus-") or vis.get(r.name) != "public":
                report.repos[r.name] = RepoResult(r.name, skipped="not a public argus repo")
    runner = runner or Runner(out, mode=mode)
    batch = mode == "batch"

    # ── 1. notes ─────────────────────────────────────────────────────────────
    jobs_by_repo: dict[str, list[notes_mod.NoteJob]] = {}
    for repo in repos:
        res = report.repos.setdefault(repo.name, RepoResult(repo.name))
        try:
            jobs, cached, oversize = notes_mod.scan_notes(repo, out)
        except Exception as exc:  # noqa: BLE001 — e.g. mirror missing; other repos go on
            res.skipped = f"cannot scan: {type(exc).__name__}: {exc}"[:300]
            continue
        res.notes_cached, res.oversize = cached, oversize
        jobs_by_repo[repo.name] = jobs
    note_reqs = [Request(j.custom_id, p := notes_mod.note_params(j), estimate_usd(p, EST_OUTPUT["note"], batch=batch),
                         f"wiki-notes:{j.repo}")
                 for jobs in jobs_by_repo.values() for j in jobs]
    by_cid = {j.custom_id: j for jobs in jobs_by_repo.values() for j in jobs}

    if dry_run:
        report.estimate_usd = sum(r.est_usd for r in note_reqs) + _estimate_pages(repos, report, out, jobs_by_repo, batch)
        report.stages["notes"] = {"requested": len(note_reqs), "est_usd": round(sum(r.est_usd for r in note_reqs), 4)}
        return report

    try:
        results, st = runner.run("notes", note_reqs)
    except BudgetExceeded as exc:
        report.stopped = str(exc)
        return report
    _stage(report, "notes", st)
    for cid, r in results.items():
        job = by_cid[cid]
        ok = r.ok and notes_mod.save_note(job, r.text or "", out)
        if ok:
            report.repos[job.repo].notes_written += 1
        else:
            report.repos[job.repo].notes_failed += 1
    # saved notes are on disk now; unusable replies are dropped so the next run retries them
    runner.forget(list(results))

    # ── 2. drafts ────────────────────────────────────────────────────────────
    ctx: dict[str, dict[str, Any]] = {}
    draft_reqs: list[Request] = []
    for repo in repos:
        res = report.repos[repo.name]
        if res.skipped:
            continue
        repo_notes = notes_mod.load_notes(repo, out)
        if not repo_notes:
            res.skipped = "no file notes"
            continue
        structure = scrub(structure_block(repo))
        context = scrub(pages_mod.repo_context(repo, repo_notes, structure, ecosystem_text=eco_text))
        ctx[repo.name] = {"repo": repo, "structure": structure, "root": resolve_scan_root(repo), "drafts": {}}
        for page in pages_mod.PAGES:
            params = pages_mod.draft_params(page, context, public=public)
            cid = request_id(f"{'pd' if public else 'd'}-{repo.name}-{page}", params)
            ctx[repo.name]["drafts"][page] = cid
            draft_reqs.append(Request(cid, params, estimate_usd(params, EST_OUTPUT["draft"], batch=batch), f"wiki:{repo.name}"))
    try:
        drafts, st = runner.run("drafts", draft_reqs)
    except BudgetExceeded as exc:
        report.stopped = str(exc)
        return report
    _stage(report, "drafts", st)

    # ── 3. check-and-fix ─────────────────────────────────────────────────────
    verify_reqs: list[Request] = []
    for name, c in ctx.items():
        c["verify"] = {}
        for page, dcid in c["drafts"].items():
            d = drafts[dcid]
            if not d.ok:
                report.repos[name].pages_failed[page] = f"draft: {d.error or d.stop_reason}"
                continue
            cited = scrub(citations.cited_code(d.text or "", c["root"]))  # code can hold LAN URLs too
            params = pages_mod.verify_params(page, d.text or "", c["structure"], cited, public=public)
            cid = request_id(f"{'pv' if public else 'v'}-{name}-{page}", params)
            c["verify"][page] = cid
            verify_reqs.append(Request(cid, params, estimate_usd(params, EST_OUTPUT["verify"], batch=batch), f"wiki:{name}"))
    try:
        checked, st = runner.run("verify", verify_reqs)
    except BudgetExceeded as exc:
        report.stopped = str(exc)
        return report
    _stage(report, "verify", st)

    # ── 4. citation pass + write ─────────────────────────────────────────────
    for name, c in ctx.items():
        written = _write_repo(name, c, checked, report, out, public=public)
        # a page's paid-for draft is kept until its checked version is written
        runner.forget([cid for page in written for cid in (c["drafts"][page], c["verify"][page])])
    if not public:
        _write_top_index(out)
    return report


def _estimate_pages(repos, report: BuildReport, out: Path, jobs_by_repo, batch: bool) -> float:
    """Dry-run price of drafts + check-and-fix, from the context each repo would send."""
    total = 0.0
    for repo in repos:
        if report.repos[repo.name].skipped:
            continue
        repo_notes = notes_mod.load_notes(repo, out)
        structure = structure_block(repo)
        context = pages_mod.repo_context(repo, repo_notes, structure) + "x" * (EST_NOTE_CHARS * len(jobs_by_repo.get(repo.name, [])))
        for page in pages_mod.PAGES:
            total += estimate_usd(pages_mod.draft_params(page, context), EST_OUTPUT["draft"], batch=batch)
            # check-and-fix sends structure + cited code (~1/3 of the context) + the draft
            fake = pages_mod.verify_params(page, "x" * 24_000, structure, "x" * (len(context) // 3))
            total += estimate_usd(fake, EST_OUTPUT["verify"], batch=batch)
    return total


def _write_repo(name: str, c: dict[str, Any], checked: dict, report: BuildReport, out: Path,
                *, public: bool = False) -> list[str]:
    """Write the repo's pages and INDEX.json; returns the LLM pages written this run."""
    repo, root, res = c["repo"], c["root"], report.repos[name]
    d = wiki_dir(out) / "_public" / name if public else wiki_dir(out) / name
    d.mkdir(parents=True, exist_ok=True)
    try:
        previous = {p["slug"]: p for p in json.loads((d / "INDEX.json").read_text(encoding="utf-8")).get("pages", [])}
    except (OSError, ValueError):
        previous = {}
    base = Path(repo.path) if "://" not in (repo.path or "") else root
    sha, branch = _git(base, "rev-parse", "HEAD"), _git(base, "rev-parse", "--abbrev-ref", "HEAD")
    entries: dict[str, dict[str, Any]] = {}

    for page, (title, _brief) in pages_mod.PAGES.items():
        cid = c["verify"].get(page)
        r = checked.get(cid) if cid else None
        if r is None or not r.ok:
            if r is not None:
                res.pages_failed[page] = f"check-and-fix: {r.error or r.stop_reason}"
            if page in previous and (d / previous[page]["file"]).is_file():
                entries[page] = previous[page] | {"stale": True}
            continue
        # keep the checked text before the citation pass, so a checker change can be
        # re-applied later without paying for the model calls again
        raw_dir = wiki_dir(out) / ("_raw_public" if public else "_raw") / name
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{page}.md").write_text(r.text or "", encoding="utf-8")
        md, dropped = citations.drop_failed_claims(r.text or "", root)
        rep = citations.check(md, root)
        (d / f"{page}.md").write_text(md, encoding="utf-8")
        res.pages_written.append(page)
        res.dropped_claims += len(dropped)
        entries[page] = {"slug": page, "title": title, "file": f"{page}.md", "generated_at": _now(),
                         "source_sha": sha, "words": len(md.split()), "citations": rep.to_dict(),
                         "dropped_claims": dropped[:30], "cited_files": citations.cited_files(md, root),
                         "generator": "llm", "model": pages_mod.PAGE_MODEL}

    if not public:  # findings are internal only
        fmd = build_findings_page(name, out)
        (d / f"{FINDINGS_SLUG}.md").write_text(fmd, encoding="utf-8")
        entries[FINDINGS_SLUG] = {"slug": FINDINGS_SLUG, "title": "Notable findings", "file": f"{FINDINGS_SLUG}.md",
                                  "generated_at": _now(), "source_sha": sha, "generator": "data",
                                  "citations": citations.check(fmd, root).to_dict(), "cited_files": {}}
    order = list(pages_mod.PAGES) + [FINDINGS_SLUG]
    index = {
        "repo": name, "branch": branch, "source_sha": sha, "built_at": _now(), "variant": "public" if public else "internal",
        "models": {"notes": notes_mod.NOTES_MODEL, "pages": pages_mod.PAGE_MODEL, "effort": pages_mod.PAGE_EFFORT},
        "notes": {"cached": res.notes_cached, "written": res.notes_written, "failed": res.notes_failed,
                  "oversize_skipped": res.oversize},
        "pages": [entries[s] for s in order if s in entries],
    }
    (d / "INDEX.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return list(res.pages_written)
