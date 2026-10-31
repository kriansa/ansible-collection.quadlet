"""Plain systemd units from units/, which today means timers."""

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.units import SystemdUnit, Timer

INSTALL = "\n[Install]\nWantedBy=timers.target\n"


def render(filename, content, *units, **quadlets):
    """
    Render one unit of an app that also has these files in units/, and these quadlets (given as
    stem=extension).
    """
    app = QuadletApp("app")
    for stem, ext in quadlets.items():
        app.add_quadlet(f"{stem}{ext}", "")
    for name in units:
        app.add_unit(name, "")
    return app.add_unit(filename, content).render()


class TestResolveTimerUnit:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ("Unit=backup.container", "Unit=backup.service"),
            ("Unit=backup.kube", "Unit=backup.service"),
            ("Unit=backup.pod", "Unit=backup-pod.service"),
            ("Unit=data.volume", "Unit=data-volume.service"),
            ("Unit=app.network", "Unit=app-network.service"),
            ("  Unit=backup.container", "  Unit=backup.service"),
            ("Unit=backup.container  ", "Unit=backup.service"),
            ("Unit = backup.pod", "Unit = backup-pod.service"),
        ],
    )
    def test_quadlet_suffix_becomes_generated_service(self, line, expected):
        assert render("x.timer", line).split("\n", maxsplit=1)[0] == expected

    @pytest.mark.parametrize(
        "line", ["Unit=backup.service", "Unit=x.target", "Unit=", "OnCalendar=daily", "Unit"]
    )
    def test_left_alone(self, line):
        assert render("x.timer", line).split("\n", maxsplit=1)[0] == line


