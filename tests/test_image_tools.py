"""Tools baked into the image stay at the versions the rest of the repo expects."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_images_semgrep_is_the_rules_extras_pin() -> None:
    # The rules extra's pin matches argus-ci's catalogue; the image's must match it, or suite-rule hits
    # in the container would differ from the host's and from CI's.
    extra = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["rules"]
    pin = next(s.split("==", 1)[1] for s in extra if s.startswith("semgrep=="))
    image = re.search(r"^ARG SEMGREP_VERSION=(\S+)$", (ROOT / "Dockerfile").read_text(), re.M)
    assert image and image.group(1) == pin
