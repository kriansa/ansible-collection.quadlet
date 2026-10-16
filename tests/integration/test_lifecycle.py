"""A. What each `state` does on a real host, and that repeating a deploy changes nothing."""

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

MAIN = f"[Unit]\nDescription=qa main\n\n[Container]\n{IMAGE}\n{IDLE}\n"
DB = f"[Container]\n{IMAGE}\n{IDLE}\n"
NETWORK = "[Network]\n"
DATA = "[Volume]\n"


def full_app(make_app, tag):
    """main + db on a shared network, main mounting a volume and requiring db."""
    app = make_app(tag)
    app.write("quadlets/main.container",
              f"[Unit]\nRequires=db.service\nAfter=db.service\n\n[Container]\n{IMAGE}\n{IDLE}\n"
              "Network=main.network\nVolume=data.volume:/data\n")
    app.write("quadlets/db.container", DB + "Network=main.network\n")
    app.write("quadlets/main.network", NETWORK)
    app.write("quadlets/data.volume", DATA)
    return app


def test_installed_on_a_fresh_host(make_app, host):
    app = make_app("installed")
    app.write("quadlets/main.container", MAIN)

    step = app.deploy("installed")

    assert step.changed
    assert step.msg == "quadlet files deployed"
    deployed = host.stat(app.quadlet_path("main.container"))
    assert (deployed.owner, deployed.mode) == ("0:0", "644")
    content = host.read(app.quadlet_path("main.container"))
    assert f"ContainerName={app.name}--main" in content
    assert "UserNS=auto:size=65536" in content
    assert "RequiresMountsFor=/var/lib/containers/storage" in content
    assert host.unit(app.service("main"), "LoadState", "ActiveState") == {
        "LoadState": "loaded", "ActiveState": "inactive",
    }
    assert step.starts(app.service("main")) == 0


def test_installed_twice_changes_nothing(make_app, host):
    app = make_app("installed2")
    app.write("quadlets/main.container", MAIN)
    app.deploy("installed")
    mtime = host.mtime(app.quadlet_path("main.container"))

    again = app.deploy("installed")

    assert not again.changed
    assert again.msg == "application already up to date"
    assert host.mtime(app.quadlet_path("main.container")) == mtime


def test_started_on_a_fresh_host(make_app, host):
    app = full_app(make_app, "started")

    step = app.deploy("started")

    assert step.changed
    assert step.msg == "quadlet files deployed and service started"
    for stem in ("main", "db", "main-network", "data-volume"):
        assert host.active_state(app.service(stem)) == "active", stem
    assert step.starts(app.service("main")) == 1
    assert step.starts(app.service("db")) == 1
    # The ownership bootstrap ran and left its marker
    assert host.listdir("/var/lib/quadlet-app") .count(f"{app.name}--data.0_0.done") == 1

    again = app.deploy("started")
    assert not again.changed
    assert again.starts(app.service("main")) == 0


def test_installed_then_started_only_starts(make_app, host):
    app = make_app("thenstart")
    app.write("quadlets/main.container", MAIN)
    app.deploy("installed")

    step = app.deploy("started")

    assert step.changed
    assert step.starts(app.service("main")) == 1
    assert host.active_state(app.service("main")) == "active"


def test_stopped_by_hand_is_started_again(make_app, host):
    app = make_app("stopped")
    app.write("quadlets/main.container", MAIN)
    app.deploy("started")
    host.run(f"systemctl stop {app.service('main')}")

    step = app.deploy("started")

    assert step.changed
    assert step.starts(app.service("main")) == 1
    assert host.active_state(app.service("main")) == "active"


def test_restarted_restarts_every_time(make_app, host):
    app = full_app(make_app, "restarted")
    app.deploy("started")
    init = app.service("data-volume-init")
    init_id = host.invocation_id(init)

    for _attempt in range(2):
        step = app.deploy("restarted")
        assert step.changed
        assert step.msg == "quadlet files deployed and service restarted"
        assert step.starts(app.service("main")) == 1
        assert step.starts(app.service("db")) == 1
    # The ownership bootstrap did not change, so it is not re-run
    assert host.invocation_id(init) == init_id


def test_restarted_on_a_fresh_host(make_app, host):
    app = make_app("restartfresh")
    app.write("quadlets/main.container", MAIN)
    step = app.deploy("restarted")
    assert step.changed
    assert host.active_state(app.service("main")) == "active"


def test_installed_while_running_does_not_restart(make_app, host):
    """Pinned: the new file is in place, but the running container keeps the old definition."""
    app = make_app("instrunning")
    app.write("quadlets/main.container", MAIN + "Environment=VERSION=1\n")
    app.deploy("started")

    app.write("quadlets/main.container", MAIN + "Environment=VERSION=2\n")
    step = app.deploy("installed")

    assert step.changed
    assert step.starts(app.service("main")) == 0
    env = host.inspect(app.container("main"))["Config"]["Env"]
    assert "VERSION=1" in env


def test_check_mode_is_skipped(make_app, host):
    app = make_app("check")
    app.write("quadlets/main.container", MAIN)

    step = app.deploy("started", extra_args=["--check"])

    assert step.result.status == "skipped"
    assert "does not support check mode" in step.msg
    assert not host.exists(app.quadlet_path("main.container"))
