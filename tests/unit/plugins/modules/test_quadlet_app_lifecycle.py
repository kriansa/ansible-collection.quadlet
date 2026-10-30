"""
QuadletAppModule.run(): which files are written and which systemd commands run, per situation.

Files go to a temporary host directory (fake_host) and systemd is faked (systemd), so a second
run sees what the first one wrote, the way it would on a real host.
"""

import os

import pytest

from ansible_collections.kriansa.quadlet.plugins.modules import quadlet_app
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import AnsibleFailJson, FakeModule
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    make_files_data,
    run_module,
)

MAIN = "[Unit]\nDescription=main\n\n[Container]\nImage=alpine\nVolume=data.volume:/data\n"
DB = "[Container]\nImage=alpine\n"
DATA = "[Volume]\nX-Owner=1000:1000\n"
JOB = "[Container]\nImage=alpine\n\n[Service]\nType=oneshot\n"
TIMER = "[Timer]\nOnCalendar=daily\n"

MAIN_SVC = "app--main.service"
INIT_SVC = "app--data-volume-init.service"


def app(**overrides):
    quadlets = {"main.container": MAIN, "db.container": DB, "data.volume": DATA}
    quadlets.update(overrides.pop("quadlets", {}))
    return make_files_data(quadlets=quadlets, **overrides)


def with_timer(quadlets=None, units=None, **overrides):
    return app(
        quadlets={"job.container": JOB, **(quadlets or {})},
        units={"job.timer": TIMER, **(units or {})},
        **overrides,
    )


@pytest.fixture
def deployed(fake_host, systemd):
    """An app already deployed and running, with the call log cleared."""
    outcome = run_module(app(), state="started")
    assert outcome.changed
    systemd.calls.clear()
    return systemd


class TestFreshHost:
    def test_installed(self, fake_host, systemd):
        outcome = run_module(app(), state="installed")

        assert outcome.exit == {
            "changed": True,
            "application_name": "app",
            "service_name": MAIN_SVC,
            "quadlet_files": [
                "app--data.volume",
                "app--db.container",
                "app--main.container",
                "app--data-volume-init.container",
            ],
            "unit_files": [],
            "msg": "quadlet files deployed",
        }
        # No bootstrap: it runs when main is first started, and systemd does that on its own
        assert systemd.log == [
            "generator",
            "daemon-reload",
        ]
        assert sorted(fake_host.files()) == [
            "etc/containers/systemd/app--data-volume-init.container",
            "etc/containers/systemd/app--data.volume",
            "etc/containers/systemd/app--db.container",
            "etc/containers/systemd/app--main.container",
        ]

    def test_started(self, fake_host, systemd):
        outcome = run_module(app(), state="started")

        assert outcome.msg == "quadlet files deployed and service started"
        assert systemd.log == [
            f"is-active {MAIN_SVC}",
            "generator",
            "daemon-reload",
            f"list-dependencies --all --type service {MAIN_SVC}",
            # Restarting a unit that is not running starts it
            f"restart {INIT_SVC} {MAIN_SVC}",
        ]
        assert os.listdir(fake_host.state_dir) == ["app--data.1000_1000.done"]

    def test_restarted(self, fake_host, systemd):
        systemd.dependencies[MAIN_SVC] = ["app--db.service", INIT_SVC]
        outcome = run_module(app(), state="restarted")

        assert outcome.msg == "quadlet files deployed and service restarted"
        assert systemd.log == [
            "generator",
            "daemon-reload",
            f"list-dependencies --all --type service {MAIN_SVC}",
            # One transaction: pending bootstrap units first, then dependencies, then main
            f"restart {INIT_SVC} app--db.service {MAIN_SVC}",
        ]

    def test_files_are_written_with_their_contents(self, fake_host, systemd):
        run_module(
            app(init={"main": {"seed.sql": "SELECT 1;\n"}},
                config={"main": {"sub/app.conf": "x=1\n"}}),
            state="installed",
        )
        files = fake_host.files()
        assert files["srv/app/init/main/seed.sql"] == "SELECT 1;\n"
        assert files["srv/app/config/main/sub/app.conf"] == "x=1\n"
        assert files["etc/containers/systemd/app--data.volume"] == (
            "[Volume]\nVolumeName=app--data\nUser=1000\nGroup=1000\n"
        )


