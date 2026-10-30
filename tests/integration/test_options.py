"""I. Module options and task keywords: force, systemctl_timeout, become, async, loops."""

import time

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import (
    DEPLOY_TASK,
    IDLE,
    IMAGE,
)

pytestmark = pytest.mark.integration


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def test_force_rewrites_and_restarts_without_changes(make_app, host):
    app = make_app("force")
    app.write("quadlets/main.container", main("Volume=data.volume:/data\n"))
    app.write("quadlets/data.volume", "[Volume]\nX-Owner=3:3\n")
    app.deploy("started")
    path = app.quadlet_path("main.container")
    content, mtime = host.read(path), host.mtime(path)
    time.sleep(1.1)

    step = app.deploy("started", args={"force": True})

    assert step.changed
    assert host.read(path) == content
    assert host.mtime(path) != mtime
    assert step.starts(app.service("main")) == 1
    # Restarted, but skipped by its condition: its marker is still there
    init = app.service("data-volume-init")
    assert host.unit(init, "ConditionResult")["ConditionResult"] == "no"
    assert f"{app.name}--data.3_3.done" in host.listdir("/var/lib/quadlet-app")


@pytest.mark.slow
def test_systemctl_timeout(make_app, host):
    app = make_app("timeout")
    app.write("quadlets/main.container", main() + "\n[Service]\nExecStartPre=/usr/bin/sleep 15\n")

    step = app.deploy("started", args={"systemctl_timeout": 3})

    assert step.failed
    assert step.msg == f"systemctl restart {app.service('main')} timed out after 3s"
    # Only the client gave up; systemd carries on with the start job
    deadline = time.monotonic() + 60
    while host.active_state(app.service("main")) != "active" and time.monotonic() < deadline:
        time.sleep(1)
    assert host.active_state(app.service("main")) == "active"

    # Nothing changed and main is running now, so the next run has nothing to do
    assert not app.deploy("started", args={"systemctl_timeout": 3}).changed


def test_without_become(make_app, host):
    app = make_app("nobecome")
    app.write("quadlets/main.container", main())

    step = app.deploy("installed", become=False)

    assert step.failed
    assert "Permission denied" in step.msg
    assert step.msg.startswith(("Failed to prepare /var/lib/quadlet-app", "Failed to write file"))


def test_async_is_rejected(make_app, host):
    app = make_app("async")
    app.write("quadlets/main.container", main())
    play = {
        "hosts": "testhost",
        "gather_facts": False,
        "become": True,
        "tasks": [
            {
                "name": DEPLOY_TASK,
                "async": 60,
                "poll": 5,
                "kriansa.quadlet.quadlet_app": {"src": f"apps/{app.name}"},
            }
        ],
    }

    run = app.runner.run_plays([play], app.playbook_dir, name="async")

    assert run.task(DEPLOY_TASK).failed
    assert "does not support async" in run.task(DEPLOY_TASK).msg


def test_loop_over_several_apps(make_app, host):
    first, second = make_app("loopa"), make_app("loopb")
    for app in (first, second):
        app.write("quadlets/main.container", main())
    play = {
        "hosts": "testhost",
        "gather_facts": False,
        "become": True,
        "tasks": [
            {
                "name": DEPLOY_TASK,
                "loop": [first.name, second.name],
                "kriansa.quadlet.quadlet_app": {"src": "apps/{{ item }}", "state": "started"},
            }
        ],
    }

    run = first.runner.run_plays([play], first.playbook_dir, name="loop")

    assert run.returncode == 0, run.output
    for app in (first, second):
        assert host.active_state(app.service("main")) == "active"
