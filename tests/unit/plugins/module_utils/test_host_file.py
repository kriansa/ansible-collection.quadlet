"""HostFile and HostFiles: whether a file needs writing, and writing it."""

import builtins
import os

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFiles


@pytest.fixture
def app(fake_host):
    return QuadletApp("app")


def config_file(app, content, path="f"):
    """A config file of the app, which is deployed as it is."""
    return app.add_data_file("config", "main", path, content)


def on_host(file, content):
    """Put content at a file's destination, as if an earlier run had written it."""
    os.makedirs(os.path.dirname(file.dest_path), exist_ok=True)
    with open(file.dest_path, "w", encoding="utf-8") as f:
        f.write(content)


class TestNeeded:
    def test_force_always_deploys(self):
        assert HostFiles([], force=True).needed() is True

    def test_nothing_to_deploy(self):
        assert HostFiles([], force=False).needed() is False

    def test_all_unchanged(self, app):
        file = config_file(app, "same")
        on_host(file, "same")
        assert HostFiles([file], force=False).needed() is False

    def test_one_changed(self, app):
        same, other = config_file(app, "same", "a"), config_file(app, "new", "b")
        on_host(same, "same")
        on_host(other, "old")
        assert HostFiles([same, other], force=False).needed() is True

    def test_force_counts_an_unchanged_file_as_changed(self, app):
        file = config_file(app, "same")
        on_host(file, "same")
        assert HostFiles([file], force=False).changed(file) is False
        assert HostFiles([file], force=True).changed(file) is True


class TestDiffersFromHost:
    def test_missing(self, app):
        assert config_file(app, "x").differs_from_host() is True

    def test_directory(self, app):
        file = config_file(app, "x")
        os.makedirs(file.dest_path)
        assert file.differs_from_host() is True

    def test_identical(self, app):
        file = config_file(app, "content\n")
        on_host(file, "content\n")
        assert file.differs_from_host() is False

    def test_trailing_newline_counts(self, app):
        file = config_file(app, "content")
        on_host(file, "content\n")
        assert file.differs_from_host() is True

    def test_windows_line_endings_match_once_written(self, app):
        # Otherwise such a file would be deployed again on every run
        file = config_file(app, "a=1\r\nb=2\r\n")
        file.write()
        assert file.differs_from_host() is False

    def test_line_endings_count(self, app):
        file = config_file(app, "a=1\r\n")
        on_host(file, "a=1\n")
        assert file.differs_from_host() is True

    def test_unreadable(self, app, monkeypatch):
        file = config_file(app, "content")
        on_host(file, "content")
        real_open = builtins.open

        def guarded_open(path, *args, **kwargs):
            if str(path) == file.dest_path:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded_open)
        assert file.differs_from_host() is True

    def test_existing_file_not_utf8(self, app):
        file = config_file(app, "x")
        os.makedirs(os.path.dirname(file.dest_path))
        with open(file.dest_path, "wb") as f:
            f.write(b"\xff\xfe")
        assert file.differs_from_host() is True

    def test_content_that_cannot_be_encoded_raises(self, app):
        # Pinned: a lone surrogate (which is what non-UTF-8 input decodes to) is not caught here
        file = config_file(app, "\udcff")
        on_host(file, "x")
        with pytest.raises(UnicodeEncodeError):
            file.differs_from_host()


class TestWrite:
    def test_modes_and_overwrite(self, app, fake_host):
        target = config_file(app, "new", "a/b.conf")
        quadlet = app.add_quadlet("x.network", "q")
        on_host(target, "old")
        os.chmod(target.dest_path, 0o600)

        HostFiles([target, quadlet], force=False).write()

        with open(target.dest_path, encoding="utf-8") as f:
            assert f.read() == "new"
        assert oct(os.stat(target.dest_path).st_mode & 0o777) == "0o644"
        assert oct(os.stat(quadlet.dest_path).st_mode & 0o777) == "0o644"
        assert quadlet.dest_path == f"{fake_host.quadlet_dir}/app--x.network"

    def test_new_directories_are_world_readable(self, app, fake_host):
        old_umask = os.umask(0o022)
        try:
            config_file(app, "x").write()
        finally:
            os.umask(old_umask)
        assert oct((fake_host.srv_dir / "app/config/main").stat().st_mode & 0o777) == "0o755"

    def test_directory_cannot_be_created(self, app, fake_host, monkeypatch):
        real_makedirs = os.makedirs

        def makedirs(path, *args, **kwargs):
            if "srv" in str(path):
                raise OSError("read-only file system")
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(os, "makedirs", makedirs)
        with pytest.raises(QuadletAppError) as excinfo:
            config_file(app, "x").write()
        assert excinfo.value.msg == (
            f"Failed to create directory {fake_host.srv_dir / 'app/config/main'}: "
            "read-only file system"
        )

    def test_file_cannot_be_written(self, app):
        file = config_file(app, "x")
        os.makedirs(file.dest_path)  # a directory where the file should go
        with pytest.raises(QuadletAppError) as excinfo:
            file.write()
        assert excinfo.value.msg.startswith(f"Failed to write file {file.dest_path}: ")
        assert os.listdir(os.path.dirname(file.dest_path)) == ["f"]

    @pytest.mark.parametrize(
        "content, error",
        [
            (
                "a\udcffb",
                "'utf-8' codec can't encode character '\\udcff' in position 1: surrogates not allowed",
            ),
            (42, "write() argument must be str, not int"),
        ],
    )
    def test_a_failed_write_leaves_the_previous_file_in_place(self, app, content, error):
        file = config_file(app, content)
        on_host(file, "old")

        with pytest.raises(QuadletAppError) as excinfo:
            file.write()

        assert excinfo.value.msg == f"Failed to write file {file.dest_path}: {error}"
        with open(file.dest_path, encoding="utf-8") as f:
            assert f.read() == "old"
        # Nor is anything left behind next to it
        assert os.listdir(os.path.dirname(file.dest_path)) == ["f"]
