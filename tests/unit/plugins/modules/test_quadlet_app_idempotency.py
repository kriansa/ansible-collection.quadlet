"""QuadletIdempotency: deciding whether anything needs writing."""

import builtins

import pytest

from ansible_collections.kriansa.commons.plugins.modules.quadlet_app import QuadletIdempotency


@pytest.fixture
def idem():
    return QuadletIdempotency("app", force=False)


class TestNeedsDeployment:
    def test_force_always_deploys(self):
        assert QuadletIdempotency("app", force=True).needs_deployment({}) is True

    def test_nothing_to_deploy(self, idem):
        assert idem.needs_deployment({}) is False

    def test_all_unchanged(self, idem, tmp_path):
        path = tmp_path / "f"
        path.write_text("same")
        assert idem.needs_deployment({str(path): "same"}) is False

    def test_one_changed(self, idem, tmp_path):
        same, other = tmp_path / "a", tmp_path / "b"
        same.write_text("same")
        other.write_text("old")
        assert idem.needs_deployment({str(same): "same", str(other): "new"}) is True


class TestFileChanged:
    def test_missing(self, idem, tmp_path):
        assert idem.file_changed(str(tmp_path / "nope"), "x") is True

    def test_directory(self, idem, tmp_path):
        assert idem.file_changed(str(tmp_path), "x") is True

    def test_identical(self, idem, tmp_path):
        path = tmp_path / "f"
        path.write_text("content\n")
        assert idem.file_changed(str(path), "content\n") is False

    def test_trailing_newline_counts(self, idem, tmp_path):
        path = tmp_path / "f"
        path.write_text("content\n")
        assert idem.file_changed(str(path), "content") is True

    def test_unreadable(self, idem, tmp_path, monkeypatch):
        path = tmp_path / "f"
        path.write_text("content")
        real_open = builtins.open

        def guarded_open(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError("denied")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", guarded_open)
        assert idem.file_changed(str(path), "content") is True

    def test_existing_file_not_utf8(self, idem, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"\xff\xfe")
        assert idem.file_changed(str(path), "x") is True

    def test_content_that_cannot_be_encoded_raises(self, idem, tmp_path):
        # Pinned: a lone surrogate (which is what non-UTF-8 input decodes to) is not caught here
        path = tmp_path / "f"
        path.write_text("x")
        with pytest.raises(UnicodeEncodeError):
            idem.file_changed(str(path), "\udcff")


def test_checksum_is_sha256_of_utf8():
    assert QuadletIdempotency._calculate_checksum("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
