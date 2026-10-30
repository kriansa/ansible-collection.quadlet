"""
The quadlet types: the text each one is deployed with, and the ownership of volumes.

Cases marked "pinned" record what the code does today even where that is arguably wrong. They
exist so a refactor cannot change the behaviour without a test saying so; fixing one is a
deliberate change to its expectation.
"""

import os

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import run_bootstrap_unit

APP = "app"
USERNS_BLOCK = (
    "# Default from quadlet_app. Set UserNS= explicitly to override; UserNS=host is what a\n"
    "# container needs if it reads the host journal or other processes' /proc entries.\n"
    "UserNS=auto:size=65536"
)
STORAGE = "RequiresMountsFor=/var/lib/containers/storage"
# The other files of the app the tests render a quadlet in, by name, with their content
APP_FILES = {
    name: ""
    for name in [
        "main.container", "db.container", "backup.container", "job.container", "x.kube",
        "main.network", "backend.pod", "data.volume", "x.timer",
    ]
}


def render(filename, content, files=None):
    """Render one quadlet of an app that also has these other files (APP_FILES by default)."""
    app = QuadletApp(APP)
    for name, source in (APP_FILES if files is None else files).items():
        if name == filename:
            continue
        if name.endswith(".timer"):
            app.add_unit(name, source)
        else:
            app.add_quadlet(name, source)
    return app.add_quadlet(filename, content).render()


class TestPerType:
    """Which rules apply to which quadlet type."""

    SOURCE = (
        "[Unit]\n"
        "After=db.service\n"
        "\n"
        "[{section}]\n"
        "Network=main.network\n"
        "Volume=%config%:/etc/x\n"
        "Volume=data.volume:/data\n"
        "X-Owner=1000:1000\n"
    )

    def run(self, ext, section):
        return render(f"main{ext}", self.SOURCE.format(section=section))

    def test_container_gets_every_rule(self):
        assert self.run(".container", "Container") == (
            "[Unit]\n"
            "After=app--db.service\n"
            f"{STORAGE}\n"
            # data.volume is the app's, so its ownership bootstrap runs first
            "Requires=app--data-volume-init.service\n"
            "After=app--data-volume-init.service\n"
            "\n"
            "[Container]\n"
            "ContainerName=app--main\n"
            "Network=app--main.network\n"
            "Volume=/srv/app/config/main:/etc/x:ro,Z,idmap\n"
            "Volume=app--data.volume:/data:rw,Z,idmap\n"
            # X-Owner is only stripped from .volume files; quadlet would reject it here
            "X-Owner=1000:1000\n"
            f"{USERNS_BLOCK}\n"
        )

    def test_pod_gets_rules_1_2_3_only(self):
        assert self.run(".pod", "Pod") == (
            "[Unit]\n"
            "After=app--db.service\n"
            "\n"
            "[Pod]\n"
            "PodName=app--main\n"
            "Network=app--main.network\n"
            "Volume=/srv/app/config/main:/etc/x:ro,Z,idmap\n"
            "Volume=app--data.volume:/data:rw,Z,idmap\n"
            "X-Owner=1000:1000\n"
        )

    def test_kube_gets_rule_1_only(self):
        assert self.run(".kube", "Kube") == (
            "[Unit]\n"
            "After=app--db.service\n"
            "\n"
            "[Kube]\n"
            "Network=app--main.network\n"
            "Volume=%config%:/etc/x\n"
            "Volume=app--data.volume:/data:rw,Z,idmap\n"
            "X-Owner=1000:1000\n"
        )

    def test_volume_gets_rule_3_and_its_owner(self):
        # Pinned: Rule 1 does not apply to .volume, so its [Unit] references stay unprefixed
        assert self.run(".volume", "Volume") == (
            "[Unit]\n"
            "After=db.service\n"
            "\n"
            "[Volume]\n"
            "VolumeName=app--main\n"
            "Network=main.network\n"
            "Volume=%config%:/etc/x\n"
            "Volume=data.volume:/data\n"
            "User=1000\n"
            "Group=1000\n"
        )

    def test_network_gets_rule_3_only(self):
        assert self.run(".network", "Network") == (
            "[Unit]\n"
            "After=db.service\n"
            "\n"
            "[Network]\n"
            "NetworkName=app--main\n"
            "Network=main.network\n"
            "Volume=%config%:/etc/x\n"
            "Volume=data.volume:/data\n"
            "X-Owner=1000:1000\n"
        )

    def test_unknown_type_is_deployed_as_written(self):
        content = "[Build]\nNetwork=main.network\n"
        assert render("main.build", content) == content


