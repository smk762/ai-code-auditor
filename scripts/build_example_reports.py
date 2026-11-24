#!/usr/bin/env python3
"""Rebuild reports/examples/ by running the real pipelines on three invented repos.

    pip install -e ".[dev]" bandit
    python scripts/build_example_reports.py [--keep DIR]

No model is called. The repos are written to a workspace (``--keep`` leaves it for screenshots), scanned
by ``repo-run`` and ``ecosystem-run``, and three patches go through ``/audit/validate_patch``: one with a
test that fails before it, one without a test, and one that breaks a test. Validation installs pytest
from PyPI. Every report is copied as written, apart from a note under the title and workspace paths.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "examples"
NOTE = "> Real output from a scan of three invented repos, with no model. Rebuild: `scripts/build_example_reports.py`."

EMAIL = '''def normalize_email(value: str, default: str = "") -> str:
    text = (value or default).strip().lower()
    if "@" not in text:
        raise ValueError("not an email address")
    local, _, domain = text.partition("@")
    return f"{local.split('+')[0]}@{domain.rstrip('.')}"
'''
PYPROJECT = '[project]\nname = "{0}"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\npythonpath = ["."]\n'
REPOS: dict[str, dict[str, str]] = {
    "orders-api": {
        "pyproject.toml": PYPROJECT.format("orders-api"),
        "orders_api/__init__.py": "",
        "orders_api/health.py": "import subprocess\n\n\ndef ping(host: str) -> int:\n"
                                '    return subprocess.call("ping -c1 " + host, shell=True)\n',
        "orders_api/store.py": 'def find_order(cursor, order_id: str):\n'
                               '    cursor.execute(f"SELECT * FROM orders WHERE id = \'{order_id}\'")\n'
                               "    return cursor.fetchone()\n",
        "orders_api/cart.py": "from orders_api.pricing import total\n\n\ndef checkout(items: list[dict]) -> dict:\n"
                              '    return {"items": items, "total": total(items)}\n\n\n'
                              "def line_count(items: list[dict]) -> int:\n    return len(items)\n",
        "orders_api/pricing.py": "def total(items: list[dict]) -> int:\n    from orders_api.cart import line_count\n\n"
                                 "    shipping = 0 if line_count(items) > 2 else 5\n"
                                 '    return sum(i["price"] for i in items) + shipping\n',
        "orders_api/contact.py": EMAIL,
        "tests/test_pricing.py": "from orders_api.pricing import total\n\n\ndef test_shipping():\n"
                                 '    assert total([{"price": 10}]) == 15\n',
    },
    "billing": {
        "pyproject.toml": PYPROJECT.format("billing"),
        "billing/__init__.py": "",
        "billing/receipts.py": "import hashlib\n\n\ndef receipt_id(payload: bytes) -> str:\n"
                               "    return hashlib.md5(payload).hexdigest()\n",
        "billing/retry.py": "import time\n\n\ndef settle(client, invoice):\n"
                            "    # HACK: the gateway drops the first call after idle\n"
                            "    time.sleep(2)\n    return client.settle(invoice)\n",
        "billing/contact.py": EMAIL,
        "tests/test_receipts.py": "from billing.receipts import receipt_id\n\n\ndef test_stable():\n"
                                  '    assert receipt_id(b"a") == receipt_id(b"a")\n',
    },
    "storefront": {
        "pyproject.toml": PYPROJECT.format("storefront"),
        "storefront/__init__.py": "",
        "storefront/summary.py": "from orders_api.pricing import total\n\n\ndef basket_total(items: list[dict]) -> int:\n"
                                 "    return total(items)\n",
        "storefront/invoices.py": "import os\n\nimport requests\n\nBILLING_URL = os.environ.get(\"BILLING_URL\", \"\")\n\n\n"
                                  "def fetch_invoice(number: str) -> dict:\n"
                                  '    return requests.get(f"{BILLING_URL}/invoices/{number}").json()\n',
    },
}
RULES = """rules:
  - name: service_boundary
    description: A UI repo talks to its worker over HTTP, so it must not import the worker's package.
    severity: MEDIUM
    forbid:
      - from_roles: [ui]
        to_roles: [worker]
service_roles:
  - {name: orders-api, role: worker}
  - {name: billing, role: worker}
  - {name: storefront, role: ui}
call_graph_rules:
  allowed_calls:
    - {from: storefront, to: orders-api}
    - {from: orders-api, to: billing}
  service_url_env_vars:
    - {env_fragment: BILLING_URL, service: billing}
