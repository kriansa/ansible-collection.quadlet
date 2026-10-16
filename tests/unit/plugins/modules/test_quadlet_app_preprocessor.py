"""
QuadletPreprocessor: the text rewriting applied to every quadlet before it is deployed.

Cases marked "pinned" record what the code does today even where that is arguably wrong. They
exist so a refactor cannot change the behaviour without a test saying so; fixing one is a
deliberate change to its expectation.
"""

import pytest

from ansible_collections.kriansa.commons.plugins.modules.quadlet_app import QuadletPreprocessor

APP = "app"
USERNS_BLOCK = (
    "# Default from quadlet_app. Set UserNS= explicitly to override; UserNS=host is what a\n"
    "# container needs if it reads the host journal or other processes' /proc entries.\n"
    "UserNS=auto:size=65536"
)
STORAGE = "RequiresMountsFor=/var/lib/containers/storage"


@pytest.fixture
def pre():
    return QuadletPreprocessor(APP)


class TestRule1PrefixResources:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Network=main.network", "Network=app--main.network"),
            ("Pod=backend.pod", "Pod=app--backend.pod"),
            ("Volume=data.volume:/data", "Volume=app--data.volume:/data:rw,Z,idmap"),
            ("Volume=data.volume:/data:ro", "Volume=app--data.volume:/data:ro"),
            ("Volume=data.volume:/data:rw,U", "Volume=app--data.volume:/data:rw,U"),
            # A .network or .pod source is prefixed but gets no default options
            ("Volume=main.network:/x", "Volume=app--main.network:/x"),
            ("After=db.service", "After=app--db.service"),
            ("Wants=db.container", "Wants=app--db.container"),
            ("Requires=main.service", "Requires=app--main.service"),
            ("Requisite=x.service", "Requisite=app--x.service"),
            ("BindsTo=x.service", "BindsTo=app--x.service"),
            ("PartOf=x.service", "PartOf=app--x.service"),
            ("Upholds=x.service", "Upholds=app--x.service"),
            ("Conflicts=x.kube", "Conflicts=app--x.kube"),
            ("Before=x.timer", "Before=app--x.timer"),
            ("After=data.volume", "After=app--data.volume"),
            ("Unit=backup.service", "Unit=app--backup.service"),
            ("  Network=main.network", "  Network=app--main.network"),
        ],
    )
    def test_prefixed(self, pre, line, expected):
        assert pre._apply_rule1_prefix_resources(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            "Network=host",
            "Network=none",
            # A network another app created: already a full podman name
            "Network=ingress--main",
            "After=network-online.target",
            "Wants=multi-user.target",
            "Requires=/abs/x.service",
            "Volume=/abs/path:/data",
            "Volume=named:/data",
            "Volume=%config%:/etc/x",
            "Image=docker.io/library/alpine",
            "Environment=Network=main.network",
            # Spaces around "=" make the directive name "Network ", which is not recognised
            "Network = main.network",
            "no equals sign here",
            "",
            "   ",
        ],
    )
    def test_left_alone(self, pre, line):
        assert pre._apply_rule1_prefix_resources(line) == line

    def test_volume_without_destination_gets_options_as_destination(self, pre):
        # Pinned: the docs' own example. The options land where the destination would be.
        assert pre._apply_rule1_prefix_resources("Volume=data.volume") == (
            "Volume=app--data.volume:rw,Z,idmap"
        )

    def test_network_with_options_is_not_prefixed(self, pre):
        # Pinned: the value no longer ends in .network, so it is not recognised
        line = "Network=main.network:ip=192.0.2.5"
        assert pre._apply_rule1_prefix_resources(line) == line

    def test_only_the_start_of_a_multi_unit_list_is_prefixed(self, pre):
        # Pinned: the whole value is treated as one name
        assert pre._apply_rule1_prefix_resources("After=a.service b.service") == (
            "After=app--a.service b.service"
        )

    def test_units_from_outside_the_app_are_prefixed_too(self, pre):
        # Pinned: there is no way to tell a host unit from an app unit
        assert pre._apply_rule1_prefix_resources("After=sshd.service") == "After=app--sshd.service"

    def test_crlf_lines_are_not_prefixed(self, pre):
        # Pinned: the trailing \r is part of the value, so it no longer ends in a known suffix
        assert pre._apply_rule1_prefix_resources("Network=main.network\r") == "Network=main.network\r"

    def test_crlf_volume_gets_options_after_the_carriage_return(self, pre):
        # Pinned
        assert pre._apply_rule1_prefix_resources("Volume=data.volume:/d\r") == (
            "Volume=app--data.volume:/d\r:rw,Z,idmap"
        )


