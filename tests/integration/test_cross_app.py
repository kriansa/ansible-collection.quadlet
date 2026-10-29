"""L. Apps joining a network another app owns."""

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def network_owner(make_app):
    owner = make_app("ing")
    owner.write("quadlets/main.container", f"[Container]\n{IMAGE}\n{IDLE}\nNetwork=main.network\n")
    owner.write("quadlets/main.network", "[Network]\n")
    return owner


def joiner(make_app, owner, network_line=None):
    app = make_app("web")
    line = network_line or f"Network={owner.name}--main"
    app.write("quadlets/main.container", f"[Container]\n{IMAGE}\n{IDLE}\n{line}\n")
    return app


def test_joining_another_apps_network(make_app, host):
    owner = network_owner(make_app)
    owner.deploy("started")
    web = joiner(make_app, owner)

    web.deploy("started")

    networks = host.inspect(web.container("main"))["NetworkSettings"]["Networks"]
    assert f"{owner.name}--main" in networks
    lookup = host.container_exec(web.container("main"), f"nslookup {owner.container('main')}", check=False)
    assert lookup.returncode == 0, lookup.stdout + lookup.stderr


def test_joining_before_the_network_exists(make_app, host):
    """Pinned: ordering between apps is the playbook's job."""
    owner = network_owner(make_app)
    web = joiner(make_app, owner)

    step = web.deploy("started")

    assert step.failed
    assert host.active_state(web.service("main")) != "active"


def test_joining_by_the_other_apps_quadlet_name(make_app, host):
    """"<other>--main.network" is not a file of this app, so it is left as written."""
    owner = network_owner(make_app)
    owner.deploy("started")
    web = joiner(make_app, owner, network_line=f"Network={owner.name}--main.network")

    step = web.deploy("started")

    assert not step.failed, step.msg
    assert f"Network={owner.name}--main.network\n" in host.read(web.quadlet_path("main.container"))
    networks = host.inspect(web.container("main"))["NetworkSettings"]["Networks"]
    assert f"{owner.name}--main" in networks


def test_restarting_the_owner_leaves_the_joiner_alone(make_app, host):
    owner = network_owner(make_app)
    owner.deploy("started")
    web = joiner(make_app, owner)
    web.deploy("started")

    step = owner.deploy("restarted")

    assert step.starts(owner.service("main")) == 1
    assert step.starts(web.service("main")) == 0
