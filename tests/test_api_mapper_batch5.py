"""ecosystem.api_mapper: FastAPI decorators in their common spellings are found."""
from __future__ import annotations

from pathlib import Path

from ecosystem.api_mapper import discover_api_endpoints


def test_common_route_decorator_forms_are_found(tmp_path: Path) -> None:
    f = tmp_path / "routes.py"
    f.write_text('@app.get( "/spaced")\n'
                 '@router.post(\n    "/black",\n    response_model=X,\n)\n'
                 '@router.put(path="/keyword")\n'
                 '@audit_router.delete("/named")\n'
                 '@api.patch("/other/{item_id}")\n')
    assert discover_api_endpoints(f) == ["DELETE /named", "GET /spaced", "PATCH /other/{item_id}",
                                         "POST /black", "PUT /keyword"]