class TestNames:
    """Rule 3: what podman creates is named after the app, unless the file names it."""

    @pytest.mark.parametrize(
        "filename, section, directive",
        [
            ("main.container", "Container", "ContainerName=app--main"),
            ("backend.pod", "Pod", "PodName=app--backend"),
            ("data.volume", "Volume", "VolumeName=app--data"),
            ("main.network", "Network", "NetworkName=app--main"),
        ],
    )
    def test_injected_after_the_header(self, filename, section, directive):
        out = render(filename, f"[Unit]\nDescription=x\n\n[{section}]\nFoo=1\n")
        assert f"\n[{section}]\n{directive}\nFoo=1\n" in out

    @pytest.mark.parametrize("name", ["ContainerName=custom", "ContainerName = custom"])
    def test_explicit_name_is_kept(self, name):
        out = render("main.container", f"[Container]\n{name}\nImage=x\n")
        assert f"[Container]\n{name}\nImage=x\n" in out
        assert "app--main" not in out

    def test_kube_is_untouched(self):
        content = "# header\n[Kube]\nYaml=x.yaml\n"
        assert render("main.kube", content) == content


class TestRendering:
    def test_comments_and_blank_lines_are_untouched(self):
        content = "[Container]\n# Network=main.network\n\nImage=x\n"
        assert render("main.container", content) == (
            f"[Unit]\n{STORAGE}\n\n"
            "[Container]\nContainerName=app--main\n# Network=main.network\n\nImage=x\n"
            f"{USERNS_BLOCK}\n"
        )

    def test_leading_comments_stay_first(self):
        content = "# What the app is for\n\n# The main container\n[Container]\nImage=x\n"
        assert render("main.container", content) == (
            f"# What the app is for\n\n[Unit]\n{STORAGE}\n\n"
            "# The main container\n[Container]\nContainerName=app--main\nImage=x\n"
            f"{USERNS_BLOCK}\n"
        )

    def test_without_trailing_newline(self):
        out = render("main.network", "[Network]\nDriver=bridge")
        assert out == "[Network]\nNetworkName=app--main\nDriver=bridge"

    def test_a_prefixed_name_is_not_prefixed_again(self):
        # "app--main.network" is not a file of the app; the app's file is "main.network"
        once = render("main.container", "[Container]\nNetwork=main.network\n")
        twice = render("main.container", once)
        assert "Network=app--main.network\n" in twice

    def test_rendered_once(self):
        quadlet = QuadletApp(APP).add_quadlet("main.network", "[Network]\n")
        assert quadlet.content == "[Network]\nNetworkName=app--main\n"
        assert quadlet.content is quadlet.content

    @pytest.mark.parametrize(
        "filename, deployed, service",
        [
            ("main.container", "app--main.container", "app--main.service"),
            ("web.kube", "app--web.kube", "app--web.service"),
            ("backend.pod", "app--backend.pod", "app--backend-pod.service"),
            ("data.volume", "app--data.volume", "app--data-volume.service"),
            ("main.network", "app--main.network", "app--main-network.service"),
        ],
    )
    def test_names_on_the_host(self, fake_host, filename, deployed, service):
        quadlet = QuadletApp(APP).add_quadlet(filename, "")
        assert quadlet.deployed_name == deployed
        assert quadlet.dest_path == f"{fake_host.quadlet_dir}/{deployed}"
        assert quadlet.service_name == service


