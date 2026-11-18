"""Every service runs under an init process, so orphaned children (git's detached gc) get reaped."""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_every_service_runs_under_init():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert services
    missing = [name for name, svc in services.items() if svc.get("init") is not True]
    assert not missing, f"services without init: true: {missing}"