class TestUnchanged:
    def test_installed_is_a_no_op(self, deployed):
        outcome = run_module(app(), state="installed")
        assert outcome.exit["changed"] is False
        assert outcome.msg == "application already up to date"
        assert deployed.log == []

    def test_started_and_running_is_a_no_op(self, deployed):
        outcome = run_module(app(), state="started")
        assert outcome.exit["changed"] is False
        assert deployed.log == [f"is-active {MAIN_SVC}"]

    def test_started_but_stopped_only_starts(self, deployed):
        deployed.active.discard(MAIN_SVC)
        outcome = run_module(app(), state="started")
        assert outcome.changed
        # No reload and no generator run: no file changed
        assert deployed.log == [
            f"is-active {MAIN_SVC}",
            f"is-active {MAIN_SVC}",
            f"start {MAIN_SVC}",
        ]

    def test_restarted_always_restarts(self, deployed):
        deployed.dependencies[MAIN_SVC] = ["app--db.service"]
        for _attempt in range(2):
            deployed.calls.clear()
            outcome = run_module(app(), state="restarted")
            assert outcome.changed
            assert deployed.state_changes == [["restart", "app--db.service", MAIN_SVC]]

    def test_applied_bootstrap_units_are_not_restarted(self, deployed):
        # main requires its bootstrap unit, so the dependency walk finds it - and it is left out
        deployed.dependencies[MAIN_SVC] = [INIT_SVC, "app--db.service"]
        run_module(app(), state="restarted")
        assert deployed.state_changes == [["restart", "app--db.service", MAIN_SVC]]

    def test_started_reruns_a_bootstrap_whose_marker_is_gone(self, deployed, fake_host):
        """Deleting the marker is how the docs say to re-run the chown, and a deploy does it."""
        (fake_host.state_dir / "app--data.1000_1000.done").unlink()

        outcome = run_module(app(), state="started")

        assert outcome.changed
        assert deployed.state_changes == [["restart", INIT_SVC]]
        assert os.listdir(fake_host.state_dir) == ["app--data.1000_1000.done"]

    def test_started_but_stopped_runs_a_pending_bootstrap_first(self, deployed, fake_host):
        # The bootstrap unit stays active after running, so starting main would not run it again
        deployed.active.discard(MAIN_SVC)
        (fake_host.state_dir / "app--data.1000_1000.done").unlink()

        run_module(app(), state="started")

        assert deployed.state_changes == [["restart", INIT_SVC], ["start", MAIN_SVC]]

    def test_installed_leaves_a_pending_bootstrap_alone(self, deployed, fake_host):
        (fake_host.state_dir / "app--data.1000_1000.done").unlink()

        outcome = run_module(app(), state="installed")

        assert outcome.exit["changed"] is False
        assert deployed.state_changes == []


class TestChanged:
    def test_started_and_running_restarts_the_app_once(self, deployed):
        deployed.dependencies[MAIN_SVC] = ["app--db.service", INIT_SVC]
        outcome = run_module(app(quadlets={"db.container": DB + "Environment=V=2\n"}), state="started")

        assert outcome.changed
        assert deployed.state_changes == [
            ["daemon-reload"],
            ["restart", "app--db.service", MAIN_SVC],
        ]

    def test_installed_while_running_does_not_restart(self, deployed):
        # Pinned: the new files are in place but the running containers keep the old ones
        outcome = run_module(app(quadlets={"db.container": DB + "Environment=V=2\n"}), state="installed")
        assert outcome.changed
        assert deployed.state_changes == [["daemon-reload"]]

    def test_started_but_stopped_restarts_the_app_too(self, deployed):
        """db may still be running its old definition; starting main alone would leave it so."""
        deployed.active.discard(MAIN_SVC)
        deployed.dependencies[MAIN_SVC] = ["app--db.service", INIT_SVC]
        run_module(app(quadlets={"db.container": DB + "Environment=V=2\n"}), state="started")
        assert deployed.state_changes == [
            ["daemon-reload"],
            ["restart", "app--db.service", MAIN_SVC],
        ]

    def test_owner_change_reruns_its_bootstrap_unit_in_the_same_transaction(self, deployed):
        deployed.dependencies[MAIN_SVC] = [INIT_SVC]
        run_module(app(quadlets={"data.volume": "[Volume]\nX-Owner=2000:2000\n"}), state="started")
        assert deployed.state_changes == [["daemon-reload"], ["restart", INIT_SVC, MAIN_SVC]]

    def test_running_state_is_read_before_anything_restarts(self, deployed):
        """
        Regression test: restarting a bootstrap unit restarts main too, and main reads as
        `activating` for a while. Had the module asked whether main runs only after that, it
        would have started main instead of restarting the whole app.
        """
        deployed.dependencies[MAIN_SVC] = [INIT_SVC, "app--db.service"]

        def init_restart_takes_main_down(fake, args):
            if args[0] == "restart" and INIT_SVC in args:
                fake.active.discard(MAIN_SVC)

        deployed.on_command = init_restart_takes_main_down
        run_module(
            app(quadlets={"data.volume": "[Volume]\nX-Owner=2000:2000\n",
                          "db.container": DB + "Environment=V=2\n"}),
            state="started",
        )
        assert deployed.state_changes == [
            ["daemon-reload"],
            ["restart", INIT_SVC, "app--db.service", MAIN_SVC],
        ]

    def test_owner_change_under_installed_waits_for_the_next_start(self, deployed, fake_host):
        """
        Re-running a bootstrap unit restarts every container mounting its volume, which installed
        must not do. The next run allowed to restart them applies the new owner.
        """
        changed = app(quadlets={"data.volume": "[Volume]\nX-Owner=2000:2000\n"})

        run_module(changed, state="installed")

        assert deployed.state_changes == [["daemon-reload"]]
        # The marker still says what the volume is actually owned by
        assert os.listdir(fake_host.state_dir) == ["app--data.1000_1000.done"]

        deployed.calls.clear()
        outcome = run_module(changed, state="started")

        assert outcome.changed
        assert deployed.state_changes == [["restart", INIT_SVC]]
        assert os.listdir(fake_host.state_dir) == ["app--data.2000_2000.done"]

    def test_the_bootstrap_replaces_the_previous_marker(self, deployed, fake_host):
        run_module(app(quadlets={"data.volume": "[Volume]\nX-Owner=2000:2000\n"}), state="started")
        assert os.listdir(fake_host.state_dir) == ["app--data.2000_2000.done"]

    def test_the_module_never_removes_a_marker_itself(self, deployed, fake_host):
        stale = fake_host.state_dir / "app--data.5_5.done"
        stale.write_text("")

        run_module(app(), state="started", force=True)

        # Only the bootstrap unit removes markers, and it had nothing to do
        assert stale.exists()


