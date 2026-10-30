"""QuadletGenerator: podman's own check of the app's quadlets, before any reaches the host."""

import subprocess
from types import SimpleNamespace

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.generator import QuadletGenerator
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import GENERATOR
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    install_processes,
    make_files_data,
    run_module,
)

MAIN = "[Container]\nImage=alpine\n"


def raising(exc):
    def run(argv, **kwargs):
        raise exc

    return run


def install(monkeypatch, run):
    """Replace subprocess.run, wherever the module starts a process, with a function."""
    install_processes(
        monkeypatch, SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )


def test_failure_reports_the_generator_output_and_writes_nothing(fake_host, systemd):
    systemd.generator_result = (1, "partial", 'converting "app--main.container": bad key\n')
    outcome = run_module(make_files_data(quadlets={"main.container": MAIN}), state="started")

    assert outcome.failure == {
        "msg": 'converting "app--main.container": bad key',
        "cmd": f"{GENERATOR} -dryrun -v",
        "rc": 1,
        "stdout": "partial",
        "stderr": 'converting "app--main.container": bad key\n',
    }
    # Validation runs on a staged copy, before anything reaches the host
    assert fake_host.files() == {}
    assert "daemon-reload" not in systemd.log


def test_validates_only_this_apps_quadlets(fake_host, systemd):
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
        "app--data-volume-init.container",
        "app--data.volume",
        "app--main.container",
    ]
    assert (
        call.staged["app--main.container"]
        == (fake_host.files()["etc/containers/systemd/app--main.container"])
    )


def test_quadlets_of_other_apps_it_names_are_validated_with_it(fake_host, systemd):
    """
    The generator cannot resolve a network of another app without its file. Those it names - and
    those they name in turn - are staged; nothing else of the other apps is.
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
        "app--main.container",
        "ing--certs.volume",
        "ing--edge.pod",
        "ing--main.network",
    ]


def test_retry_after_a_failure_validates_again(fake_host, systemd):
    files = make_files_data(quadlets={"main.container": MAIN})
    systemd.generator_result = (1, "", "bad\n")
    assert run_module(files, state="started").failure

    systemd.generator_result = (0, "", "")
    outcome = run_module(files, state="started")

    assert outcome.changed
    assert len(systemd.generator_calls) == 2
    assert "daemon-reload" in systemd.log
    assert "etc/containers/systemd/app--main.container" in fake_host.files()


def test_runs_with_a_fixed_timeout(fake_host, systemd):
    run_module(make_files_data(quadlets={"main.container": MAIN}), state="installed")
    assert [(call.argv, call.timeout) for call in systemd.generator_calls] == [
        ([GENERATOR, "-dryrun", "-v"], 30)
    ]


def test_timeout(monkeypatch):
    install(monkeypatch, raising(subprocess.TimeoutExpired(GENERATOR, 30)))
    with pytest.raises(QuadletAppError) as excinfo:
        QuadletGenerator().validate([])
    assert excinfo.value.msg == f"{GENERATOR} -dryrun -v timed out"


def test_cannot_be_executed(monkeypatch):
    install(monkeypatch, raising(FileNotFoundError("no such file")))
    with pytest.raises(QuadletAppError) as excinfo:
        QuadletGenerator().validate([])
    assert excinfo.value.msg == "Failed to execute quadlet syntax validation: no such file"
