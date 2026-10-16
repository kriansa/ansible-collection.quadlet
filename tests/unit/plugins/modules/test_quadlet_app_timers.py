"""Preprocessing of plain systemd units from units/, which today means timers."""

import pytest

from ansible_collections.kriansa.commons.plugins.modules.quadlet_app import QuadletPreprocessor

INSTALL = "\n[Install]\nWantedBy=timers.target\n"


def pre(**quadlet_types):
    preprocessor = QuadletPreprocessor("app")
    preprocessor.quadlet_types = dict(quadlet_types)
    return preprocessor


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
        ],
    )
    def test_quadlet_suffix_becomes_generated_service(self, line, expected):
        assert pre()._resolve_timer_unit(line) == expected

    @pytest.mark.parametrize(
        "line", ["Unit=backup.service", "Unit=x.target", "Unit=", "OnCalendar=daily", "Unit"]
    )
    def test_left_alone(self, line):
        assert pre()._resolve_timer_unit(line) == line


class TestPreprocessUnitFile:
    def test_minimal_timer_gets_unit_and_install(self):
        assert pre().preprocess_unit_file("[Timer]\nOnCalendar=daily\n", "backup.timer") == (
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
        out = pre(backup=quadlet_ext).preprocess_unit_file("[Timer]\nOnCalendar=daily\n", "backup.timer")
        assert f"Unit={service}\n" in out

    def test_no_matching_quadlet_assumes_a_service(self):
        out = pre(other=".pod").preprocess_unit_file("[Timer]\nOnCalendar=daily\n", "backup.timer")
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
        out = pre().preprocess_unit_file(f"[Timer]\nOnCalendar=daily\nUnit={unit}\n", "backup.timer")
        assert out == f"[Timer]\nOnCalendar=daily\nUnit={expected}\n" + INSTALL

    def test_explicit_target_unit_is_not_prefixed(self):
        out = pre().preprocess_unit_file("[Timer]\nOnCalendar=daily\nUnit=x.target\n", "backup.timer")
        assert "Unit=x.target\n" in out

    def test_commented_unit_does_not_count(self):
        out = pre().preprocess_unit_file("[Timer]\n# Unit=other.service\nOnCalendar=daily\n", "b.timer")
        assert out == "[Timer]\n# Unit=other.service\nOnCalendar=daily\nUnit=app--b.service\n" + INSTALL

    def test_empty_unit_suppresses_injection(self):
        # Pinned: the timer deploys with an empty Unit= and never fires
        out = pre().preprocess_unit_file("[Timer]\nOnCalendar=daily\nUnit=\n", "backup.timer")
        assert out == "[Timer]\nOnCalendar=daily\nUnit=\n" + INSTALL

    def test_timer_without_timer_section_only_gets_install(self):
        # Pinned: nothing would make it fire, so no Unit= is added to hide that
        assert pre().preprocess_unit_file("[Unit]\nDescription=x\n", "backup.timer") == (
            "[Unit]\nDescription=x\n" + INSTALL
        )

    def test_existing_wanted_by_is_kept(self):
        content = "[Timer]\nOnCalendar=daily\nUnit=x.service\n\n[Install]\nWantedBy=multi-user.target\n"
        assert pre().preprocess_unit_file(content, "x.timer") == content.replace(
            "Unit=x.service", "Unit=app--x.service"
        )

    def test_existing_install_without_wanted_by_gets_it(self):
        content = "[Timer]\nOnCalendar=daily\nUnit=x.service\n\n[Install]\nAlso=y.timer\n"
        assert pre().preprocess_unit_file(content, "x.timer") == (
            "[Timer]\nOnCalendar=daily\nUnit=app--x.service\n\n"
            # Pinned: Also= is not one of the prefixed directives
            "[Install]\nAlso=y.timer\nWantedBy=timers.target\n"
        )

    def test_dependencies_are_prefixed(self):
        content = "[Unit]\nAfter=db.service\nWants=db.service\n\n[Timer]\nOnCalendar=daily\n"
        out = pre().preprocess_unit_file(content, "x.timer")
        assert out.startswith("[Unit]\nAfter=app--db.service\nWants=app--db.service\n")

    def test_comments_and_blank_lines_are_untouched(self):
        content = "# Nightly\n\n[Timer]\n# Unit=a.container\nOnCalendar=daily\n"
        out = pre().preprocess_unit_file(content, "x.timer")
        assert out.startswith("# Nightly\n\n[Timer]\n# Unit=a.container\n")

    def test_typical_timer(self):
        """The usual shape: no Unit=, no [Install], relying on both being filled in."""
        content = "[Unit]\nDescription=Renew\n\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n"
        out = pre(**{"renew-certs": ".container"}).preprocess_unit_file(content, "renew-certs.timer")
        assert out == (
            "[Unit]\nDescription=Renew\n\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\n"
            "Persistent=true\nUnit=app--renew-certs.service\n" + INSTALL
        )


def test_non_timer_units_only_get_prefixed():
    # units/ only accepts .timer today, but the method handles any unit type
    content = "[Unit]\nAfter=db.service\n\n[Service]\nExecStart=/bin/true\n"
    assert pre().preprocess_unit_file(content, "x.service") == (
        "[Unit]\nAfter=app--db.service\n\n[Service]\nExecStart=/bin/true\n"
    )
