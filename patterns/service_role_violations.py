"""
Service-role violation detectors.

Two checks:

1. UI-in-worker — flags HTML/template response patterns in repos whose
   declared role is ``worker`` or ``orchestrator``.  These services should be
   pure API/job-processing nodes; UI code belongs in the ``ui``-role service.

2. Competing providers — flags when a repo contains two or more provider
   classes whose names match a common interface pattern (e.g. *ImageProvider,
   *VideoProvider) but that call different backend services.  This indicates a
   dual-path where the active route depends on deployment config, reducing
   observability.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from auditor.contracts import RuleViolation


def _repo_relative(file_path: Path, repo_files: list[Path]) -> str:
    """Render *file_path* relative to the common prefix of *repo_files*.

    Falls back to the absolute path string when the common prefix is
    indeterminate (single-file scans, mixed roots), so reports never collapse
    to ambiguous basenames.
    """
    try:
        common = Path(*Path(repo_files[0]).parts[: _common_prefix_len(repo_files)])
        return str(file_path.relative_to(common))
    except (ValueError, IndexError):
        return str(file_path)


def _common_prefix_len(paths: list[Path]) -> int:
    if not paths:
        return 0
    parts_lists = [p.parts for p in paths]
    shortest = min(len(parts) for parts in parts_lists)
    for i in range(shortest):
        first = parts_lists[0][i]
        if any(parts[i] != first for parts in parts_lists):
            return i
    return shortest

# Patterns in raw source that indicate an HTML/template response.
_UI_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bTemplateResponse\s*\('),
    re.compile(r'\bHTMLResponse\s*\('),
    re.compile(r'\bJinja2Templates\s*\('),
    re.compile(r'render_template\s*\('),
    re.compile(r'content_type\s*=\s*["\']text/html'),
    re.compile(r'media_type\s*=\s*["\']text/html'),
]

# Regex to detect provider class names that indicate a backend choice.
# e.g. GothmogImageProvider, SelfHostedVideoProvider
_PROVIDER_SUFFIX_RE = re.compile(
    r'^(?P<backend>\w+?)(?P<iface>Image|Video|Audio|Chat|Embed)Provider$'
)


def detect_ui_in_worker(
    repo_files: dict[str, list[Path]],
    service_roles: list[dict],
) -> list[RuleViolation]:
    """
    Flag HTML/template response patterns found inside worker or orchestrator repos.
    """
    worker_roles = {"worker", "orchestrator"}
    role_map: dict[str, str] = {
        item["name"]: item["role"]
        for item in service_roles
        if "name" in item and "role" in item
    }

    violations: list[RuleViolation] = []
    for repo_name, files in repo_files.items():
        if role_map.get(repo_name) not in worker_roles:
            continue
        for file_path in files:
            if file_path.suffix not in {".py", ".html", ".jinja", ".jinja2", ".j2"}:
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for pattern in _UI_PATTERNS:
                match = pattern.search(source)
                if match:
                    line = source[: match.start()].count("\n") + 1
                    rel = _repo_relative(file_path, files)
                    violations.append(
                        RuleViolation(
                            rule_name="ui_in_worker",
                            violating_path=f"{repo_name}: {rel}:{line}",
                            entities=[repo_name],
                            evidence=(
                                f"UI response pattern '{match.group().strip()}' found in "
                                f"{repo_name!r} (role: {role_map.get(repo_name, 'unknown')}). "
                                f"HTML pages belong in the ui-role service."
                            ),
                            severity="MEDIUM",
                        )
                    )
                    break  # one violation per file is enough

    return violations


def detect_competing_providers(
    repo_files: dict[str, list[Path]],
    service_roles: list[dict],
) -> list[RuleViolation]:
    """
    Flag repos that contain multiple provider classes for the same interface
    (e.g. GothmogImageProvider + SelfHostedImageProvider) because this means
    the active call path depends on deployment config, reducing observability.

    This is not always wrong — loraline intentionally has both — but surfacing
    it helps reviewers decide whether the dual path is deliberate and documented.
    """
    role_map: dict[str, str] = {
        item["name"]: item["role"]
        for item in service_roles
        if "name" in item and "role" in item
    }

    violations: list[RuleViolation] = []
    for repo_name, files in repo_files.items():
        # interface → list of (backend, file_path, line)
        iface_providers: dict[str, list[tuple[str, str, int]]] = {}

        for file_path in files:
            if file_path.suffix != ".py":
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(source, filename=str(file_path))
            except (OSError, SyntaxError):
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                m = _PROVIDER_SUFFIX_RE.match(node.name)
                if not m:
                    continue
                iface = m.group("iface")
                backend = m.group("backend")
                iface_providers.setdefault(iface, []).append(
                    (backend, str(file_path), node.lineno)
                )

        for iface, providers in iface_providers.items():
            if len(providers) < 2:
                continue
            backends = [p[0] for p in providers]
            locations = [
                f"{_repo_relative(Path(p[1]), files)}:{p[2]}" for p in providers
            ]
            violations.append(
                RuleViolation(
                    rule_name="competing_providers",
                    violating_path=f"{repo_name}: {iface}Provider",
                    entities=[repo_name],
                    evidence=(
                        f"{repo_name!r} has {len(providers)} competing {iface}Provider "
                        f"implementations ({', '.join(backends)}). "
                        f"Active path depends on deployment config. "
                        f"Locations: {', '.join(locations)}."
                    ),
                    severity="MEDIUM",
                )
            )

    return violations
