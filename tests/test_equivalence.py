from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from auditor import equivalence
from auditor.agent_pack import load_pack, rebuild_index, write_pack
from auditor.audit_api import app
from auditor.llm_client import AUDIT_INSTRUCTIONS_V2, AUDIT_INSTRUCTIONS_V3, PROMPTS
from tests.test_agent_pack import PATCH, _mk_pack, _validate_setup


class _FakeLLM:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


def test_v3_prompt_extends_v2_and_is_opt_in() -> None:
    assert PROMPTS["v3"] is AUDIT_INSTRUCTIONS_V3
    assert AUDIT_INSTRUCTIONS_V3.startswith(AUDIT_INSTRUCTIONS_V2.split("\n\nDo NOT report:")[0])
    assert "os.environ" in AUDIT_INSTRUCTIONS_V3 and "contract|state" in AUDIT_INSTRUCTIONS_V3
    assert "Do NOT report:" in AUDIT_INSTRUCTIONS_V3


def test_classify_splits_refuted_and_unproven() -> None:
    llm = _FakeLLM("""```json
    [{"file": "a.py", "line": 3, "kind": "equivalent", "summary": "rename", "reason": "same body"},
     {"file": "a.py", "line": 9, "kind": "trust_me", "summary": "dict→set", "reason": "order",
      "counterexample": "items=[2,1] → old [2,1], new [1,2]"},
     {"file": "b.py", "line": 1, "kind": "trust_me", "summary": "x", "reason": "none found"},
     {"file": "c.py", "line": 1, "kind": "probably fine", "summary": "y"}]
    ```""")
    r = equivalence.classify_patch("diff --git a/a.py b/a.py\n+x\n", llm)
    assert r["ok"] and "+x" in llm.prompts[0]
    assert [c["line"] for c in r["refuted"]] == [9]
    # unknown labels count as unproven, not as equivalent
    assert {c["file"] for c in r["unproven"]} == {"b.py", "c.py"}


def test_classify_never_raises() -> None:
    assert equivalence.classify_patch("   ")["changes"] == []
    assert equivalence.classify_patch("d", _FakeLLM("no json"))["ok"] is False

    class Boom:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("down")

    assert equivalence.classify_patch("d", Boom()) == {"ok": False, "error": "down"}


def test_enabled_flag(monkeypatch) -> None:
    monkeypatch.delenv("AI_AUDIT_VALIDATE_SEMANTIC", raising=False)
    assert equivalence.enabled(None) is False
    monkeypatch.setenv("AI_AUDIT_VALIDATE_SEMANTIC", "1")
    assert equivalence.enabled(None) is True
    assert equivalence.enabled(False) is False


def _post(client: TestClient, **extra):
    return client.post("/audit/validate_patch",
                       json={"repo": "argus-cortex", "patch": PATCH, "pack_id": "pack-fixme", **extra}).json()


def test_refuted_trust_me_blocks_auto_resolve(tmp_path: Path, monkeypatch) -> None:
    out = _validate_setup(tmp_path, monkeypatch, passed=True)
    write_pack(_mk_pack("pack-fixme"), out)
    rebuild_index(out)
    refuted = {"ok": True, "changes": [], "unproven": [],
               "refuted": [{"kind": "trust_me", "counterexample": "x=None"}]}
    monkeypatch.setattr(equivalence, "classify_patch", lambda patch: refuted)

    body = _post(TestClient(app), semantic_check=True)
    assert body["passed"] is True and body["semantic"] == refuted
    assert body["pack"]["updated"] is False and "counterexample" in body["pack"]["reason"]
    assert load_pack(out, "pack-fixme")["status"] == "open"


def test_semantic_check_off_by_default_and_unproven_still_resolves(tmp_path: Path, monkeypatch) -> None:
    out = _validate_setup(tmp_path, monkeypatch, passed=True)
    monkeypatch.delenv("AI_AUDIT_VALIDATE_SEMANTIC", raising=False)
    write_pack(_mk_pack("pack-fixme"), out)
    rebuild_index(out)
    calls: list[str] = []
    monkeypatch.setattr(equivalence, "classify_patch",
                        lambda patch: calls.append(patch) or {"ok": True, "changes": [], "refuted": [],
                                                               "unproven": [{"kind": "trust_me"}]})
    client = TestClient(app)
    off = client.post("/audit/validate_patch", json={"repo": "argus-cortex", "patch": PATCH}).json()
    assert "semantic" not in off and calls == []

    on = _post(client, semantic_check=True)
    assert on["semantic"]["unproven"] and on["pack"]["status"] == "resolved"
