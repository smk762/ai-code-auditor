from __future__ import annotations

import re
from pathlib import Path


API_PATTERN = re.compile(r"(GET|POST|PUT|DELETE|PATCH)\s+(/[\w/\-{}:]+)", re.IGNORECASE)
ROUTE_CALL_PATTERN = re.compile(r"@(app|router)\.(get|post|put|delete|patch)\(['\"]([^'\"]+)['\"]")


def discover_api_endpoints(file_path: Path) -> list[str]:
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    endpoints: set[str] = set()
    for method, route in API_PATTERN.findall(text):
        endpoints.add(f"{method.upper()} {route}")
    for _, method, route in ROUTE_CALL_PATTERN.findall(text):
        endpoints.add(f"{method.upper()} {route}")
    return sorted(endpoints)
