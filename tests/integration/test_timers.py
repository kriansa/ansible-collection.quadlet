"""E. Timers from units/: enabled, running, restarted only when their own file changes, and firing."""

import time

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

MAIN = f"[Container]\n{IMAGE}\n{IDLE}\n"
JOB = f"[Container]\n{IMAGE}\nExec=/bin/true\n\n[Service]\nType=oneshot\n"
FAR_FUTURE = "[Timer]\nOnCalendar=2099-01-01\n"


def timer_app(make_app, tag, timer=FAR_FUTURE):
    app = make_app(tag)
    app.write("quadlets/main.container", MAIN)
    app.write("quadlets/job.container", JOB)
    app.write("units/job.timer", timer)
    return app


def timer_unit(app, stem="job"):
    return f"{app.name}--{stem}.timer"


def timer_state(host, app):
    return host.unit(timer_unit(app), "InvocationID", "ActiveEnterTimestampMonotonic", "ActiveState",
                     "UnitFileState")


def test_enabled_and_started_even_when_only_installed(make_app, host):
    app = timer_app(make_app, "timerinst")

    step = app.deploy("installed")

    assert step.result.result["unit_files"] == [timer_unit(app)]
    state = timer_state(host, app)
    assert (state["ActiveState"], state["UnitFileState"]) == ("active", "enabled")
    assert host.read(f"/etc/systemd/system/{timer_unit(app)}") == (
        f"[Timer]\nOnCalendar=2099-01-01\nUnit={app.name}--job.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    assert host.exists(f"/etc/systemd/system/timers.target.wants/{timer_unit(app)}")
    assert host.active_state(app.service("main")) == "inactive"


def test_untouched_by_a_run_with_nothing_to_do(make_app, host):
    app = timer_app(make_app, "timernoop")
    app.deploy("started")
    before = timer_state(host, app)

    step = app.deploy("started")

    assert not step.changed
    assert timer_state(host, app) == before


def test_untouched_by_an_unrelated_change(make_app, host):
    app = timer_app(make_app, "timerunrel")
    app.deploy("started")
    before = timer_state(host, app)

    app.write("quadlets/main.container", MAIN + "Environment=V=2\n")
    step = app.deploy("started")

    assert step.starts(app.service("main")) == 1
    assert timer_state(host, app)["InvocationID"] == before["InvocationID"]


def test_edited_schedule_restarts_it_once(make_app, host):
    app = timer_app(make_app, "timeredit")
    app.deploy("started")

    app.write("units/job.timer", "[Timer]\nOnCalendar=2098-06-01\n")
    step = app.deploy("started")

    assert step.starts(timer_unit(app)) == 1
    assert "2098-06-01" in host.unit(timer_unit(app), "TimersCalendar")["TimersCalendar"]


def test_stopped_by_hand_is_started_again(make_app, host):
    app = timer_app(make_app, "timerstop")
    app.deploy("installed")
    host.run(f"systemctl stop {timer_unit(app)}")

    step = app.deploy("installed")

    assert step.changed
    assert timer_state(host, app)["ActiveState"] == "active"


def test_disabled_by_hand_is_enabled_again(make_app, host):
    app = timer_app(make_app, "timerdisable")
    app.deploy("installed")
    host.run(f"systemctl disable {timer_unit(app)}")

    step = app.deploy("installed")

    assert step.changed
    assert timer_state(host, app)["UnitFileState"] == "enabled"


@pytest.mark.slow
@pytest.mark.parametrize("unit_line", ["", "Unit=job.container\n"])
def test_it_fires(make_app, host, unit_line):
    app = timer_app(make_app, "timerfire", timer=f"[Timer]\nOnActiveSec=2s\n{unit_line}")

    app.deploy("installed")

    job = app.service("job")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = host.unit(job, "InvocationID", "ActiveState", "Result", "ExecMainStatus")
        if state["InvocationID"] and state["ActiveState"] == "inactive":
            break
        time.sleep(1)
    assert state["InvocationID"], "the job never ran"
    assert (state["Result"], state["ExecMainStatus"]) == ("success", "0")


def test_pod_target_resolves_to_the_pod_service(make_app, host):
    app = make_app("timerpod")
    app.write("quadlets/main.container", MAIN)
    app.write("quadlets/batch.pod", "[Pod]\n")
    app.write("units/batch.timer", FAR_FUTURE)

    app.deploy("installed")

    assert f"Unit={app.name}--batch-pod.service" in host.read(f"/etc/systemd/system/{timer_unit(app, 'batch')}")
    assert host.active_state(timer_unit(app, "batch")) == "active"


def test_removed_timer_keeps_running(make_app, host):
    """Pinned: nothing is ever removed from the host."""
    app = timer_app(make_app, "timerremove")
    app.deploy("installed")

    app.delete("units/job.timer")
    app.deploy("installed")

    state = timer_state(host, app)
    assert (state["ActiveState"], state["UnitFileState"]) == ("active", "enabled")


def test_timer_in_quadlets_is_rejected(make_app, host):
    app = make_app("timermisplaced")
    app.write("quadlets/main.container", MAIN)
    app.write("quadlets/job.timer", FAR_FUTURE)

    step = app.deploy("installed")

    assert step.failed
    assert step.msg == ("Failed to process files on control node: "
                        "job.timer is a systemd unit, not a quadlet. Move it to units/.")
    assert not host.exists(app.quadlet_path("main.container"))


def test_stray_file_in_units_is_rejected(make_app, host):
    app = make_app("timerstray")
    app.write("quadlets/main.container", MAIN)
    app.write("units/job.service", "[Service]\nExecStart=/bin/true\n")

    step = app.deploy("installed")

    assert step.failed
    assert step.msg == ("Failed to process files on control node: "
                        "Unsupported file in units/: job.service. Only .timer files are supported.")


def test_timer_without_timer_section(make_app, host):
    """Pinned: deployed and enabled, but systemd refuses to start a timer with nothing to wait for."""
    app = timer_app(make_app, "timernosection", timer="[Unit]\nDescription=broken\n")

    step = app.deploy("installed")

    assert step.failed
    assert step.msg.startswith(f"systemctl restart {timer_unit(app)} failed:")
    assert "has a bad unit file setting" in step.msg
    assert host.exists(f"/etc/systemd/system/{timer_unit(app)}")


def test_force_restarts_timers(make_app, host):
    app = timer_app(make_app, "timerforce")
    app.deploy("installed")

    step = app.deploy("installed", args={"force": True})

    assert step.starts(timer_unit(app)) == 1
