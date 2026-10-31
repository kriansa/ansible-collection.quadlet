"""M. The quadlet_stack role: apps deployed together, their networks first, providers around them."""

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def container(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def network_owner(stack):
    """An app owning a network other apps join."""
    owner = stack.app("ing")
    owner.write("quadlets/main.container", container("Network=main.network\n"))
    owner.write("quadlets/main.network", "[Network]\nSubnet=10.89.70.0/24\n")
    return owner


def joiner(stack, owner, tag="web"):
    """An app joining the owner's network by its quadlet's name."""
    app = stack.app(tag)
    app.write("quadlets/main.container", container(f"Network={owner.name}--main.network\n"))
    return app


def claims(app, *ids, role="qa_provider"):
    """Give an app a manifest claiming these ids from the provider."""
    lines = "".join(f'    - "{claim_id}"\n' for claim_id in ids)
    app.write("application.yml", f'version: "v1"\nresource_claims:\n  {role}:\n{lines}')


class TestNetworksFirst:
    def test_one_app_deploys_where_its_networks_owner_never_was(self, make_stack, host):
        stack = make_stack
        owner = network_owner(stack)
        web = joiner(stack, owner)

        run = stack.deploy(deploy_only=[web.name])

        assert not run.failed, run.run.output
        # The owner's network exists, but nothing else of it was deployed
        assert host.exists(owner.quadlet_path("main.network"))
        assert host.run(f"podman network exists {owner.name}--main", check=False).returncode == 0
        assert not host.exists(owner.quadlet_path("main.container"))
        assert host.active_state(web.service("main")) == "active"
        networks = host.inspect(web.container("main"))["NetworkSettings"]["Networks"]
        assert f"{owner.name}--main" in networks

    def test_a_network_already_there_is_left_for_its_own_app(self, make_stack, host):
        stack = make_stack
        owner = network_owner(stack)
        web = joiner(stack, owner)
        assert not stack.deploy().failed
        deployed = host.read(owner.quadlet_path("main.network"))

        owner.write("quadlets/main.network", "[Network]\nSubnet=10.89.70.0/24\nLabel=v=2\n")
        assert not stack.deploy(deploy_only=[web.name]).failed
        assert host.read(owner.quadlet_path("main.network")) == deployed

        assert not stack.deploy(deploy_only=[owner.name]).failed
        assert "Label=v=2" in host.read(owner.quadlet_path("main.network"))

    def test_deploying_the_owner_leaves_the_apps_joined_to_it_running(self, make_stack, host):
        stack = make_stack
        owner = network_owner(stack)
        web = joiner(stack, owner)
        assert not stack.deploy().failed
        before = host.invocation_id(web.service("main"))
        owner_before = host.invocation_id(owner.service("main"))

        owner.write("quadlets/main.container", container("Network=main.network\nEnvironment=V=2\n"))
        run = stack.deploy(deploy_only=[owner.name])

        assert not run.failed, run.run.output
        assert host.invocation_id(owner.service("main")) != owner_before
        assert host.invocation_id(web.service("main")) == before


class TestProviders:
    def test_before_and_after_the_deploy_with_every_apps_claims(self, make_stack, host):
        stack = make_stack
        stack.add_provider()
        first = stack.app("one")
        first.write("quadlets/main.container", container())
        claims(first, "claim-a", "claim-b")
        second = stack.app("two")
        second.write("quadlets/main.container", container())
        claims(second, "claim-c")

        # The tags a playbook gives with apply: reach every task the role includes, the
        # provider's among them
        run = stack.deploy(deploy_only=[first.name], tags=["custom"])

        assert not run.failed, run.run.output
        assert stack.provider_calls() == [
            # Before: nothing deployed yet; every app's claims, the unselected app's included
            f"before {first.name} claim-a 3 inactive",
            f"before {first.name} claim-b 3 inactive",
            f"before {second.name} claim-c 3 inactive",
            # After: the selected app is running, the other was never deployed
            f"after {first.name} claim-a 3 active",
            f"after {first.name} claim-b 3 active",
            f"after {second.name} claim-c 3 inactive",
            "reload",
        ]

    def test_a_provider_that_does_not_exist_stops_the_run_before_any_app(self, make_stack, host):
        stack = make_stack
        app = stack.app("lonely")
        app.write("quadlets/main.container", container())
        claims(app, "x", role="qa_no_such_provider")

        run = stack.deploy()

        assert run.failed
        assert "qa_no_such_provider" in run.run.output
        assert not host.exists(app.quadlet_path("main.container"))


class TestSelection:
    def test_an_unknown_app_stops_the_run_before_anything(self, make_stack, host):
        stack = make_stack
        owner = network_owner(stack)

        run = stack.deploy(deploy_only=["qa-no-such-app"])

        assert run.failed
        assert run.msg == "unknown app(s): qa-no-such-app"
        assert not host.exists(owner.quadlet_path("main.network"))

    def test_the_first_failing_app_stops_the_rest(self, make_stack, host):
        stack = make_stack
        apps = [stack.app(tag) for tag in ("a", "b", "c")]
        for app in apps:
            app.write("quadlets/main.container", container())
        # Only rendered when the app itself is deployed
        apps[1].write("config/main/broken.conf", "{{ qa_undefined_variable }}\n")

        run = stack.deploy()

        assert run.failed
        assert run.msg.startswith(
            f"Deploying {apps[1].name} failed, and the apps after it were not deployed. "
            f"Deployed before it: {apps[0].name}. The error: "
        )
        assert "qa_undefined_variable" in run.msg
        assert host.active_state(apps[0].service("main")) == "active"
        assert not host.exists(apps[1].quadlet_path("main.container"))
        assert not host.exists(apps[2].quadlet_path("main.container"))


def test_the_manifest_decides_the_main_quadlet_and_the_timeout(make_stack, host):
    stack = make_stack
    app = stack.app("manifest")
    app.write("quadlets/web.container", container())
    app.write("quadlets/sidecar.container", container())
    app.write("application.yml", 'version: "v1"\nmain_container: web\nsystemctl_timeout: 77\n')

    run = stack.deploy()

    assert not run.failed, run.run.output
    deployed = run.results("kriansa.quadlet.quadlet_app")[-1].result
    assert deployed["service_name"] == app.service("web")
    assert deployed["invocation"]["module_args"]["_systemctl_timeout"] == 77
    assert host.active_state(app.service("web")) == "active"
    # Not started: it is not main, and main does not depend on it
    assert host.active_state(app.service("sidecar")) != "active"
