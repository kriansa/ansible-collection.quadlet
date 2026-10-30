# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""Where an app's files come from: sent by the action plugin, or read from the app's directory."""

import os
from pathlib import Path

from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.module_utils.app import QuadletApp
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError

# The quadlet types a directory under init/ or config/ can be named after
DATA_DIR_QUADLET_EXTENSIONS = (".container", ".pod")

# The kind the action plugin gives each file, and the group of the app it belongs to
ACTION_PLUGIN_GROUPS = {"quadlet": "quadlets", "unit": "units", "init": "init", "config": "config"}


class ActionPluginFiles:
    """The app's files as the action plugin sends them, already templated on the control node."""

    def __init__(self, files: list[dict] | None, contents: list[str] | None):
        """
        Args:
            files: What each file is - its kind, and its name, or its container directory and path
            contents: The content of each file in files, at the same position
        """
        self.files = files or []
        self.contents = contents or []

    def load(self, name: str) -> QuadletApp:
        """
        Build the app out of the files.

        Raises:
            QuadletAppError: If the name is invalid, or the names and contents do not match up
        """
        app = QuadletApp(name)
        groups = self._grouped()
        for entry in groups["quadlets"]:
            app.add_quadlet(entry["name"], entry["content"])
        for entry in groups["units"]:
            app.add_unit(entry["name"], entry["content"])
        for kind in ("init", "config"):
            for entry in groups[kind]:
                app.add_data_file(kind, entry["container"], entry["path"], entry["content"])
        return app

    def _grouped(self) -> dict[str, list[dict]]:
        """Each file with its content next to its name, or its container directory and path."""
        if len(self.files) != len(self.contents):
            raise QuadletAppError(
                "_files and _contents must have the same length "
                f"({len(self.files)} and {len(self.contents)})"
            )

        groups: dict[str, list[dict]] = {group: [] for group in ACTION_PLUGIN_GROUPS.values()}
        for entry, content in zip(self.files, self.contents, strict=True):
            fields = {key: value for key, value in entry.items() if key != "kind"}
            groups[ACTION_PLUGIN_GROUPS[entry["kind"]]].append({**fields, "content": content})
        return groups


