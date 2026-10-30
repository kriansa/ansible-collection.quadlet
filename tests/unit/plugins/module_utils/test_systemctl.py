"""Systemd: how the module talks to systemd, and how each failure surfaces."""

import subprocess
from types import SimpleNamespace

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.systemctl import Systemd
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    install_processes,
    make_files_data,
    run_module,
)

MAIN = "[Container]\nImage=alpine\n"


def install(monkeypatch, run):
    """Replace subprocess.run, wherever the module starts a process, with a function."""
    install_processes(monkeypatch, SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))


def returning(returncode=0, stdout="", stderr=""):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return run


def raising(exc):
    def run(argv, **kwargs):
        raise exc
    return run


def recording(seen):
    """A run that succeeds, noting each command and its timeout."""
    def run(argv, **kwargs):
        seen.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


class TestRun:
    def test_success_uses_the_configured_timeout(self, monkeypatch):
        seen = []
        install(monkeypatch, recording(seen))
        Systemd(45).run("start", "x.service")
        assert seen == [(["systemctl", "start", "x.service"], 45)]

    def test_failure(self, monkeypatch):
        install(monkeypatch, returning(1, "out", "Job for x.service failed.\n"))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(120).run("start", "x.service")
        assert excinfo.value.msg == "systemctl start x.service failed: Job for x.service failed."
        assert excinfo.value.details == {
            "cmd": "systemctl start x.service",
            "rc": 1,
            "stdout": "out",
            "stderr": "Job for x.service failed.\n",
        }

    def test_timeout(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 45)))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(45).run("start", "x.service")
        assert excinfo.value.msg == "systemctl start x.service timed out after 45s"

    def test_cannot_be_executed(self, monkeypatch):
        install(monkeypatch, raising(OSError("exec format error")))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(120).run("daemon-reload")
        assert excinfo.value.msg == "Failed to execute systemctl: exec format error"

    def test_failure_during_a_deploy(self, fake_host, systemd):
        systemd.failures[("restart", "app--main.service")] = (1, "Job failed\n")
        outcome = run_module(make_files_data(quadlets={"main.container": MAIN}), state="started")
        assert outcome.failure["msg"] == "systemctl restart app--main.service failed: Job failed"
        assert outcome.failure["rc"] == 1


class TestDependencies:
    def test_parses_app_units_only(self, monkeypatch):
        output = (
            "app--main.service\n"
            "  app--db.service\n"
            "  system.slice\n"
            "  sysinit.target\n"
            "    app--cache.service\n"
            "  other--x.service\n"
        )
        install(monkeypatch, returning(0, output))
        assert Systemd(120).dependencies("app--main.service", "app--") == [
            "app--db.service",
            "app--cache.service",
        ]

    def test_query_is_bounded(self, monkeypatch):
        seen = []
        install(monkeypatch, recording(seen))
        Systemd(120).dependencies("app--main.service", "app--")
        assert seen == [
            (["systemctl", "list-dependencies", "--all", "--type", "service", "app--main.service"], 10)
        ]

    def test_failure_means_no_dependencies(self, monkeypatch):
        install(monkeypatch, returning(1, "", "Unit not found"))
        assert Systemd(120).dependencies("app--main.service", "app--") == []

    def test_timeout_means_no_dependencies(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 10)))
        assert Systemd(120).dependencies("app--main.service", "app--") == []

    def test_missing_systemctl_fails(self, monkeypatch):
        install(monkeypatch, raising(FileNotFoundError("systemctl")))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(120).dependencies("app--main.service", "app--")
        assert excinfo.value.msg == "systemctl command not found: systemctl"


class TestStateQueries:
    @pytest.mark.parametrize(
        "returncode, stdout, expected",
        [
            (0, "active\n", True),
            (3, "inactive\n", False),
            (3, "activating\n", False),
            (0, "reloading\n", False),
            (3, "failed\n", False),
            (4, "", False),
        ],
    )
    def test_is_active(self, monkeypatch, returncode, stdout, expected):
        install(monkeypatch, returning(returncode, stdout))
        assert Systemd(120).is_active("x.service") is expected

    @pytest.mark.parametrize(
        "returncode, stdout, expected",
        [
            (0, "enabled\n", True),
            (0, "enabled-runtime\n", False),
            (0, "static\n", False),
            (0, "alias\n", False),
            (1, "disabled\n", False),
            (1, "masked\n", False),
        ],
    )
    def test_is_enabled(self, monkeypatch, returncode, stdout, expected):
        install(monkeypatch, returning(returncode, stdout))
        assert Systemd(120).is_enabled("x.timer") is expected

    @pytest.mark.parametrize("query", ["is_active", "is_enabled"])
    def test_errors_read_as_no(self, monkeypatch, query):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 10)))
        assert getattr(Systemd(120), query)("x") is False

    @pytest.mark.parametrize("query, verb", [("is_active", "is-active"), ("is_enabled", "is-enabled")])
    def test_queries_are_bounded(self, monkeypatch, query, verb):
        seen = []
        install(monkeypatch, recording(seen))
        getattr(Systemd(120), query)("x")
        assert seen == [(["systemctl", verb, "x"], 10)]


class TestProperties:
    def test_none_asked_runs_nothing(self):
        # The fixture forbidding real processes would fail the test if anything ran
        assert Systemd(120).properties([]) == {}

    def test_parsed_per_unit(self, monkeypatch):
        output = "Id=a.service\nType=notify\n\nId=b.service\nActiveState=failed\n"
        install(monkeypatch, returning(0, output))
        assert Systemd(120).properties(["a.service", "b.service"]) == {
            "a.service": {"Id": "a.service", "Type": "notify"},
            "b.service": {"Id": "b.service", "ActiveState": "failed"},
        }

    def test_failure(self, monkeypatch):
        install(monkeypatch, returning(1, "", "Access denied\n"))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(120).properties(["a.service"])
        assert excinfo.value.msg == (
            "systemctl show -p Id,Type,ActiveState,SubState,InvocationID a.service failed: "
            "Access denied"
        )
        assert excinfo.value.details == {}

    def test_timeout(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 10)))
        with pytest.raises(QuadletAppError) as excinfo:
            Systemd(120).properties(["a.service"])
        assert excinfo.value.msg == (
            "systemctl show -p Id,Type,ActiveState,SubState,InvocationID a.service timed out"
        )


class TestJournalTail:
    def test_last_lines(self, monkeypatch):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "boom\n", "")

        install(monkeypatch, run)
        assert Systemd(120).journal_tail("x.service") == "boom"
        assert seen == [
            ["journalctl", "--unit", "x.service", "--lines", "10", "--no-pager", "--output", "cat"]
        ]

    @pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("journalctl", 10), OSError("x")])
    def test_unavailable_reads_as_empty(self, monkeypatch, exc):
        install(monkeypatch, raising(exc))
        assert Systemd(120).journal_tail("x.service") == ""
