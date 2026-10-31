# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Action plugin for quadlet_app module.

This plugin runs on the control node and:
1. Discovers files in the src directory
2. Templates them with Jinja2 variables
3. Reads the app's manifest
4. Sends templated content, and what the manifest says, to the module on the managed node
"""

import os

# pylint: disable=import-error
from ansible.module_utils.parsing.convert_bool import boolean  # type: ignore[import-not-found]
from ansible.utils.display import Display  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.plugin_utils.action import (  # type: ignore[import-not-found]
    QuadletAction,
)
from ansible_collections.kriansa.quadlet.plugins.plugin_utils.control_node_app import (  # type: ignore[import-not-found]
    ControlNodeApp,
)

# pylint: enable=import-error

display = Display()


class ActionModule(QuadletAction):  # pylint: disable=too-few-public-methods
    """Action plugin for quadlet_app - handles control node file operations."""

    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        """Execute action plugin on control node."""
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        self._use_task_vars(task_vars)

        # Get and validate source path
        src = self._task.args.get("src", None)
        if not src:
            result["failed"] = True
            result["msg"] = "src parameter is required"
            return result

        src_path = self._resolve_src_path(src)
        if not os.path.exists(src_path):
            result["failed"] = True
            result["msg"] = f"Source directory not found on control node: {src_path}"
            return result

        if not os.path.isdir(src_path):
            result["failed"] = True
            result["msg"] = f"Source path is not a directory: {src_path}"
            return result

        # Determine app name (normalize to lowercase)
        # normpath first: the basename of "apps/myapp/" is empty
        app_name = self._task.args.get("name") or os.path.basename(os.path.normpath(src_path))
        app = ControlNodeApp(src_path, app_name.lower(), self._loader, self._templar)
        # Read the way the module reads it: "false" is false
        try:
            networks_only = boolean(self._task.args.get("_networks_only", False), strict=True)
        except TypeError as e:
            result["failed"] = True
            result["msg"] = f"_networks_only: {e}"
            return result

        # Discover and template files
        try:
            systemctl_timeout, main_container = self._manifest_values(app)
            files_data = app.files_data(networks_only=networks_only)
        except Exception as e:  # noqa: BLE001
            result["failed"] = True
            result["msg"] = f"Failed to process files on control node: {e!s}"
            return result

        if networks_only and not files_data["quadlets"]:
            # Nothing to create, so nothing to ask of the host
            result.update(
                changed=False, application_name=app.name, network_files=[], msg="no networks"
            )
            return result

        # Pass templated content to module. Names and contents travel apart: contents hold secrets
        # and are no_log, and Ansible masks every string of a no_log argument wherever it shows up
        # in the output - names among them, it would turn "app--main.container" into "app--********"
        module_args = self._task.args.copy()
        # The manifest the quadlet_stack role read stays here: the module needs two of its values,
        # and the claims in it have no business in the managed host's logs
        module_args.pop("_manifest", None)
        module_args["_files"], module_args["_contents"] = app.split_files_data(files_data)
        module_args["_control_node_processed"] = True
        module_args["_systemctl_timeout"] = systemctl_timeout
        module_args["_main_container"] = main_container

        result.update(
            self._execute_module(
                module_name="kriansa.quadlet.quadlet_app",
                module_args=module_args,
                task_vars=task_vars,
            )
        )

        return result

    def _manifest_values(self, app: ControlNodeApp) -> tuple[int, str]:
        """
        What the app's manifest says the module needs to know: (systemctl_timeout, main_container).

        The quadlet_stack role has already read every manifest, and passes the app's in. Reading it
        again here could disagree with what the role told the app's providers, since this task
        renders with other variables in scope. Called on its own, the plugin reads it itself.
        """
        manifest = self._task.args.get("_manifest")
        if manifest is None:
            parsed = app.manifest()
            for warning in parsed.warnings:
                display.warning(warning)
            return parsed.systemctl_timeout, parsed.main_container

        if not isinstance(manifest, dict) or not {"systemctl_timeout", "main_container"} <= set(
            manifest
        ):
            raise ValueError(
                "_manifest is set by the quadlet_stack role, to an app of what "
                "quadlet_stack_info returns"
            )
        return manifest["systemctl_timeout"], manifest["main_container"]