class TestRule2ReplacePaths:
    @pytest.mark.parametrize(
        "line, container, expected",
        [
            ("Volume=%init%:/docker-entrypoint-initdb.d", "main",
             "Volume=/srv/app/init/main:/docker-entrypoint-initdb.d:ro,Z,idmap"),
            ("Volume=%config%:/etc/nginx/conf.d", "main",
             "Volume=/srv/app/config/main:/etc/nginx/conf.d:ro,Z,idmap"),
            ("Volume=%config%/nginx.conf:/etc/nginx/nginx.conf", "main",
             "Volume=/srv/app/config/main/nginx.conf:/etc/nginx/nginx.conf:ro,Z,idmap"),
            ("Volume=%init%/sub/f.sql:/x", "db", "Volume=/srv/app/init/db/sub/f.sql:/x:ro,Z,idmap"),
            # No slash after the placeholder: the rest is still a subpath
            ("Volume=%init%sub:/x", "main", "Volume=/srv/app/init/main/sub:/x:ro,Z,idmap"),
            ("Volume=%config%:/etc/x:rw", "main", "Volume=/srv/app/config/main:/etc/x:rw"),
            ("Volume=%config%:/etc/x:rw,Z,U", "main", "Volume=/srv/app/config/main:/etc/x:rw,Z,U"),
            ("  Volume=%config%:/x", "main", "  Volume=/srv/app/config/main:/x:ro,Z,idmap"),
        ],
    )
    def test_replaced(self, pre, line, container, expected):
        assert pre._apply_rule2_replace_paths(line, container) == expected

    def test_explicitly_empty_options_are_kept_empty(self, pre):
        # Pinned: an empty third field counts as "specified"
        assert pre._apply_rule2_replace_paths("Volume=%config%:/x:", "main") == (
            "Volume=/srv/app/config/main:/x:"
        )

    def test_without_destination(self, pre):
        # Pinned: produces an empty destination field
        assert pre._apply_rule2_replace_paths("Volume=%config%", "main") == (
            "Volume=/srv/app/config/main::ro,Z,idmap"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "Volume=/abs:/x",
            "Volume=data.volume:/x",
            "Environment=PATH=%init%",
            "Exec=cat %config%",
            "# Volume=%init%:/x",
        ],
    )
    def test_left_alone(self, pre, line):
        assert pre._apply_rule2_replace_paths(line, "main") == line


class TestRule3InjectNames:
    @pytest.mark.parametrize(
        "filename, section, directive",
        [
            ("main.container", "Container", "ContainerName=app--main"),
            ("backend.pod", "Pod", "PodName=app--backend"),
            ("data.volume", "Volume", "VolumeName=app--data"),
            ("main.network", "Network", "NetworkName=app--main"),
        ],
    )
    def test_injected_after_the_header(self, pre, filename, section, directive):
        content = f"[Unit]\nDescription=x\n\n[{section}]\nFoo=1\n"
        assert pre._apply_rule3_inject_names(content, filename) == (
            f"[Unit]\nDescription=x\n\n[{section}]\n{directive}\nFoo=1\n"
        )

    def test_explicit_name_is_kept(self, pre):
        content = "[Container]\nContainerName=custom\nImage=x\n"
        assert pre._apply_rule3_inject_names(content, "main.container") == content

    def test_commented_name_does_not_count(self, pre):
        content = "[Container]\n# ContainerName=custom\nImage=x\n"
        assert pre._apply_rule3_inject_names(content, "main.container") == (
            "[Container]\nContainerName=app--main\n# ContainerName=custom\nImage=x\n"
        )

    def test_no_target_section_leaves_content_alone(self, pre):
        content = "[Unit]\nDescription=x\n"
        assert pre._apply_rule3_inject_names(content, "main.container") == content

    def test_only_the_first_of_duplicate_sections(self, pre):
        content = "[Container]\nImage=x\n[Container]\nExec=y\n"
        assert pre._apply_rule3_inject_names(content, "main.container") == (
            "[Container]\nContainerName=app--main\nImage=x\n[Container]\nExec=y\n"
        )

    def test_kube_is_untouched(self, pre):
        content = "# header\n[Kube]\nYaml=x.yaml\n"
        assert pre._apply_rule3_inject_names(content, "main.kube") == content

    def test_lines_before_the_first_header_are_dropped(self, pre):
        # Pinned bug: comments at the top of a .container/.volume never reach the host
        content = "# Why this volume exists\n# and who owns it\n\n[Volume]\nDriver=local\n"
        assert pre._apply_rule3_inject_names(content, "data.volume") == (
            "[Volume]\nVolumeName=app--data\nDriver=local\n"
        )

    def test_file_without_any_header_becomes_empty(self, pre):
        # Pinned bug
        assert pre._apply_rule3_inject_names("Image=x\nExec=y\n", "main.container") == ""

    def test_header_with_inner_spaces_is_not_matched(self, pre):
        # Pinned: "[ Container ]" is a different section name to this code
        content = "[ Container ]\nImage=x\n"
        assert pre._apply_rule3_inject_names(content, "main.container") == content


