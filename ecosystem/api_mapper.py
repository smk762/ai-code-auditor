from __future__ import annotations

import re
from pathlib import Path


API_PATTERN = re.compile(r"(GET|POST|PUT|DELETE|PATCH)\s+(/[\w/\-{}:.]+)", re.IGNORECASE)
# any receiver (app, router, api, audit_router...), black's line break after "(", and path="..."
ROUTE_CALL_PATTERN = re.compile(r"@\w+\.(get|post|put|delete|patch)\(\s*(?:path\s*=\s*)?['\"]([^'\"]+)['\"]")
# A route parameter is "{name}" or a Starlette converter "{name:path}"; anything else in braces
# ("{i:04d}", "{0}") is a format template from an f-string, not a real endpoint.
_BRACES = re.compile(r"\{([^{}]*)\}")
_PARAM = re.compile(r"[A-Za-z_]\w*(?::(?:str|int|float|path|uuid))?")


def is_test_source(file_path: Path) -> bool:
    """Test modules and conftest: their routes are fixtures, not the repo's API surface."""
    name = file_path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _clean_route(route: str) -> str | None:
    route = route.rstrip(".,:")  # sentence punctuation after a path in prose or docs
    if not route.startswith("/") or route.count("{") != route.count("}"):
        return None
    if any(not _PARAM.fullmatch(inner) for inner in _BRACES.findall(route)):
        return None
    return route


def discover_api_endpoints(file_path: Path) -> list[str]:
    if is_test_source(file_path):
        return []
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    endpoints: set[str] = set()
    for method, route in API_PATTERN.findall(text):
        if cleaned := _clean_route(route):
            endpoints.add(f"{method.upper()} {cleaned}")
    for method, route in ROUTE_CALL_PATTERN.findall(text):
        if cleaned := _clean_route(route):
            endpoints.add(f"{method.upper()} {cleaned}")
    return sorted(endpoints)