class TestForce:
    def test_rewrites_and_restarts_everything(self, deployed):
        deployed.dependencies[MAIN_SVC] = ["app--db.service", INIT_SVC]
        outcome = run_module(app(), state="started", force=True)
        assert outcome.changed
        # Except the bootstrap unit: its marker is in place, so it would only be skipped
        assert deployed.state_changes == [
            ["daemon-reload"],
            ["restart", "app--db.service", MAIN_SVC],
        ]

    def test_restarts_timers(self, fake_host, systemd):
        run_module(with_timer(), state="installed")
        systemd.calls.clear()
        run_module(with_timer(), state="installed", force=True)
        assert ["restart", "app--job.timer"] in systemd.state_changes


class TestTimers:
    def test_fresh_timer_is_enabled_and_started_under_installed(self, fake_host, systemd):
        outcome = run_module(with_timer(), state="installed")
        assert outcome.exit["unit_files"] == ["app--job.timer"]
        assert systemd.log == [
            "is-enabled app--job.timer",
            "generator",
            "daemon-reload",
            "is-enabled app--job.timer",
            "enable app--job.timer",
            # Changed, so restarted without asking whether it runs
            "restart app--job.timer",
        ]
        assert fake_host.files()["etc/systemd/system/app--job.timer"] == (
            "[Timer]\nOnCalendar=daily\nUnit=app--job.service\n\n[Install]\nWantedBy=timers.target\n"
        )

    def test_healthy_timer_is_left_alone(self, fake_host, systemd):
        run_module(with_timer(), state="installed")
        systemd.calls.clear()
        outcome = run_module(with_timer(), state="installed")
        assert outcome.exit["changed"] is False
        assert systemd.log == [
            "is-enabled app--job.timer",
            "is-active app--job.timer",
        ]

    def test_stopped_timer_is_restarted(self, fake_host, systemd):
        run_module(with_timer(), state="installed")
        systemd.active.discard("app--job.timer")
        systemd.calls.clear()
        outcome = run_module(with_timer(), state="installed")
        assert outcome.changed
        assert systemd.state_changes == [["restart", "app--job.timer"]]

    def test_disabled_timer_is_enabled(self, fake_host, systemd):
        run_module(with_timer(), state="installed")
        systemd.enabled.discard("app--job.timer")
        systemd.calls.clear()
        outcome = run_module(with_timer(), state="installed")
        assert outcome.changed
        # It is still active, so enabling is all it needs
        assert systemd.state_changes == [["enable", "app--job.timer"]]

    def test_edited_timer_is_restarted(self, fake_host, systemd):
        run_module(with_timer(), state="started")
        systemd.calls.clear()
        run_module(with_timer(units={"job.timer": "[Timer]\nOnCalendar=hourly\n"}), state="started")
        assert ["restart", "app--job.timer"] in systemd.state_changes

    def test_unrelated_change_does_not_restart_the_timer(self, fake_host, systemd):
        run_module(with_timer(), state="started")
        systemd.calls.clear()
        run_module(with_timer(quadlets={"db.container": DB + "Environment=V=2\n"}), state="started")
        assert not any(change[0] == "restart" and "app--job.timer" in change
                       for change in systemd.state_changes)

    def test_timers_are_reconciled_in_sorted_order(self, fake_host, systemd):
        files = app(units={"b.timer": TIMER, "a.timer": TIMER})
        run_module(files, state="installed")
        enables = [args[1] for args in systemd.state_changes if args[0] == "enable"]
        assert enables == ["app--a.timer", "app--b.timer"]

    def test_a_quadlet_can_name_a_timer_of_the_app(self, fake_host, systemd):
        files = with_timer(quadlets={"main.container": "[Unit]\nAfter=sshd.service job.timer\n\n"
                                                       "[Container]\nImage=alpine\n"})
        run_module(files, state="installed")
        main = fake_host.files()["etc/containers/systemd/app--main.container"]
        assert "After=sshd.service app--job.timer\n" in main


