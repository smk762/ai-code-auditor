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


def test_semgrep_runs_with_an_unwritable_home(tmp_path: Path) -> None:
    # The container runs as a uid whose HOME is /; semgrep must not depend on writing there.
    import os
    import subprocess

    fake = tmp_path / "opt" / "semgrep"
    script = (ROOT / "docker" / "semgrep.sh").read_text().replace("/opt/semgrep/bin/semgrep", str(fake))
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho "HOME=$HOME METRICS=$SEMGREP_SEND_METRICS"\n')
    fake.chmod(0o755)
    wrapper = tmp_path / "semgrep"
    wrapper.write_text(script)
    wrapper.chmod(0o755)
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    out = subprocess.run([str(wrapper)], env={"PATH": os.environ["PATH"], "HOME": str(locked), "TMPDIR": str(tmp_path)},
                         capture_output=True, text=True, check=True).stdout
    assert f"HOME={tmp_path}/semgrep-home-{os.getuid()}" in out and "METRICS=off" in out
    out = subprocess.run([str(wrapper)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                         capture_output=True, text=True, check=True).stdout
    assert f"HOME={tmp_path} " in out  # a writable HOME is left alone
