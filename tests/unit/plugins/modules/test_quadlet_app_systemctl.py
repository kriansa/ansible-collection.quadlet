"""How the module talks to systemd and the quadlet generator, and how each failure surfaces."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from ansible_collections.kriansa.quadlet.plugins.modules import quadlet_app
from ansible_collections.kriansa.quadlet.plugins.modules.quadlet_app import QuadletAppModule
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import (
    GENERATOR,
    AnsibleFailJson,
    FakeModule,
)
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    make_files_data,
    run_module,
)

MAIN = "[Container]\nImage=alpine\n"


def make_module(systemctl_timeout=120):
    module = QuadletAppModule(
        FakeModule({"src": "/apps/app", "state": "started", "force": False,
                    "systemctl_timeout": systemctl_timeout, "settle_time": 0})
    )
    module.app_name = "app"
    return module


def install(monkeypatch, run):
    """Replace the module's subprocess.run with a function."""
    monkeypatch.setattr(
        quadlet_app, "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def returning(returncode=0, stdout="", stderr=""):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return run


def raising(exc):
    def run(argv, **kwargs):
        raise exc
    return run


class TestGenerator:
    def test_failure_reports_the_generator_output_and_writes_nothing(self, fake_host, systemd):
        systemd.generator_result = (1, "partial", "converting \"app--main.container\": bad key\n")
        outcome = run_module(make_files_data(quadlets={"main.container": MAIN}), state="started")

        assert outcome.failure == {
            "msg": 'converting "app--main.container": bad key',
            "cmd": f"{GENERATOR} -dryrun -v",
            "rc": 1,
            "stdout": "partial",
            "stderr": "converting \"app--main.container\": bad key\n",
        }
        # Validation runs on a staged copy, before anything reaches the host
        assert fake_host.files() == {}
        assert "daemon-reload" not in systemd.log

    def test_validates_only_this_apps_quadlets(self, fake_host, systemd):
        """Another app's file on the host - broken or not - has no say in this deploy."""
        fake_host.quadlet_dir.mkdir(parents=True)
        (fake_host.quadlet_dir / "other--main.container").write_text("[Container]\nBogusKey=1\n")
        files = make_files_data(
            quadlets={"main.container": MAIN, "data.volume": "[Volume]\n"},
            units={"job.timer": "[Timer]\nOnCalendar=daily\n"},
            config={"main": {"app.conf": "x\n"}},
        )

        run_module(files, state="installed")

        [call] = systemd.generator_calls
        assert call.unit_dirs != str(fake_host.quadlet_dir)
        # Every quadlet the app deploys, generated ones included; no timers or config files
        assert sorted(call.staged) == [
            "app--data-volume-init.container", "app--data.volume", "app--main.container",
        ]
        assert call.staged["app--main.container"] == (
            fake_host.files()["etc/containers/systemd/app--main.container"]
        )

    def test_quadlets_of_other_apps_it_names_are_validated_with_it(self, fake_host, systemd):
        """
        The generator cannot resolve a network of another app without its file. Those it names -
        and those they name in turn - are staged; nothing else of the other apps is.
        """
        fake_host.quadlet_dir.mkdir(parents=True)
        for name, content in {
            "ing--main.network": "[Network]\n",
            "ing--edge.pod": "[Pod]\nNetwork=ing--main.network\nVolume=ing--certs.volume:/c\n",
            "ing--certs.volume": "[Volume]\n",
            "ing--main.container": "[Container]\nBogusKey=1\n",
        }.items():
            (fake_host.quadlet_dir / name).write_text(content)
        main = (
            "[Container]\nImage=alpine\nNetwork=ing--main.network:ip=192.0.2.5\nPod=ing--edge.pod\n"
            "Network=missing.network\nVolume=/srv/x:/x\n"
        )

        run_module(make_files_data(quadlets={"main.container": main}), state="installed")

        [call] = systemd.generator_calls
        assert sorted(call.staged) == [
            "app--main.container", "ing--certs.volume", "ing--edge.pod", "ing--main.network",
        ]

    def test_retry_after_a_failure_validates_again(self, fake_host, systemd):
        files = make_files_data(quadlets={"main.container": MAIN})
        systemd.generator_result = (1, "", "bad\n")
        assert run_module(files, state="started").failure

        systemd.generator_result = (0, "", "")
        outcome = run_module(files, state="started")

        assert outcome.changed
        assert len(systemd.generator_calls) == 2
        assert "daemon-reload" in systemd.log
        assert "etc/containers/systemd/app--main.container" in fake_host.files()

    def test_runs_with_a_fixed_timeout(self, fake_host, systemd):
        run_module(make_files_data(quadlets={"main.container": MAIN}), state="installed")
        assert [(call.argv, call.timeout) for call in systemd.generator_calls] == [
            ([GENERATOR, "-dryrun", "-v"], 30)
        ]

    def test_timeout(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired(GENERATOR, 30)))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._validate_quadlet_syntax({})
        assert excinfo.value.result["msg"] == f"{GENERATOR} -dryrun -v timed out"

    def test_cannot_be_executed(self, monkeypatch):
        install(monkeypatch, raising(FileNotFoundError("no such file")))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._validate_quadlet_syntax({})
        assert excinfo.value.result["msg"] == (
            "Failed to execute quadlet syntax validation: no such file"
        )

    def test_a_generator_that_cannot_run_stops_there(self, monkeypatch):
        """Even if fail_json returned, nothing would read the result of a run that never happened."""
        install(monkeypatch, raising(FileNotFoundError("x")))
        module = make_module()
        failures = []
        module.module.fail_json = lambda **kwargs: failures.append(kwargs)
        module._validate_quadlet_syntax({})
        assert len(failures) == 1


