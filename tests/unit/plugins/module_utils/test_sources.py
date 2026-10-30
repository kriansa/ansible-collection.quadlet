"""
Where an app's files come from: the action plugin's arguments, or the app's directory.

The directory is only read when the module runs without its action plugin, which a playbook never
does. Its checks are covered anyway: the code exists, and quadlet_stack moves these rules into its
action plugin, where they will fire.
"""

import os
from pathlib import Path

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.sources import (
    ActionPluginFiles,
    AppDirectory,
)
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import raiser


def write(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_app(root: Path, files=(), dirs=()) -> Path:
    """Create an app directory holding the given files (relative paths) and empty directories."""
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        write(root, rel, "[Container]\nImage=alpine\n")
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    (root / "quadlets").mkdir(parents=True)
    return root


@pytest.fixture
def valid_app(tmp_path):
    return make_app(tmp_path / "myapp", files=["quadlets/main.container"])


def names(entries):
    return [entry[0] for entry in entries]


class TestActionPluginFiles:
    def test_every_file_lands_in_its_place(self):
        files = [
            {"kind": "quadlet", "name": "main.container"},
            {"kind": "unit", "name": "job.timer"},
            {"kind": "init", "container": "main", "path": "a.sql"},
            {"kind": "config", "container": "main", "path": "sub/b.conf"},
        ]
        app = ActionPluginFiles(files, ["[Container]\n", "[Timer]\n", "a", "b"]).load("MyApp")
        assert app.name == "myapp"
        assert [(q.filename, q.source) for q in app.quadlets] == [
            ("main.container", "[Container]\n")
        ]
        assert [(u.filename, u.source) for u in app.units] == [("job.timer", "[Timer]\n")]
        assert [(f.kind, f.container, f.path, f.source) for f in app.data_files] == [
            ("init", "main", "a.sql", "a"),
            ("config", "main", "sub/b.conf", "b"),
        ]

    def test_none_is_an_empty_app(self):
        app = ActionPluginFiles(None, None).load("app")
        assert app.files() == []

    def test_names_and_contents_must_match_up(self):
        with pytest.raises(QuadletAppError) as excinfo:
            ActionPluginFiles([{"kind": "quadlet", "name": "main.container"}], []).load("app")
        assert excinfo.value.msg == "_files and _contents must have the same length (1 and 0)"

    def test_the_name_is_checked_first(self):
        with pytest.raises(QuadletAppError, match="Invalid application name"):
            ActionPluginFiles([{"kind": "quadlet", "name": "main.container"}], []).load("1bad")


class TestLoad:
    def test_returns_the_app_with_the_name_lowercased(self, valid_app):
        app = AppDirectory(str(valid_app)).load("MyApp")
        assert app.name == "myapp"
        assert [quadlet.filename for quadlet in app.quadlets] == ["main.container"]

    def test_source_is_checked_before_the_name(self, tmp_path):
        # Both are wrong; the missing directory is what gets reported
        with pytest.raises(QuadletAppError, match="Source directory not found"):
            AppDirectory(str(tmp_path / "missing")).load("1bad")

    def test_name_is_checked_before_main_container(self, tmp_path):
        app = make_app(tmp_path / "x", dirs=["quadlets"])
        with pytest.raises(QuadletAppError, match="Invalid application name"):
            AppDirectory(str(app)).load("1bad")

    def test_reads_every_kind_of_file(self, valid_app):
        write(valid_app, "units/job.timer", "[Timer]\n")
        write(valid_app, "init/main/a.sql", "a")
        write(valid_app, "config/main/b.conf", "b")
        app = AppDirectory(str(valid_app)).load("app")
        assert [unit.filename for unit in app.units] == ["job.timer"]
        assert [(f.kind, f.path, f.source) for f in app.data_files] == [
            ("init", "a.sql", "a"),
            ("config", "b.conf", "b"),
        ]

    def test_an_unreadable_unit_is_reported_before_a_bad_owner(self, valid_app):
        write(valid_app, "quadlets/data.volume", "[Volume]\nX-Owner=bad\n")
        unit = write(valid_app, "units/job.timer", "[Timer]\n")
        unit.write_bytes(b"\xff")
        with pytest.raises(QuadletAppError, match=f"^File {unit} is not valid UTF-8"):
            AppDirectory(str(valid_app)).load("app")


class TestCheckLocation:
    def test_missing(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(QuadletAppError, match=f"^Source directory not found: {missing}$"):
            AppDirectory(str(missing)).check_location()

    def test_is_a_file(self, tmp_path):
        path = tmp_path / "file"
        path.write_text("")
        with pytest.raises(QuadletAppError, match=f"^Source path is not a directory: {path}$"):
            AppDirectory(str(path)).check_location()

    def test_quadlets_missing(self, tmp_path):
        app = make_app(tmp_path / "app")
        with pytest.raises(
            QuadletAppError,
            match="Required directory not found: .*/quadlets. "
            "The quadlets/ subdirectory is mandatory.",
        ):
            AppDirectory(str(app)).check_location()

    def test_quadlets_is_a_file(self, tmp_path):
        app = make_app(tmp_path / "app")
        (app / "quadlets").write_text("")
        with pytest.raises(QuadletAppError, match="Required directory not found"):
            AppDirectory(str(app)).check_location()


class TestCheckLayout:
    def test_present(self, valid_app):
        AppDirectory(str(valid_app)).check_layout()

    def test_main_container_missing(self, tmp_path):
        app = make_app(tmp_path / "app", files=["quadlets/db.container"])
        with pytest.raises(
            QuadletAppError,
            match="Required file not found: .*/quadlets/main.container. "
            "The quadlets/main.container file is mandatory.",
        ):
            AppDirectory(str(app)).check_layout()

    def test_main_container_is_a_directory(self, tmp_path):
        app = make_app(tmp_path / "app", dirs=["quadlets/main.container"])
        with pytest.raises(QuadletAppError, match="Required file not found"):
            AppDirectory(str(app)).check_layout()

    def test_main_pod_does_not_count(self, tmp_path):
        app = make_app(tmp_path / "app", files=["quadlets/main.pod"])
        with pytest.raises(QuadletAppError, match="Required file not found"):
            AppDirectory(str(app)).check_layout()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_kind_as_a_file_is_skipped(self, valid_app, kind):
        (valid_app / kind).write_text("")
        AppDirectory(str(valid_app)).check_layout()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_top_level_files_are_ignored(self, valid_app, kind):
        make_app(valid_app, files=[f"{kind}/README"])
        AppDirectory(str(valid_app)).check_layout()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_matching_container_stem(self, valid_app, kind):
        make_app(valid_app, dirs=[f"{kind}/main"])
        AppDirectory(str(valid_app)).check_layout()

    def test_matching_pod_stem(self, valid_app):
        make_app(valid_app, files=["quadlets/backend.pod"], dirs=["config/backend"])
        AppDirectory(str(valid_app)).check_layout()

    @pytest.mark.parametrize("suffix", [".container", ".pod"])
    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_suffixed_directory_is_rejected(self, valid_app, kind, suffix):
        make_app(valid_app, dirs=[f"{kind}/main{suffix}"])
        with pytest.raises(QuadletAppError) as excinfo:
            AppDirectory(str(valid_app)).check_layout()
        assert str(excinfo.value) == (
            f"Invalid {kind} subdirectory: main{suffix}. Use the quadlet stem without suffix "
            f"(e.g., main/ instead of main{suffix}/)"
        )

    def test_other_suffixes_are_treated_as_stems(self, valid_app):
        # Not rejected as "suffixed"; it simply matches no quadlet named "main.volume"
        make_app(valid_app, dirs=["config/main.volume"])
        with pytest.raises(
            QuadletAppError, match="No corresponding quadlet file for config/main.volume"
        ):
            AppDirectory(str(valid_app)).check_layout()

    def test_orphan_directory(self, valid_app):
        make_app(valid_app, dirs=["config/foo"])
        with pytest.raises(QuadletAppError) as excinfo:
            AppDirectory(str(valid_app)).check_layout()
        assert str(excinfo.value) == (
            "No corresponding quadlet file for config/foo. "
            "Expected a file like quadlets/foo.container or quadlets/foo.pod"
        )

    def test_kube_does_not_count_as_a_match(self, tmp_path):
        app = make_app(
            tmp_path / "app",
            files=["quadlets/main.container", "quadlets/web.kube"],
            dirs=["config/web"],
        )
        with pytest.raises(QuadletAppError, match="No corresponding quadlet file for config/web"):
            AppDirectory(str(app)).check_layout()

    def test_ambiguous_container_and_pod(self, valid_app):
        make_app(valid_app, files=["quadlets/main.pod"], dirs=["init/main"])
        with pytest.raises(QuadletAppError) as excinfo:
            AppDirectory(str(valid_app)).check_layout()
        assert str(excinfo.value) == (
            "Ambiguous init subdirectory: main. Multiple matching quadlet files found: "
            "main.container, main.pod. Remove one of the conflicting quadlet files"
        )

    def test_unreadable_directory(self, valid_app, monkeypatch):
        make_app(valid_app, dirs=["init/main"])
        real_listdir = os.listdir

        def listdir(path):
            if str(path).endswith("/init"):
                raise PermissionError("denied")
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", listdir)
        with pytest.raises(QuadletAppError, match="^Cannot read init: denied$"):
            AppDirectory(str(valid_app)).check_layout()


class TestQuadletFiles:
    def test_absent(self, app):
        assert AppDirectory(str(app)).quadlet_files() == []

    def test_sorted_with_full_paths(self, app):
        for name in ["main.container", "data.volume", "app.network", "b.pod", "k.kube"]:
            write(app, f"quadlets/{name}")
        assert AppDirectory(str(app)).quadlet_files() == [
            (name, str(app / "quadlets" / name))
            for name in ["app.network", "b.pod", "data.volume", "k.kube", "main.container"]
        ]

    def test_other_files_are_skipped_silently(self, app):
        # Pinned: unlike units/, a stray file here is ignored rather than rejected
        for name in ["main.container", "README.md", "notes.yaml", "x.service", "x.build"]:
            write(app, f"quadlets/{name}")
        assert names(AppDirectory(str(app)).quadlet_files()) == ["main.container"]

    def test_subdirectories_are_skipped(self, app):
        write(app, "quadlets/main.container")
        (app / "quadlets" / "sub.container").mkdir()
        assert names(AppDirectory(str(app)).quadlet_files()) == ["main.container"]

    def test_timer_is_rejected(self, app):
        write(app, "quadlets/main.container")
        write(app, "quadlets/backup.timer")
        with pytest.raises(QuadletAppError) as excinfo:
            AppDirectory(str(app)).quadlet_files()
        assert (
            str(excinfo.value)
            == "backup.timer is a systemd unit, not a quadlet. Move it to units/."
        )

    def test_unreadable(self, app, monkeypatch):
        monkeypatch.setattr(os, "listdir", raiser(OSError("boom")))
        with pytest.raises(QuadletAppError, match="^Cannot read quadlets directory: boom$"):
            AppDirectory(str(app)).quadlet_files()


class TestUnitFiles:
    def test_absent(self, app):
        assert AppDirectory(str(app)).unit_files() == []

    def test_sorted(self, app):
        write(app, "units/b.timer")
        write(app, "units/a.timer")
        assert AppDirectory(str(app)).unit_files() == [
            ("a.timer", str(app / "units/a.timer")),
            ("b.timer", str(app / "units/b.timer")),
        ]

    def test_subdirectories_are_skipped(self, app):
        write(app, "units/a.timer")
        (app / "units" / "dir.timer").mkdir()
        assert names(AppDirectory(str(app)).unit_files()) == ["a.timer"]

    @pytest.mark.parametrize("stray", ["backup.service", "README", "x.timer.bak"])
    def test_stray_file_is_rejected(self, app, stray):
        write(app, f"units/{stray}")
        with pytest.raises(QuadletAppError) as excinfo:
            AppDirectory(str(app)).unit_files()
        assert str(excinfo.value) == (
            f"Unsupported file in units/: {stray}. Only .timer files are supported."
        )

    def test_unreadable(self, app, monkeypatch):
        write(app, "units/a.timer")
        real_listdir = os.listdir
        monkeypatch.setattr(
            os,
            "listdir",
            lambda path: (
                raiser(OSError("boom"))() if str(path).endswith("units") else real_listdir(path)
            ),
        )
        with pytest.raises(QuadletAppError, match="^Cannot read units directory: boom$"):
            AppDirectory(str(app)).unit_files()


@pytest.mark.parametrize("kind", ["init", "config"])
class TestDataFiles:
    def test_absent(self, app, kind):
        assert AppDirectory(str(app)).data_files(kind) == []

    def test_top_level_files_are_skipped(self, app, kind):
        write(app, f"{kind}/README")
        assert AppDirectory(str(app)).data_files(kind) == []

    def test_nested_files_relative_to_the_container_directory(self, app, kind):
        write(app, f"{kind}/main/a.conf")
        write(app, f"{kind}/main/sub/dir/b.conf")
        write(app, f"{kind}/db/schema.sql")
        found = AppDirectory(str(app)).data_files(kind)
        assert sorted(found) == sorted(
            [
                ("db", "schema.sql", str(app / kind / "db/schema.sql")),
                ("main", "a.conf", str(app / kind / "main/a.conf")),
                ("main", "sub/dir/b.conf", str(app / kind / "main/sub/dir/b.conf")),
            ]
        )
        # Container directories are visited in sorted order
        assert [entry[0] for entry in found] == ["db", "main", "main"]

    def test_files_within_a_directory_are_sorted(self, app, kind):
        for name in ["c", "a", "b"]:
            write(app, f"{kind}/main/{name}")
        assert [entry[1] for entry in AppDirectory(str(app)).data_files(kind)] == ["a", "b", "c"]

    def test_hidden_files_are_included(self, app, kind):
        write(app, f"{kind}/main/.env")
        assert [entry[1] for entry in AppDirectory(str(app)).data_files(kind)] == [".env"]

    def test_empty_container_directory(self, app, kind):
        (app / kind / "main").mkdir(parents=True)
        assert AppDirectory(str(app)).data_files(kind) == []

    def test_suffixed_directory_name_is_kept_raw(self, app, kind):
        # Listing does not validate; check_layout is what rejects this
        write(app, f"{kind}/main.container/f")
        assert AppDirectory(str(app)).data_files(kind) == [
            ("main.container", "f", str(app / kind / "main.container/f"))
        ]
