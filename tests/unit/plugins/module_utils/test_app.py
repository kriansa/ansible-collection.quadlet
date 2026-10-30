"""
QuadletApp: the app's name, its files, and the rules resolving what its files refer to.

Cases marked "pinned" record what the code does today even where that is arguably wrong. They
exist so a refactor cannot change the behaviour without a test saying so; fixing one is a
deliberate change to its expectation.
"""

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import UnitFile

APP = "app"
# The files of the app the rules run for. Rule 1 prefixes only what the app defines.
QUADLETS = (
    "main.container",
    "db.container",
    "backup.container",
    "job.container",
    "x.kube",
    "main.network",
    "backend.pod",
    "data.volume",
)
UNITS = ("x.timer",)


def make_app(quadlets=QUADLETS, units=UNITS):
    app = QuadletApp(APP)
    for name in quadlets:
        app.add_quadlet(name, "")
    for name in units:
        app.add_unit(name, "")
    return app


def prefixed(text):
    """Rule 1, applied to every directive of the text."""
    unit = UnitFile(text)
    unit.map_directives(make_app().prefix_reference)
    return unit.text


def placed(text, stem="main"):
    """Rule 2, applied to every directive of the text, as of the quadlet with this name."""
    app = make_app()
    unit = UnitFile(text)
    unit.map_directives(lambda directive: app.resolve_data_mount(directive, stem))
    return unit.text


class TestName:
    @pytest.mark.parametrize(
        "name",
        [
            "a",
            "ab",
            "a1",
            "a-b",
            "a_b",
            "web-server",
            "api_v2",
            "db1",
            # Pinned: consecutive hyphens are accepted, though they read like the app--name separator
            "a--b",
        ],
    )
    def test_valid(self, name):
        assert QuadletApp(name).name == name

    def test_lowercased(self):
        assert QuadletApp("MyApp").name == "myapp"

    @pytest.mark.parametrize(
        "name", ["", "1", "1a", "-a", "a-", "a_", "a.b", "a b", "_a", "ä", "a/b", "ab\n", "a\n"]
    )
    def test_invalid(self, name):
        with pytest.raises(QuadletAppError) as excinfo:
            QuadletApp(name)
        assert str(excinfo.value) == (
            f"Invalid application name: {name.lower()}. Must start with a letter (a-z), end with a "
            "letter or number, and contain only lowercase letters, numbers, hyphens, and "
            "underscores."
        )


class TestRule1PrefixReferences:
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
    def test_prefixed(self, line, expected):
        assert prefixed(line) == expected

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
            "no equals sign here",
            "",
            "   ",
        ],
    )
    def test_left_alone(self, line):
        assert prefixed(line) == line

    @pytest.mark.parametrize(
        "line, expected",
        [
            # The services podman generates from the app's quadlets are the app's too
            ("After=main-network.service", "After=app--main-network.service"),
            ("Requires=data-volume.service", "Requires=app--data-volume.service"),
            ("BindsTo=backend-pod.service", "BindsTo=app--backend-pod.service"),
            ("Also=db.service", "Also=app--db.service"),
            # Each unit of a list on its own merits, the spacing kept as written
            (
                "After=db.service sshd.service x.service",
                "After=app--db.service sshd.service app--x.service",
            ),
            ("Wants=db.service\tbackup.service ", "Wants=app--db.service\tapp--backup.service "),
            # A network with options
            ("Network=main.network:ip=192.0.2.5", "Network=app--main.network:ip=192.0.2.5"),
            # Without a destination there is nowhere for options to go: they would be read as one
            ("Volume=data.volume", "Volume=app--data.volume"),
            # A file saved with Windows line endings keeps a \r at the end of every line
            ("Network=main.network\r", "Network=app--main.network\r"),
            ("After=db.service sshd.service\r", "After=app--db.service sshd.service\r"),
            ("Volume=data.volume:/d\r", "Volume=app--data.volume:/d:rw,Z,idmap\r"),
        ],
    )
    def test_prefixed_where_the_app_defines_it(self, line, expected):
        assert prefixed(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            # A unit of the host
            "After=sshd.service",
            "Unit=logrotate.service",
            # Another app's, written out in full
            "Requires=other--db.service",
            "Network=other--main.network",
            "Pod=other--backend.pod",
            "Volume=other--data.volume:/data",
            # A name the app does not define, which podman's generator will report
            "Network=missing.network",
            # The generated bootstrap units are not files of the app
            "After=data-volume-init.service",
        ],
    )
    def test_left_alone_where_the_app_does_not_define_it(self, line):
        assert prefixed(line) == line

    @pytest.mark.parametrize(
        "line, expected",
        [
            # systemd accepts `Key = value`; it must read the same as `Key=value`
            ("Network = main.network", "Network = app--main.network"),
            ("Volume = data.volume:/data", "Volume = app--data.volume:/data:rw,Z,idmap"),
            ("After\t=\tdb.service", "After\t=\tapp--db.service"),
            ("  Requires =main.service", "  Requires =app--main.service"),
        ],
    )
    def test_whitespace_around_equals(self, line, expected):
        assert prefixed(line) == expected

    @pytest.mark.parametrize("line", ["Environment=X = y", "Exec=/bin/echo Network = main.network"])
    def test_equals_inside_a_value_is_not_a_directive(self, line):
        assert prefixed(line) == line