"""
# (label, repo, text in the pack's file path, edits to make)
PATCHES: list[tuple[str, str, str, dict[str, tuple[str, str]]]] = [
    ("Fix with a test that fails before it", "orders-api", "health.py", {
        "orders_api/health.py": ('"ping -c1 " + host, shell=True', '["ping", "-c1", "--", host]'),
        "tests/test_health.py": ("", "from orders_api import health\n\n\ndef test_host_is_one_argument(monkeypatch):\n"
                                     "    seen = []\n"
                                     "    monkeypatch.setattr(health.subprocess, \"call\", "
                                     "lambda cmd, **kw: seen.append((cmd, kw)) or 0)\n"
                                     '    health.ping("example.invalid; id")\n'
                                     '    assert seen == [(["ping", "-c1", "--", "example.invalid; id"], {})]\n')}),
    ("Fix without a test", "billing", "receipts.py", {
        "billing/receipts.py": ("hashlib.md5(", "hashlib.sha256(")}),
    ("Fix that breaks an existing test", "orders-api", "store.py", {
        "orders_api/store.py": ("f\"SELECT * FROM orders WHERE id = '{order_id}'\"",
                                '"SELECT * FROM orders WHERE id = %s", (order_id,)'),
        "orders_api/pricing.py": ("else 5", "else 4")}),
]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=example", "-c", "user.email=example@example.invalid", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def build_workspace(ws: Path) -> None:
    (ws / "config").mkdir(parents=True)
    entries, mirrors = [], []
    for name, files in REPOS.items():
        origin = ws / "origins" / name
        for rel, text in files.items():
            path = origin / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(origin, "init", "-q", "-b", "dev")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "initial")
        git(ws, "clone", "-q", str(origin), str(ws / name))
        entries.append(f"  - {{name: {name}, path: {ws / name}, branch: dev, provider: local}}")
        mirrors.append(f"  - {{name: {name}, path: {ws / name}, url: {origin}}}")
    (ws / "config/ecosystem.yaml").write_text("repos:\n" + "\n".join(entries) + "\n", encoding="utf-8")
    (ws / "config/repo_mirrors.yaml").write_text(
        f"mirror_root: {ws}\ndefault_branch: dev\nmirrors:\n" + "\n".join(mirrors) + "\n", encoding="utf-8")
    (ws / "config/architecture_rules.yaml").write_text(RULES, encoding="utf-8")


def make_patch(ws: Path, repo: str, edits: dict[str, tuple[str, str]]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / repo
        git(ws, "clone", "-q", str(ws / "origins" / repo), str(tree))
        for rel, (old, new) in edits.items():
            path = tree / rel
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if old not in text:
                raise SystemExit(f"{repo}/{rel}: nothing to replace")
            path.write_text(text.replace(old, new) if old else new, encoding="utf-8")
        git(tree, "add", "-A")
        return git(tree, "diff", "--cached")


def brief(repo: str, where: str) -> str:
    """The brief an agent gets for one pack, before any patch."""
    from auditor.agent_pack import AgentCodingPack, list_packs, load_pack

    pack = next(p for p in list_packs("reports", repo=repo, layer="specialist")
                if where in p["file_path"] and p["severity"] == "HIGH")
    return AgentCodingPack.from_dict(load_pack("reports", pack["id"])).to_brief_md()


def validate(ws: Path) -> str:
    """Send each patch to the API; a markdown table of what it answered."""
    from fastapi.testclient import TestClient

    from auditor.agent_pack import list_packs
    from auditor.audit_api import app

    client = TestClient(app)
    lines = ["# Patch Validation", "", "Responses from `POST /audit/validate_patch`, one patch per pack.", "",
             "| Patch | Repo | Pack | Checks passed | Pack status | Evidence | Reason |", "|---|---|---|---|---|---|---|"]
    for label, repo, where, edits in PATCHES:
        pack = next(p for p in list_packs("reports", repo=repo, layer="specialist")
                    if where in p["file_path"] and p["severity"] in ("HIGH", "MEDIUM"))
        res = client.post("/audit/validate_patch", json={
            "repo": repo, "patch": make_patch(ws, repo, edits), "pack_id": pack["id"]})
        res.raise_for_status()
        body = res.json()
        got, ev = body.get("pack") or {}, body.get("evidence") or {}
        status = got.get("status") or "open"
        reason = got.get("reason") or ""
        if not body["passed"]:
            reason += ": " + "; ".join(body.get("failure_summary") or [])[:200]
        lines.append(f"| {label} | {repo} | {pack['title']} | {'yes' if body['passed'] else 'no'} | {status} | "
                     f"{ev.get('kind') or 'none'} | {' '.join(str(reason).split()).replace('|', '/')} |")
        print(json.dumps({"patch": label, "passed": body["passed"], "pack": got, "evidence": ev})[:900])
    return "\n".join(lines) + "\n"


def publish(ws: Path, text: str, name: str) -> None:
    for repo in REPOS:
        text = text.replace(f"{ws / repo}/", f"{repo}/")
    text = text.replace(str(ws), "<workspace>")
    title, _, body = text.partition("\n")
    (OUT / name).write_text(f"{title}\n\n{NOTE}\n{body.rstrip()}\n", encoding="utf-8")


def run(ws: Path) -> None:
    build_workspace(ws)
    os.chdir(ws)
    os.environ.update({"AI_AUDIT_LLM_CACHE_ONLY": "1", "AI_AUDIT_PACK_RAG_INGEST": "0",
                       "AI_AUDIT_TEST_ENV_CHECK": "0", "AI_AUDIT_TRUST_EVAL": "0",
                       "DATABASE_URL": f"sqlite:///{ws}/audit.db", "REDIS_LOCK_REQUIRED": "0",
                       "AI_AUDIT_API_TOKEN": "", "AI_AUDIT_OUTPUT_DIR": "reports"})
    sys.path.insert(0, str(ROOT))
    from pipelines import ecosystem_audit, nightly_repo_audit

    nightly_repo_audit.run()
    ecosystem_audit.run()
    reports = ws / "reports"
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.md"):
        old.unlink()
    for name in ("nightly_repo_report", "security_report", "architecture_violations", "extraction_candidates"):
        publish(ws, (reports / f"{name}.md").read_text(encoding="utf-8"), f"{name}.example.md")
    publish(ws, brief("orders-api", "health.py"), "agent_brief.example.md")
    publish(ws, validate(ws), "patch_validation.example.md")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", default="", metavar="DIR", help="Build in this empty or missing directory and leave it")
    args = ap.parse_args()
    if args.keep:
        ws = Path(args.keep).resolve()
        if ws.exists() and any(ws.iterdir()):
            raise SystemExit(f"{ws} is not empty")
        run(ws)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            run(Path(tmp).resolve())
            os.chdir(ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
