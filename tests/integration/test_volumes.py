"""
C. Volume ownership, idmapped mounts and user namespaces, with SELinux enforcing.
D. The ownership markers under /var/lib/quadlet-app.
"""

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

STATE_DIR = "/var/lib/quadlet-app"


def container(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def volume_app(make_app, host, tag, *, owner=None, user=None, seed=True):
    """main mounting data.volume at /data, optionally running as `user`."""
    app = make_app(tag)
    extra = "Volume=data.volume:/data\n" + (f"User={user}\n" if user else "")
    app.write("quadlets/main.container", container(extra))
    app.write("quadlets/data.volume", "[Volume]\n" + (f"X-Owner={owner}\n" if owner else ""))
    mountpoint = host.seed_volume(f"{app.name}--data") if seed else None
    return app, mountpoint


class TestOwnership:
    def test_default_owner_is_container_root(self, make_app, host):
        app, mountpoint = volume_app(make_app, host, "defowner")
        app.deploy("started")

        host.container_exec(app.container("main"), "touch /data/written")

        assert host.stat(f"{mountpoint}/written").owner == "0:0"
        assert f"{app.name}--data.0_0.done" in host.listdir(STATE_DIR)

    def test_declared_owner_lets_a_non_root_image_write(self, make_app, host):
        app, mountpoint = volume_app(make_app, host, "nonroot", owner="10000:10000", user="10000:10000")
        app.deploy("started")

        written = host.container_exec(app.container("main"), "touch /data/written && id -u")

        assert written.stdout.strip() == "10000"
        # The id inside the container is the id on disk: the mount is idmapped
        assert host.stat(f"{mountpoint}/written").owner == "10000:10000"
        assert host.stat(mountpoint).owner == "10000:10000"

    def test_existing_foreign_ownership_is_migrated(self, make_app, host):
        """A volume left owned by some old host range is brought back to container-relative ids."""
        app, mountpoint = volume_app(make_app, host, "migrate", owner="1000:1000")
        host.run(
            f"mkdir -p {mountpoint}/sub && touch {mountpoint}/sub/f && "
            f"ln -s /etc/hostname {mountpoint}/link && chown -hR 1234567:1234567 {mountpoint}"
        )

        app.deploy("started")

        for rel in ("", "/seed", "/sub", "/sub/f", "/link"):
            assert host.stat(f"{mountpoint}{rel}").owner == "1000:1000", rel
        # The symlink itself was chowned, not what it points at
        assert host.stat("/etc/hostname").owner == "0:0"

    def test_bootstrap_is_skipped_once_its_marker_exists(self, make_app, host):
        app = volume_app(make_app, host, "skipped", owner="5:5")[0]
        app.deploy("started")
        init = app.service("data-volume-init")

        host.run(f"systemctl restart {init}")

        assert host.unit(init, "ConditionResult")["ConditionResult"] == "no"

    def test_rerun_after_deleting_the_marker(self, make_app, host):
        """The troubleshooting procedure from the docs."""
        app, mountpoint = volume_app(make_app, host, "rerun", owner="5:5")
        app.deploy("started")
        host.run(f"chown -R 0:0 {mountpoint}")

        host.run(f"rm {STATE_DIR}/{app.name}--data.5_5.done && "
                 f"systemctl restart {app.service('data-volume-init')}")

        assert host.stat(f"{mountpoint}/seed").owner == "5:5"
        assert f"{app.name}--data.5_5.done" in host.listdir(STATE_DIR)

    @pytest.mark.parametrize("value, message", [
        # Pinned: the file name is hidden by no_log, because it is one of the strings of the
        # _files_data parameter - so the message cannot say which file is wrong
        ("abc", "Invalid X-Owner in ********: 'abc'."),
        ("70000:0", "X-Owner in ******** is out of range: '70000:0'."),
    ])
    def test_invalid_owner_deploys_nothing(self, make_app, host, value, message):
        app = volume_app(make_app, host, "badowner", owner=value, seed=False)[0]

        step = app.deploy("started")

        assert step.failed
        assert step.msg.startswith(message)
        assert not host.exists(app.quadlet_path("main.container"))

    def test_empty_volume_is_reset_by_podman(self, make_app, host):
        """
        Pinned: with podman's local driver, an *empty* volume's root is chowned to the container's
        user every time a container starts - undoing X-Owner, while the marker keeps the bootstrap
        from ever running again. Volumes managed by a volume driver (stratis, for instance) are
        never chowned by podman, so they are not affected.
        """
        app = volume_app(make_app, host, "emptyvol", owner="1000:1000", seed=False)[0]
        app.deploy("started")

        mountpoint = host.volume_mountpoint(f"{app.name}--data")
        assert host.stat(mountpoint).owner == "0:0"
        assert f"{app.name}--data.1000_1000.done" in host.listdir(STATE_DIR)

    def test_explicit_mount_options_bypass_idmap(self, make_app, host):
        """
        Pinned: options written by hand replace the idmapped default entirely. Without idmap,
        container root is an unprivileged host id under UserNS=auto, and the volume - owned by
        the container-relative 0:0 the bootstrap wrote - is not writable to it.
        """
        app = make_app("noidmap")
        app.write("quadlets/main.container", container("Volume=data.volume:/data:rw,z\n"))
        app.write("quadlets/data.volume", "[Volume]\n")
        host.seed_volume(f"{app.name}--data")
        app.deploy("started")

        written = host.container_exec(app.container("main"), "touch /data/written", check=False)

        assert written.returncode != 0
        assert "Permission denied" in written.stderr

    def test_removed_volume_is_left_behind(self, make_app, host):
        """Pinned: nothing is ever removed from the host."""
        app = volume_app(make_app, host, "removedvol", owner="7:7")[0]
        app.write("quadlets/cache.volume", "[Volume]\n")
        app.write("quadlets/main.container", container("Volume=data.volume:/data\nVolume=cache.volume:/cache\n"))
        app.deploy("started")

        app.delete("quadlets/cache.volume")
        app.write("quadlets/main.container", container("Volume=data.volume:/data\n"))
        app.deploy("started")

        assert host.exists(app.quadlet_path("cache.volume"))
        assert host.exists(app.quadlet_path("cache-volume-init.container"))
        assert f"{app.name}--cache.0_0.done" in host.listdir(STATE_DIR)
        assert host.run(f"podman volume exists {app.name}--cache", check=False).returncode == 0


class TestUserNamespaces:
    @staticmethod
    def mapped_ids(uid_map):
        return sum(count for _inside, _outside, count in uid_map)

    def test_default_is_an_auto_range_of_65536(self, make_app, host):
        app = make_app("userns")
        app.write("quadlets/main.container", container())
        app.deploy("started")

        uid_map = host.uid_map(app.container("main"))

        assert self.mapped_ids(uid_map) == 65536
        assert min(outside for _inside, outside, _count in uid_map) >= 1000000
        pid = host.inspect(app.container("main"))["State"]["Pid"]
        gid_map = [[int(n) for n in line.split()]
                   for line in host.output(f"cat /proc/{pid}/gid_map").splitlines()]
        assert self.mapped_ids(gid_map) == 65536

    def test_host(self, make_app, host):
        app = make_app("usernshost")
        app.write("quadlets/main.container", container("UserNS=host\n"))
        app.deploy("started")
        assert host.uid_map(app.container("main")) == [[0, 0, 4294967295]]

    def test_bare_auto_gets_the_full_range(self, make_app, host):
        app = make_app("usernsauto")
        app.write("quadlets/main.container", container("UserNS=auto\n"))
        app.deploy("started")
        assert "UserNS=auto:size=65536" in host.read(app.quadlet_path("main.container"))
        assert self.mapped_ids(host.uid_map(app.container("main"))) == 65536

    def test_explicit_size_is_respected(self, make_app, host):
        app = make_app("usernssize")
        app.write("quadlets/main.container", container("UserNS=auto:size=2048\n"))
        app.deploy("started")
        assert self.mapped_ids(host.uid_map(app.container("main"))) == 2048


class TestMarkers:
    def test_owner_change_replaces_only_that_volumes_marker(self, make_app, host):
        app = volume_app(make_app, host, "markers", owner="1:1")[0]
        app.deploy("started")
        others = [f"{app.name}--data2.9_9.done", "qaother--data.1_1.done"]
        host.run(" && ".join(f"touch {STATE_DIR}/{name}" for name in others))

        app.write("quadlets/data.volume", "[Volume]\nX-Owner=2:2\n")
        app.deploy("started")

        markers = host.listdir(STATE_DIR)
        assert f"{app.name}--data.2_2.done" in markers
        assert f"{app.name}--data.1_1.done" not in markers
        assert all(name in markers for name in others)
        host.run(" && ".join(f"rm {STATE_DIR}/{name}" for name in others))

    def test_stale_marker_survives_until_files_change(self, make_app, host):
        """Pinned: markers are only cleaned up as part of writing files."""
        app = volume_app(make_app, host, "stale", owner="1:1")[0]
        app.deploy("started")
        stale = f"{STATE_DIR}/{app.name}--data.5_5.done"
        host.run(f"touch {stale}")

        app.deploy("started")
        assert host.exists(stale)

        app.write("quadlets/main.container", container("Volume=data.volume:/data\nEnvironment=X=1\n"))
        app.deploy("started")
        assert not host.exists(stale)

    def test_force_cleans_stale_markers(self, make_app, host):
        app = volume_app(make_app, host, "forcestale", owner="1:1")[0]
        app.deploy("started")
        stale = f"{STATE_DIR}/{app.name}--data.5_5.done"
        host.run(f"touch {stale}")

        app.deploy("started", args={"force": True})

        assert not host.exists(stale)
