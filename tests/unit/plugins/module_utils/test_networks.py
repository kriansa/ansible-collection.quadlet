"""
AppNetworks: the networks of one app, created before any app is deployed.

Files go to a temporary host directory (fake_host) and systemd is faked (systemd).
"""

import pytest

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.networks import AppNetworks
from ansible_collections.kriansa.quadlet.plugins.module_utils.systemctl import Systemd

FRONT = "[Network]\nSubnet=10.0.0.0/24\n"
BACK = "[Network]\nInternal=true\n"


def make_app(front=FRONT):
    app = QuadletApp("app")
    app.add_quadlet("main.container", "[Container]\nImage=alpine\nNetwork=front.network\n")
    app.add_quadlet("front.network", front)
    app.add_quadlet("back.network", BACK)
    app.add_quadlet("data.volume", "[Volume]\n")
    return app


def networks(app=None, timeout=300):
    return AppNetworks(app or make_app(), Systemd(timeout))


class TestCreate:
    def test_fresh_host(self, fake_host, systemd):
        assert networks().create() is True

        # Only the networks: the app's other files are its own deploy's to write
        assert fake_host.files() == {
            "etc/containers/systemd/app--back.network": "[Network]\nNetworkName=app--back\nInternal=true\n",
            "etc/containers/systemd/app--front.network": "[Network]\nNetworkName=app--front\nSubnet=10.0.0.0/24\n",
        }
        assert systemd.log == [
            "generator",
            "daemon-reload",
            "is-active app--front-network.service",
            "is-active app--back-network.service",
            "start app--front-network.service app--back-network.service",
        ]
        # The generator judged the networks alone
        assert sorted(systemd.generator_calls[0].staged) == [
            "app--back.network",
            "app--front.network",
        ]

    def test_the_apps_timeout(self, fake_host, systemd):
        networks(timeout=42).create()
        changing = [
            call
            for call in systemd.calls
            if call.systemctl is not None and call.systemctl[0] != "is-active"
        ]
        assert [call.timeout for call in changing] == [42, 42]

    def test_settled(self, fake_host, systemd):
        networks().create()
        systemd.calls.clear()

        assert networks().create() is False
        assert systemd.state_changes == []
        assert systemd.generator_calls == []

    def test_a_changed_network_on_the_host_is_left_alone(self, fake_host, systemd):
        networks().create()
        systemd.calls.clear()

        app = QuadletApp("app")
        app.add_quadlet("front.network", "[Network]\nSubnet=10.9.0.0/24\n")
        app.add_quadlet("back.network", BACK)

        assert networks(app).create() is False
        assert "10.0.0.0/24" in fake_host.files()["etc/containers/systemd/app--front.network"]
        assert systemd.state_changes == []

    def test_only_the_missing_one_is_written(self, fake_host, systemd):
        networks().create()
        (fake_host.quadlet_dir / "app--back.network").unlink()
        systemd.calls.clear()

        assert networks().create() is True
        assert sorted(systemd.generator_calls[0].staged) == ["app--back.network"]
        assert systemd.state_changes == [["daemon-reload"]]

    def test_a_stopped_network_is_started_without_writing(self, fake_host, systemd):
        networks().create()
        systemd.active.discard("app--back-network.service")
        systemd.calls.clear()

        assert networks().create() is True
        assert systemd.state_changes == [["start", "app--back-network.service"]]
        assert systemd.generator_calls == []

    def test_an_app_without_networks(self, fake_host, systemd):
        app = QuadletApp("app")
        app.add_quadlet("main.container", "[Container]\n")
        assert networks(app).create() is False
        assert systemd.calls == []
        assert fake_host.files() == {}

    def test_a_network_that_failed_to_start_gets_its_fixed_file(self, fake_host, systemd):
        """
        A definition that cannot start - a subnet another network has, say - would otherwise stay
        on the host, failing every run, since only the app's own deploy rewrites a file there.
        """
        systemd.failures[("start",)] = (1, "subnet in use")
        with pytest.raises(QuadletAppError, match="subnet in use"):
            networks().create()
        del systemd.failures[("start",)]

        app = make_app(front="[Network]\nSubnet=10.9.0.0/24\n")
        assert networks(app).create() is True
        assert "10.9.0.0/24" in fake_host.files()["etc/containers/systemd/app--front.network"]

    def test_each_unit_is_asked_about_once(self, fake_host, systemd):
        # Whether to rewrite a network's file, and then whether to start it: writing the file and
        # reloading systemd leave the answer as it was
        networks().create()
        systemd.active.clear()
        systemd.calls.clear()

        networks(make_app(front="[Network]\nSubnet=10.9.0.0/24\n")).create()

        assert [line for line in systemd.log if line.startswith("is-active")] == [
            "is-active app--front-network.service",
            "is-active app--back-network.service",
        ]
        assert systemd.state_changes == [
            ["daemon-reload"],
            ["start", "app--front-network.service", "app--back-network.service"],
        ]

    def test_a_network_podman_lost_is_created_again(self, fake_host, systemd):
        networks().create()
        systemd.lost_networks.add("app--back")
        systemd.calls.clear()

        assert networks().create() is True
        # Its unit still runs, so starting it would do nothing
        assert systemd.state_changes == [["restart", "app--back-network.service"]]

    def test_a_running_network_renamed_by_its_app_is_left_alone(self, fake_host, systemd):
        """
        Its unit creates the name the host's file has until its own app is deployed. Asked about
        the new name, podman would never have it, and the network - with every container joined
        to it - would be restarted on every run, to create the old name again.
        """
        networks().create()
        systemd.lost_networks.add("front-v2")
        systemd.calls.clear()

        renamed = make_app(front="[Network]\nNetworkName=front-v2\n")
        assert networks(renamed).create() is False
        assert systemd.state_changes == []
        assert "front-v2" not in fake_host.files()["etc/containers/systemd/app--front.network"]

    def test_a_running_network_whose_old_name_podman_lost(self, fake_host, systemd):
        networks().create()
        systemd.lost_networks.add("app--front")
        systemd.calls.clear()

        renamed = make_app(front="[Network]\nNetworkName=front-v2\n")
        assert networks(renamed).create() is True
        assert systemd.state_changes == [["restart", "app--front-network.service"]]

    def test_a_rejected_network_is_never_written(self, fake_host, systemd):
        systemd.generator_result = (1, "", "bad network")
        with pytest.raises(QuadletAppError, match="^bad network$"):
            networks().create()
        assert fake_host.files() == {}
        assert systemd.state_changes == []


class TestToWrite:
    def to_write(self):
        return [network.filename for network in AppNetworks(make_app(), Systemd(1)).to_write()]

    def test_missing_ones(self, fake_host, systemd):
        assert self.to_write() == ["front.network", "back.network"]
        fake_host.quadlet_dir.mkdir(parents=True)
        (fake_host.quadlet_dir / "app--front.network").write_text("anything")
        systemd.active.add("app--front-network.service")
        assert self.to_write() == ["back.network"]

    def test_a_different_one_whose_unit_is_not_running(self, fake_host, systemd):
        # Nothing can be using it, and its definition may be what kept it from starting
        fake_host.quadlet_dir.mkdir(parents=True)
        (fake_host.quadlet_dir / "app--front.network").write_text("anything")
        assert self.to_write() == ["front.network", "back.network"]

    def test_the_same_one_whose_unit_is_not_running(self, fake_host, systemd):
        networks().create()
        systemd.active.clear()
        assert self.to_write() == []
