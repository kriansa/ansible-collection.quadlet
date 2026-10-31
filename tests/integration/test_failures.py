"""J. How deploys fail, and what each failure leaves on the host."""

import time

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

GENERATOR = "/usr/lib/systemd/system-generators/podman-system-generator"


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def test_invalid_quadlet_fails_before_anything_is_written(make_app, host):
    app = make_app("badkey")
    app.write("quadlets/main.container", main("BogusKey=1\n"))

    step = app.deploy("started")

    assert step.failed
    assert "unsupported key 'BogusKey'" in step.msg
    assert step.result.result["rc"] == 1
    assert step.result.result["cmd"] == f"{GENERATOR} -dryrun -v"
    assert not host.exists(app.quadlet_path("main.container"))

    # Nothing was written, so a retry validates again and fails the same way
    again = app.deploy("started")
    assert again.failed
    assert "unsupported key 'BogusKey'" in again.msg

    app.write("quadlets/main.container", main())
    fixed = app.deploy("started")
    assert not fixed.failed
    assert host.active_state(app.service("main")) == "active"


def test_another_apps_broken_file_does_not_affect_this_deploy(make_app, host):
    """Validation covers this app's files only; a broken file elsewhere on the host is not ours."""
    broken = make_app("brokenother")
    host.run(f"printf '[Container]\\nBogusKey=1\\n' > {broken.quadlet_path('main.container')}")
    healthy = make_app("healthy")
    healthy.write("quadlets/main.container", main())

    step = healthy.deploy("started")

    assert not step.failed, step.msg
    assert host.active_state(healthy.service("main")) == "active"


def test_container_that_cannot_start(make_app, host):
    app = make_app("noimage")
    app.write(
        "quadlets/main.container",
        "[Container]\nImage=docker.io/library/qa-does-not-exist:1\nPull=never\n",
    )

    step = app.deploy("started")

    assert step.failed
    assert step.msg.startswith(f"systemctl restart {app.service('main')} failed: Job for ")
    assert host.exists(app.quadlet_path("main.container"))
    assert app.deploy("started").failed


def test_container_that_exits_at_once_fails_the_deploy(make_app, host):
    """
    Quadlet containers report ready as soon as the container starts, so a container that then
    exits has already passed `systemctl start`. The settle check catches it, and shows its output.
    """
    app = make_app("exitone")
    app.write(
        "quadlets/main.container",
        f"[Container]\n{IMAGE}\nExec=/bin/sh -c 'echo cannot open the database; exit 3'\n",
    )

    step = app.deploy("started")

    assert step.failed
    assert step.msg.startswith(
        f"{app.service('main')} did not stay running after it was started: "
        "it is failed. Its last log lines:\n"
    ), step.msg
    assert "cannot open the database" in step.msg


def test_dependency_in_a_crash_loop_fails_the_deploy(make_app, host):
    app = make_app("crashloop")
    app.write("quadlets/main.container", main() + "\n[Unit]\nWants=db.service\n")
    app.write(
        "quadlets/db.container",
        f"[Container]\n{IMAGE}\nExec=/bin/sh -c 'sleep 1; exit 1'\n"
        "\n[Service]\nRestart=always\nRestartSec=1\n",
    )

    step = app.deploy("started")

    assert step.failed
    assert step.msg.startswith(f"{app.service('db')} did not stay running after it was started:")


def test_oneshot_dependency_that_finished_does_not_fail_the_deploy(make_app, host):
    app = make_app("oneshotdep")
    app.write(
        "quadlets/main.container",
        main() + "\n[Unit]\nRequires=migrate.service\nAfter=migrate.service\n",
    )
    app.write(
        "quadlets/migrate.container",
        f"[Container]\n{IMAGE}\nExec=/bin/true\n\n[Service]\nType=oneshot\n",
    )

    step = app.deploy("started")

    assert not step.failed, step.msg
    assert host.active_state(app.service("main")) == "active"


def test_settle_time_zero_turns_the_check_off(make_app, host):
    app = make_app("nosettle")
    app.write("quadlets/main.container", f"[Container]\n{IMAGE}\nExec=/bin/false\n")

    step = app.deploy("started", args={"settle_time": 0})

    assert not step.failed, step.msg
    deadline = time.monotonic() + 30
    while host.active_state(app.service("main")) != "failed" and time.monotonic() < deadline:
        time.sleep(1)
    assert host.unit(app.service("main"), "ActiveState", "Result") == {
        "ActiveState": "failed",
        "Result": "exit-code",
    }


def test_app_without_main_container(make_app, host):
    """Checked under every state, before anything is written."""
    app = make_app("nomain")
    app.write("quadlets/db.container", main())

    step = app.deploy("installed")

    assert step.failed
    assert step.msg.startswith(f"The app {app.name} has no main quadlet: expected one of ")
    assert not host.exists(app.quadlet_path("db.container"))


@pytest.mark.parametrize(
    "src, message",
    [
        ("apps/does-not-exist", "Source directory not found on control node: "),
        ("ansible.cfg", "Source path is not a directory: "),
    ],
)
def test_bad_src(make_app, host, src, message):
    app = make_app("badsrc")
    (app.playbook_dir / "ansible.cfg").write_text("")

    step = app.deploy("started", src=src)

    assert step.failed
    assert step.msg.startswith(message)
    assert step.msg.endswith(src)


def test_missing_src(make_app, host):
    app = make_app("nosrc")
    play = {
        "hosts": "testhost",
        "gather_facts": False,
        "become": True,
        "tasks": [{"name": "deploy the app", "kriansa.quadlet.quadlet_app": {"state": "started"}}],
    }

    run = app.runner.run_plays([play], app.playbook_dir, name="nosrc")

    assert run.task("deploy the app").msg == "src parameter is required"