class TestRule2ResolveDataMounts:
    @pytest.mark.parametrize(
        "line, stem, expected",
        [
            (
                "Volume=%init%:/docker-entrypoint-initdb.d",
                "main",
                "Volume=/srv/app/init/main:/docker-entrypoint-initdb.d:ro,Z,idmap",
            ),
            (
                "Volume=%config%:/etc/nginx/conf.d",
                "main",
                "Volume=/srv/app/config/main:/etc/nginx/conf.d:ro,Z,idmap",
            ),
            (
                "Volume=%config%/nginx.conf:/etc/nginx/nginx.conf",
                "main",
                "Volume=/srv/app/config/main/nginx.conf:/etc/nginx/nginx.conf:ro,Z,idmap",
            ),
            ("Volume=%init%/sub/f.sql:/x", "db", "Volume=/srv/app/init/db/sub/f.sql:/x:ro,Z,idmap"),
            # No slash after the placeholder: the rest is still a subpath
            ("Volume=%init%sub:/x", "main", "Volume=/srv/app/init/main/sub:/x:ro,Z,idmap"),
            ("Volume=%config%:/etc/x:rw", "main", "Volume=/srv/app/config/main:/etc/x:rw"),
            ("Volume=%config%:/etc/x:rw,Z,U", "main", "Volume=/srv/app/config/main:/etc/x:rw,Z,U"),
            ("  Volume=%config%:/x", "main", "  Volume=/srv/app/config/main:/x:ro,Z,idmap"),
            ("Volume = %config%:/etc/x", "main", "Volume = /srv/app/config/main:/etc/x:ro,Z,idmap"),
        ],
    )
    def test_replaced(self, line, stem, expected):
        assert placed(line, stem) == expected

    @pytest.mark.parametrize(
        "line, expected",
        [
            # A file saved with Windows line endings keeps a \r at the end of every line
            ("Volume=%config%:/x\r", "Volume=/srv/app/config/main:/x:ro,Z,idmap\r"),
            ("Volume=%init%/sub:/y:ro\r", "Volume=/srv/app/init/main/sub:/y:ro\r"),
        ],
    )
    def test_crlf(self, line, expected):
        assert placed(line) == expected

    def test_explicitly_empty_options_are_kept_empty(self):
        # Pinned: an empty third field counts as "specified"
        assert placed("Volume=%config%:/x:") == "Volume=/srv/app/config/main:/x:"

    def test_without_destination(self):
        # Pinned: produces an empty destination field
        assert placed("Volume=%config%") == "Volume=/srv/app/config/main::ro,Z,idmap"

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
    def test_left_alone(self, line):
        assert placed(line) == line

    def test_follows_where_the_app_is_deployed(self, fake_host):
        assert placed("Volume=%config%:/x") == (
            f"Volume={fake_host.srv_dir}/app/config/main:/x:ro,Z,idmap"
        )