class TestUserNS:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("UserNS=auto", "UserNS=auto:size=65536"),
            ("UserNS=auto:gidmapping=0:1:5", "UserNS=auto:gidmapping=0:1:5,size=65536"),
            ("UserNS=auto ", "UserNS=auto:size=65536"),
            ("  UserNS=auto", "  UserNS=auto:size=65536"),
            ("UserNS = auto", "UserNS = auto:size=65536"),
            # Trailing whitespace goes, the \r of a Windows line ending with it
            ("UserNS=auto\r", "UserNS=auto:size=65536"),
            # Pinned: anything starting with "auto" is treated as auto
            ("UserNS=autox", "UserNS=autox:size=65536"),
        ],
    )
    def test_bare_auto_gets_a_size(self, line, expected):
        assert render("main.container", f"[Container]\n{line}\n", files={}) == (
            f"[Unit]\n{STORAGE}\n\n[Container]\nContainerName=app--main\n{expected}\n"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "UserNS=auto:size=1024",
            "UserNS=auto:uidmapping=0:1:5",
            "UserNS=keep-id",
            "UserNS=host",
            "UserNS=nomap",
            "Image=auto",
        ],
    )
    def test_left_alone(self, line):
        assert f"\n{line}\n" in render("main.container", f"[Container]\n{line}\n")

    def test_normalised_in_containers_only(self):
        assert "UserNS=auto\n" in render("backend.pod", "[Pod]\nUserNS=auto\n")

    def test_default_appended_to_container_section(self):
        content = "[Container]\nImage=x\n\n[Service]\nRestart=always\n"
        assert render("main.container", content) == (
            f"[Unit]\n{STORAGE}\n\n[Container]\nContainerName=app--main\nImage=x\n{USERNS_BLOCK}\n"
            "\n[Service]\nRestart=always\n"
        )

    @pytest.mark.parametrize("directive", ["UserNS=host", "UserNS = host"])
    def test_explicit_userns_is_respected(self, directive):
        out = render("main.container", f"[Container]\nImage=x\n{directive}\n")
        assert "UserNS=auto" not in out

    def test_commented_userns_does_not_count(self):
        out = render("main.container", "[Container]\n#UserNS=host\n")
        assert out.endswith(f"[Container]\nContainerName=app--main\n#UserNS=host\n{USERNS_BLOCK}\n")

    @pytest.mark.parametrize("directive", ["Pod=backend.pod", "Pod = backend.pod"])
    def test_container_in_a_pod_gets_no_default(self, directive):
        """A container joining a pod shares the pod's user namespace; podman refuses another."""
        out = render("main.container", f"[Container]\nImage=x\n{directive}\n")
        assert "UserNS" not in out
        assert "app--backend.pod" in out

    def test_without_container_section_one_is_opened_at_the_top(self):
        # Pinned: [Container] ends up ahead of [Unit]
        assert render("main.container", "[Unit]\nDescription=x\n") == (
            f"[Container]\n{USERNS_BLOCK}\n\n[Unit]\nDescription=x\n{STORAGE}\n"
        )


