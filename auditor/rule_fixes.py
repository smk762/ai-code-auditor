"""What to do about a static-tool finding, per rule.

Bandit says what is wrong but not how to fix it, so its findings carry one generic recommendation. A
brief shows the rule's fix instead. This happens when the brief is rendered: the stored pack keeps the
generic text, because class guards match a pack's children on the words stored in it
(``class_packs``), and boilerplate there would match every pack of a rule.
"""
from __future__ import annotations

GENERIC = "Review and remediate this security finding."

_SHELL = ("Pass the command as a list of arguments and drop `shell=True`, so input can't add shell syntax. Put `--` before a "
          "value that could start with `-`.")
_HASH = ("If the digest protects anything (a password, token, signature or identifier others must not forge), use SHA-256 or "
         "a password hash, and check stored digests and their lengths first. If it doesn't, pass `usedforsecurity=False`.")
_XML = "Parse with `defusedxml`, or reject documents that contain a DTD, so entities can't expand or read files."

BANDIT: dict[str, str] = {
    "B101": "In a test this needs no change. In code, raise an exception instead: `assert` is removed under `python -O`.",
    "B102": "Replace `exec` with an explicit dispatch over the allowed operations.",
    "B104": "Bind to a specific interface, taken from config, unless every network this host is on may connect.",
    "B105": "Move the value to the environment or a secret store, and rotate it if it was ever real.",
    "B106": "Move the value to the environment or a secret store, and rotate it if it was ever real.",
    "B108": "Create the file with `tempfile`, so its name can't be predicted or pre-created.",
    "B110": "Catch the exception you expect, and log or handle it. Let anything else propagate.",
    "B112": "Catch the exception you expect, and log why the item was skipped. Let anything else propagate.",
    "B113": "Pass `timeout=` (connect and read) to the request, and handle the timeout where the caller can.",
    "B301": "Don't unpickle data from outside the process. Use JSON or another format that can't run code.",
    "B303": _HASH,
    "B307": "Replace `eval` with `ast.literal_eval` for data, or an explicit dispatch for operations.",
    "B311": "Use `secrets` if the value must not be guessable. For sampling or jitter it needs no change.",
    "B314": _XML,
    "B318": _XML,
    "B324": _HASH,
    "B404": "Importing `subprocess` is not a defect. Check this module's calls; if they take no untrusted input, close as "
            "`wontfix` and say so.",
    "B501": "Remove `verify=False`. For a private CA, pass its bundle path instead.",
    "B506": "Use `yaml.safe_load`.",
    "B602": _SHELL,
    "B603": "Check that no argument comes from untrusted input, or validate it. If none does, close as `wontfix` and say so.",
    "B604": _SHELL,
    "B605": "Replace the shell call with `subprocess.run` and a list of arguments.",
    "B607": "Use the program's full path, or a fixed `PATH`, if an attacker could control it.",
    "B608": "Pass values as query parameters (`cursor.execute(sql, params)`). Take table and column names from an allowlist: "
            "they can't be parameters.",
    "B701": "Create the Jinja environment with `autoescape=True` or `select_autoescape()`.",
}


def recommendation(stored: str, source: str, finding_id: str) -> str:
    """*stored*, or the rule's fix when *stored* is the generic text and the rule has one."""
    parts = (finding_id or "").split(":")
    if stored != GENERIC or source != "bandit" or len(parts) < 3 or parts[0] != "bandit":
        return stored
    return BANDIT.get(parts[1], stored)