class TestLookups:
    def test_quadlet_of_any_type_the_last_one_wins(self):
        # Pinned: with job.container and job.pod both present, the pod is what a timer would run
        app = make_app(quadlets=["job.container", "job.pod"], units=[])
        assert app.quadlet("job").kind == ".pod"
        assert app.quadlet("missing") is None

    def test_volume(self):
        app = make_app()
        assert app.volume("data").filename == "data.volume"
        assert app.volume("main") is None

    def test_volumes_and_timers(self):
        app = make_app()
        assert [volume.filename for volume in app.volumes] == ["data.volume"]
        assert [timer.filename for timer in app.timers] == ["x.timer"]

    def test_deployed_names_put_the_generated_quadlets_last(self):
        app = make_app(quadlets=["a.volume", "main.container", "b.volume"])
        assert app.deployed_quadlets == [
            "app--a.volume",
            "app--main.container",
            "app--b.volume",
            "app--a-volume-init.container",
            "app--b-volume-init.container",
        ]
        assert app.deployed_units == ["app--x.timer"]

    def test_main_service(self):
        assert make_app().main_service == "app--main.service"

    def test_services_are_the_long_running_ones_sorted_by_file(self):
        app = make_app(quadlets=["a.pod", "a-z.container", "web.kube", "n.network", "d.volume"])
        # Sorted by file name, which is not the order of the service names
        assert app.services() == ["app--a-z.service", "app--a-pod.service", "app--web.service"]

    def test_volume_init_services(self):
        app = make_app(quadlets=["b.volume", "a.volume"])
        assert app.volume_init_services() == [
            "app--b-volume-init.service",
            "app--a-volume-init.service",
        ]

    def test_pending_volume_inits_are_those_without_their_marker(self, fake_host):
        app = QuadletApp(APP)
        app.add_quadlet("a.volume", "[Volume]\nX-Owner=1:1\n")
        app.add_quadlet("b.volume", "[Volume]\n")
        fake_host.state_dir.mkdir(parents=True)
        # b's marker, but for another owner than the one it declares now
        (fake_host.state_dir / "app--a.1_1.done").write_text("")
        (fake_host.state_dir / "app--b.5_5.done").write_text("")
        assert app.pending_volume_inits() == ["app--b-volume-init.service"]


class TestFiles:
    def test_in_deployment_order(self, fake_host):
        app = QuadletApp(APP)
        app.add_data_file("init", "main", "a.sql", "a")
        app.add_unit("job.timer", "[Timer]\n")
        app.add_quadlet("main.container", "[Container]\n")
        app.add_quadlet("data.volume", "[Volume]\n")
        app.add_data_file("config", "main", "b.conf", "b")
        assert [file.dest_path for file in app.files()] == [
            f"{fake_host.quadlet_dir}/app--main.container",
            f"{fake_host.quadlet_dir}/app--data.volume",
            f"{fake_host.quadlet_dir}/app--data-volume-init.container",
            f"{fake_host.unit_dir}/app--job.timer",
            f"{fake_host.srv_dir}/app/init/main/a.sql",
            f"{fake_host.srv_dir}/app/config/main/b.conf",
        ]

    def test_a_generated_quadlet_replaces_one_of_the_same_name(self):
        # Pinned: the app's own file keeps its place, but it is the generated one that is deployed
        app = QuadletApp(APP)
        app.add_quadlet("data-volume-init.container", "[Container]\nImage=mine\n")
        app.add_quadlet("data.volume", "[Volume]\n")
        files = app.files()
        assert [file.dest_path.rsplit("/", 1)[1] for file in files] == [
            "app--data-volume-init.container",
            "app--data.volume",
        ]
        assert files[0].content.startswith("# Generated by kriansa.quadlet.quadlet_app")

    def test_data_files_go_under_the_quadlet_stem(self):
        app = QuadletApp(APP)
        file = app.add_data_file("config", "my.app", "sub/x.conf", "x")
        # Pinned: the directory name loses whatever looks like a suffix
        assert file.dest_path == "/srv/app/config/my/sub/x.conf"
        assert file.content == "x"