class TestValidationInRun:
    def test_invalid_name(self, fake_host, systemd):
        outcome = run_module(app(), src="/apps/1bad")
        assert outcome.failure["msg"].startswith("Invalid application name: 1bad.")
        assert systemd.calls == []
        assert fake_host.files() == {}

    def test_name_parameter_overrides_src_and_is_lowercased(self, fake_host, systemd):
        outcome = run_module(app(), src="/apps/ignored", name="MyApp", state="installed")
        assert outcome.exit["application_name"] == "myapp"
        assert outcome.exit["service_name"] == "myapp--main.service"

    @pytest.mark.parametrize("src", ["/apps/app/", "/apps/app//", "apps/app/."])
    def test_trailing_slash_src_without_name(self, fake_host, systemd, src):
        outcome = run_module(app(), src=src, state="installed")
        assert outcome.exit["application_name"] == "app"

    def test_name_with_a_trailing_newline(self, fake_host, systemd):
        outcome = run_module(app(), name="app\n", state="installed")
        assert outcome.failure["msg"].startswith("Invalid application name: app\n.")
        assert fake_host.files() == {}

    def test_invalid_owner_writes_nothing(self, fake_host, systemd):
        outcome = run_module(app(quadlets={"data.volume": "[Volume]\nX-Owner=70000:0\n"}))
        assert outcome.failure["msg"].startswith("X-Owner in data.volume is out of range")
        assert fake_host.files() == {}
        assert systemd.calls == []

    def test_content_that_cannot_be_written_is_reported(self, fake_host, systemd):
        # The action plugin only ever sends text; this is for anything that gets past it
        files = app(config={"main": {"n": 42}})
        outcome = run_module(files, state="installed")
        path = fake_host.srv_dir / "app/config/main/n"
        assert outcome.failure == {
            "msg": f"Failed to write file {path}: write() argument must be str, not int"
        }
        assert not path.exists()


def test_success_message_for_an_unknown_state(fake_host, systemd):
    # The argument spec only allows the three states; this is for anything that gets past it
    outcome = run_module(app(), state="other")
    assert outcome.changed
    assert outcome.msg == "quadlet files deployed"


def test_anything_else_that_goes_wrong_is_reported_as_unexpected(fake_host, systemd):
    # The action plugin only ever sends these four kinds of file; this is for anything that gets
    # past it
    fake = FakeModule({
        "src": "/apps/app", "name": None, "state": "installed", "force": False,
        "systemctl_timeout": 120, "settle_time": 0, "_control_node_processed": True,
        "_files": [{"kind": "bogus", "name": "x"}], "_contents": [""],
    })
    with pytest.raises(AnsibleFailJson) as excinfo:
        quadlet_app.QuadletAppModule(fake).run()
    assert excinfo.value.result == {"msg": "Unexpected error: 'bogus'"}
    assert fake_host.files() == {}
    assert systemd.calls == []


def test_single_letter_name(fake_host, systemd):
    outcome = run_module(app(), src="/apps/a", state="installed")
    assert outcome.exit["application_name"] == "a"


def test_main_coming_up_on_its_own_reports_no_change(deployed):
    """
    Pinned race: main reads as stopped when the module decides it has work to do, and as
    running by the time it would start it. Nothing is done, and the run says so.
    """
    deployed.scripted_states[MAIN_SVC] = ["inactive", "active"]
    outcome = run_module(app(), state="started")
    assert outcome.exit["changed"] is False
    assert outcome.msg == "quadlet files deployed and service started"
    assert deployed.state_changes == []