class TestSystemctl:
    def test_success_uses_the_configured_timeout(self, monkeypatch):
        seen = []
        install(monkeypatch, lambda argv, **kw: seen.append((argv, kw["timeout"]))
                or subprocess.CompletedProcess(argv, 0, "", ""))
        make_module(systemctl_timeout=45)._systemctl(["start", "x.service"])
        assert seen == [(["systemctl", "start", "x.service"], 45)]

    def test_failure(self, monkeypatch):
        install(monkeypatch, returning(1, "out", "Job for x.service failed.\n"))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._systemctl(["start", "x.service"])
        assert excinfo.value.result == {
            "msg": "systemctl start x.service failed: Job for x.service failed.",
            "cmd": "systemctl start x.service",
            "rc": 1,
            "stdout": "out",
            "stderr": "Job for x.service failed.\n",
        }

    def test_timeout(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 45)))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module(systemctl_timeout=45)._systemctl(["start", "x.service"])
        assert excinfo.value.result["msg"] == "systemctl start x.service timed out after 45s"

    def test_cannot_be_executed(self, monkeypatch):
        install(monkeypatch, raising(OSError("exec format error")))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._systemctl(["daemon-reload"])
        assert excinfo.value.result["msg"] == "Failed to execute systemctl: exec format error"

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
        assert make_module()._get_app_dependencies("app--main.service") == [
            "app--db.service",
            "app--cache.service",
        ]

    def test_query_is_bounded(self, monkeypatch):
        seen = []
        install(monkeypatch, lambda argv, **kw: seen.append((argv, kw["timeout"]))
                or subprocess.CompletedProcess(argv, 0, "", ""))
        make_module()._get_app_dependencies("app--main.service")
        assert seen == [(["systemctl", "list-dependencies", "--all", "--type", "service", "app--main.service"], 10)]

    def test_failure_means_no_dependencies(self, monkeypatch):
        install(monkeypatch, returning(1, "", "Unit not found"))
        assert make_module()._get_app_dependencies("app--main.service") == []

    def test_timeout_means_no_dependencies(self, monkeypatch):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 10)))
        assert make_module()._get_app_dependencies("app--main.service") == []

    def test_missing_systemctl_fails(self, monkeypatch):
        install(monkeypatch, raising(FileNotFoundError("systemctl")))
        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._get_app_dependencies("app--main.service")
        assert excinfo.value.result["msg"] == "systemctl command not found: systemctl"


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
        assert make_module()._is_service_active("x.service") is expected

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
        assert make_module()._is_unit_enabled("x.timer") is expected

    @pytest.mark.parametrize("method", ["_is_service_active", "_is_unit_enabled"])
    def test_errors_read_as_no(self, monkeypatch, method):
        install(monkeypatch, raising(subprocess.TimeoutExpired("systemctl", 10)))
        assert getattr(make_module(), method)("x") is False

    @pytest.mark.parametrize("method, verb", [("_is_service_active", "is-active"),
                                              ("_is_unit_enabled", "is-enabled")])
    def test_queries_are_bounded(self, monkeypatch, method, verb):
        seen = []
        install(monkeypatch, lambda argv, **kw: seen.append((argv, kw["timeout"]))
                or subprocess.CompletedProcess(argv, 0, "", ""))
        getattr(make_module(), method)("x")
        assert seen == [(["systemctl", verb, "x"], 10)]


