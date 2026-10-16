"""J. How deploys fail, and what each failure leaves on the host."""

import time

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

GENERATOR = "/usr/lib/systemd/system-generators/podman-system-generator"


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def test_invalid_quadlet_fails_validation_after_writing(make_app, host):
    app = make_app("badkey")
    app.write("quadlets/main.container", main("BogusKey=1\n"))

    step = app.deploy("started")

    assert step.failed
    assert "unsupported key 'BogusKey'" in step.msg
    assert step.result.result["rc"] == 1
    assert step.result.result["cmd"] == f"{GENERATOR} -dryrun -v"
    # Pinned: validation runs after the files are written, and they stay there
    assert host.exists(app.quadlet_path("main.container"))
    # ... but systemd was not reloaded, so it never learnt about the unit
    assert host.unit(app.service("main"), "LoadState")["LoadState"] == "not-found"

    # Pinned: with nothing left to write, the next run skips validation and fails on the unit
    again = app.deploy("started")
    assert again.failed
    assert again.msg.startswith(f"systemctl start {app.name}--********.service failed:") or \
        again.msg.startswith(f"systemctl start {app.service('main')} failed:")
    assert "not found" in again.msg

    app.write("quadlets/main.container", main())
    fixed = app.deploy("started")
    assert not fixed.failed
    assert host.active_state(app.service("main")) == "active"


def test_another_apps_broken_file_fails_this_deploy(make_app, host):
    """Pinned: validation covers every quadlet on the host, not just this app's."""
    broken = make_app("brokenother")
    broken.write("quadlets/main.container", main("BogusKey=1\n"))
    broken.deploy("installed")
    healthy = make_app("healthy")
    healthy.write("quadlets/main.container", main())

    step = healthy.deploy("started")

    assert step.failed
    assert broken.name in step.msg
    assert "unsupported key 'BogusKey'" in step.msg

    host.run(f"rm {broken.quadlet_path('main.container')}")

    # Pinned: this app's files were written by the failed run, so the retry sees nothing to
    # write, skips validation and the reload, and fails starting a unit systemd never loaded
    retry = healthy.deploy("started")
    assert retry.failed
    assert "not found" in retry.msg

    assert not healthy.deploy("started", args={"force": True}).failed
    assert host.active_state(healthy.service("main")) == "active"


def test_container_that_cannot_start(make_app, host):
    app = make_app("noimage")
    app.write("quadlets/main.container",
              "[Container]\nImage=docker.io/library/qa-does-not-exist:1\nPull=never\n")

    step = app.deploy("started")

    assert step.failed
    assert step.msg.startswith(f"systemctl start {app.service('main')} failed: Job for ")
    assert host.exists(app.quadlet_path("main.container"))
    assert app.deploy("started").failed


def test_container_that_exits_at_once_does_not_fail_the_deploy(make_app, host):
    """
    Pinned: quadlet containers report ready as soon as the container starts, so a container that
    then exits with an error has already passed `systemctl start`. The unit fails afterwards.
    """
    app = make_app("exitone")
    app.write("quadlets/main.container", f"[Container]\n{IMAGE}\nExec=/bin/false\n")

    step = app.deploy("started")

    assert not step.failed
    deadline = time.monotonic() + 30
    while host.active_state(app.service("main")) != "failed" and time.monotonic() < deadline:
        time.sleep(1)
    assert host.unit(app.service("main"), "ActiveState", "Result") == {
        "ActiveState": "failed", "Result": "exit-code",
    }


def test_app_without_main_container(make_app, host):
    """Pinned: nothing checks for main.container; it only matters once main has to start."""
    app = make_app("nomain")
    app.write("quadlets/db.container", main())

    assert not app.deploy("installed").failed
    started = app.deploy("started")
    assert started.failed
    assert "not found" in started.msg


@pytest.mark.parametrize("src, message", [
    ("apps/does-not-exist", "Source directory not found on control node: "),
    ("ansible.cfg", "Source path is not a directory: "),
])
def test_bad_src(make_app, host, src, message):
    app = make_app("badsrc")
    (app.playbook_dir / "ansible.cfg").write_text("")

    step = app.deploy("started", src=src)

    assert step.failed
    assert step.msg.startswith(message)
    assert step.msg.endswith(src)


def test_missing_src(make_app, host):
    app = make_app("nosrc")
    play = {"hosts": "testhost", "gather_facts": False, "become": True,
            "tasks": [{"name": "deploy the app", "kriansa.commons.quadlet_app": {"state": "started"}}]}

    run = app.runner.run_plays([play], app.playbook_dir, name="nosrc")

    assert run.task("deploy the app").msg == "src parameter is required"