class TestUnitDirectives:
    def test_storage_dependency_added(self):
        out = render("main.container", "[Unit]\nDescription=x\n\n[Container]\n")
        assert out.startswith(f"[Unit]\nDescription=x\n{STORAGE}\n\n[Container]\n")

    @pytest.mark.parametrize(
        "existing",
        [
            STORAGE,
            "RequiresMountsFor=/mnt/x /var/lib/containers/storage",
            "  RequiresMountsFor=/var/lib/containers/storage",
            "RequiresMountsFor = /var/lib/containers/storage",
        ],
    )
    def test_storage_dependency_already_declared(self, existing):
        out = render("main.container", f"[Unit]\n{existing}\n\n[Container]\nImage=x\n")
        assert out.startswith(f"[Unit]\n{existing}\n\n[Container]\n")

    def test_other_requires_mounts_for_is_kept_alongside(self):
        out = render("main.container", "[Unit]\nRequiresMountsFor=/mnt/x\n\n[Container]\n")
        assert out.startswith(f"[Unit]\nRequiresMountsFor=/mnt/x\n{STORAGE}\n\n[Container]\n")

    def test_trailing_slash_is_not_recognised(self):
        # Pinned: the path is compared as text
        content = "[Unit]\nRequiresMountsFor=/var/lib/containers/storage/\n\n[Container]\n"
        out = render("main.container", content)
        assert out.startswith(
            f"[Unit]\nRequiresMountsFor=/var/lib/containers/storage/\n{STORAGE}\n\n[Container]\n"
        )

    def test_commented_storage_dependency_does_not_count(self):
        out = render("main.container", f"[Unit]\n# {STORAGE}\n\n[Container]\n")
        assert out.startswith(f"[Unit]\n# {STORAGE}\n{STORAGE}\n\n[Container]\n")

    def test_without_unit_section_one_is_opened_at_the_top(self):
        assert render("main.container", "[Container]\nImage=x\n") == (
            f"[Unit]\n{STORAGE}\n\n[Container]\nContainerName=app--main\nImage=x\n{USERNS_BLOCK}\n"
        )

    def test_ordered_after_the_bootstrap_of_the_apps_volumes_in_mount_order(self):
        content = (
            f"[Unit]\n{STORAGE}\n\n[Container]\n"
            "Volume=b.volume:/b\nVolume=a.volume:/a\nVolume=c.volume:/c\nVolume=b.volume:/b2\n"
        )
        out = render("main.container", content, files={"a.volume": "", "b.volume": "X-Owner=1:1"})
        assert out.startswith(
            f"[Unit]\n{STORAGE}\n"
            "Requires=app--b-volume-init.service\nAfter=app--b-volume-init.service\n"
            "Requires=app--a-volume-init.service\nAfter=app--a-volume-init.service\n"
            "\n[Container]\n"
        )

    def test_another_apps_volume_gets_no_ordering(self):
        content = "[Container]\nVolume=data.volume:/d\nVolume=other--data.volume:/c\n"
        out = render("main.container", content, files={"data.volume": "[Volume]\nX-Owner=1:1\n"})
        assert out.startswith(
            f"[Unit]\n{STORAGE}\n"
            "Requires=app--data-volume-init.service\nAfter=app--data-volume-init.service\n\n"
        )
        assert "other--data-volume-init" not in out

    def test_nothing_to_add(self):
        content = f"[Unit]\n{STORAGE}\n\n[Container]\nVolume=unowned.volume:/u\n"
        assert render("main.container", content, files={}).startswith(
            f"[Unit]\n{STORAGE}\n\n[Container]\n"
        )

    def test_pod_mounting_a_volume_gets_no_ordering(self):
        # Pinned: only .container files are ordered against the ownership bootstrap
        out = render("backend.pod", "[Pod]\nVolume=data.volume:/d\n", files={"data.volume": ""})
        assert "volume-init" not in out


class TestMountedVolumes:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("Volume=data.volume:/x", ["data"]),
            ("Volume=data.volume", ["data"]),
            ("  Volume=data.volume:/x", ["data"]),
            ("Volume=b.volume:/x\nVolume=a.volume:/y\nVolume=b.volume:/z", ["b", "a"]),
            ("Volume=/abs/data.volume:/x", []),
            ("Volume=%config%:/x", []),
            ("Volume=named:/x", []),
            ("Volume=main.network:/x", []),
            ("# Volume=data.volume:/x", []),
            ("Volume = data.volume:/x", ["data"]),
            # As written: whichever app the volume is of
            ("Volume=other--data.volume:/x", ["other--data"]),
        ],
    )
    def test_stems(self, content, expected):
        assert QuadletApp(APP).add_quadlet("main.container", content).mounted_volumes() == expected


