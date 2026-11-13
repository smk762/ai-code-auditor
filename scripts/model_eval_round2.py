#!/usr/bin/env python3
"""Round 2: tighter prompt (v2) + Opus verification of MEDIUM+ findings, on the SAME units.

    python scripts/model_eval_round2.py --base reports/model_eval/<round-1 dir>

Reuses round-1 results for haiku-low and opus-medium (v1 prompt) instead of paying again,
runs Haiku with the v2 prompt, has Opus confirm/reject each MEDIUM+ v2 finding, then
re-judges the pooled findings blind (one fresh judge pass, so all arms share issue keys).

Arms reported:
  haiku-v1          round-1 Haiku (reused)
  opus-v1           round-1 Opus  (reused)
  haiku-v2          Haiku, v2 prompt
  haiku-v2+verify   haiku-v2 with MEDIUM+ findings kept only if Opus confirms
                    (Opus may also re-grade severity); LOW findings pass through
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import anthropic  # noqa: E402

import model_eval as me  # noqa: E402
from auditor.chunker import extract_code_units  # noqa: E402
from auditor.config import ModelProviderConfig, load_ecosystem_config  # noqa: E402
from auditor.ecosystem_briefing import build_repo_briefing  # noqa: E402
from auditor.llm_client import AUDIT_INSTRUCTIONS_V2, LLMClient, _parse_findings_json  # noqa: E402
from auditor.repo_resolver import resolve_scan_root  # noqa: E402
from auditor.repo_scanner import scan_repo  # noqa: E402

HAIKU_V2 = me.Arm("haiku-v2", "claude-haiku-5-5", "low")
VERIFIER = me.Arm("verifier", "claude-opus-5-5", "medium")
SEV_ORDER = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
REUSED = {"haiku-v1": "haiku-low", "opus-v1": "opus-medium"}

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["confirm", "reject"]},
                    "severity": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["id", "verdict", "severity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

VERIFY_INSTRUCTIONS = """A fast first-pass reviewer flagged the findings below in this code unit. \
Check each one against the code. Confirm it only if the defect is real and visible in (or clearly \
implied by) the code and context shown; reject it if it is wrong, speculative, depends on code not \
shown, or describes intended behaviour such as a test fake. For confirmed findings give the severity \
you would assign (CRITICAL / HIGH / MEDIUM / LOW)."""


def is_medium_plus(sev: str) -> bool:
    return sev.upper() in ("MEDIUM", "HIGH", "CRITICAL")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="round-1 output directory")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    base = Path(args.base)
    out = ROOT / "reports" / "model_eval" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-round2")
    out.mkdir(parents=True, exist_ok=True)

    # ── Rebuild the exact round-1 units ─────────────────────────────────────
    wanted = {u["id"]: u for u in json.loads((base / "sample.json").read_text())}
    units, roots, briefs = {}, {}, {}
    for repo in load_ecosystem_config().repos:
        if repo.name not in {u["repo"] for u in wanted.values()}:
            continue
        root = resolve_scan_root(repo)
        briefs[repo.name] = build_repo_briefing(repo.name, root)
        for f in scan_repo(repo):
            for u in extract_code_units(repo.name, f):
                w = wanted.get(u.id)
                if w and (u.start_line, u.end_line) == (w["start_line"], w["end_line"]):
                    units[u.id], roots[u.id] = u, str(root)
    print(f"matched {len(units)}/{len(wanted)} round-1 units unchanged")

    def client_for(uid: str) -> LLMClient:
        return LLMClient(
            provider_config=ModelProviderConfig(provider="anthropic", model="eval", endpoint=""),
            repo_briefing=briefs[units[uid].repo],
        )

    prompts_v2 = {uid: client_for(uid).build_prompt(units[uid], roots[uid], instructions=AUDIT_INSTRUCTIONS_V2)
                  for uid in units}
    # Judge and verifier see context + code without either reviewer's instructions.
    context = {uid: client_for(uid).build_prompt(units[uid], roots[uid], instructions="(reviewer instructions omitted)")
               for uid in units}

    # ── Reused round-1 results ──────────────────────────────────────────────
    results: dict[tuple[str, str], dict] = {}
    for line in (base / "raw.jsonl").open():
        r = json.loads(line)
        for new_name, old_name in REUSED.items():
            if r["arm"] == old_name and r["unit"] in units:
                results[(new_name, r["unit"])] = r

    client = anthropic.Anthropic(max_retries=4)

    # ── Haiku with v2 prompt ────────────────────────────────────────────────
    def run_v2(uid: str) -> None:
        r = me.call(client, HAIKU_V2, prompts_v2[uid], max_tokens=8192)
        parsed = _parse_findings_json(r.get("text", ""), units[uid]) if "text" in r else None
        r["parse_ok"] = parsed is not None
        r["findings"] = [asdict(f) for f in (parsed or [])]
        results[("haiku-v2", uid)] = r

    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(run_v2, list(units)))
    print("haiku-v2 done")

    # ── Opus verifies haiku-v2's MEDIUM+ findings ───────────────────────────
    verdicts: dict[tuple[str, int], dict] = {}  # (uid, finding index) -> verdict
    verify_cost: dict[str, float] = {}

    def run_verify(uid: str) -> None:
        fs = results[("haiku-v2", uid)]["findings"]
        idx = [i for i, f in enumerate(fs) if is_medium_plus(f["severity"])]
        if not idx:
            return
        listing = "\n".join(
            f"[V{i}] severity={fs[i]['severity']} line={fs[i]['line']}\ntitle: {fs[i]['title']}\n"
            f"description: {fs[i]['description']}\n" for i in idx
        )
        prompt = f"{VERIFY_INSTRUCTIONS}\n\n=== CONTEXT AND CODE ===\n{context[uid]}\n\n=== FINDINGS ===\n{listing}"
        r = me.call(client, VERIFIER, prompt, max_tokens=8192, schema=VERIFY_SCHEMA)
        verify_cost[uid] = r.get("cost", 0.0)
        try:
            for v in json.loads(r.get("text", ""))["findings"]:
                i = int(v["id"].lstrip("V"))
                verdicts[(uid, i)] = v
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # unverifiable → treated as rejected below

    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(run_verify, list(units)))
    print(f"verified {len(verdicts)} MEDIUM+ findings")

    # ── Blind judge over the pooled arms ────────────────────────────────────
    judged_arms = ["haiku-v1", "opus-v1", "haiku-v2"]
    judgements: dict[str, dict] = {}
    labels: dict[str, dict[str, tuple[str, int]]] = {}

    def run_judge(uid: str) -> None:
        pooled = [
            {**f, "_src": (a, i)}
            for a in judged_arms
            for i, f in enumerate(results.get((a, uid), {}).get("findings", []))
        ]
        random.Random(uid).shuffle(pooled)
        labelled = [{**f, "id": f"F{n + 1}"} for n, f in enumerate(pooled)]
        labels[uid] = {f["id"]: f["_src"] for f in labelled}
        r = me.call(client, me.JUDGE, me.judge_prompt(context[uid], labelled), max_tokens=16000,
                    schema=me.JUDGE_SCHEMA)
        try:
            r["judgement"] = json.loads(r.get("text", ""))
        except json.JSONDecodeError:
            r["judgement"] = None
        judgements[uid] = r

    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(run_judge, list(units)))

    # ── Score ───────────────────────────────────────────────────────────────
    verdict_of: dict[tuple[str, str, int], dict] = {}  # (arm, uid, idx) -> judge verdict
    all_real: dict[tuple[str, str], str] = {}  # (uid, issue_key) -> true severity
    for uid, r in judgements.items():
        j = r.get("judgement")
        if not j:
            continue
        for f in j["findings"]:
            src = labels[uid].get(f["id"])
            if not src:
                continue
            verdict_of[(src[0], uid, src[1])] = f
            if f["verdict"] == "real":
                k = (uid, f["issue_key"])
                prev = all_real.get(k, "INFO")
                sev = f["true_severity"] if f["true_severity"] != "NONE" else "LOW"
                all_real[k] = max(prev, sev, key=SEV_ORDER.index)
        for m in j["missed_issues"]:
            all_real.setdefault((uid, m["issue_key"]), m["severity"])
    med_real = {k for k, s in all_real.items() if is_medium_plus(s)}

    def arm_findings(arm: str) -> list[tuple[str, int, dict, str]]:
        """(uid, idx, finding, claimed severity) for an arm; verify arm filters haiku-v2."""
        rows = []
        src_arm = "haiku-v2" if arm == "haiku-v2+verify" else arm
        for uid in units:
            for i, f in enumerate(results.get((src_arm, uid), {}).get("findings", [])):
                sev = f["severity"]
                if arm == "haiku-v2+verify" and is_medium_plus(sev):
                    v = verdicts.get((uid, i))
                    if not v or v["verdict"] != "confirm":
                        continue
                    sev = v["severity"]
                rows.append((uid, i, f, sev))
        return rows

    def arm_cost(arm: str) -> float:
        src = "haiku-v2" if arm == "haiku-v2+verify" else arm
        c = sum(results.get((src, uid), {}).get("cost", 0.0) for uid in units)
        return c + (sum(verify_cost.values()) if arm == "haiku-v2+verify" else 0.0)

    rows_md = []
    for arm in ["haiku-v1", "opus-v1", "haiku-v2", "haiku-v2+verify"]:
        src = "haiku-v2" if arm == "haiku-v2+verify" else arm
        fs = arm_findings(arm)
        real = fp = med_claims = med_claims_real = 0
        hit: set = set()
        for uid, i, _f, sev in fs:
            v = verdict_of.get((src, uid, i))
            if not v:
                continue
            ok = v["verdict"] == "real"
            real += ok
            fp += v["verdict"] == "false_positive"
            if is_medium_plus(sev):
                med_claims += 1
                med_claims_real += ok
            if ok:
                hit.add((uid, v["issue_key"]))
        cost = arm_cost(arm)
        rows_md.append(
            f"| {arm} | {len(fs)} | {real / (real + fp) if real + fp else 0:.0%} | "
            f"{med_claims} | {med_claims_real / med_claims if med_claims else 0:.0%} | "
            f"{len(hit & med_real)}/{len(med_real)} | {len(hit)}/{len(all_real)} | "
            f"${cost / len(units):.4f} | ${cost / len(units) * 6357 * 0.5:.2f} |"
        )

    judge_cost = sum(r.get("cost", 0.0) for r in judgements.values())
    new_spend = arm_cost("haiku-v2+verify") + judge_cost
    md = [
        f"# Model eval round 2 — {out.name}",
        "",
        f"{len(units)} units (same as {base.name}). Known real issues: {len(all_real)}, "
        f"of which MEDIUM+: {len(med_real)}. New spend this round: ${new_spend:.2f} "
        f"(judge ${judge_cost:.2f}, verifier ${sum(verify_cost.values()):.2f}).",
        "",
        "| Arm | Findings | Precision | MEDIUM+ claims | MEDIUM+ claim precision | Recall MEDIUM+ | Recall all | $/unit | Full sweep via batch |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows_md,
        "",
        "Precision = judged real / (real + false positive). 'MEDIUM+ claim precision' = share of findings the "
        "arm itself rated MEDIUM or worse that the judge found real — what lands at the top of a report. "
        "Recall is against the pooled known issues (all arms + judge's missed issues). v2 deliberately drops "
        "maintainability findings, so its 'Recall all' is expected to fall; MEDIUM+ recall is the number that matters. "
        "Verification uses a different Opus prompt from the judge, but both are Opus.",
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    with (out / "raw.jsonl").open("w") as fh:
        for (arm, uid), r in results.items():
            if arm == "haiku-v2":
                fh.write(json.dumps({"arm": arm, "unit": uid, **{k: v for k, v in r.items() if k != "text"}}) + "\n")
    (out / "verdicts.json").write_text(json.dumps({f"{u}:{i}": v for (u, i), v in verdicts.items()}, indent=1))
    with (out / "judgements.jsonl").open("w") as fh:
        for uid, r in judgements.items():
            fh.write(json.dumps({"unit": uid, "labels": labels.get(uid), **r}, default=list) + "\n")
    print("\n".join(md))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
