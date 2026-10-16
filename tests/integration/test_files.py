"""F. Files from init/ and config/: where they land, how they are mounted, and what a change does."""

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def test_layout_modes_and_content(make_app, host):
    app = make_app("files")
    app.write("quadlets/main.container", main())
    app.write("config/main/app.conf", "a=1\n")
    app.write("config/main/sub/dir/deep.conf", "deep\n")
    app.write("config/main/empty", "")
    app.write("config/main/.env", "HIDDEN=1")
    app.write("config/main/run.sh", "#!/bin/sh\necho hi\n\n")
    (app.directory / "config/main/run.sh").chmod(0o755)
    app.write("init/main/schema.sql", "CREATE TABLE t (id int);\n")

    app.deploy("installed")

    base = f"/srv/{app.name}"
    assert host.read(f"{base}/config/main/app.conf") == "a=1\n"
    assert host.read(f"{base}/config/main/sub/dir/deep.conf") == "deep\n"
    assert host.read(f"{base}/config/main/empty") == ""
    assert host.read(f"{base}/config/main/.env") == "HIDDEN=1"
    assert host.read(f"{base}/config/main/run.sh") == "#!/bin/sh\necho hi\n\n"
    assert host.read(f"{base}/init/main/schema.sql") == "CREATE TABLE t (id int);\n"
    for rel in ("config/main/app.conf", "config/main/sub/dir/deep.conf", "config/main/.env"):
        info = host.stat(f"{base}/{rel}")
        assert (info.owner, info.mode) == ("0:0", "644"), rel
    for rel in ("config", "config/main", "config/main/sub", "config/main/sub/dir"):
        info = host.stat(f"{base}/{rel}")
        assert (info.owner, info.mode) == ("0:0", "755"), rel
    # Pinned: the executable bit is not carried over
    assert host.stat(f"{base}/config/main/run.sh").mode == "644"


def test_config_mount_is_read_only_idmapped_and_labelled(make_app, host):
    app = make_app("cfgmount")
    app.write("quadlets/main.container", main("Volume=%config%:/etc/app\n"))
    app.write("config/main/app.conf", "a=1\n")
    since = host.now()

    app.deploy("started")
    container = app.container("main")

    listing = host.container_exec(container, "stat -c '%u:%g' /etc/app/app.conf").stdout.strip()
    assert listing == "0:0"
    write = host.container_exec(container, "touch /etc/app/new", check=False)
    assert write.returncode != 0
    assert "Read-only file system" in write.stderr

    mount = next(m for m in host.inspect(container)["Mounts"] if m["Destination"] == "/etc/app")
    assert mount["RW"] is False
    # podman inspect does not report idmap; the kernel's mount table does
    mountinfo = host.container_exec(container, "grep ' /etc/app ' /proc/self/mountinfo").stdout
    assert "idmapped" in mountinfo

    config_dir = host.stat(f"/srv/{app.name}/config/main")
    assert config_dir.owner == "0:0"
    assert config_dir.selinux_type == "container_file_t"
    # :Z gives the directory a category private to this container
    assert ":c" in config_dir.context
    assert host.denials_since(since) == ""


def test_single_file_and_subdirectory_mounts(make_app, host):
    app = make_app("cfgfile")
    app.write("quadlets/main.container", main(
        "Volume=%config%/app.conf:/etc/app.conf\nVolume=%init%/sql:/docker-entrypoint-initdb.d\n"
    ))
    app.write("config/main/app.conf", "a=1\n")
    app.write("init/main/sql/01.sql", "SELECT 1;\n")

    app.deploy("started")

    container = app.container("main")
    assert host.container_exec(container, "cat /etc/app.conf").stdout == "a=1\n"
    assert host.container_exec(container, "cat /docker-entrypoint-initdb.d/01.sql").stdout == "SELECT 1;\n"


def test_config_change_restarts_the_app_once(make_app, host):
    app = make_app("cfgchange")
    app.write("quadlets/main.container", main("Volume=%config%:/etc/app\n"))
    app.write("config/main/app.conf", "a=1\n")
    app.deploy("started")

    app.write("config/main/app.conf", "a=2\n")
    step = app.deploy("started")

    assert step.changed
    assert step.starts(app.service("main")) == 1
    assert host.container_exec(app.container("main"), "cat /etc/app/app.conf").stdout == "a=2\n"


def test_removed_config_file_is_left_behind(make_app, host):
    """Pinned: nothing is ever removed from the host, and a removal alone is not a change."""
    app = make_app("cfgremove")
    app.write("quadlets/main.container", main())
    app.write("config/main/keep.conf", "k\n")
    app.write("config/main/gone.conf", "g\n")
    app.deploy("started")

    app.delete("config/main/gone.conf")
    step = app.deploy("started")

    assert not step.changed
    assert host.read(f"/srv/{app.name}/config/main/gone.conf") == "g\n"


def test_directory_matching_no_container_is_deployed(make_app, host):
    """Pinned: the action plugin path does not check config/ against the quadlets."""
    app = make_app("cfgorphan")
    app.write("quadlets/main.container", main())
    app.write("config/nomatch/f", "x")

    step = app.deploy("installed")

    assert not step.failed
    assert host.read(f"/srv/{app.name}/config/nomatch/f") == "x"


def test_suffixed_directory_loses_its_suffix(make_app, host):
    """Pinned: config/main.container/ is deployed as config/main/."""
    app = make_app("cfgsuffix")
    app.write("quadlets/main.container", main())
    app.write("config/main.container/f", "x")

    app.deploy("installed")

    assert host.read(f"/srv/{app.name}/config/main/f") == "x"
