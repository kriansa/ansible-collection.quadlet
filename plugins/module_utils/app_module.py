# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The quadlet_app module's run: from its parameters to the result Ansible reports."""

import os

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.generator import QuadletGenerator
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFiles
from ansible_collections.kriansa.quadlet.plugins.module_utils.networks import AppNetworks
from ansible_collections.kriansa.quadlet.plugins.module_utils.services import AppServices
from ansible_collections.kriansa.quadlet.plugins.module_utils.sources import (
    ActionPluginFiles,
    AppDirectory,
)
from ansible_collections.kriansa.quadlet.plugins.module_utils.systemctl import Systemd


class QuadletAppModule:
    """One run of the module: deploys an app and brings its services to the state asked for."""

    def __init__(self, module):
        """
        Args:
            module: The AnsibleModule, with the task's parameters
        """
        self.module = module
        params = module.params

        self.src = params["src"]
        # normpath first: the basename of "apps/myapp/" is empty
        self.app_name = params.get("name") or os.path.basename(os.path.normpath(self.src))
        self.state = params["state"]
        self.force = params["force"]
        self.settle_time = params["settle_time"]

        # Facts about the app rather than choices of the task: the action plugin reads them from
        # the app's manifest
        self.systemctl_timeout = params["_systemctl_timeout"]
        self.main_container = params["_main_container"]

        # Only create the app's networks that are missing from the host - the pass the
        # quadlet_stack role makes over every app before it deploys any
        self.networks_only = params["_networks_only"]

        # Whether the action plugin sent the files, templated on the control node. Without it, the
        # module reads them from src on the managed host.
        self.control_node_processed = params.get("_control_node_processed", False)
        self.files = params.get("_files")
        self.contents = params.get("_contents")

    def run(self) -> None:
        """Deploy, and end the module with the outcome."""
        try:
            self._deploy()
        except QuadletAppError as e:
            self.module.fail_json(msg=e.msg, **e.details)
        except Exception as e:  # noqa: BLE001
            self.module.fail_json(msg=f"Unexpected error: {e!s}")

    def _deploy(self) -> None:
        app = self._source().load(self.app_name, self.main_container)
        if self.networks_only:
            self._create_networks(app)
            return

        app.check_main()
        files = HostFiles(app.files(), self.force)
        services = AppServices(
            app, Systemd(self.systemctl_timeout), files, self.state, self.settle_time
        )

        needs_update = files.needed()
        needs_service_management = services.need_attention()

        if not needs_update and not needs_service_management:
            # No changes needed - files and service state are correct
            self._exit(app, changed=False, msg="application already up to date")
            return

        # Files are only written once podman's generator has accepted them: a file it rejects must
        # never reach the host
        if needs_update:
            QuadletGenerator().validate(files.files)
            files.write()

        services_changed = services.apply(files_changed=needs_update)
        for warning in services.warnings:
            self.module.warn(warning)
        self._exit(app, changed=needs_update or services_changed, msg=self._success_message())

    def _create_networks(self, app: QuadletApp) -> None:
        changed = AppNetworks(app, Systemd(self.systemctl_timeout)).create()
        self.module.exit_json(
            changed=changed,
            application_name=app.name,
            network_files=[network.deployed_name for network in app.networks],
            msg="networks created" if changed else "networks already present",
        )

    def _source(self) -> ActionPluginFiles | AppDirectory:
        if self.control_node_processed:
            return ActionPluginFiles(self.files, self.contents)
        return AppDirectory(self.src)

    def _exit(self, app: QuadletApp, changed: bool, msg: str) -> None:
        self.module.exit_json(
            changed=changed,
            application_name=app.name,
            service_name=app.main_service,
            quadlet_files=app.deployed_quadlets,
            unit_files=app.deployed_units,
            msg=msg,
        )

    def _success_message(self) -> str:
        if self.state == "installed":
            return "quadlet files deployed"
        if self.state == "started":
            return "quadlet files deployed and service started"
        if self.state == "restarted":
            return "quadlet files deployed and service restarted"
        return "quadlet files deployed"
