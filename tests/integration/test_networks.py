"""N. An app's networks: created when podman lacks them, and retried when they failed to start."""

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def container(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def networks_of(host, app) -> set:
    return set(host.inspect(app.container("main"))["NetworkSettings"]["Networks"])


def test_a_renamed_network_is_created_and_main_joins_it(make_app, host):
    """
    The network's unit stays active once it has run, so main requiring it would never run it
    again: the deploy has to, or main starts on a network that does not exist.
    """
    app = make_app("rename")
    app.write("quadlets/main.container", container("Network=main.network\n"))
    app.write("quadlets/main.network", "[Network]\n")
    assert not app.deploy("started").failed

    app.write("quadlets/main.network", f"[Network]\nNetworkName={app.name}--v2\n")
    step = app.deploy("started")

    assert not step.failed, step.msg
    assert host.active_state(app.service("main")) == "active"
    assert networks_of(host, app) == {f"{app.name}--v2"}


def test_a_network_removed_by_hand_is_created_again(make_app, host):
    app = make_app("netgone")
    app.write("quadlets/main.container", container("Network=main.network\n"))
    app.write("quadlets/main.network", "[Network]\n")
    assert not app.deploy("started").failed

    # Takes the container using it down too
    host.run(f"podman network rm -f {app.name}--main")
    host.run(f"systemctl stop {app.service('main')}", check=False)

    step = app.deploy("started")

    assert not step.failed, step.msg
    assert host.run(f"podman network exists {app.name}--main", check=False).returncode == 0
    assert host.active_state(app.service("main")) == "active"


def test_the_stack_retries_a_network_that_failed_to_start(make_stack, host):
    """
    A network whose definition cannot start would otherwise stay on the host as it is, failing
    every run, since only its own app's deploy - which never comes - rewrites a file there.
    """
    stack = make_stack
    app = stack.app("collide")
    # Named after the app, so its cleanup removes it
    host.run(f"podman network create --subnet 10.89.71.0/24 {app.name}--taken")
    app.write("quadlets/main.container", container("Network=main.network\n"))
    app.write("quadlets/main.network", "[Network]\nSubnet=10.89.71.0/24\n")

    assert stack.deploy().failed

    app.write("quadlets/main.network", "[Network]\nSubnet=10.89.72.0/24\n")
    run = stack.deploy()

    assert not run.failed, run.run.output
    assert "10.89.72.0/24" in host.read(app.quadlet_path("main.network"))
    assert host.active_state(app.service("main")) == "active"
