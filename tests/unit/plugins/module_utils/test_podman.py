"""Podman: what the module asks podman about the host."""

import subprocess
from types import SimpleNamespace

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.podman import Podman
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import install_processes


def install(monkeypatch, run):
    install_processes(
        monkeypatch, SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )


class TestNetworkExists:
    @pytest.mark.parametrize("returncode, exists", [(0, True), (1, False)])
    def test_answer(self, monkeypatch, returncode, exists):
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs["timeout"]))
            return subprocess.CompletedProcess(argv, returncode, "", "")

        install(monkeypatch, run)
        assert Podman.network_exists("app--main") is exists
        assert seen == [(["podman", "network", "exists", "app--main"], 30)]

    def test_podman_failing_to_answer(self, monkeypatch):
        install(
            monkeypatch,
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 125, "", "storage broken"),
        )
        with pytest.raises(QuadletAppError) as excinfo:
            Podman.network_exists("app--main")
        assert excinfo.value.msg == "podman network exists app--main failed: storage broken"

    def test_timeout(self, monkeypatch):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 30)

        install(monkeypatch, run)
        with pytest.raises(QuadletAppError, match="^podman network exists app--main timed out$"):
            Podman.network_exists("app--main")

    def test_podman_missing(self, monkeypatch):
        def run(argv, **kwargs):
            raise FileNotFoundError("podman")

        install(monkeypatch, run)
        with pytest.raises(QuadletAppError, match="^Failed to execute podman: podman$"):
            Podman.network_exists("app--main")