class TestPreprocessQuadletFilePerType:
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

    def run(self, pre, ext, section):
        return pre.preprocess_quadlet_file(self.SOURCE.format(section=section), f"main{ext}")

    def test_container_gets_every_rule(self, pre):
        assert self.run(pre, ".container", "Container") == (
            "[Unit]\n"
            "After=app--db.service\n"
            f"{STORAGE}\n"
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

    def test_pod_gets_rules_1_2_3_only(self, pre):
        assert self.run(pre, ".pod", "Pod") == (
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

    def test_kube_gets_rule_1_only(self, pre):
        assert self.run(pre, ".kube", "Kube") == (
            "[Unit]\n"
            "After=app--db.service\n"
            "\n"
            "[Kube]\n"
            "Network=app--main.network\n"
            "Volume=%config%:/etc/x\n"
            "Volume=app--data.volume:/data:rw,Z,idmap\n"
            "X-Owner=1000:1000\n"
        )

    def test_volume_gets_rule_3_and_loses_x_owner(self, pre):
        # Pinned: Rule 1 does not apply to .volume, so its [Unit] references stay unprefixed
        assert self.run(pre, ".volume", "Volume") == (
            "[Unit]\n"
            "After=db.service\n"
            "\n"
            "[Volume]\n"
            "VolumeName=app--main\n"
            "Network=main.network\n"
            "Volume=%config%:/etc/x\n"
            "Volume=data.volume:/data\n"
        )

    def test_network_gets_rule_3_only(self, pre):
        assert self.run(pre, ".network", "Network") == (
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


class TestPreprocessQuadletFile:
    def test_comments_and_blank_lines_are_untouched(self, pre):
        content = "[Container]\n# Network=main.network\n\nImage=x\n"
        assert pre.preprocess_quadlet_file(content, "main.container") == (
            f"[Unit]\n{STORAGE}\n\n"
            "[Container]\nContainerName=app--main\n# Network=main.network\n\nImage=x\n"
            f"{USERNS_BLOCK}\n"
        )

    def test_without_trailing_newline(self, pre):
        out = pre.preprocess_quadlet_file("[Network]\nDriver=bridge", "main.network")
        assert out == "[Network]\nNetworkName=app--main\nDriver=bridge"

    def test_running_twice_prefixes_twice(self, pre):
        # Pinned: preprocessing is not idempotent, so it must only ever run once per file
        once = pre.preprocess_quadlet_file("[Container]\nNetwork=main.network\n", "main.container")
        twice = pre.preprocess_quadlet_file(once, "main.container")
        assert "Network=app--app--main.network" in twice

    def test_volume_init_ordering_only_for_owned_mounted_volumes(self):
        pre = QuadletPreprocessor(APP, volume_owners={"data": "1000:1000"})
        out = pre.preprocess_quadlet_file(
            "[Container]\nVolume=data.volume:/d\nVolume=cache.volume:/c\n", "main.container"
        )
        assert out.startswith(
            "[Unit]\n"
            f"{STORAGE}\n"
            "Requires=app--data-volume-init.service\n"
            "After=app--data-volume-init.service\n"
            "\n"
        )
        assert "cache-volume-init" not in out

    def test_pod_mounting_an_owned_volume_gets_no_ordering(self):
        # Pinned: only .container files are ordered against the ownership bootstrap
        pre = QuadletPreprocessor(APP, volume_owners={"data": "1000:1000"})
        out = pre.preprocess_quadlet_file("[Pod]\nVolume=data.volume:/d\n", "backend.pod")
        assert "volume-init" not in out


class TestUserNS:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("UserNS=auto", "UserNS=auto:size=65536"),
            ("UserNS=auto:gidmapping=0:1:5", "UserNS=auto:gidmapping=0:1:5,size=65536"),
            ("UserNS=auto ", "UserNS=auto:size=65536"),
            ("  UserNS=auto", "  UserNS=auto:size=65536"),
            # Pinned: anything starting with "auto" is treated as auto
            ("UserNS=autox", "UserNS=autox:size=65536"),
        ],
    )
    def test_bare_auto_gets_a_size(self, pre, line, expected):
        assert pre._normalize_userns(line) == expected

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
    def test_left_alone(self, pre, line):
        assert pre._normalize_userns(line) == line

    def test_normalised_in_containers_only(self, pre):
        out = pre.preprocess_quadlet_file("[Pod]\nUserNS=auto\n", "backend.pod")
        assert "UserNS=auto\n" in out

    def test_default_appended_to_container_section(self, pre):
        content = "[Container]\nImage=x\n\n[Service]\nRestart=always\n"
        assert pre._inject_default_userns(content) == (
            f"[Container]\nImage=x\n{USERNS_BLOCK}\n\n[Service]\nRestart=always\n"
        )

    def test_explicit_userns_is_respected(self, pre):
        content = "[Container]\nUserNS=host\n"
        assert pre._inject_default_userns(content) == content

    def test_commented_userns_does_not_count(self, pre):
        content = "[Container]\n#UserNS=host\n"
        assert pre._inject_default_userns(content) == f"[Container]\n#UserNS=host\n{USERNS_BLOCK}\n"

    def test_without_container_section_one_is_opened_at_the_top(self, pre):
        # Pinned: [Container] ends up ahead of [Unit]
        assert pre._inject_default_userns("[Unit]\nDescription=x\n") == (
            f"[Container]\n{USERNS_BLOCK}\n\n[Unit]\nDescription=x\n"
        )


class TestUnitDirectives:
    def test_storage_dependency_added(self, pre):
        assert pre._inject_unit_directives("[Unit]\nDescription=x\n\n[Container]\n", []) == (
            f"[Unit]\nDescription=x\n{STORAGE}\n\n[Container]\n"
        )

    @pytest.mark.parametrize(
        "existing",
        [
            STORAGE,
            "RequiresMountsFor=/mnt/x /var/lib/containers/storage",
            "  RequiresMountsFor=/var/lib/containers/storage",
        ],
    )
    def test_storage_dependency_already_declared(self, pre, existing):
        content = f"[Unit]\n{existing}\n"
        assert pre._inject_unit_directives(content, []) == content

    def test_other_requires_mounts_for_is_kept_alongside(self, pre):
        assert pre._inject_unit_directives("[Unit]\nRequiresMountsFor=/mnt/x\n", []) == (
            f"[Unit]\nRequiresMountsFor=/mnt/x\n{STORAGE}\n"
        )

    def test_trailing_slash_is_not_recognised(self, pre):
        # Pinned: the path is compared as text
        content = "[Unit]\nRequiresMountsFor=/var/lib/containers/storage/\n"
        assert pre._inject_unit_directives(content, []).endswith(f"{STORAGE}\n")

    def test_commented_storage_dependency_does_not_count(self, pre):
        content = f"[Unit]\n# {STORAGE}\n"
        assert pre._inject_unit_directives(content, []) == f"[Unit]\n# {STORAGE}\n{STORAGE}\n"

    def test_without_unit_section_one_is_opened_at_the_top(self, pre):
        assert pre._inject_unit_directives("[Container]\nImage=x\n", []) == (
            f"[Unit]\n{STORAGE}\n\n[Container]\nImage=x\n"
        )

    def test_owned_volumes_in_mount_order(self):
        pre = QuadletPreprocessor(APP, volume_owners={"a": "0:0", "b": "1:1"})
        out = pre._inject_unit_directives(f"[Unit]\n{STORAGE}\n", ["b", "a", "c"])
        assert out == (
            f"[Unit]\n{STORAGE}\n"
            "Requires=app--b-volume-init.service\nAfter=app--b-volume-init.service\n"
            "Requires=app--a-volume-init.service\nAfter=app--a-volume-init.service\n"
        )

    def test_nothing_to_add(self, pre):
        content = f"[Unit]\n{STORAGE}\n"
        assert pre._inject_unit_directives(content, ["unowned"]) == content


class TestReferencedVolumes:
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
            ("Volume = data.volume:/x", []),
        ],
    )
    def test_stems(self, pre, content, expected):
        assert pre.referenced_volumes(content) == expected