class TestDeployFiles:
    def test_modes_and_overwrite(self, fake_host):
        module = make_module()
        target = fake_host.srv_dir / "app/config/main/a/b.conf"
        target.parent.mkdir(parents=True)
        target.write_text("old")
        target.chmod(0o600)

        module._deploy_files({str(target): "new", str(fake_host.quadlet_dir / "x.container"): "q"})

        assert target.read_text() == "new"
        assert oct(target.stat().st_mode & 0o777) == "0o644"
        assert oct((fake_host.quadlet_dir / "x.container").stat().st_mode & 0o777) == "0o644"

    def test_new_directories_are_world_readable(self, fake_host):
        old_umask = os.umask(0o022)
        try:
            make_module()._deploy_files({str(fake_host.srv_dir / "app/config/main/f"): "x"})
        finally:
            os.umask(old_umask)
        assert oct((fake_host.srv_dir / "app/config/main").stat().st_mode & 0o777) == "0o755"

    def test_directory_cannot_be_created(self, fake_host, monkeypatch):
        module = make_module()
        real_makedirs = os.makedirs

        def makedirs(path, *args, **kwargs):
            if "srv" in str(path):
                raise OSError("read-only file system")
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr(os, "makedirs", makedirs)
        with pytest.raises(AnsibleFailJson) as excinfo:
            module._deploy_files({str(fake_host.srv_dir / "app/f"): "x"})
        assert excinfo.value.result["msg"] == (
            f"Failed to create directory {fake_host.srv_dir / 'app'}: read-only file system"
        )

    def test_file_cannot_be_written(self, fake_host):
        module = make_module()
        target = fake_host.srv_dir / "app/f"
        target.mkdir(parents=True)  # a directory where the file should go
        with pytest.raises(AnsibleFailJson) as excinfo:
            module._deploy_files({str(target): "x"})
        assert excinfo.value.result["msg"].startswith(f"Failed to write file {target}: ")
        assert os.listdir(target.parent) == ["f"]

    @pytest.mark.parametrize("content, error", [
        ("a\udcffb",
         "'utf-8' codec can't encode character '\\udcff' in position 1: surrogates not allowed"),
        (42, "write() argument must be str, not int"),
    ])
    def test_a_failed_write_leaves_the_previous_file_in_place(self, fake_host, content, error):
        target = fake_host.srv_dir / "app/config/main/f"
        target.parent.mkdir(parents=True)
        target.write_text("old")

        with pytest.raises(AnsibleFailJson) as excinfo:
            make_module()._deploy_files({str(target): content})

        assert excinfo.value.result["msg"] == f"Failed to write file {target}: {error}"
        assert target.read_text() == "old"
        # Nor is anything left behind next to it
        assert os.listdir(target.parent) == ["f"]
