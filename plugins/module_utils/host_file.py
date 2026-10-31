# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The files the module puts on the host, and whether the host needs any of them written."""

import functools
import hashlib
import os
import tempfile
from pathlib import Path

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError


class HostFile:
    """
    A file the module puts on the host.

    Subclasses say where it goes and how its content is made. The content is made on first use and
    then kept: the rules that make it read the whole app, which is complete by the time anything
    asks for it.
    """

    @property
    def dest_path(self) -> str:
        """Where the file goes on the host."""
        raise NotImplementedError

    def render(self) -> str:
        """Make the content of the file."""
        raise NotImplementedError

    @functools.cached_property
    def content(self) -> str:
        """What the file holds on the host."""
        return self.render()

    def exists_on_host(self) -> bool:
        """Whether anything is at the file's destination on the host, whatever it holds."""
        return os.path.exists(self.dest_path)

    def host_content(self) -> str | None:
        """
        What the host holds at the file's destination; None if it holds no readable file there.
        """
        if not os.path.isfile(self.dest_path):
            return None
        # Read as written, line endings included: read otherwise, a file saved with Windows line
        # endings would never match its own content, and be deployed again on every run
        try:
            with open(self.dest_path, encoding="utf-8", newline="") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def differs_from_host(self) -> bool:
        """
        Whether the host holds anything other than this content at the file's destination.

        A destination that is missing, is not a regular file, or cannot be read counts as
        different.
        """
        existing_content = self.host_content()
        if existing_content is None:
            return True
        return self._checksum(existing_content) != self._checksum(self.content)

    def write(self) -> None:
        """
        Put the file on the host, creating its directory if needed.

        Raises:
            QuadletAppError: If the directory cannot be created or the file cannot be written
        """
        content = self.content
        dest_dir = os.path.dirname(self.dest_path)
        try:
            os.makedirs(dest_dir, mode=0o755, exist_ok=True)
        except OSError as e:
            raise QuadletAppError(f"Failed to create directory {dest_dir}: {e}") from e

        # Written next to its destination under a temporary name and then renamed over it, so a
        # write that fails halfway leaves the previous version in place, not a truncated file. The
        # temporary name ends in random characters, so neither podman's generator nor systemd
        # would load it should it ever be left behind.
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=dest_dir, prefix=f".{os.path.basename(self.dest_path)}."
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(temp_path, 0o644)
                os.replace(temp_path, self.dest_path)
            except BaseException:
                os.unlink(temp_path)
                raise
        except (OSError, UnicodeError, TypeError) as e:
            raise QuadletAppError(f"Failed to write file {self.dest_path}: {e}") from e

    @staticmethod
    def _checksum(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class HostFiles:
    """The files of one deployment, and whether the host needs them written."""

    def __init__(self, files: list[HostFile], force: bool):
        """
        Args:
            files: Every file the app deploys
            force: Write them all, whether the host holds the same content or not
        """
        self.files = files
        self.force = force

    def changed(self, file: HostFile) -> bool:
        """Whether one of the files is to be written."""
        return self.force or file.differs_from_host()

    def needed(self) -> bool:
        """Whether there is anything to write at all."""
        return self.force or any(file.differs_from_host() for file in self.files)

    def write(self) -> None:
        """Write every file."""
        for file in self.files:
            file.write()


class DataFile(HostFile):
    """
    A file from the app's init/ or config/ directory, mounted into the container it is for.

    Deployed exactly as it is: none of the quadlet rules apply.
    """

    def __init__(self, app, kind: str, container: str, path: str, source: str):
        """
        Args:
            app: The QuadletApp the file belongs to
            kind: "init" or "config"
            container: The directory it is in, named after the quadlet that mounts it
            path: Its path inside that directory
            source: Its content, already templated by Ansible
        """
        self.app = app
        self.kind = kind
        self.container = container
        self.path = path
        self.source = source

    @property
    def dest_path(self) -> str:
        return os.path.join(self.app.base_dir, self.kind, Path(self.container).stem, self.path)

    def render(self) -> str:
        return self.source