class AppDirectory:
    """
    An app's directory on the managed host, read when the action plugin has not sent the files.

    The action plugin always runs from a playbook, so this is only used when the module runs on
    its own. The checks made here are the ones the action plugin would otherwise make.
    """

    def __init__(self, src: str):
        """
        Args:
            src: Path to the directory holding quadlets/, units/, init/ and config/
        """
        self.src = src

    def load(self, name: str) -> QuadletApp:
        """
        Check the directory and build the app out of its files.

        Raises:
            QuadletAppError: If the directory, the name or a file is not usable
        """
        self.check_location()
        app = QuadletApp(name)
        self.check_layout()

        quadlet_files = self.quadlet_files()
        unit_files = self.unit_files()
        data_files = {kind: self.data_files(kind) for kind in ("init", "config")}

        # Every quadlet and unit is read before any is added: a volume checks its owner as it is
        # added, and a file that cannot be read is the error to report first
        quadlets = [(filename, self._read(path)) for filename, path in quadlet_files]
        units = [(filename, self._read(path)) for filename, path in unit_files]
        for filename, source in quadlets:
            app.add_quadlet(filename, source)
        for filename, source in units:
            app.add_unit(filename, source)

        for kind, files in data_files.items():
            for container, rel_path, path in files:
                app.add_data_file(kind, container, rel_path, self._read(path))

        return app

    def check_location(self) -> None:
        """Check the directory exists, and has the mandatory quadlets/ subdirectory."""
        if not os.path.exists(self.src):
            raise QuadletAppError(f"Source directory not found: {self.src}")

        if not os.path.isdir(self.src):
            raise QuadletAppError(f"Source path is not a directory: {self.src}")

        quadlets_dir = os.path.join(self.src, "quadlets")
        if not os.path.isdir(quadlets_dir):
            raise QuadletAppError(
                f"Required directory not found: {quadlets_dir}. "
                f"The quadlets/ subdirectory is mandatory."
            )

    def check_layout(self) -> None:
        """
        Check what the directory holds.

        Rules:
        1. quadlets/main.container exists
        2. init/ and config/ subdirectories use the quadlet stem without suffix (e.g., main/, not
           main.container/)
        3. A corresponding .container or .pod quadlet file exists for each of them
        4. The match is unambiguous (no both main.container and main.pod)
        """
        main_container = os.path.join(self.src, "quadlets", "main.container")
        if not os.path.isfile(main_container):
            raise QuadletAppError(
                f"Required file not found: {main_container}. "
                "The quadlets/main.container file is mandatory."
            )

        for dir_name in ["init", "config"]:
            dir_path = os.path.join(self.src, dir_name)
            if not os.path.isdir(dir_path):
                continue

            try:
                entries = os.listdir(dir_path)
            except OSError as e:
                raise QuadletAppError(f"Cannot read {dir_name}: {e}") from e

            for entry in entries:
                if os.path.isdir(os.path.join(dir_path, entry)):
                    self._check_data_dir(dir_name, entry)

    def quadlet_files(self) -> list[tuple[str, str]]:
        """
        The quadlet files in quadlets/, sorted. Anything else there is skipped.

        Returns:
            (filename, full_path) of each
        """
        quadlets_dir = os.path.join(self.src, "quadlets")
        quadlet_files = []

        try:
            entries = os.listdir(quadlets_dir)
        except OSError as e:
            raise QuadletAppError(f"Cannot read quadlets directory: {e}") from e

        for filename in sorted(entries):
            full_path = os.path.join(quadlets_dir, filename)
            if not os.path.isfile(full_path):
                continue

            if filename.endswith(constants.VALID_UNIT_EXTENSIONS):
                raise QuadletAppError(
                    f"{filename} is a systemd unit, not a quadlet. Move it to units/."
                )

            if filename.endswith(constants.VALID_QUADLET_EXTENSIONS):
                quadlet_files.append((filename, full_path))

        return quadlet_files

    def unit_files(self) -> list[tuple[str, str]]:
        """
        The plain systemd unit files in units/, sorted.

        Returns:
            (filename, full_path) of each
        """
        units_dir = os.path.join(self.src, "units")
        if not os.path.isdir(units_dir):
            return []

        unit_files = []

        try:
            entries = os.listdir(units_dir)
        except OSError as e:
            raise QuadletAppError(f"Cannot read units directory: {e}") from e

        for filename in sorted(entries):
            full_path = os.path.join(units_dir, filename)
            if not os.path.isfile(full_path):
                continue

            # Strays are rejected rather than skipped: unlike quadlets/, this directory has no
            # history of holding anything else, so an unrecognized file is a mistake
            if not filename.endswith(constants.VALID_UNIT_EXTENSIONS):
                raise QuadletAppError(
                    f"Unsupported file in units/: {filename}. "
                    f"Only {', '.join(constants.VALID_UNIT_EXTENSIONS)} files are supported."
                )

            unit_files.append((filename, full_path))

        return unit_files

    def data_files(self, kind: str) -> list[tuple[str, str, str]]:
        """
        Every file in init/ or config/, by the container directory it is in.

        Args:
            kind: "init" or "config"

        Returns:
            (container_dir, path relative to it, full_path) of each, sorted by directory and name
        """
        base_dir = os.path.join(self.src, kind)
        if not os.path.isdir(base_dir):
            return []

        data_files = []

        for container_dir in sorted(os.listdir(base_dir)):
            container_path = os.path.join(base_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            for root, _dirs, files in os.walk(container_path):
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, container_path)
                    data_files.append((container_dir, rel_path, full_path))

        return data_files

    def _check_data_dir(self, dir_name: str, entry: str) -> None:
        """Check one directory under init/ or config/ names exactly one container or pod."""
        if Path(entry).suffix in DATA_DIR_QUADLET_EXTENSIONS:
            raise QuadletAppError(
                f"Invalid {dir_name} subdirectory: {entry}. "
                f"Use the quadlet stem without suffix (e.g., {Path(entry).stem}/ instead of {entry}/)"
            )

        quadlets_dir = os.path.join(self.src, "quadlets")
        matching = [
            f
            for f in os.listdir(quadlets_dir)
            if Path(f).stem == entry and Path(f).suffix in DATA_DIR_QUADLET_EXTENSIONS
        ]

        if not matching:
            raise QuadletAppError(
                f"No corresponding quadlet file for {dir_name}/{entry}. "
                f"Expected a file like quadlets/{entry}.container or quadlets/{entry}.pod"
            )

        if len(matching) > 1:
            raise QuadletAppError(
                f"Ambiguous {dir_name} subdirectory: {entry}. "
                f"Multiple matching quadlet files found: {', '.join(sorted(matching))}. "
                f"Remove one of the conflicting quadlet files"
            )

    @staticmethod
    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError as e:
            raise QuadletAppError(f"File {path} is not valid UTF-8: {e}") from e
        except OSError as e:
            raise QuadletAppError(f"Failed to read file {path}: {e}") from e
