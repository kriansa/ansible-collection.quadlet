"""
B. Which units a changed deploy restarts, and that each restarts exactly once.

"Restarted once" is counted from systemd's own journal events, so a unit that systemd restarts
behind the module's back - through a Requires= chain, say - is counted too.
"""

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def container(extra: str = "", unit: str = "") -> str:
    unit_section = f"[Unit]\n{unit}\n" if unit else ""
    return f"{unit_section}[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def env(host, app, stem) -> list:
    return host.inspect(app.container(stem))["Config"]["Env"]


def test_changed_secondary_restarts_the_app_once(make_app, host):
    """
    The shape that used to restart main two or three times per deploy: main waits to be healthy
    before it counts as started, depends on a secondary container, and mounts an owned volume.
    """
    app = make_app("healthcheck")
    app.write("quadlets/main.container", container(
        "Volume=data.volume:/data\n"
        "HealthCmd=/bin/sh -c 'sleep 1'\nHealthInterval=2s\nNotify=healthy\n",
        unit="Wants=web.service\nAfter=web.service",
    ))
    app.write("quadlets/web.container", container("Environment=VERSION=1\n"))
    app.write("quadlets/data.volume", "[Volume]\nX-Owner=1000:1000\n")
    app.deploy("started")
    init_id = host.invocation_id(app.service("data-volume-init"))

    app.write("quadlets/web.container", container("Environment=VERSION=2\n"))
    step = app.deploy("started")

    assert step.changed
    assert step.starts(app.service("web")) == 1
    assert step.starts(app.service("main")) == 1
    assert "VERSION=2" in env(host, app, "web")
    assert host.invocation_id(app.service("data-volume-init")) == init_id


def test_owner_change_runs_the_bootstrap_before_main_starts(make_app, host):
    app = make_app("ownerchange")
    app.write("quadlets/main.container", container("Volume=data.volume:/data\n"))
    app.write("quadlets/data.volume", "[Volume]\nX-Owner=1000:1000\n")
    mountpoint = host.seed_volume(f"{app.name}--data")
    app.deploy("started")

    app.write("quadlets/data.volume", "[Volume]\nX-Owner=2000:2000\n")
    step = app.deploy("started")

    init, main = app.service("data-volume-init"), app.service("main")
    assert step.starts(init) == 1
    assert step.starts(main) == 1
    events = host.output(
        f"journalctl --quiet -o cat --output-fields=UNIT --after-cursor='{step.since}' "
        f"MESSAGE_ID=39f53479d3a045ac8e11786248231fbf UNIT={init} UNIT={main}"
    ).splitlines()
    assert events == [init, main]
    assert host.listdir("/var/lib/quadlet-app") == [f"{app.name}--data.2000_2000.done"]
    assert host.stat(mountpoint).owner == "2000:2000"
    assert host.stat(f"{mountpoint}/seed").owner == "2000:2000"


def test_transitive_dependency_is_not_restarted(make_app, host):
    """
    Pinned: only main's direct dependencies are restarted. cache is two Requires= away, so its
    new definition is deployed but the running container keeps the old one.
    """
    app = make_app("transitive")
    app.write("quadlets/main.container", container(unit="Requires=db.service\nAfter=db.service"))
    app.write("quadlets/db.container", container(unit="Requires=cache.service\nAfter=cache.service"))
    app.write("quadlets/cache.container", container("Environment=VERSION=1\n"))
    app.deploy("started")

    app.write("quadlets/cache.container", container("Environment=VERSION=2\n"))
    step = app.deploy("started")

    assert step.starts(app.service("main")) == 1
    assert step.starts(app.service("db")) == 1
    assert step.starts(app.service("cache")) == 0
    assert "VERSION=1" in env(host, app, "cache")


def test_unrelated_container_is_neither_started_nor_restarted(make_app, host):
    """
    Pinned: a container main does not depend on is left to systemd. Its [Install] section only
    takes effect at boot, and a change to it does not restart it.
    """
    app = make_app("standalone")
    app.write("quadlets/main.container", container())
    worker = container("Environment=VERSION=1\n") + "\n[Install]\nWantedBy=multi-user.target\n"
    app.write("quadlets/worker.container", worker)
    app.deploy("started")
    assert host.active_state(app.service("worker")) == "inactive"

    host.run(f"systemctl start {app.service('worker')}")
    app.write("quadlets/worker.container", worker.replace("VERSION=1", "VERSION=2"))
    step = app.deploy("started")

    assert step.changed
    assert step.starts(app.service("worker")) == 0
    assert "VERSION=1" in env(host, app, "worker")


def test_main_stopped_by_hand_is_started_and_its_dependency_left_alone(make_app, host):
    """Pinned: with main not running, a changed deploy only starts main."""
    app = make_app("mainstopped")
    app.write("quadlets/main.container", container(unit="Wants=db.service"))
    app.write("quadlets/db.container", container("Environment=VERSION=1\n"))
    app.deploy("started")
    host.run(f"systemctl stop {app.service('main')}")

    app.write("quadlets/db.container", container("Environment=VERSION=2\n"))
    step = app.deploy("started")

    assert step.starts(app.service("main")) == 1
    assert step.starts(app.service("db")) == 0
    assert "VERSION=1" in env(host, app, "db")


def test_volume_of_a_secondary_container_changes_owner(make_app, host):
    """
    The bootstrap unit of a volume main does not mount is outside main's dependency tree. It is
    re-run anyway, and systemd restarts the container requiring it, once.
    """
    app = make_app("secondaryvol")
    app.write("quadlets/main.container", container(unit="Wants=worker.service"))
    app.write("quadlets/worker.container", container("Volume=cache.volume:/cache\n"))
    app.write("quadlets/cache.volume", "[Volume]\nX-Owner=10:10\n")
    mountpoint = host.seed_volume(f"{app.name}--cache")
    app.deploy("started")

    app.write("quadlets/cache.volume", "[Volume]\nX-Owner=20:20\n")
    step = app.deploy("started")

    assert step.starts(app.service("cache-volume-init")) == 1
    assert step.starts(app.service("worker")) == 1
    assert step.starts(app.service("main")) == 1
    assert host.stat(mountpoint).owner == "20:20"
