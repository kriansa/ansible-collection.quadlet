"""
The settle check: once a run has started or restarted the app, the services it started have to
stay up for settle_time seconds, or the task fails.

Podman tells systemd a container is ready as soon as it starts, so `systemctl start` succeeds even
for a container whose process dies a moment later.
"""

from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    make_files_data,
    run_module,
)

MAIN = "[Unit]\nWants=db.service\n\n[Container]\nImage=alpine\n"
DB = "[Container]\nImage=alpine\n"
MAIN_SVC = "app--main.service"
DB_SVC = "app--db.service"
SHOW = "show -p Id,Type,ActiveState,SubState,InvocationID"


def app(**quadlets):
    return make_files_data(quadlets={"main.container": MAIN, "db.container": DB, **quadlets})


def deploy(systemd, state="started", settle_time=5, **kwargs):
    systemd.dependencies["app--main.service"] = [DB_SVC]
    return run_module(app(**kwargs), state=state, settle_time=settle_time)


def test_services_that_stay_up_pass(fake_host, systemd):
    outcome = deploy(systemd)
    assert outcome.changed
    assert systemd.sleeps == [5]


def test_a_container_that_exits_right_after_starting_fails_the_task(fake_host, systemd):
    systemd.exits_after_start.add(MAIN_SVC)
    systemd.journal[MAIN_SVC] = "boom\napp--main.service: Failed with result 'exit-code'.\n"

    outcome = deploy(systemd)

    assert outcome.failure == {
        "msg": "app--main.service did not stay running after it was started: it is failed. "
        "Its last log lines:\nboom\napp--main.service: Failed with result 'exit-code'."
    }


def test_a_dependency_that_keeps_crashing_fails_the_task(fake_host, systemd):
    systemd.crash_loops.add(DB_SVC)

    outcome = deploy(systemd)

    assert outcome.failure == {
        "msg": "app--db.service did not stay running after it was started: systemd had to start "
        "it again."
    }


def test_every_service_that_did_not_stay_up_is_reported(fake_host, systemd):
    systemd.exits_after_start.update({MAIN_SVC, DB_SVC})
    outcome = deploy(systemd)
    assert outcome.failure["msg"] == (
        "app--db.service did not stay running after it was started: it is failed.\n\n"
        "app--main.service did not stay running after it was started: it is failed."
    )


def test_a_oneshot_service_that_finished_is_fine(fake_host, systemd):
    """systemctl waits for a oneshot to finish, and fails itself when one does not succeed."""
    systemd.types[DB_SVC] = "oneshot"

    def finish(fake, args):
        if args[0] == "restart":
            fake.active.discard(DB_SVC)

    systemd.on_command = finish
    assert deploy(systemd).changed


def test_only_the_apps_long_running_services_are_watched(fake_host, systemd):
    deploy(systemd, **{"data.volume": "[Volume]\n", "edge.pod": "[Pod]\n"})
    shows = [line for line in systemd.log if line.startswith(SHOW)]
    services = "app--db.service app--edge-pod.service app--main.service"
    # Before and right after starting them, then the ones started, once the wait is over
    assert shows == [
        f"{SHOW} {services}",
        f"{SHOW} {services}",
        f"{SHOW} app--db.service app--main.service",
    ]


def test_only_services_this_run_started_are_watched(fake_host, systemd):
    deploy(systemd)
    systemd.active.discard(MAIN_SVC)
    systemd.calls.clear()
    systemd.sleeps.clear()

    # Nothing changed and main is stopped: this run starts main, and only main
    outcome = run_module(app(), state="started", settle_time=5)

    assert outcome.changed
    assert systemd.sleeps == [5]
    assert systemd.log[-1] == f"{SHOW} {MAIN_SVC}"


def test_not_under_installed(fake_host, systemd):
    systemd.exits_after_start.add(MAIN_SVC)
    assert deploy(systemd, state="installed").changed
    assert systemd.sleeps == []
    assert not any(line.startswith(SHOW) for line in systemd.log)


def test_not_when_nothing_was_started(fake_host, systemd):
    deploy(systemd)
    systemd.calls.clear()
    systemd.sleeps.clear()

    outcome = run_module(app(), state="started", settle_time=5)

    assert outcome.exit["changed"] is False
    assert systemd.sleeps == []


def test_zero_turns_it_off(fake_host, systemd):
    systemd.exits_after_start.add(MAIN_SVC)
    assert deploy(systemd, settle_time=0).changed
    assert systemd.sleeps == []
    assert not any(line.startswith(SHOW) for line in systemd.log)
