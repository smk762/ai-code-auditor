"""Architecture contracts (#57): role rules over repo-to-repo imports, checked at load and seeded.

A rule in ``config/architecture_rules.yaml`` forbids imports between service roles::

    - name: service_boundary
      severity: MEDIUM
      forbid:
        - from_roles: [ui]
          to_roles: [worker]
      exclude_paths: ["tests/*", "*/tests/*", "scripts/*"]

Roles come from ``service_roles``. A repo depends on another when one of its Python files imports
the other's top-level package (the repo name with ``-`` as ``_``: ``argus-cortex`` is
``argus_cortex``). TS/JS imports aren't read yet. One violation per (importer, imported) repo
pair, so the architecture pack id (``agent_pack.architecture_pack_id``: rule plus
``"src -> tgt"``) stays the same however many files carry the import.

``validate`` runs inside ``config.load_architecture_rules``: every role and repo a rule names must
resolve, and a rule that could match nothing is a load error rather than a silent no-op (the
original ``gateway``/``database`` rules named roles no repo had, so they checked nothing).

``fixtures`` builds, for every rule, a dependency graph with exactly one seeded violation and a
clean one, and checks the rule fires once on the first and never on the second. The trust gate
``arch.contracts`` is the share of rules that do (``trust_eval``).
"""
from __future__ import annotations

import ast
import fnmatch
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auditor.contracts import RuleViolation

logger = logging.getLogger(__name__)

# Keys the evaluator understands. ``required_imports`` / ``forbidden_patterns`` are what
# ``approve-extraction`` writes (``use_<lib>``): accepted at load, but no evaluator reads them
# yet, so the seeded-fixture gate counts such a rule as not firing.
ROLE_KEY = "forbid"
UNEVALUATED_KEYS = ("required_imports", "forbidden_patterns")
LEGACY_KEYS = ("forbidden_edges", "allowed")
_MAX_EVIDENCE_SITES = 5


@dataclass(frozen=True)
class ImportSite:
    path: str   # repo-relative
    line: int
    module: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} ({self.module})"


Dependencies = dict[tuple[str, str], list[ImportSite]]


# ── dependencies ─────────────────────────────────────────────────────────────

def package_name(repo: str) -> str:
    return repo.replace("-", "_")


def repo_dependencies(repo_files: dict[str, list[Any]], repo_roots: dict[str, str],
                      repos: Iterable[str]) -> Dependencies:
    """Which repo's Python files import which other repo's package. Reads the files, writes nothing."""
    owners = {package_name(r): r for r in repos}
    deps: Dependencies = defaultdict(list)
    for repo, files in repo_files.items():
        root = Path(repo_roots.get(repo) or "/")
        for f in files:
            path = Path(f)
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
                continue
            try:
                rel = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                rel = path.as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    mods = [node.module]
                else:
                    continue
                for mod in mods:
                    owner = owners.get(mod.split(".")[0])
                    if owner and owner != repo:
                        deps[(repo, owner)].append(ImportSite(rel, node.lineno, mod))
    return dict(deps)


# ── evaluation ───────────────────────────────────────────────────────────────

def role_members(service_roles: list[dict[str, Any]]) -> dict[str, list[str]]:
    members: dict[str, list[str]] = defaultdict(list)
    for item in service_roles or []:
        if isinstance(item, dict) and item.get("name") and item.get("role"):
            members[str(item["role"])].append(str(item["name"]))
    return dict(members)


def _excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def evaluate_rule(rule: dict[str, Any], service_roles: list[dict[str, Any]],
                  deps: Dependencies) -> list[RuleViolation]:
    """One violation per forbidden (importer, imported) repo pair with an import outside ``exclude_paths``."""
    forbid = rule.get(ROLE_KEY) or []
    if not forbid:
        return []
    role_of = {str(i["name"]): str(i["role"]) for i in service_roles or [] if isinstance(i, dict)
               and i.get("name") and i.get("role")}
    exclude = [str(p) for p in rule.get("exclude_paths") or []]
    name = str(rule.get("name") or "unnamed_rule")
    out: list[RuleViolation] = []
    for (src, tgt), sites in sorted(deps.items()):
        src_role, tgt_role = role_of.get(src), role_of.get(tgt)
        if src == tgt or src_role is None or tgt_role is None:
            continue
        if not any(src_role in (f.get("from_roles") or []) and tgt_role in (f.get("to_roles") or [])
                   for f in forbid):
            continue
        kept = sorted((s for s in sites if not _excluded(s.path, exclude)), key=lambda s: (s.path, s.line))
        if not kept:
            continue
        shown = "; ".join(str(s) for s in kept[:_MAX_EVIDENCE_SITES])
        more = f"; +{len(kept) - _MAX_EVIDENCE_SITES} more" if len(kept) > _MAX_EVIDENCE_SITES else ""
        out.append(RuleViolation(
            rule_name=name,
            violating_path=f"{src} -> {tgt}",
            entities=[src, tgt],
            evidence=f"{src} ({src_role}) imports {tgt} ({tgt_role}): {shown}{more}",
            severity=str(rule.get("severity") or "MEDIUM").upper(),
        ))
    return out


