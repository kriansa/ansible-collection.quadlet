"""
The module-only path: the module reading the app directory itself, without the action plugin.

A playbook never takes this path - the action plugin always runs - so these tests are mostly
about the difference between the two: the checks only this path makes are checks real deploys
skip.
"""

import builtins
from pathlib import Path

import pytest

from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    make_files_data,
    run_module,
)

QUADLETS = {
    "main.container": "[Container]\nImage=alpine\nVolume=data.volume:/d\nVolume=%config%:/etc/x\n",
    "data.volume": "[Volume]\nX-Owner=10:10\n",
}
UNITS = {"job.timer": "[Timer]\nOnCalendar=daily\n"}
CONFIG = {"main": {"a.conf": "a\n", "sub/b.conf": "b\n"}}


def write_app(root: Path, quadlets=None, units=None, config=None, init=None) -> Path:
    for kind, files in (("quadlets", quadlets), ("units", units)):
        for name, content in (files or {}).items():
            path = root / kind / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    for kind, tree in (("config", config), ("init", init)):
        for container, files in (tree or {}).items():
            for rel, content in files.items():
                path = root / kind / container / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
    return root


def test_same_output_as_the_action_plugin_path(tmp_path, fake_host, systemd):
    app_dir = write_app(tmp_path / "app", QUADLETS, UNITS, CONFIG)
    fallback = run_module(None, src=str(app_dir), state="installed", control_node_processed=False)
    assert fallback.exit is not None, fallback.failure
    via_fallback = fake_host.files()

    for path in fake_host.root.rglob("*"):
        if path.is_file():
            path.unlink()
    action = run_module(make_files_data(QUADLETS, UNITS, config=CONFIG), state="installed")

    assert fake_host.files() == via_fallback
    assert action.exit == fallback.exit


class TestChecksOnlyThisPathMakes:
    def test_missing_main_container(self, tmp_path, fake_host, systemd):
        app_dir = write_app(tmp_path / "app", {"db.container": "[Container]\n"})
        outcome = run_module(None, src=str(app_dir), control_node_processed=False)
        assert outcome.failure["msg"].startswith("Required file not found:")

    def test_missing_main_container_through_the_action_plugin_path(self, fake_host, systemd):
        # Pinned: the same app deploys without complaint when the action plugin sent it
        outcome = run_module(make_files_data({"db.container": "[Container]\n"}), state="installed")
        assert outcome.changed

    def test_orphan_config_directory(self, tmp_path, fake_host, systemd):
        app_dir = write_app(tmp_path / "app", QUADLETS, config={"nomatch": {"f": "x"}})
        outcome = run_module(None, src=str(app_dir), control_node_processed=False)
        assert outcome.failure["msg"].startswith("No corresponding quadlet file for config/nomatch.")

    def test_orphan_config_directory_through_the_action_plugin_path(self, fake_host, systemd):
        # Pinned: deployed to /srv/app/config/nomatch/
        outcome = run_module(make_files_data(QUADLETS, config={"nomatch": {"f": "x"}}), state="installed")
        assert outcome.changed
        assert fake_host.files()["srv/app/config/nomatch/f"] == "x"

    def test_suffixed_config_directory_through_the_action_plugin_path(self, fake_host, systemd):
        # Pinned: the suffix is silently dropped
        outcome = run_module(
            make_files_data(QUADLETS, config={"main.container": {"f": "x"}}), state="installed"
        )
        assert outcome.changed
        assert fake_host.files()["srv/app/config/main/f"] == "x"

    def test_empty_app_through_the_action_plugin_path(self, fake_host, systemd):
        # Pinned: nothing to deploy reads as "already up to date"
        outcome = run_module(make_files_data(), state="installed")
        assert outcome.exit["changed"] is False
        assert outcome.msg == "application already up to date"


class TestReadingFiles:
    def test_unreadable_file(self, tmp_path, fake_host, systemd, monkeypatch):
        app_dir = write_app(tmp_path / "app", QUADLETS)
        target = app_dir / "quadlets" / "main.container"
        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            if str(file) == str(target):
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded_open)
        outcome = run_module(None, src=str(app_dir), control_node_processed=False)
        assert outcome.failure == {"msg": f"Failed to read file {target}: denied"}

    def test_not_utf8(self, tmp_path, fake_host, systemd):
        app_dir = write_app(tmp_path / "app", QUADLETS)
        target = app_dir / "config" / "main" / "bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\xfe")
        outcome = run_module(None, src=str(app_dir), control_node_processed=False)
        assert outcome.failure["msg"].startswith(f"File {target} is not valid UTF-8:")

    def test_stray_unit_file(self, tmp_path, fake_host, systemd):
        app_dir = write_app(tmp_path / "app", QUADLETS, {"x.service": "[Service]\n"})
        outcome = run_module(None, src=str(app_dir), control_node_processed=False)
        assert outcome.failure == {
            "msg": "Unsupported file in units/: x.service. Only .timer files are supported."
        }

    @pytest.mark.parametrize("name", ["MyApp", "my-app"])
    def test_name_from_the_directory(self, tmp_path, fake_host, systemd, name):
        app_dir = write_app(tmp_path / name, QUADLETS)
        outcome = run_module(None, src=str(app_dir), state="installed", control_node_processed=False)
        assert outcome.exit["application_name"] == name.lower()
