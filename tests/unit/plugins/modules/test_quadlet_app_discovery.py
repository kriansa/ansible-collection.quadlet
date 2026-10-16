"""QuadletFileDiscovery: which files the module-only path picks up from an app directory."""

import os
from pathlib import Path

import pytest

from ansible_collections.kriansa.commons.plugins.modules.quadlet_app import (
    QuadletFileDiscovery,
    ValidationError,
)
from ansible_collections.kriansa.commons.tests.unit._support.fakes import raiser


def write(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    (root / "quadlets").mkdir(parents=True)
    return root


def names(entries):
    return [entry[0] for entry in entries]


class TestDiscoverAll:
    def test_returns_every_kind_even_when_empty(self, app):
        assert QuadletFileDiscovery(str(app)).discover_all_files() == {
            "quadlets": [], "units": [], "init": [], "config": []
        }


class TestQuadlets:
    def test_sorted_with_full_paths(self, app):
        for name in ["main.container", "data.volume", "app.network", "b.pod", "k.kube"]:
            write(app, f"quadlets/{name}")
        found = QuadletFileDiscovery(str(app))._discover_quadlet_files()
        assert found == [
            (name, str(app / "quadlets" / name))
            for name in ["app.network", "b.pod", "data.volume", "k.kube", "main.container"]
        ]

    def test_other_files_are_skipped_silently(self, app):
        # Pinned: unlike units/, a stray file here is ignored rather than rejected
        for name in ["main.container", "README.md", "notes.yaml", "x.service", "x.build"]:
            write(app, f"quadlets/{name}")
        assert names(QuadletFileDiscovery(str(app))._discover_quadlet_files()) == ["main.container"]

    def test_subdirectories_are_skipped(self, app):
        write(app, "quadlets/main.container")
        (app / "quadlets" / "sub.container").mkdir()
        assert names(QuadletFileDiscovery(str(app))._discover_quadlet_files()) == ["main.container"]

    def test_timer_is_rejected(self, app):
        write(app, "quadlets/main.container")
        write(app, "quadlets/backup.timer")
        with pytest.raises(ValidationError) as excinfo:
            QuadletFileDiscovery(str(app))._discover_quadlet_files()
        assert str(excinfo.value) == "backup.timer is a systemd unit, not a quadlet. Move it to units/."

    def test_unreadable(self, app, monkeypatch):
        monkeypatch.setattr(os, "listdir", raiser(OSError("boom")))
        with pytest.raises(ValidationError, match="^Cannot read quadlets directory: boom$"):
            QuadletFileDiscovery(str(app))._discover_quadlet_files()


class TestUnits:
    def test_absent(self, app):
        assert QuadletFileDiscovery(str(app))._discover_unit_files() == []

    def test_sorted(self, app):
        write(app, "units/b.timer")
        write(app, "units/a.timer")
        assert QuadletFileDiscovery(str(app))._discover_unit_files() == [
            ("a.timer", str(app / "units/a.timer")),
            ("b.timer", str(app / "units/b.timer")),
        ]

    def test_subdirectories_are_skipped(self, app):
        write(app, "units/a.timer")
        (app / "units" / "dir.timer").mkdir()
        assert names(QuadletFileDiscovery(str(app))._discover_unit_files()) == ["a.timer"]

    @pytest.mark.parametrize("stray", ["backup.service", "README", "x.timer.bak"])
    def test_stray_file_is_rejected(self, app, stray):
        write(app, f"units/{stray}")
        with pytest.raises(ValidationError) as excinfo:
            QuadletFileDiscovery(str(app))._discover_unit_files()
        assert str(excinfo.value) == (
            f"Unsupported file in units/: {stray}. Only .timer files are supported."
        )

    def test_unreadable(self, app, monkeypatch):
        write(app, "units/a.timer")
        real_listdir = os.listdir
        monkeypatch.setattr(
            os, "listdir",
            lambda path: raiser(OSError("boom"))() if str(path).endswith("units")
            else real_listdir(path),
        )
        with pytest.raises(ValidationError, match="^Cannot read units directory: boom$"):
            QuadletFileDiscovery(str(app))._discover_unit_files()


@pytest.mark.parametrize("kind", ["init", "config"])
class TestInitConfig:
    def discover(self, app, kind):
        discovery = QuadletFileDiscovery(str(app))
        return discovery._discover_init_files() if kind == "init" else discovery._discover_config_files()

    def test_absent(self, app, kind):
        assert self.discover(app, kind) == []

    def test_top_level_files_are_skipped(self, app, kind):
        write(app, f"{kind}/README")
        assert self.discover(app, kind) == []

    def test_nested_files_relative_to_the_container_directory(self, app, kind):
        write(app, f"{kind}/main/a.conf")
        write(app, f"{kind}/main/sub/dir/b.conf")
        write(app, f"{kind}/db/schema.sql")
        found = self.discover(app, kind)
        assert sorted(found) == sorted([
            ("db", "schema.sql", str(app / kind / "db/schema.sql")),
            ("main", "a.conf", str(app / kind / "main/a.conf")),
            ("main", "sub/dir/b.conf", str(app / kind / "main/sub/dir/b.conf")),
        ])
        # Container directories are visited in sorted order
        assert [entry[0] for entry in found] == ["db", "main", "main"]

    def test_files_within_a_directory_are_sorted(self, app, kind):
        for name in ["c", "a", "b"]:
            write(app, f"{kind}/main/{name}")
        assert [entry[1] for entry in self.discover(app, kind)] == ["a", "b", "c"]

    def test_hidden_files_are_included(self, app, kind):
        write(app, f"{kind}/main/.env")
        assert [entry[1] for entry in self.discover(app, kind)] == [".env"]

    def test_empty_container_directory(self, app, kind):
        (app / kind / "main").mkdir(parents=True)
        assert self.discover(app, kind) == []

    def test_suffixed_directory_name_is_kept_raw(self, app, kind):
        # Discovery does not validate; QuadletValidator is what rejects this
        write(app, f"{kind}/main.container/f")
        assert self.discover(app, kind) == [("main.container", "f", str(app / kind / "main.container/f"))]