def evaluate(rules: dict[str, Any], deps: Dependencies) -> list[RuleViolation]:
    roles = rules.get("service_roles") or []
    return [v for r in rules.get("rules") or [] if isinstance(r, dict) for v in evaluate_rule(r, roles, deps)]


# ── validation ───────────────────────────────────────────────────────────────

def known_repos(ecosystem_path: str | Path = "config/ecosystem.yaml",
                mirrors_path: str | Path = "config/repo_mirrors.yaml") -> set[str] | None:
    """Every repo the auditor knows (ecosystem repos, enabled or not, and mirrors); None if neither file exists."""
    names: set[str] = set()
    found = False
    for path, key in ((Path(ecosystem_path), "repos"), (Path(mirrors_path), "mirrors")):
        if not path.exists():
            continue
        found = True
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in (raw.get(key) if isinstance(raw, dict) else None) or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return names if found else None


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    return value


def validate(data: dict[str, Any], repos: set[str] | None) -> None:
    """Raise ValueError naming every rule that refers to an unknown role or repo, or can match nothing."""
    errors: list[str] = []

    def repo_ok(name: Any, where: str) -> None:
        if repos is not None and name not in repos:
            errors.append(f"{where}: unknown repo {name!r} (not in config/ecosystem.yaml or config/repo_mirrors.yaml)")

    roles = data.get("service_roles") or []
    if not isinstance(roles, list):
        errors.append("service_roles must be a list")
        roles = []
    seen: set[str] = set()
    for i, item in enumerate(roles):
        if not isinstance(item, dict) or not item.get("name") or not isinstance(item.get("role"), str) \
                or not item["role"].strip():
            errors.append(f"service_roles[{i}]: needs a name and a role")
            continue
        if item["name"] in seen:
            errors.append(f"service_roles: {item['name']!r} is listed twice")
        seen.add(item["name"])
        repo_ok(item["name"], f"service_roles[{item['name']}]")
    members = role_members(roles)

    names: set[str] = set()
    for i, rule in enumerate(data.get("rules") or []):
        if not isinstance(rule, dict) or not rule.get("name"):
            errors.append(f"rules[{i}]: needs a name")
            continue
        rn = f"rule {rule['name']!r}"
        if rule["name"] in names:
            errors.append(f"{rn}: name used twice")
        names.add(rule["name"])
        legacy = [k for k in LEGACY_KEYS if k in rule]
        if legacy:
            errors.append(f"{rn}: {', '.join(legacy)} match graph DEPENDS_ON edges, which the ecosystem run "
                          f"never builds, so the rule can match nothing; use `forbid` with from_roles/to_roles")
        for repo in rule.get("applies_to") or []:
            repo_ok(repo, f"{rn} applies_to")
        if ROLE_KEY not in rule and not any(k in rule for k in UNEVALUATED_KEYS) and not legacy:
            errors.append(f"{rn}: no condition (`forbid`), so it can match nothing")
        if ROLE_KEY in rule:
            forbid = rule[ROLE_KEY]
            if not isinstance(forbid, list) or not forbid:
                errors.append(f"{rn}: `forbid` must be a non-empty list of {{from_roles, to_roles}}")
                forbid = []
            for j, f in enumerate(forbid):
                where = f"{rn} forbid[{j}]"
                if not isinstance(f, dict):
                    errors.append(f"{where}: must be a mapping with from_roles and to_roles")
                    continue
                pair: dict[str, list[str]] = {}
                for side in ("from_roles", "to_roles"):
                    vals = _str_list(f.get(side))
                    if not vals:
                        errors.append(f"{where}: {side} must be a non-empty list of roles")
                        continue
                    bad = [v for v in vals if v not in members]
                    if bad:
                        errors.append(f"{where}: {side} names {bad}, which no repo in service_roles has "
                                      f"(declared roles: {sorted(members)})")
                    pair[side] = vals
                if len(pair) == 2 and not _seed_pair(pair["from_roles"], pair["to_roles"], members) \
                        and not any(v not in members for v in pair["from_roles"] + pair["to_roles"]):
                    errors.append(f"{where}: no two different repos have these roles, so it can match nothing")
            if "exclude_paths" in rule and _str_list(rule["exclude_paths"]) is None:
                errors.append(f"{rn}: exclude_paths must be a list of glob patterns")

    for i, d in enumerate(data.get("domain_ownership") or []):
        if not isinstance(d, dict):
            errors.append(f"domain_ownership[{i}]: must be a mapping")
            continue
        where = f"domain_ownership[{d.get('domain', i)}]"
        repo_ok(d.get("owner"), f"{where} owner")
        for repo in d.get("forbidden_in") or []:
            repo_ok(repo, f"{where} forbidden_in")

    cg = data.get("call_graph_rules") or {}
    if isinstance(cg, dict):
        for i, c in enumerate(cg.get("allowed_calls") or []):
            for side in ("from", "to"):
                if isinstance(c, dict) and c.get(side) != "*":
                    repo_ok(c.get(side), f"call_graph_rules.allowed_calls[{i}].{side}")
        for i, e in enumerate(cg.get("service_url_env_vars") or []):
            if isinstance(e, dict):
                repo_ok(e.get("service"), f"call_graph_rules.service_url_env_vars[{i}].service")

    if errors:
        raise ValueError("architecture rules: " + "; ".join(errors))


