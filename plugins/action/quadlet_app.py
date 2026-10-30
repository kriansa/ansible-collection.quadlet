# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Action plugin for quadlet_app module.

This plugin runs on the control node and:
1. Discovers files in the src directory
2. Templates them with Jinja2 variables
3. Sends templated content to the module on the managed node
"""

import os

# pylint: disable=import-error
from ansible.errors import AnsibleError, AnsibleFileNotFound  # type: ignore[import-not-found]
from ansible.module_utils.common.text.converters import to_text  # type: ignore[import-not-found]
from ansible.plugins.action import ActionBase  # type: ignore[import-not-found]
from ansible.template import trust_as_template  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.module_utils import (  # type: ignore[import-not-found]
    constants,
)

# pylint: enable=import-error


class ActionModule(ActionBase):  # pylint: disable=too-few-public-methods
    """Action plugin for quadlet_app - handles control node file operations."""

    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        """Execute action plugin on control node."""
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        # Merge task-level vars with all available vars
        all_vars = task_vars.copy()
        if hasattr(self._task, "vars") and self._task.vars:
            for var_name, var_value in self._task.vars.items():
                templated_value = self._templar.template(var_value)
                all_vars[var_name] = templated_value

        # Create Templar with all variables (update existing _templar)
        self._templar.available_variables = all_vars

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
        app_name = app_name.lower()

        # Discover and template files
        try:
            files_data = self._discover_and_template_files(src_path, app_name)
        except Exception as e:  # noqa: BLE001
            result["failed"] = True
            result["msg"] = f"Failed to process files on control node: {e!s}"
            return result

        # Pass templated content to module. Names and contents travel apart: contents hold secrets
        # and are no_log, and Ansible masks every string of a no_log argument wherever it shows up
        # in the output - names among them, it would turn "app--main.container" into "app--********"
        module_args = self._task.args.copy()
        module_args["_files"], module_args["_contents"] = self._split_files_data(files_data)
        module_args["_control_node_processed"] = True

        result.update(
            self._execute_module(
                module_name="kriansa.quadlet.quadlet_app",
                module_args=module_args,
                task_vars=task_vars,
            )
        )

        return result

    @staticmethod
    def _split_files_data(files_data):
        """
        Separate what is safe to log from what is not.

        Returns:
            (files, contents): files describes each file - its kind, and its name, or its container
            directory and path - and contents holds the matching content at the same position
        """
        files, contents = [], []
        for group, kind in (("quadlets", "quadlet"), ("units", "unit"), ("init", "init"),
                            ("config", "config")):
            for entry in files_data[group]:
                files.append({"kind": kind, **{k: v for k, v in entry.items() if k != "content"}})
                contents.append(entry["content"])
        return files, contents

    def _resolve_src_path(self, src):
        """Resolve source path relative to playbook directory."""
        try:
            return self._find_needle("files", src)
        except AnsibleFileNotFound:
            if not os.path.isabs(src):
                playbook_dir = self._loader.get_basedir()
                return os.path.join(playbook_dir, src)
            return src

    def _discover_and_template_files(self, src_path, app_name):
        """
        Discover and template all files.

        Args:
            src_path: Source directory path
            app_name: Application name for prefix function

        Returns:
            Dict with 'quadlets', 'units', 'init', and 'config' file lists
        """
        files_data = {"quadlets": [], "units": [], "init": [], "config": []}

        quadlets_dir = os.path.join(src_path, "quadlets")
        if os.path.isdir(quadlets_dir):
            files_data["quadlets"] = self._process_quadlet_files(quadlets_dir, app_name)

        units_dir = os.path.join(src_path, "units")
        if os.path.isdir(units_dir):
            files_data["units"] = self._process_unit_files(units_dir, app_name)

        init_dir = os.path.join(src_path, "init")
        if os.path.isdir(init_dir):
            files_data["init"] = self._process_init_config_files(init_dir, app_name)

        config_dir = os.path.join(src_path, "config")
        if os.path.isdir(config_dir):
            files_data["config"] = self._process_init_config_files(config_dir, app_name)

        return files_data

    def _process_quadlet_files(self, quadlets_dir, app_name):
        """Process all quadlet files in quadlets/ directory."""
        quadlet_files = []

        for filename in sorted(os.listdir(quadlets_dir)):
            file_path = os.path.join(quadlets_dir, filename)
            if not os.path.isfile(file_path):
                continue

            if filename.endswith(constants.VALID_UNIT_EXTENSIONS):
                raise AnsibleError(
                    f"{filename} is a systemd unit, not a quadlet. Move it to units/."
                )

            if not filename.endswith(constants.VALID_QUADLET_EXTENSIONS):
                continue

            content = self._template_file(file_path, app_name)
            quadlet_files.append({"name": filename, "content": content})

        return quadlet_files

    def _process_unit_files(self, units_dir, app_name):
        """Process all systemd unit files in units/ directory."""
        unit_files = []

        for filename in sorted(os.listdir(units_dir)):
            file_path = os.path.join(units_dir, filename)
            if not os.path.isfile(file_path):
                continue

            # Strays are rejected rather than skipped: unlike quadlets/, this directory has no
            # history of holding anything else, so an unrecognized file is a mistake
            if not filename.endswith(constants.VALID_UNIT_EXTENSIONS):
                raise AnsibleError(
                    f"Unsupported file in units/: {filename}. "
                    f"Only {', '.join(constants.VALID_UNIT_EXTENSIONS)} files are supported."
                )

            content = self._template_file(file_path, app_name)
            unit_files.append({"name": filename, "content": content})

        return unit_files

    def _process_init_config_files(self, base_dir, app_name):
        """Process all files in init/ or config/ directory."""
        processed_files = []

        for container_dir in sorted(os.listdir(base_dir)):
            container_path = os.path.join(base_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            for root, _, files in os.walk(container_path):
                for filename in sorted(files):
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, container_path)
                    content = self._template_file(file_path, app_name)

                    processed_files.append(
                        {"container": container_dir, "path": rel_path, "content": content}
                    )

        return processed_files

    def _template_file(self, file_path, app_name):
        """Read and template a file using Ansible's full templating engine."""
        try:
            # Read file content using Ansible's loader
            file_content = self._loader.get_text_file_contents(file_path)

            # The loader does not refuse a file that is not UTF-8: it only warns, and hands over
            # the text with each bad byte replaced by a placeholder character that cannot be
            # written out again. Turning the placeholders back into the original bytes and
            # decoding those strictly raises the error the loader held back, naming the first bad
            # byte, before anything reaches the host.
            file_content.encode("utf-8", "surrogateescape").decode("utf-8")

            # CRITICAL: Mark content as trusted for templating
            # This is required by Ansible's security mechanism
            trusted_content = trust_as_template(file_content)

            # Prepare template variables
            temp_vars = self._templar.available_variables.copy()
            temp_vars["quadlet_app_name"] = app_name

            # Create a new templar with updated variables
            # This gives us full Ansible templating support
            data_templar = self._templar.copy_with_new_env(available_variables=temp_vars)

            # Template the content with full Ansible features
            # escape_backslashes=False prevents double-escaping in files
            # preserve_trailing_newlines=True keeps file formatting intact
            templated_content = data_templar.template(
                trusted_content,
                escape_backslashes=False,
                preserve_trailing_newlines=True,
            )

            # A file that is a single expression, like "{{ port }}", renders to that expression's
            # value - a number, a list, nothing at all - rather than to text. It becomes text the
            # way Ansible's template module turns it into a file.
            if templated_content is None:
                return ""
            return to_text(templated_content, nonstring="simplerepr")

        except UnicodeDecodeError as e:
            raise AnsibleError(f"File {file_path} is not valid UTF-8: {e}") from e
        except Exception as e:
            raise AnsibleError(f"Failed to template file {file_path}: {e}") from e