class TestTimer:
    def test_minimal_timer_gets_unit_and_install(self):
        assert render("backup.timer", "[Timer]\nOnCalendar=daily\n") == (
            "[Timer]\nOnCalendar=daily\nUnit=app--backup.service\n" + INSTALL
        )

    @pytest.mark.parametrize(
        "quadlet_ext, service",
        [
            (".container", "app--backup.service"),
            (".kube", "app--backup.service"),
            (".pod", "app--backup-pod.service"),
            (".volume", "app--backup-volume.service"),
            (".network", "app--backup-network.service"),
        ],
    )
    def test_omitted_unit_resolves_against_the_quadlet_of_the_same_name(self, quadlet_ext, service):
        out = render("backup.timer", "[Timer]\nOnCalendar=daily\n", backup=quadlet_ext)
        assert f"Unit={service}\n" in out

    def test_no_matching_quadlet_assumes_a_service(self):
        out = render("backup.timer", "[Timer]\nOnCalendar=daily\n", other=".pod")
        assert "Unit=app--backup.service\n" in out

    @pytest.mark.parametrize(
        "unit, expected",
        [
            ("backup.container", "app--backup.service"),
            ("job.pod", "app--job-pod.service"),
            ("backup.service", "app--backup.service"),
        ],
    )
    def test_explicit_unit_is_resolved_then_prefixed(self, unit, expected):
        out = render(
            "backup.timer",
            f"[Timer]\nOnCalendar=daily\nUnit={unit}\n",
            backup=".container",
            job=".pod",
        )
        assert out == f"[Timer]\nOnCalendar=daily\nUnit={expected}\n" + INSTALL

    def test_explicit_unit_of_the_host_is_not_prefixed(self):
        out = render("backup.timer", "[Timer]\nOnCalendar=daily\nUnit=logrotate.service\n")
        assert "Unit=logrotate.service\n" in out

    def test_explicit_target_unit_is_not_prefixed(self):
        out = render("backup.timer", "[Timer]\nOnCalendar=daily\nUnit=x.target\n")
        assert "Unit=x.target\n" in out

    def test_spaced_unit_is_resolved_and_not_duplicated(self):
        out = render(
            "x.timer", "[Timer]\nOnCalendar=daily\nUnit = job.container\n", job=".container"
        )
        assert "Unit = app--job.service\n" in out
        assert out.count("Unit") == 1

    def test_commented_unit_does_not_count(self):
        out = render("b.timer", "[Timer]\n# Unit=other.service\nOnCalendar=daily\n")
        assert (
            out
            == "[Timer]\n# Unit=other.service\nOnCalendar=daily\nUnit=app--b.service\n" + INSTALL
        )

    def test_empty_unit_suppresses_injection(self):
        # Pinned: the timer deploys with an empty Unit= and never fires
        out = render("backup.timer", "[Timer]\nOnCalendar=daily\nUnit=\n")
        assert out == "[Timer]\nOnCalendar=daily\nUnit=\n" + INSTALL

    @pytest.mark.parametrize(
        "content, target",
        [
            ("[Timer]\nOnCalendar=daily\n", "app--backup.service"),
            ("[Timer]\nOnCalendar=daily\nUnit=job.pod\n", "app--job-pod.service"),
            ("[Timer]\nOnCalendar=daily\nUnit=logrotate.service\n", "logrotate.service"),
        ],
    )
    def test_target_service(self, content, target):
        app = QuadletApp("app")
        app.add_quadlet("backup.container", "")
        app.add_quadlet("job.pod", "")
        assert app.add_unit("backup.timer", content).target_service == target

    def test_timer_without_timer_section_only_gets_install(self):
        # Pinned: nothing would make it fire, so no Unit= is added to hide that
        assert render("backup.timer", "[Unit]\nDescription=x\n") == (
            "[Unit]\nDescription=x\n" + INSTALL
        )

    def test_existing_wanted_by_is_kept(self):
        content = (
            "[Timer]\nOnCalendar=daily\nUnit=x.service\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        assert render("x.timer", content, x=".container") == content.replace(
            "Unit=x.service", "Unit=app--x.service"
        )

    def test_existing_install_without_wanted_by_gets_it(self):
        content = "[Timer]\nOnCalendar=daily\nUnit=x.service\n\n[Install]\nAlso=y.timer\n"
        assert render("x.timer", content, "y.timer", x=".container") == (
            "[Timer]\nOnCalendar=daily\nUnit=app--x.service\n\n"
            "[Install]\nAlso=app--y.timer\nWantedBy=timers.target\n"
        )

    def test_dependencies_are_prefixed(self):
        content = "[Unit]\nAfter=db.service\nWants=db.service\n\n[Timer]\nOnCalendar=daily\n"
        out = render("x.timer", content, db=".container")
        assert out.startswith("[Unit]\nAfter=app--db.service\nWants=app--db.service\n")

    def test_comments_and_blank_lines_are_untouched(self):
        content = "# Nightly\n\n[Timer]\n# Unit=a.container\nOnCalendar=daily\n"
        out = render("x.timer", content)
        assert out.startswith("# Nightly\n\n[Timer]\n# Unit=a.container\n")

    def test_typical_timer(self):
        """The usual shape: no Unit=, no [Install], relying on both being filled in."""
        content = "[Unit]\nDescription=Renew\n\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n"
        out = render("renew-certs.timer", content, **{"renew-certs": ".container"})
        assert out == (
            "[Unit]\nDescription=Renew\n\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\n"
            "Persistent=true\nUnit=app--renew-certs.service\n" + INSTALL
        )


def test_non_timer_units_only_get_prefixed():
    # units/ only accepts .timer today, but any other unit type is handled
    content = "[Unit]\nAfter=db.service\n\n[Service]\nExecStart=/bin/true\n"
    assert render("x.service", content, db=".container") == (
        "[Unit]\nAfter=app--db.service\n\n[Service]\nExecStart=/bin/true\n"
    )


@pytest.mark.parametrize("filename, is_timer", [("x.timer", True), ("x.service", False)])
def test_names_on_the_host(fake_host, filename, is_timer):
    unit = QuadletApp("app").add_unit(filename, "")
    assert isinstance(unit, SystemdUnit)
    assert isinstance(unit, Timer) is is_timer
    assert unit.deployed_name == f"app--{filename}"
    assert unit.dest_path == f"{fake_host.unit_dir}/app--{filename}"
