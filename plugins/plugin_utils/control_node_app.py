# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""An app's directory as the control node sees it: its files, rendered with Ansible's templating."""

import os

# pylint: disable=import-error
from ansible.errors import AnsibleError  # type: ignore[import-not-found]
from ansible.module_utils.common.text.converters import to_text  # type: ignore[import-not-found]
from ansible.template import trust_as_template  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.module_utils import (  # type: ignore[import-not-found]
    constants,
)
from ansible_collections.kriansa.quadlet.plugins.module_utils.app import (  # type: ignore[import-not-found]
    LONG_RUNNING_KINDS,
    QuadletApp,
)
from ansible_collections.kriansa.quadlet.plugins.module_utils.sources import (  # type: ignore[import-not-found]
    ACTION_PLUGIN_GROUPS,
)
from ansible_collections.kriansa.quadlet.plugins.plugin_utils.manifest import (  # type: ignore[import-not-found]
    MANIFEST_FILENAMES,
    Manifest,
)

# pylint: enable=import-error

# The quadlets read to learn the app's shape on the control node: those whose service keeps
# running, and the networks they join
SHAPE_QUADLET_EXTENSIONS = (*LONG_RUNNING_KINDS, ".network")


class ControlNodeApp:
    """
    An app directory on the control node.

    Every file is rendered with Ansible's full templating - variables, lookups, filters, control
    flow - plus `quadlet_app_name`, the name the app is deployed under.
    """

    def __init__(self, path: str, name: str, loader, templar):
        """
        Args:
            path: The app's directory
            name: The name the app is deployed under, already lowercased
            loader: Ansible's DataLoader, which reads (and decrypts) the files
            templar: A Templar holding every variable the task sees
        """
        self.path = path
        self.name = name
        self._loader = loader
        self._templar = templar

    def manifest_path(self) -> str | None:
        """
        Where the app's manifest is, if it has one.

        Raises:
            AnsibleError: If it has two, under both names - which one counts would be a guess
        """
        found = [
            os.path.join(self.path, filename)
            for filename in MANIFEST_FILENAMES
            if os.path.isfile(os.path.join(self.path, filename))
        ]
        if len(found) > 1:
            raise AnsibleError(
                f"The app {self.name} has two manifests, {' and '.join(found)}. Keep only one."
            )
        return found[0] if found else None

    def manifest(self) -> Manifest:
        """
        The app's manifest, rendered and read; the defaults if it has none.

        Raises:
            AnsibleError: If it cannot be rendered, or is not a valid manifest
        """
        path = self.manifest_path()
        if path is None:
            return Manifest()
        return Manifest.parse(self.render(path), path)

    def model(self, main_container: str) -> QuadletApp:
        """
        The app as the module sees it, built from the quadlets that make up its shape.

        Only its containers, pods, kube files and networks are rendered: that is enough to find
        its main quadlet and the networks it joins with the module's own rules.

        Raises:
            AnsibleError: If a quadlet cannot be rendered
            QuadletAppError: If the app's name is invalid
        """
        app = QuadletApp(self.name, main_container)
        quadlets_dir = os.path.join(self.path, "quadlets")
        if os.path.isdir(quadlets_dir):
            for entry in self._quadlet_files(quadlets_dir, SHAPE_QUADLET_EXTENSIONS):
                app.add_quadlet(entry["name"], entry["content"])
        return app

    def config_dirs(self) -> list[str]:
        """
        The names of the directories under config/ that hold a file, each named after the quadlet
        mounting it.

        One without a file is left out: the module creates a directory on the host by writing a
        file into it, so an empty one never exists there.
        """
        config_dir = os.path.join(self.path, "config")
        if not os.path.isdir(config_dir):
            return []
        return sorted(
            entry
            for entry in os.listdir(config_dir)
            if os.path.isdir(os.path.join(config_dir, entry))
            and any(files for _root, _dirs, files in os.walk(os.path.join(config_dir, entry)))
        )

    def files_data(self, networks_only: bool = False) -> dict[str, list[dict]]:
        """
        Every file of the app, rendered.

        Args:
            networks_only: Only the network quadlets, for the pass creating every app's networks.
                Nothing else is rendered, so no lookup in another file runs.

        Returns:
            Dict with 'quadlets', 'units', 'init', and 'config' file lists
        """
        files_data: dict[str, list[dict]] = {"quadlets": [], "units": [], "init": [], "config": []}

        quadlets_dir = os.path.join(self.path, "quadlets")
        if networks_only:
            if os.path.isdir(quadlets_dir):
                files_data["quadlets"] = self._quadlet_files(quadlets_dir, (".network",))
            return files_data

        if os.path.isdir(quadlets_dir):
            files_data["quadlets"] = self._quadlet_files(quadlets_dir)

        units_dir = os.path.join(self.path, "units")
        if os.path.isdir(units_dir):
            files_data["units"] = self._unit_files(units_dir)

        for kind in ("init", "config"):
            kind_dir = os.path.join(self.path, kind)
            if os.path.isdir(kind_dir):
                files_data[kind] = self._data_files(kind_dir)

        return files_data

    @staticmethod
    def split_files_data(files_data: dict[str, list[dict]]) -> tuple[list[dict], list[str]]:
        """
        Separate what is safe to log from what is not.

        Returns:
            (files, contents): files describes each file - its kind, and its name, or its container
            directory and path - and contents holds the matching content at the same position
        """
        files, contents = [], []
        # Each group by the kind the module reads it as
        for kind, group in ACTION_PLUGIN_GROUPS.items():
            for entry in files_data[group]:
                files.append({"kind": kind, **{k: v for k, v in entry.items() if k != "content"}})
                contents.append(entry["content"])
        return files, contents

    def render(self, file_path: str) -> str:
        """Read a file and render it with Ansible's full templating engine."""
        try:
            file_content = self._loader.get_text_file_contents(file_path)

            # The loader does not refuse a file that is not UTF-8: it only warns, and hands over
            # the text with each bad byte replaced by a placeholder character that cannot be
            # written out again. Turning the placeholders back into the original bytes and
            # decoding those strictly raises the error the loader held back, naming the first bad
            # byte, before anything reaches the host.
            file_content.encode("utf-8", "surrogateescape").decode("utf-8")

            # Ansible only renders text it has been told is a template rather than data
            trusted_content = trust_as_template(file_content)

            temp_vars = self._templar.available_variables.copy()
            temp_vars["quadlet_app_name"] = self.name
            data_templar = self._templar.copy_with_new_env(available_variables=temp_vars)

            # escape_backslashes=False prevents double-escaping in files, and
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

    def _quadlet_files(
        self, quadlets_dir: str, extensions: tuple[str, ...] = constants.VALID_QUADLET_EXTENSIONS
    ) -> list[dict]:
        """
        The quadlet files in quadlets/, sorted and rendered. Anything else there is skipped.

        Args:
            extensions: The quadlet types to read
        """
        quadlet_files = []

        for filename in sorted(os.listdir(quadlets_dir)):
            file_path = os.path.join(quadlets_dir, filename)
            if not os.path.isfile(file_path):
                continue

            if filename.endswith(constants.VALID_UNIT_EXTENSIONS):
                raise AnsibleError(
                    f"{filename} is a systemd unit, not a quadlet. Move it to units/."
                )

            if not filename.endswith(extensions):
                continue

            quadlet_files.append({"name": filename, "content": self.render(file_path)})

        return quadlet_files

    def _unit_files(self, units_dir: str) -> list[dict]:
        """Every systemd unit file in units/, sorted and rendered."""
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

            unit_files.append({"name": filename, "content": self.render(file_path)})

        return unit_files

    def _data_files(self, base_dir: str) -> list[dict]:
        """Every file in init/ or config/, by the container directory it is in, rendered."""
        processed_files = []

        for container_dir in sorted(os.listdir(base_dir)):
            container_path = os.path.join(base_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            for root, _dirs, files in os.walk(container_path):
                for filename in sorted(files):
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, container_path)
                    processed_files.append(
                        {
                            "container": container_dir,
                            "path": rel_path,
                            "content": self.render(file_path),
                        }
                    )

        return processed_files
