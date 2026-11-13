#!/usr/bin/env python3
"""Compare Claude models on the per-unit audit task, judged blind by a stronger model.

    python scripts/model_eval.py --n 50 --dry-run      # sample + cost estimate, no API calls
    python scripts/model_eval.py --n 50                 # run (spends API credit)

Every arm gets the exact production prompt (LLMClient.build_prompt). The judge sees each
unit's code plus all arms' findings, shuffled and anonymised, and labels each finding
real / false_positive / unclear, groups duplicates under one issue_key, and lists real
issues nobody found. Recall is measured against that pooled set of known issues, so it is
relative, not absolute.

Writes reports/model_eval/<timestamp>/{sample.json,raw.jsonl,judgements.jsonl,summary.md}.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from auditor.chunker import extract_code_units  # noqa: E402
from auditor.config import ModelProviderConfig, load_ecosystem_config  # noqa: E402
from auditor.contracts import CodeUnit  # noqa: E402
from auditor.ecosystem_briefing import build_repo_briefing  # noqa: E402
from auditor.llm_client import LLMClient, _parse_findings_json  # noqa: E402
from auditor.repo_resolver import resolve_scan_root  # noqa: E402
from auditor.repo_scanner import scan_repo  # noqa: E402

# $ per 1M tokens (input, output) — Anthropic first-party rates.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


@dataclass(frozen=True)
class Arm:
    name: str
    model: str
    effort: str


ARMS = [
    Arm("haiku-low", "claude-haiku-5-5", "low"),
    Arm("sonnet-low", "claude-sonnet-5-5", "low"),
    Arm("sonnet-medium", "claude-sonnet-5-5", "medium"),
    Arm("opus-medium", "claude-opus-5-5", "medium"),
]
JUDGE = Arm("judge", "claude-opus-5-5", "high")

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_|_test\.|\.(test|spec)\.")
RISKY = re.compile(
    r"auth|token|jwt|session|passw|secret|permission|role|admin|owner|tenant|user_id|"
    r"sql|execute\(|query|subprocess|shell|open\(|upload|path|request\.|@app\.|@router|"
    r"router\.|Depends\(|verify|signature|presign|redirect",
    re.IGNORECASE,
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["real", "false_positive", "unclear"]},
                    "issue_key": {"type": "string"},
                    "true_severity": {
                        "type": "string",
                        "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "NONE"],
                    },
                },
                "required": ["id", "verdict", "issue_key", "true_severity"],
                "additionalProperties": False,
            },
        },
        "missed_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]},
                },
                "required": ["issue_key", "title", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings", "missed_issues"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTIONS = """You are adjudicating an automated code review. Several anonymous reviewers \
examined the code unit below (with the context shown) and produced the findings listed after it.

For EVERY finding, decide:
- verdict: "real" if it describes a genuine defect, vulnerability, contract violation, or \
maintainability problem that a senior engineer on this codebase would want fixed; \
"false_positive" if it is wrong, speculative without basis in the code, or describes intended \
behaviour (e.g. test fakes, deliberate stubs); "unclear" only if the code shown genuinely cannot \
settle it.
- issue_key: a short snake_case key for the underlying issue. Findings that describe the SAME \
underlying issue MUST share the same issue_key, even if worded differently or given different \
severities. False positives get the key "fp".
- true_severity: the severity you would assign (NONE for false positives).

Then list in missed_issues any real issues in this unit that NO finding covers (use new issue_keys). \
Only list issues you are confident are real and visible in the code shown; do not pad.

