"""
Every deployed golden must be a valid quadlet set.

Golden files pin what the module writes; this proves what it writes is something podman's
generator accepts. It runs the same generator the module itself runs on the host after a deploy,
here against the golden directories, so it needs podman installed but no VM.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ansible_collections.kriansa.quadlet.tests.unit._support.examples import example_apps
from ansible_collections.kriansa.quadlet.tests.unit._support.golden import GOLDEN_DIR

QUADLET = Path("/usr/libexec/podman/quadlet")

pytestmark = [
    pytest.mark.quadlet,
    pytest.mark.skipif(not QUADLET.exists(), reason=f"{QUADLET} is not installed"),
]

APPS = example_apps()
GOLDEN = GOLDEN_DIR / "quadlet_app"


def quadlet_dir(app) -> Path:
    return GOLDEN / app.id / "deployed" / "etc" / "containers" / "systemd"


def dryrun(*unit_dirs: Path) -> subprocess.CompletedProcess:
    """Run the generator over these directories, the way systemd would at boot."""
    env = {**os.environ, "QUADLET_UNIT_DIRS": ":".join(str(d) for d in unit_dirs)}
    return subprocess.run(
        [str(QUADLET), "-dryrun", "-no-kmsg-log"],
        capture_output=True, text=True, env=env, check=False, timeout=60,
    )


def generated_units(stdout: str) -> list:
    """The unit names in dryrun output, which prints each as a ---name--- header."""
    return re.findall(r"^---(.+)---$", stdout, re.MULTILINE)


@pytest.mark.parametrize("app", APPS, ids=[app.id for app in APPS])
def test_app_is_accepted_by_the_generator(app):
    result = dryrun(quadlet_dir(app))
    assert result.returncode == 0, result.stderr

    # Exit 0 is not enough on its own: one generated unit per quadlet proves none was skipped
    quadlets = sorted(p.name for p in quadlet_dir(app).iterdir())
    assert len(generated_units(result.stdout)) == len(quadlets), (quadlets, result.stdout)


def test_all_apps_are_accepted_together():
    """Every app on one host, as the module's own validation sees /etc/containers/systemd."""
    dirs = [quadlet_dir(app) for app in APPS]
    result = dryrun(*dirs)
    assert result.returncode == 0, result.stderr

    total = sum(len(list(d.iterdir())) for d in dirs)
    assert len(generated_units(result.stdout)) == total


def test_gate_fails_on_an_invalid_quadlet(tmp_path):
    """Negative control: a gate that cannot fail proves nothing."""
    app = next(app for app in APPS if (quadlet_dir(app) / f"{app.name}--main.container").exists())
    broken = tmp_path / "systemd"
    shutil.copytree(quadlet_dir(app), broken)
    main = broken / f"{app.name}--main.container"
    main.write_text(main.read_text().replace("[Container]\n", "[Container]\nBogusKey=1\n", 1))

    result = dryrun(broken)
    assert result.returncode != 0
    assert "BogusKey" in result.stderr
