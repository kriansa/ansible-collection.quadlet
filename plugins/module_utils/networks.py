# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""An app's networks, created on the host ahead of any app being deployed."""

import functools

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.generator import QuadletGenerator
from ansible_collections.kriansa.quadlet.plugins.module_utils.podman import Podman
from ansible_collections.kriansa.quadlet.plugins.module_utils.quadlets import Network
from ansible_collections.kriansa.quadlet.plugins.module_utils.systemctl import Systemd


class AppNetworks:
    """
    The networks of one app, made to exist before any app is deployed.

    An app can join another app's network. Podman refuses to create a container whose network is
    missing, and podman's generator rejects a quadlet naming a network file it cannot find - so
    deploying the joining app on a host where the owner never was would fail. Creating every app's
    networks first takes that ordering away.

    Only networks missing from the host are written, and those whose unit is not running - which
    nothing can be using, and which may have failed to start with a definition since fixed. One
    already there and running is left as it is, even if its definition changed: that change belongs
    to its own app's deploy, which would otherwise find nothing changed and not restart anything
    for it.
    """

    def __init__(self, app: QuadletApp, systemd: Systemd):
        """
        Args:
            app: The app whose networks to create
            systemd: Where to run and ask about units
        """
        self.app = app
        self.systemd = systemd

    def to_write(self) -> list[Network]:
        """
        The app's networks whose file has to be written: missing from the host, or different there
        while the network's unit is not running.
        """
        return [
            network
            for network in self.app.networks
            if not network.exists_on_host()
            or (network.service_name in self._inactive and network.differs_from_host())
        ]

    @functools.cached_property
    def _inactive(self) -> set[str]:
        """
        The services of the app's networks whose unit is not running.

        Asked once: writing a network's file and reloading systemd does not start or stop its unit.
        """
        return {
            network.service_name
            for network in self.app.networks
            if not self.systemd.is_active(network.service_name)
        }

    def create(self) -> bool:
        """
        Write the networks' files as to_write says, then bring every network up.

        A network whose unit is not running is started. One whose unit runs but which podman no
        longer has - removed by hand - is restarted, so podman creates it again. Starting a unit
        only creates the network if it does not exist yet, so a network in use is not disturbed.

        Returns:
            True if anything was written, started or restarted

        Raises:
            QuadletAppError: If podman's generator rejects a file, or systemctl or podman fails
        """
        changed = False

        to_write = self.to_write()
        if to_write:
            # Checked before any is written, as on a deploy: a file the generator rejects must
            # never reach the host
            QuadletGenerator().validate(to_write)
            for network in to_write:
                network.write()
            self.systemd.run("daemon-reload")
            changed = True

        stopped, lost = [], []
        for network in self.app.networks:
            if network.service_name in self._inactive:
                stopped.append(network.service_name)
            elif not Podman.network_exists(self._running_name(network)):
                lost.append(network.service_name)
        if stopped:
            self.systemd.run("start", *stopped)
            changed = True
        if lost:
            self.systemd.run("restart", *lost)
            changed = True

        return changed

    @staticmethod
    def _running_name(network: Network) -> str:
        """
        The name of the network a running unit creates: the one in the host's file.

        A running network whose file changed is left to its own app's deploy, so its unit still
        creates the name the host's file has. Asking podman for the new name instead would find
        nothing, and restart the network - and every container joined to it - on every run, all
        for nothing: the restart only creates the old name again.
        """
        deployed = network.host_content()
        return network.network_name if deployed is None else network.network_name_in(deployed)