Judge on the code and context alone. Do not reward length or confident wording."""


def price(model: str, usage: Any) -> float:
    pin, pout = PRICES[model]
    inp = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0) * 1.25
        + (usage.cache_read_input_tokens or 0) * 0.1
    )
    return (inp * pin + usage.output_tokens * pout) / 1e6


def call(client: anthropic.Anthropic, arm: Arm, content: str, max_tokens: int, schema: dict | None = None) -> dict:
    kwargs: dict[str, Any] = {"effort": arm.effort}
    if schema:
        kwargs["format"] = {"type": "json_schema", "schema": schema}
    t0 = time.time()
    try:
        msg = client.messages.create(
            model=arm.model,
            max_tokens=max_tokens,
            output_config=kwargs,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "latency_s": time.time() - t0, "cost": 0.0}
    text = "".join(b.text for b in msg.content if b.type == "text")
    return {
        "text": text,
        "stop_reason": msg.stop_reason,
        "latency_s": round(time.time() - t0, 2),
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "cost": price(arm.model, msg.usage),
    }


def sample_units(n: int, seed: int, risky_share: float) -> list[tuple[CodeUnit, str, str]]:
    """Return (unit, repo_root, repo_briefing) triples, weighted toward security-relevant code."""
    pool: list[tuple[CodeUnit, str]] = []
    briefings: dict[str, str] = {}
    for repo in load_ecosystem_config().repos:
        if not repo.enabled:
            continue
        try:
            root = resolve_scan_root(repo)
            files = scan_repo(repo)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {repo.name}: {exc}", file=sys.stderr)
            continue
        briefings[repo.name] = build_repo_briefing(repo.name, root)
        for f in files:
            for u in extract_code_units(repo.name, f):
                if TEST_PATH.search(u.file_path) or not 300 <= len(u.raw_text) <= 6000:
                    continue
                pool.append((u, str(root)))
    rng = random.Random(seed)
    risky = [p for p in pool if RISKY.search(p[0].raw_text)]
    other = [p for p in pool if not RISKY.search(p[0].raw_text)]
    n_risky = min(len(risky), round(n * risky_share))
    picked = rng.sample(risky, n_risky) + rng.sample(other, min(len(other), n - n_risky))
    rng.shuffle(picked)
    return [(u, root, briefings[u.repo]) for u, root in picked]


def judge_prompt(audit_prompt: str, labelled: list[dict]) -> str:
    lines = [
        JUDGE_INSTRUCTIONS,
        "\n=== CONTEXT AND CODE THE REVIEWERS SAW ===\n",
        audit_prompt,
        "\n=== FINDINGS (anonymised, shuffled) ===\n",
    ]
    if not labelled:
        lines.append("(no reviewer reported any finding)")
    for f in labelled:
        lines.append(
            f"[{f['id']}] severity={f['severity']} line={f['line']}\n"
            f"title: {f['title']}\ndescription: {f['description']}\n"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--risky-share", type=float, default=0.7)
    ap.add_argument("--arms", default=",".join(a.name for a in ARMS))
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    arms = [a for a in ARMS if a.name in args.arms.split(",")]
    out = ROOT / "reports" / "model_eval" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    sample = sample_units(args.n, args.seed, args.risky_share)
    prompts: dict[str, str] = {}
    for unit, root, briefing in sample:
        llm = LLMClient(
            provider_config=ModelProviderConfig(provider="anthropic", model="eval", endpoint=""),
            repo_briefing=briefing,
        )
        prompts[unit.id] = llm.build_prompt(unit, repo_path=root)
    (out / "sample.json").write_text(
        json.dumps([{**asdict(u), "raw_text": None} for u, _, _ in sample], indent=1, default=str)
    )

    approx_in = sum(len(p) for p in prompts.values()) / 3.5
    est = sum(
        (approx_in * PRICES[a.model][0] + len(sample) * 1500 * PRICES[a.model][1]) / 1e6 for a in arms
    ) + (approx_in * 1.3 * PRICES[JUDGE.model][0] + len(sample) * 4000 * PRICES[JUDGE.model][1]) / 1e6
    print(f"{len(sample)} units, ~{approx_in / len(sample):.0f} input tokens each; "
          f"arms: {[a.name for a in arms]}; rough cost estimate ${est:.2f}")
    if args.dry_run:
        print(f"dry run — sample written to {out}")
        return 0

    client = anthropic.Anthropic(max_retries=4)
    units = {u.id: u for u, _, _ in sample}

    # 1) Every arm reviews every unit.
    jobs = [(a, uid) for a in arms for uid in prompts]
    results: dict[tuple[str, str], dict] = {}

    def run_arm(job: tuple[Arm, str]) -> None:
        arm, uid = job
        r = call(client, arm, prompts[uid], max_tokens=8192)
        parsed = _parse_findings_json(r.get("text", ""), units[uid]) if "text" in r else None
        r["parse_ok"] = parsed is not None
        r["findings"] = [asdict(f) for f in (parsed or [])]
        results[(arm.name, uid)] = r

    with ThreadPoolExecutor(args.concurrency) as pool:
        for i, _ in enumerate(pool.map(run_arm, jobs), 1):
            if i % 25 == 0:
                print(f"  reviewed {i}/{len(jobs)}")
    with (out / "raw.jsonl").open("w") as fh:
        for (arm_name, uid), r in results.items():
            fh.write(json.dumps({"arm": arm_name, "unit": uid, **{k: v for k, v in r.items() if k != "text"}}) + "\n")

    # 2) Blind judging, one call per unit.
    judgements: dict[str, dict] = {}
    labels: dict[str, dict[str, str]] = {}  # uid -> {label: arm}

    def run_judge(uid: str) -> None:
        pooled = [
            {**f, "arm": a.name} for a in arms for f in results[(a.name, uid)].get("findings", [])
        ]
        random.Random(uid).shuffle(pooled)
        labelled = [{**f, "id": f"F{i + 1}"} for i, f in enumerate(pooled)]
        labels[uid] = {f["id"]: f["arm"] for f in labelled}
        r = call(client, JUDGE, judge_prompt(prompts[uid], labelled), max_tokens=16000, schema=JUDGE_SCHEMA)
        try:
            r["judgement"] = json.loads(r.get("text", ""))
        except json.JSONDecodeError:
            r["judgement"] = None
        judgements[uid] = r

    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(run_judge, list(prompts)))
    with (out / "judgements.jsonl").open("w") as fh:
        for uid, r in judgements.items():
            fh.write(json.dumps({"unit": uid, "labels": labels.get(uid), **r}) + "\n")

    # 3) Score.
    stats: dict[str, dict[str, Any]] = {a.name: defaultdict(float) for a in arms}
    all_real: set[tuple[str, str]] = set()
    all_high: set[tuple[str, str]] = set()
    found: dict[str, set] = {a.name: set() for a in arms}
    found_high: dict[str, set] = {a.name: set() for a in arms}
    judge_cost = 0.0
    judged_units = 0
    for uid, r in judgements.items():
        judge_cost += r.get("cost", 0.0)
        j = r.get("judgement")
        if not j:
            continue
        judged_units += 1
        for f in j["findings"]:
            arm = labels[uid].get(f["id"])
            if not arm:
                continue
            s = stats[arm]
            s[f["verdict"]] += 1
            if f["verdict"] == "real":
                key = (uid, f["issue_key"])
                all_real.add(key)
                found[arm].add(key)
                if f["true_severity"] in ("CRITICAL", "HIGH"):
                    all_high.add(key)
                    found_high[arm].add(key)
        for m in j["missed_issues"]:
            all_real.add((uid, m["issue_key"]))
            if m["severity"] in ("CRITICAL", "HIGH"):
                all_high.add((uid, m["issue_key"]))

    rows = []
    for a in arms:
        rs = [results[(a.name, uid)] for uid in prompts]
        cost = sum(r.get("cost", 0.0) for r in rs)
        s = stats[a.name]
        judged = s["real"] + s["false_positive"]
        rows.append({
            "arm": a.name,
            "findings": int(s["real"] + s["false_positive"] + s["unclear"]),
            "precision": s["real"] / judged if judged else 0.0,
            "recall": len(found[a.name]) / len(all_real) if all_real else 0.0,
            "high_recall": len(found_high[a.name]) / len(all_high) if all_high else 0.0,
            "real_issues": len(found[a.name]),
            "cost": cost,
            "cost_per_unit": cost / len(rs),
            "full_sweep_batch": cost / len(rs) * 6357 / 2,
            "latency_s": sum(r.get("latency_s", 0) for r in rs) / len(rs),
            "errors": sum(1 for r in rs if "error" in r),
            "parse_fail": sum(1 for r in rs if "error" not in r and not r["parse_ok"]),
        })

    total = sum(r["cost"] for r in rows) + judge_cost
    md = [
        f"# Model eval — {out.name}",
        "",
        f"{len(prompts)} units ({args.risky_share:.0%} security-relevant), seed {args.seed}. "
        f"Judge: {JUDGE.model} @ {JUDGE.effort} ({judged_units}/{len(prompts)} units judged). "
        f"Known real issues: {len(all_real)} ({len(all_high)} HIGH/CRITICAL). Total spend ${total:.2f} "
        f"(judge ${judge_cost:.2f}).",
        "",
        "| Arm | Findings | Precision | Recall (all) | Recall (HIGH+) | Real issues | $/unit | Full sweep via batch | Avg latency | Errors | Parse fails |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['arm']} | {r['findings']} | {r['precision']:.0%} | {r['recall']:.0%} | "
            f"{r['high_recall']:.0%} | {r['real_issues']} | ${r['cost_per_unit']:.4f} | "
            f"${r['full_sweep_batch']:.2f} | {r['latency_s']:.1f}s | {r['errors']} | {r['parse_fail']} |"
        )
    md += [
        "",
        "Recall is relative to the pooled set of real issues (all arms + judge's missed_issues), "
        "not absolute. The judge is an Opus model and may favour Opus-style findings despite anonymisation. "
        "'Full sweep via batch' = $/unit × 6357 units × 0.5.",
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