# ── seeded fixtures (the arch.contracts gate) ────────────────────────────────

def _seed_pair(from_roles: list[str], to_roles: list[str],
               members: dict[str, list[str]]) -> tuple[str, str] | None:
    for fr in from_roles:
        for tr in to_roles:
            for src in members.get(fr, []):
                for tgt in members.get(tr, []):
                    if src != tgt:
                        return src, tgt
    return None


def seeded_fixture(rule: dict[str, Any], service_roles: list[dict[str, Any]]
                   ) -> tuple[Dependencies, Dependencies] | None:
    """(a graph with exactly one import the rule forbids, a clean graph with the reverse import),
    or None when the rule has no role condition to seed."""
    members = role_members(service_roles)
    for f in rule.get(ROLE_KEY) or []:
        if not isinstance(f, dict):
            continue
        pair = _seed_pair(list(f.get("from_roles") or []), list(f.get("to_roles") or []), members)
        if pair:
            src, tgt = pair
            pkg = package_name(src)
            site = ImportSite(f"src/{pkg}/seeded_violation.py", 1, f"{package_name(tgt)}.internal")
            back = ImportSite(f"src/{package_name(tgt)}/seeded_clean.py", 1, f"{pkg}.internal")
            clean: Dependencies = {(tgt, src): [back]}
            if _forbids(rule, service_roles, tgt, src):
                clean = {}  # the rule forbids both directions: the clean graph has no import at all
            return {(src, tgt): [site]}, clean
    return None


def _forbids(rule: dict[str, Any], service_roles: list[dict[str, Any]], src: str, tgt: str) -> bool:
    role_of = {str(i.get("name")): str(i.get("role")) for i in service_roles or [] if isinstance(i, dict)}
    return any(role_of.get(src) in (f.get("from_roles") or []) and role_of.get(tgt) in (f.get("to_roles") or [])
               for f in rule.get(ROLE_KEY) or [] if isinstance(f, dict))


def fixtures(rules: dict[str, Any]) -> dict[str, Any]:
    """Every rule against its seeded fixture: fires exactly once on the violation, never on the clean graph."""
    roles = rules.get("service_roles") or []
    rows = []
    for rule in rules.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "unnamed_rule")
        fx = seeded_fixture(rule, roles)
        if fx is None:
            rows.append({"rule": name, "fired": False, "note": "no role condition the evaluator checks"})
            continue
        bad, clean = fx
        hits = evaluate_rule(rule, roles, bad)
        false_hits = evaluate_rule(rule, roles, clean)
        ok = len(hits) == 1 and not false_hits
        note = "" if ok else f"{len(hits)} hit(s) on the seeded violation, {len(false_hits)} on the clean graph"
        rows.append({"rule": name, "fired": ok, "note": note})
    fired = sum(r["fired"] for r in rows)
    return {"n": len(rows), "fired": fired, "rate": fired / len(rows) if rows else None, "rules": rows}
