"""Fixtures shared by the unit tests."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from ansible_collections.kriansa.commons.plugins.modules import quadlet_app
from ansible_collections.kriansa.commons.tests.unit._support.fakes import FakeSystemd


class _NoProcesses:
    """Stands in for `subprocess` inside the module unless a test installs a fake systemd."""

    TimeoutExpired = __import__("subprocess").TimeoutExpired

    @staticmethod
    def run(argv, *args, **kwargs):
        raise AssertionError(f"a unit test tried to run a real process: {argv}")


@pytest.fixture(autouse=True)
def forbid_real_processes(monkeypatch):
    """No unit test may run systemctl or the quadlet generator on this machine."""
    monkeypatch.setattr(quadlet_app, "subprocess", _NoProcesses)


@pytest.fixture
def systemd(monkeypatch, request):
    """A fake systemd answering the module's systemctl and generator calls."""
    fake = FakeSystemd()
    if "fake_host" in request.fixturenames:
        # Bootstrap units then leave their markers on the fake host, as they would on a real one
        fake.quadlet_dir = str(request.getfixturevalue("fake_host").quadlet_dir)
    monkeypatch.setattr(quadlet_app, "subprocess", fake.as_subprocess_module())
    monkeypatch.setattr(quadlet_app, "time", fake.as_time_module())
    return fake


@dataclass
class FakeHost:
    """The host directories the module writes to, relocated under a temporary directory."""

    root: Path

    @property
    def quadlet_dir(self) -> Path:
        return self.root / "etc/containers/systemd"

    @property
    def unit_dir(self) -> Path:
        return self.root / "etc/systemd/system"

    @property
    def srv_dir(self) -> Path:
        return self.root / "srv"

    @property
    def state_dir(self) -> Path:
        return self.root / "var/lib/quadlet-app"

    def files(self) -> dict:
        """Every file on the fake host, keyed relative to its root."""
        return {
            path.relative_to(self.root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """
    Point every path the module writes to at a temporary directory.

    The module reads these constants when it builds its components, so the fixture must be in
    place before the module object is created.
    """
    host = FakeHost(tmp_path / "host")
    monkeypatch.setattr(quadlet_app, "QUADLET_SYSTEMD_DIR", str(host.quadlet_dir))
    monkeypatch.setattr(quadlet_app, "SYSTEMD_UNIT_DIR", str(host.unit_dir))
    monkeypatch.setattr(quadlet_app, "QUADLET_APP_BASE_DIR", str(host.srv_dir))
    monkeypatch.setattr(quadlet_app, "QUADLET_APP_STATE_DIR", str(host.state_dir))
    return host