class TestVolumeOwner:
    """X-Owner: the uid:gid a volume's data is given, container root unless it says otherwise."""

    @staticmethod
    def owners(files):
        app = QuadletApp(APP)
        for name, content in files.items():
            app.add_quadlet(name, content)
        return {volume.stem: volume.owner for volume in app.volumes}

    @staticmethod
    def owner_of(value):
        return QuadletApp(APP).add_quadlet("d.volume", f"[Volume]\nX-Owner={value}\n").owner

    def test_only_volumes_get_an_owner_and_it_defaults_to_root(self):
        files = {
            "data.volume": "[Volume]\nDriver=local\n",
            "main.container": "[Container]\nX-Owner=1:1\n",
            "main.network": "[Network]\n",
        }
        assert self.owners(files) == {"data": "0:0"}

    def test_every_volume_is_listed(self):
        files = {"a.volume": "[Volume]\nX-Owner=1:1\n", "b.volume": "[Volume]\n"}
        assert self.owners(files) == {"a": "1:1", "b": "0:0"}

    def test_last_declaration_wins(self):
        # Pinned
        assert self.owners({"d.volume": "[Volume]\nX-Owner=1:1\nX-Owner=2:2\n"}) == {"d": "2:2"}

    @pytest.mark.parametrize(
        "line, expected",
        [
            ("  X-Owner=5:5", "5:5"),
            ("X-Owner= 5:5 ", "5:5"),
            ("# X-Owner=5:5", "0:0"),
            # systemd syntax allows whitespace around "="
            ("X-Owner = 5:5", "5:5"),
            ("X-Owner\t=5:5", "5:5"),
        ],
    )
    def test_line_forms(self, line, expected):
        assert self.owners({"d.volume": f"[Volume]\n{line}\n"}) == {"d": expected}

    def test_any_section_counts(self):
        assert self.owners({"d.volume": "[Unit]\nX-Owner=7:7\n[Volume]\n"}) == {"d": "7:7"}

    @pytest.mark.parametrize("value", ["0:0", "1000:1000", "65535:65535", "0:65535"])
    def test_accepted(self, value):
        assert self.owner_of(value) == value

    def test_leading_zeros_are_kept(self):
        # Pinned: not normalised, so the marker path carries the zeros too
        assert self.owner_of("001:002") == "001:002"

    def test_non_ascii_digits_are_accepted(self):
        # Pinned: str.isdigit() accepts them, and int() parses them
        assert self.owner_of("١٠:١٠") == "١٠:١٠"

    @pytest.mark.parametrize(
        "value", ["", "a:b", "1000", "1:2:3", "-1:0", "+1:1", "1000:", ":1000", "1: 1", "1.0:1"]
    )
    def test_malformed(self, value):
        with pytest.raises(QuadletAppError) as excinfo:
            self.owner_of(value)
        assert str(excinfo.value) == (
            f"Invalid X-Owner in d.volume: '{value}'. "
            "Expected a numeric uid:gid pair, for example X-Owner=10000:10000"
        )

    @pytest.mark.parametrize("value", ["65536:0", "0:65536", "100000:100000"])
    def test_out_of_range(self, value):
        with pytest.raises(QuadletAppError) as excinfo:
            self.owner_of(value)
        assert str(excinfo.value) == (
            f"X-Owner in d.volume is out of range: '{value}'. Ids must be below 65536, which is "
            "the size of the user namespace the consuming container gets. A higher id would be "
            "written to disk and then be invisible to the app that needs it."
        )

    def test_declared_owner_becomes_user_and_group(self):
        out = render("data.volume", "[Volume]\nDriver=local\nX-Owner=1000:2000\n", files={})
        assert out == "[Volume]\nVolumeName=app--data\nDriver=local\nUser=1000\nGroup=2000\n"

    def test_volume_without_a_declared_owner_is_left_alone(self):
        out = render("data.volume", "[Volume]\nDriver=local\n", files={})
        assert out == "[Volume]\nVolumeName=app--data\nDriver=local\n"

    @pytest.mark.parametrize("explicit", ["User=5", "Group=5"])
    def test_explicit_user_or_group_is_respected(self, explicit):
        out = render("data.volume", f"[Volume]\n{explicit}\nX-Owner=1000:1000\n", files={})
        assert out == f"[Volume]\nVolumeName=app--data\n{explicit}\n"

    def test_x_owner_is_stripped_whatever_its_spacing(self):
        out = render("data.volume", "[Volume]\nX-Owner = 1:1\n", files={})
        assert "X-Owner" not in out

    def test_marker_names_the_owner(self, fake_host):
        volume = QuadletApp(APP).add_quadlet("data.volume", "[Volume]\nX-Owner=10:20\n")
        assert volume.marker_path == f"{fake_host.state_dir}/app--data.10_20.done"
        assert not volume.ownership_applied()
        fake_host.state_dir.mkdir(parents=True)
        (fake_host.state_dir / "app--data.10_20.done").write_text("")
        assert volume.ownership_applied()


