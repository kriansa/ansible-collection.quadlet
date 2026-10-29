"""
QuadletValidator: the checks run when the module is used without its action plugin.

In practice the action plugin always runs and this path is never taken from a playbook, so these
checks never fire for real deploys. They are covered anyway: the code exists, and quadlet_stack
moves these rules into its action plugin, where they will fire.
"""

import os
from pathlib import Path

import pytest

from ansible_collections.kriansa.commons.plugins.modules.quadlet_app import (
    QuadletValidator,
    ValidationError,
)


def make_app(root: Path, files=(), dirs=()) -> Path:
    """Create an app directory holding the given files (relative paths) and empty directories."""
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Container]\nImage=alpine\n")
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def valid_app(tmp_path):
    return make_app(tmp_path / "myapp", files=["quadlets/main.container"])


class TestValidateAll:
    def test_returns_normalised_name(self, valid_app):
        assert QuadletValidator(str(valid_app), "MyApp").validate_all() == "myapp"

    def test_constructor_lowercases(self, valid_app):
        assert QuadletValidator(str(valid_app), "MyApp").app_name == "myapp"

    def test_source_is_checked_before_the_name(self, tmp_path):
        # Both are wrong; the missing directory is what gets reported
        with pytest.raises(ValidationError, match="Source directory not found"):
            QuadletValidator(str(tmp_path / "missing"), "1bad").validate_all()

    def test_name_is_checked_before_main_container(self, tmp_path):
        app = make_app(tmp_path / "x", dirs=["quadlets"])
        with pytest.raises(ValidationError, match="Invalid application name"):
            QuadletValidator(str(app), "1bad").validate_all()


class TestValidateSourceDirectory:
    def test_missing(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(ValidationError, match=f"^Source directory not found: {missing}$"):
            QuadletValidator(str(missing), "app").validate_source_directory()

    def test_is_a_file(self, tmp_path):
        path = tmp_path / "file"
        path.write_text("")
        with pytest.raises(ValidationError, match=f"^Source path is not a directory: {path}$"):
            QuadletValidator(str(path), "app").validate_source_directory()

    def test_quadlets_missing(self, tmp_path):
        app = make_app(tmp_path / "app")
        with pytest.raises(ValidationError, match="Required directory not found: .*/quadlets. "
                                                  "The quadlets/ subdirectory is mandatory."):
            QuadletValidator(str(app), "app").validate_source_directory()

    def test_quadlets_is_a_file(self, tmp_path):
        app = make_app(tmp_path / "app")
        (app / "quadlets").write_text("")
        with pytest.raises(ValidationError, match="Required directory not found"):
            QuadletValidator(str(app), "app").validate_source_directory()


class TestValidateAppName:
    @pytest.mark.parametrize(
        "name",
        [
            "a",
            "ab",
            "a1",
            "a-b",
            "a_b",
            "web-server",
            "api_v2",
            "db1",
            # Pinned: consecutive hyphens are accepted, though they read like the app--name separator
            "a--b",
            # Upper case is accepted because it is lowercased first
            "MyApp",
        ],
    )
    def test_valid(self, name):
        QuadletValidator("unused", name).validate_app_name()

    @pytest.mark.parametrize(
        "name", ["", "1", "1a", "-a", "a-", "a_", "a.b", "a b", "_a", "ä", "a/b", "ab\n", "a\n"]
    )
    def test_invalid(self, name):
        with pytest.raises(ValidationError) as excinfo:
            QuadletValidator("unused", name).validate_app_name()
        assert str(excinfo.value) == (
            f"Invalid application name: {name.lower()}. Must start with a letter (a-z), end with a "
            "letter or number, and contain only lowercase letters, numbers, hyphens, and "
            "underscores."
        )


class TestValidateMainContainer:
    def test_present(self, valid_app):
        QuadletValidator(str(valid_app), "myapp").validate_main_container()

    def test_missing(self, tmp_path):
        app = make_app(tmp_path / "app", files=["quadlets/db.container"])
        with pytest.raises(ValidationError, match="Required file not found: .*/quadlets/main.container. "
                                                  "The quadlets/main.container file is mandatory."):
            QuadletValidator(str(app), "app").validate_main_container()

    def test_main_container_is_a_directory(self, tmp_path):
        app = make_app(tmp_path / "app", dirs=["quadlets/main.container"])
        with pytest.raises(ValidationError, match="Required file not found"):
            QuadletValidator(str(app), "app").validate_main_container()

    def test_main_pod_does_not_count(self, tmp_path):
        app = make_app(tmp_path / "app", files=["quadlets/main.pod"])
        with pytest.raises(ValidationError, match="Required file not found"):
            QuadletValidator(str(app), "app").validate_main_container()


class TestValidateInitConfigStructure:
    def test_absent_directories_are_fine(self, valid_app):
        QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_kind_as_a_file_is_skipped(self, valid_app, kind):
        (valid_app / kind).write_text("")
        QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_top_level_files_are_ignored(self, valid_app, kind):
        make_app(valid_app, files=[f"{kind}/README"])
        QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_matching_container_stem(self, valid_app, kind):
        make_app(valid_app, dirs=[f"{kind}/main"])
        QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    def test_matching_pod_stem(self, valid_app):
        make_app(valid_app, files=["quadlets/backend.pod"], dirs=["config/backend"])
        QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    @pytest.mark.parametrize("suffix", [".container", ".pod"])
    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_suffixed_directory_is_rejected(self, valid_app, kind, suffix):
        make_app(valid_app, dirs=[f"{kind}/main{suffix}"])
        with pytest.raises(ValidationError) as excinfo:
            QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()
        assert str(excinfo.value) == (
            f"Invalid {kind} subdirectory: main{suffix}. Use the quadlet stem without suffix "
            f"(e.g., main/ instead of main{suffix}/)"
        )

    def test_other_suffixes_are_treated_as_stems(self, valid_app):
        # Not rejected as "suffixed"; it simply matches no quadlet named "main.volume"
        make_app(valid_app, dirs=["config/main.volume"])
        with pytest.raises(ValidationError, match="No corresponding quadlet file for config/main.volume"):
            QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()

    def test_orphan_directory(self, valid_app):
        make_app(valid_app, dirs=["config/foo"])
        with pytest.raises(ValidationError) as excinfo:
            QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()
        assert str(excinfo.value) == (
            "No corresponding quadlet file for config/foo. "
            "Expected a file like quadlets/foo.container or quadlets/foo.pod"
        )

    def test_kube_does_not_count_as_a_match(self, tmp_path):
        app = make_app(tmp_path / "app", files=["quadlets/main.container", "quadlets/web.kube"],
                       dirs=["config/web"])
        with pytest.raises(ValidationError, match="No corresponding quadlet file for config/web"):
            QuadletValidator(str(app), "app").validate_init_config_structure()

    def test_ambiguous_container_and_pod(self, valid_app):
        make_app(valid_app, files=["quadlets/main.pod"], dirs=["init/main"])
        with pytest.raises(ValidationError) as excinfo:
            QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()
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
        with pytest.raises(ValidationError, match="^Cannot read init: denied$"):
            QuadletValidator(str(valid_app), "myapp").validate_init_config_structure()