class TestTextHelpers:
    def test_strip_directive(self, pre):
        content = "[Volume]\nX-Owner=1:1\n  X-Owner=2:2\n# X-Owner=3:3\nX-OwnerFoo=4\nDriver=local\n"
        assert pre._strip_directive(content, "X-Owner") == (
            "[Volume]\n# X-Owner=3:3\nX-OwnerFoo=4\nDriver=local\n"
        )

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("[Unit]\nUserNS=host\n", True),
            ("[Unit]\n  UserNS=host\n", True),
            ("[Unit]\n#UserNS=host\n", False),
            ("[Unit]\nUserNSX=host\n", False),
            ("", False),
        ],
    )
    def test_has_directive(self, pre, content, expected):
        assert pre._has_directive(content, "UserNS") is expected

    @pytest.mark.parametrize(
        "content, expected",
        [("[Timer]\n", True), ("  [Timer]  \n", True), ("# [Timer]\n", False), ("[Timers]\n", False)],
    )
    def test_has_section(self, pre, content, expected):
        assert pre._has_section(content, "Timer") is expected


class TestAppendToSection:
    @pytest.mark.parametrize(
        "content, at_end, expected",
        [
            # Into a middle section, ahead of the blank line separating it from the next one
            ("[Unit]\nA=1\n\n[Service]\nB=2\n", False, "[Unit]\nA=1\nX=1\n\n[Service]\nB=2\n"),
            # Into the last section
            ("[Unit]\nA=1\n", False, "[Unit]\nA=1\nX=1\n"),
            # No blank line before the next header
            ("[Unit]\nA=1\n[Service]", False, "[Unit]\nA=1\nX=1\n[Service]"),
            # An empty section
            ("[Unit]\n[Service]", False, "[Unit]\nX=1\n[Service]"),
            # Blank lines only: inserted right after the header
            ("[Unit]\n\n\n[Service]\n", False, "[Unit]\nX=1\n\n\n[Service]\n"),
            # Missing section, opened at the top
            ("[Service]\nB=2\n", False, "[Unit]\nX=1\n\n[Service]\nB=2\n"),
            ("", False, "[Unit]\nX=1\n\n"),
            # Missing section, opened at the end, trailing blank lines moved before it
            ("[Service]\nB=2\n\n\n", True, "[Service]\nB=2\n\n[Unit]\nX=1\n"),
            ("", True, "[Unit]\nX=1\n"),
            # Only the first of duplicate sections is used
            ("[Unit]\nA=1\n[Unit]\nB=2\n", False, "[Unit]\nA=1\nX=1\n[Unit]\nB=2\n"),
        ],
    )
    def test_placement(self, pre, content, at_end, expected):
        assert pre._append_to_section(content, "Unit", ["X=1"], at_end=at_end) == expected

    def test_at_end_only_matters_when_the_section_is_missing(self, pre):
        content = "[Unit]\nA=1\n\n[Service]\nB=2\n"
        assert pre._append_to_section(content, "Unit", ["X=1"], at_end=True) == (
            "[Unit]\nA=1\nX=1\n\n[Service]\nB=2\n"
        )

    def test_several_directives_keep_their_order(self, pre):
        assert pre._append_to_section("[Unit]\n", "Unit", ["A=1", "B=2"]) == "[Unit]\nA=1\nB=2\n"