def test_volume_bootstrap_unit_text():
    volume = QuadletApp("myapp").add_quadlet("data.volume", "[Volume]\nX-Owner=10000:10001\n")
    assert volume.init_service_name == "myapp--data-volume-init.service"
    assert volume.bootstrap.deployed_name == "myapp--data-volume-init.container"
    assert volume.bootstrap.content == (
        "# Generated by kriansa.quadlet.quadlet_app - do not edit\n"
        "[Unit]\n"
        "Description=Ownership bootstrap for myapp--data\n"
        "ConditionPathExists=!/var/lib/quadlet-app/myapp--data.10000_10001.done\n"
        "\n"
        "[Container]\n"
        "ContainerName=myapp--data-volume-init\n"
        "Image=docker.io/library/alpine:3.24\n"
        "Volume=myapp--data.volume:/target:rw,z\n"
        "Exec=/bin/sh -c 'find /target -mindepth 1 -print0 | xargs -0r chown -h 10000:10001; "
        "chown 10000:10001 /target'\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "TimeoutStartSec=1800\n"
        "ExecStartPost=/usr/bin/mkdir -p /var/lib/quadlet-app\n"
        "ExecStartPost=/usr/bin/find /var/lib/quadlet-app -maxdepth 1 "
        "-regex '.*/myapp--data[.][^./]+_[^./]+[.]done' -delete\n"
        "ExecStartPost=/usr/bin/touch /var/lib/quadlet-app/myapp--data.10000_10001.done\n"
    )


class TestBootstrapMarkers:
    """What the bootstrap unit does to the markers when systemd runs it, acted out by the fake."""

    @staticmethod
    def unit(owner="1000:1000"):
        volume = QuadletApp(APP).add_quadlet("data.volume", f"[Volume]\nX-Owner={owner}\n")
        return volume.bootstrap.content

    def test_creates_the_state_directory(self, fake_host):
        assert run_bootstrap_unit(self.unit())
        assert os.listdir(fake_host.state_dir) == ["app--data.1000_1000.done"]

    def test_replaces_only_this_volumes_previous_markers(self, fake_host):
        fake_host.state_dir.mkdir(parents=True)
        for name in [
            "app--data.0_0.done",  # this volume, when it was owned by root
            "app--data.5_5.done",  # and before that, if a marker was left behind
            "app--data2.5_5.done",  # a different volume whose name starts the same
            "app--data.v2.5_5.done",  # a volume named data.v2
            "other--data.0_0.done",  # another app
            "app--data.notes",  # not a marker
        ]:
            (fake_host.state_dir / name).write_text("")

        assert run_bootstrap_unit(self.unit())

        assert sorted(os.listdir(fake_host.state_dir)) == [
            "app--data.1000_1000.done",
            "app--data.notes",
            "app--data.v2.5_5.done",
            "app--data2.5_5.done",
            "other--data.0_0.done",
        ]

    def test_skipped_while_its_marker_exists(self, fake_host):
        fake_host.state_dir.mkdir(parents=True)
        for name in ["app--data.1000_1000.done", "app--data.0_0.done"]:
            (fake_host.state_dir / name).write_text("")

        assert not run_bootstrap_unit(self.unit())

        assert sorted(os.listdir(fake_host.state_dir)) == [
            "app--data.0_0.done",
            "app--data.1000_1000.done",
        ]
