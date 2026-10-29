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


def test_directives_written_with_spaces_around_equals(make_app, host):
    """systemd reads `Key = value` like `Key=value`, and so must every rule of the module."""
    app = make_app("spaced")
    app.write("quadlets/main.container",
              f"[Container]\n{IMAGE}\n{IDLE}\nNetwork = app.network\nVolume = data.volume:/data\n"
              "UserNS = host\n")
    app.write("quadlets/app.network", "[Network]\n")
    app.write("quadlets/data.volume", "[Volume]\nX-Owner = 10:10\n")
    mountpoint = host.seed_volume(f"{app.name}--data")

    step = app.deploy("started")

    assert not step.failed, step.msg
    assert f"{app.name}--app" in host.inspect(app.container("main"))["NetworkSettings"]["Networks"]
    assert host.uid_map(app.container("main")) == [[0, 0, 4294967295]]
    assert host.stat(f"{mountpoint}/seed").owner == "10:10"


def test_only_names_the_app_defines_are_prefixed(make_app, host):
    """sshd.service is the host's, and stays as written; db.service is the app's."""
    app = make_app("hostunit")
    app.write("quadlets/main.container",
              "[Unit]\nWants=sshd.service db.service\nAfter=sshd.service db.service\n\n"
              f"[Container]\n{IMAGE}\n{IDLE}\nNetwork=app.network:ip=198.51.100.10\n")
    app.write("quadlets/db.container", f"[Container]\n{IMAGE}\n{IDLE}\n")
    app.write("quadlets/app.network", "[Network]\nSubnet=198.51.100.0/24\n")

    step = app.deploy("started")

    assert not step.failed, step.msg
    after = host.unit(app.service("main"), "After")["After"].split()
    assert app.service("db") in after
    assert "sshd.service" in after
    assert host.active_state(app.service("db")) == "active"
    networks = host.inspect(app.container("main"))["NetworkSettings"]["Networks"]
    assert networks[f"{app.name}--app"]["IPAddress"] == "198.51.100.10"


def test_file_with_windows_line_endings(make_app, host):
    app = make_app("crlf")
    content = (f"[Container]\n{IMAGE}\n{IDLE}\nNetwork=app.network\nVolume=data.volume:/data\n"
               "Volume=%config%:/etc/app\n")
    app.write_bytes("quadlets/main.container", content.replace("\n", "\r\n").encode())
    app.write("quadlets/app.network", "[Network]\n")
    app.write("quadlets/data.volume", "[Volume]\n")
    app.write("config/main/app.conf", "a=1\n")

    step = app.deploy("started")

    assert not step.failed, step.msg
    assert host.container_exec(app.container("main"), "cat /etc/app/app.conf").stdout == "a=1\n"
    container = host.inspect(app.container("main"))
    assert f"{app.name}--app" in container["NetworkSettings"]["Networks"]
    mount = next(m for m in container["Mounts"] if m["Destination"] == "/data")
    assert mount["Name"] == f"{app.name}--data"
    assert "idmap" in host.container_exec(app.container("main"),
                                          "grep ' /data ' /proc/self/mountinfo").stdout


def test_comments_above_the_first_header_are_kept(make_app, host):
    app = make_app("comments")
    app.write("quadlets/main.container", f"# What the app is for\n\n# The main container\n"
                                         f"[Container]\n{IMAGE}\n{IDLE}\nVolume=data.volume:/data\n")
    app.write("quadlets/data.volume", "# Owned by root\n[Volume]\n")

    step = app.deploy("started")

    assert not step.failed, step.msg
    main = host.read(app.quadlet_path("main.container"))
    # The [Unit] section the module adds opens below the file's own leading comments
    assert main.startswith("# What the app is for\n\n[Unit]\n")
    assert "\n# The main container\n[Container]\n" in main
    assert host.read(app.quadlet_path("data.volume")).startswith("# Owned by root\n[Volume]\n")
    assert host.active_state(app.service("main")) == "active"


def test_check_mode_is_skipped(make_app, host):
    app = make_app("check")
    app.write("quadlets/main.container", MAIN)

    step = app.deploy("started", extra_args=["--check"])

    assert step.result.status == "skipped"
    assert "does not support check mode" in step.msg
    assert not host.exists(app.quadlet_path("main.container"))
