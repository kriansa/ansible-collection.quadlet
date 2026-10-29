# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

# pylint: disable=import-error
from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-not-found]

# pylint: enable=import-error

DOCUMENTATION = r"""
---
module: quadlet_app
short_description: Deploy Podman Quadlet applications with automatic templating and preprocessing
version_added: "1.0.0"
description:
    - Deploys Podman Quadlet applications with automatic resource prefixing
    - Templates all files with Jinja2 variables (handled by Ansible)
    - Preprocesses Quadlet files to add application name prefixes
    - Deploys plain systemd units from units/, currently .timer files, and enables them
    - Manages init and config directories
    - Handles systemd service lifecycle (daemon-reload, start, enable)
    - Ensures idempotent deployments (only updates when files change)

options:
    src:
        description:
            - Path to the application directory containing quadlets/, units/, init/, config/
            - Can be relative to playbook directory or absolute path
            - Must contain quadlets/main.container (required)
        required: true
        type: path
    name:
        description:
            - Application name used for prefixing files and services
            - If not provided, extracted from basename of src
            - Must start with a letter (a-z)
            - Can end with a letter or number (a-z, 0-9)
            - Can contain lowercase letters, numbers, hyphens, underscores
            - Automatically converted to lowercase
        required: false
        type: str
    state:
        description:
            - Desired deployment state
            - C(installed) - Deploy files and reload systemd (no service management)
            - C(started) - Deploy files, reload systemd, start main service
            - C(restarted) - Deploy files, reload systemd, restart main service
        required: false
        type: str
        choices: ['installed', 'started', 'restarted']
        default: installed
    force:
        description:
            - Force overwrite files even if content hasn't changed
            - When false, uses checksum comparison for idempotency
        required: false
        type: bool
        default: false
    systemctl_timeout:
        description:
            - Timeout in seconds for systemctl commands (start, stop, restart, daemon-reload)
            - Increase this for services that take a long time to start (e.g., large image pulls)
        required: false
        type: int
        default: 120
    settle_time:
        description:
            - Seconds the services this run started have to stay up before the task succeeds
            - Podman reports a container as started as soon as it starts, so without this check a
              container that exits right away, or keeps crashing, would still pass
            - A service that stops, or that systemd has to start again, fails the task with its
              last log lines
            - Services that run once and finish (C(Type=oneshot)) are not watched
            - Set to 0 to turn the check off
        required: false
        type: int
        default: 5

author:
    - Daniel Pereira (@kriansa)

requirements:
    - python >= 3.9
    - podman >= 4.4

notes:
    - This module requires root privileges or appropriate systemd user permissions
    - Quadlet files must be in quadlets/ subdirectory
    - At least quadlets/main.container must exist
    - init and config subdirectories must end with .container or .pod
    - All files are automatically templated by Ansible before module receives them
    - Named volumes are mounted idmapped and chowned once by a generated bootstrap unit, rather
      than being recursively chowned to container root on every mount
    - A .volume may declare X-Owner=uid:gid to set that ownership in container-relative terms. It
      defaults to 0:0, which is correct for any image running as root; an image that drops
      privileges internally has to declare the uid it drops to.
    - Containers default to UserNS=auto:size=65536, except those joining a pod, which share the
      pod's. A container that reads the host journal or other processes' /proc entries must
      declare UserNS=host, or it will half-work - those files read as nobody while world-readable
      ones keep succeeding.
    - A .timer is not a Quadlet type. It belongs in units/, is deployed to /etc/systemd/system,
      and is the one unit this module enables - nothing generates it, so nothing else would apply
      its [Install] section.
    - Timers are enabled and started on every state, including installed, and one whose file
      changed is restarted so a new schedule takes effect immediately.
"""

EXAMPLES = r"""
# Deploy application with default settings
- name: Deploy simple web app
  kriansa.quadlet.quadlet_app:
    src: podman-apps/nginx
    state: installed

# Deploy and start application with custom name
- name: Deploy backend application
  kriansa.quadlet.quadlet_app:
    src: podman-apps/backend
    name: prod-backend
    state: started

# Deploy with variables for templating
- name: Deploy database
  kriansa.quadlet.quadlet_app:
    src: podman-apps/postgres
    state: started
  vars:
    db_version: "16"
    db_password: "{{ vault_db_password }}"

# Force restart application
- name: Update and restart application
  kriansa.quadlet.quadlet_app:
    src: podman-apps/webapp
    state: restarted
    force: true

# Deploy service with extended timeout for slow image pulls
- name: Deploy application with custom timeout
  kriansa.quadlet.quadlet_app:
    src: podman-apps/unifi-network
    state: started
    systemctl_timeout: 300

# An app with a scheduled job. quadlets/backup.container runs it, units/backup.timer schedules it,
# and the timer is enabled and started regardless of the state requested here.
- name: Deploy application with a nightly backup
  kriansa.quadlet.quadlet_app:
    src: podman-apps/postgres
    state: started
"""

RETURN = r"""
changed:
    description: Whether any changes were made
    type: bool
    returned: always
    sample: true
application_name:
    description: The application name used for deployment
    type: str
    returned: always
    sample: "my-backend-app"
service_name:
    description: The main systemd service name
    type: str
    returned: always
    sample: "my-backend-app--main.service"
quadlet_files:
    description: List of quadlet files deployed to /etc/containers/systemd
    type: list
    returned: always
    sample: ["my-app--main.container", "my-app--db.container", "my-app--data.volume"]
unit_files:
    description: List of plain systemd units deployed to /etc/systemd/system
    type: list
    returned: always
    sample: ["my-app--backup.timer"]
msg:
    description: Human-readable message about the operation
    type: str
    returned: always
    sample: "quadlet files deployed and service started"
"""

# Default deployment paths
QUADLET_SYSTEMD_DIR = "/etc/containers/systemd"
QUADLET_APP_BASE_DIR = "/srv"

# Where plain systemd units go. A timer is not a quadlet type, so it has to be deployed as itself
# rather than into the generator's directory, which systemd never reads.
SYSTEMD_UNIT_DIR = "/etc/systemd/system"

# Quadlet unit types, read from quadlets/
VALID_QUADLET_EXTENSIONS = (".container", ".volume", ".network", ".pod", ".kube")

# Plain systemd units, read from units/
VALID_UNIT_EXTENSIONS = (".timer",)

# Holds one marker per volume recording the ownership that was last applied to it. The marker
# filename carries the uid/gid, so changing a volume's declared owner changes the path and the
# bootstrap unit's condition lets it run again.
QUADLET_APP_STATE_DIR = "/var/lib/quadlet-app"

# Image for the generated volume ownership bootstrap containers. Alpine has no ENTRYPOINT and runs
# as root, so the generated unit needs no Entrypoint= or User= override.
VOLUME_INIT_IMAGE = "docker.io/library/alpine:3.24"

# Mount point for the volume inside the bootstrap container
VOLUME_INIT_TARGET = "/target"

# Ownership applied to a volume that does not declare X-Owner. Container root suits any image that
# runs as root, which is most of them; an image that drops privileges internally has to say so.
DEFAULT_VOLUME_OWNER = "0:0"

# Start timeout for the bootstrap unit. The chown is recursive, so its cost scales with the number
# of files - well past a default start timeout on a large volume. Generous rather than unlimited:
# a run this long means something is wrong, and failing leaves no marker, so it simply retries.
VOLUME_INIT_TIMEOUT_SEC = 1800

# Podman's image and volume store. Containers are ordered against it so they never start before
# the filesystem holding their image is mounted.
QUADLET_SYSTEMD_STORAGE_DIR = "/var/lib/containers/storage"


# A directive line as systemd reads it: optional indentation, the key, then "=" with optional
# whitespace on either side - `Key=value` and `Key = value` mean the same - then the value
DIRECTIVE_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


def parse_directive(line: str) -> Tuple[str, str, str, str] | None:
    """
    Split a unit file line into its parts, if it is a directive.

    Every rule reads directives through this, so they all agree on what counts as one.

    Args:
        line: One line of a unit file

    Returns:
        (indentation, key, separator, value), where the separator is "=" with whatever whitespace
        surrounded it, so a rule rewriting the value can keep the line as it was written. None for
        comments, section headers, blank lines and anything else.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";", "[")):
        return None
    match = DIRECTIVE_RE.match(line)
    if match is None:
        return None
    indentation, key, separator, value = match.groups()
    return indentation, key, separator, value


class ValidationError(Exception):
    """Custom exception for validation errors with detailed messages."""


class QuadletValidator:
    """Handles all validation logic for the quadlet_app module."""

    # Regex pattern for application name validation
    APP_NAME_PATTERN_SINGLE = r"^[a-z]$"
    APP_NAME_PATTERN_MULTI = r"^[a-z][a-z0-9_-]*[a-z0-9]$"

    # Valid init/config subdirectory suffixes
    VALID_SUBDIR_SUFFIXES = {".container", ".pod"}

    def __init__(self, src: str, app_name: str):
        """
        Initialize validator.

        Args:
            src: Source directory path
            app_name: Application name (will be normalized)
        """
        self.src = src
        self.app_name = app_name.lower()  # Normalize to lowercase

    def validate_all(self) -> str:
        """
        Run all validation checks.

        Returns:
            Normalized application name

        Raises:
            ValidationError: If any validation check fails
        """
        self.validate_source_directory()
        self.validate_app_name()
        self.validate_main_container()
        self.validate_init_config_structure()
        return self.app_name

    def validate_source_directory(self):
        """Validate source directory exists and is a directory."""
        if not os.path.exists(self.src):
            raise ValidationError(f"Source directory not found: {self.src}")

        if not os.path.isdir(self.src):
            raise ValidationError(f"Source path is not a directory: {self.src}")

        # Check that quadlets directory exists
        quadlets_dir = os.path.join(self.src, "quadlets")
        if not os.path.isdir(quadlets_dir):
            raise ValidationError(
                f"Required directory not found: {quadlets_dir}. "
                f"The quadlets/ subdirectory is mandatory."
            )

    def validate_app_name(self):
        """Validate application name format."""
        # Check pattern based on length
        if len(self.app_name) == 1:
            pattern = self.APP_NAME_PATTERN_SINGLE
        else:
            pattern = self.APP_NAME_PATTERN_MULTI

        # The whole name has to match: re.match would accept a trailing newline, since $ also
        # matches just before one
        if not re.fullmatch(pattern, self.app_name):
            raise ValidationError(
                f"Invalid application name: {self.app_name}. "
                "Must start with a letter (a-z), end with a letter or number, "
                "and contain only lowercase letters, numbers, hyphens, and underscores."
            )

    def validate_main_container(self):
        """Ensure quadlets/main.container exists."""
        main_container = os.path.join(self.src, "quadlets", "main.container")
        if not os.path.isfile(main_container):
            raise ValidationError(
                f"Required file not found: {main_container}. "
                "The quadlets/main.container file is mandatory."
            )

    def validate_init_config_structure(self):
        """
        Validate init and config subdirectory structure.

        Rules:
        1. Subdirectories use the quadlet stem without suffix (e.g., main/, not main.container/)
        2. A corresponding .container or .pod quadlet file must exist
        3. The match must be unambiguous (no both main.container and main.pod)
        """
        for dir_name in ["init", "config"]:
            dir_path = os.path.join(self.src, dir_name)
            if not os.path.isdir(dir_path):
                continue

            try:
                entries = os.listdir(dir_path)
            except OSError as e:
                raise ValidationError(f"Cannot read {dir_name}: {e}") from e

            for entry in entries:
                entry_path = os.path.join(dir_path, entry)
                if not os.path.isdir(entry_path):
                    continue

                # Reject suffixed directories
                if Path(entry).suffix in self.VALID_SUBDIR_SUFFIXES:
                    raise ValidationError(
                        f"Invalid {dir_name} subdirectory: {entry}. "
                        f"Use the quadlet stem without suffix (e.g., {Path(entry).stem}/ instead of {entry}/)"
                    )

                # Find matching quadlet files by stem
                quadlets_dir = os.path.join(self.src, "quadlets")
                matching = [
                    f for f in os.listdir(quadlets_dir)
                    if Path(f).stem == entry and Path(f).suffix in self.VALID_SUBDIR_SUFFIXES
                ]

                if not matching:
                    raise ValidationError(
                        f"No corresponding quadlet file for {dir_name}/{entry}. "
                        f"Expected a file like quadlets/{entry}.container or quadlets/{entry}.pod"
                    )

                if len(matching) > 1:
                    raise ValidationError(
                        f"Ambiguous {dir_name} subdirectory: {entry}. "
                        f"Multiple matching quadlet files found: {', '.join(sorted(matching))}. "
                        f"Remove one of the conflicting quadlet files"
                    )


class QuadletFileDiscovery:  # pylint: disable=too-few-public-methods
    """Handles discovery of quadlet files and associated resources."""

    def __init__(self, src: str):
        """
        Initialize file discovery.

        Args:
            src: Source directory path
        """
        self.src = src

    def discover_all_files(self) -> Dict[str, List[Tuple[str, str]]]:
        """
        Discover all files that need to be deployed.

        Returns:
            Dictionary with keys:
            - 'quadlets': List of (filename, full_path) for quadlet files
            - 'units': List of (filename, full_path) for plain systemd unit files
            - 'init': List of (container_dir, relative_path, full_path) for init files
            - 'config': List of (container_dir, relative_path, full_path) for config files
        """
        files: Dict[str, List] = {"quadlets": [], "units": [], "init": [], "config": []}

        files["quadlets"] = self._discover_quadlet_files()
        files["units"] = self._discover_unit_files()
        files["init"] = self._discover_init_files()
        files["config"] = self._discover_config_files()

        return files

    def _discover_quadlet_files(self) -> List[Tuple[str, str]]:
        """
        Discover all quadlet files in quadlets/ directory.

        Returns:
            List of (filename, full_path) tuples
        """
        quadlets_dir = os.path.join(self.src, "quadlets")
        quadlet_files = []

        try:
            entries = os.listdir(quadlets_dir)
        except OSError as e:
            raise ValidationError(f"Cannot read quadlets directory: {e}") from e

        for filename in sorted(entries):  # Sort for consistent ordering
            full_path = os.path.join(quadlets_dir, filename)
            if not os.path.isfile(full_path):
                continue

            if filename.endswith(VALID_UNIT_EXTENSIONS):
                raise ValidationError(
                    f"{filename} is a systemd unit, not a quadlet. Move it to units/."
                )

            if filename.endswith(VALID_QUADLET_EXTENSIONS):
                quadlet_files.append((filename, full_path))

        return quadlet_files

    def _discover_unit_files(self) -> List[Tuple[str, str]]:
        """
        Discover all plain systemd unit files in units/ directory.

        Returns:
            List of (filename, full_path) tuples
        """
        units_dir = os.path.join(self.src, "units")
        if not os.path.isdir(units_dir):
            return []

        unit_files = []

        try:
            entries = os.listdir(units_dir)
        except OSError as e:
            raise ValidationError(f"Cannot read units directory: {e}") from e

        for filename in sorted(entries):  # Sort for consistent ordering
            full_path = os.path.join(units_dir, filename)
            if not os.path.isfile(full_path):
                continue

            # Strays are rejected rather than skipped: unlike quadlets/, this directory has no
            # history of holding anything else, so an unrecognized file is a mistake
            if not filename.endswith(VALID_UNIT_EXTENSIONS):
                raise ValidationError(
                    f"Unsupported file in units/: {filename}. "
                    f"Only {', '.join(VALID_UNIT_EXTENSIONS)} files are supported."
                )

            unit_files.append((filename, full_path))

        return unit_files

    def _discover_init_files(self) -> List[Tuple[str, str, str]]:
        """
        Discover all init files in init/ directory.

        Returns:
            List of (container_dir, relative_path, full_path) tuples
        """
        init_dir = os.path.join(self.src, "init")
        if not os.path.isdir(init_dir):
            return []

        init_files = []

        # Walk through container subdirectories
        for container_dir in sorted(os.listdir(init_dir)):
            container_path = os.path.join(init_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            # Walk through all files in this container directory
            for root, _, files in os.walk(container_path):
                # Sort for consistent ordering
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    # Get path relative to container directory
                    rel_path = os.path.relpath(full_path, container_path)
                    init_files.append((container_dir, rel_path, full_path))

        return init_files

    def _discover_config_files(self) -> List[Tuple[str, str, str]]:
        """
        Discover all config files in config/ directory.

        Returns:
            List of (container_dir, relative_path, full_path) tuples
        """
        config_dir = os.path.join(self.src, "config")
        if not os.path.isdir(config_dir):
            return []

        config_files = []

        # Walk through container subdirectories
        for container_dir in sorted(os.listdir(config_dir)):
            container_path = os.path.join(config_dir, container_dir)
            if not os.path.isdir(container_path):
                continue

            # Walk through all files in this container directory
            for root, _, files in os.walk(container_path):
                # Sort for consistent ordering
                for filename in sorted(files):
                    full_path = os.path.join(root, filename)
                    # Get path relative to container directory
                    rel_path = os.path.relpath(full_path, container_path)
                    config_files.append((container_dir, rel_path, full_path))

        return config_files


class QuadletPreprocessor:
    """
    Handles preprocessing of quadlet files according to specification rules.

    Rule 1: Prefix resource references (Network=, Pod=, Volume=)
    Rule 2: Replace %init%/%config% volume paths
    """

    # Default mount options for %init%/%config% volume mounts.
    # idmap rather than U: the kernel translates ids at the mount, so the container sees these
    # files as its own without the host-side tree being chowned out from under Ansible.
    DEFAULT_INIT_CONFIG_VOLUME_OPTIONS = "ro,Z,idmap"

    # Default mount options for named .volume references. Their on-disk ids are container-relative,
    # set once by the volume's bootstrap unit, so idmap presents them correctly whatever range
    # podman hands out.
    DEFAULT_NAMED_VOLUME_OPTIONS = "rw,Z,idmap"

    # Size forced onto a bare UserNS=auto. Podman otherwise derives the range from the image's
    # highest uid, which can be as little as 1024 - far too small for an app that writes as a
    # high uid, and the resulting files are then unmappable.
    DEFAULT_USERNS_SIZE = 65536

    # File extensions that need Rule 1 (prefix resources)
    RULE1_EXTENSIONS = {".container", ".pod", ".kube"}

    # File extensions that need Rule 2 (replace paths)
    RULE2_EXTENSIONS = {".container", ".pod"}

    # Resource name directives for Rule 3 (inject names)
    RESOURCE_NAME_DIRECTIVES = {
        ".container": ("Container", "ContainerName"),
        ".pod": ("Pod", "PodName"),
        ".volume": ("Volume", "VolumeName"),
        ".network": ("Network", "NetworkName"),
    }

    # The service podman's generator emits for each quadlet type, as the suffix replacing the
    # quadlet's own. Something naming a quadlet as a systemd unit has to point at this rather than
    # at the quadlet's filename.
    QUADLET_SERVICE_SUFFIXES = {
        ".container": ".service",
        ".kube": ".service",
        ".pod": "-pod.service",
        ".volume": "-volume.service",
        ".network": "-network.service",
    }

    # Target a timer activates when it does not name one itself
    DEFAULT_TIMER_TARGET = "timers.target"

    # Directives naming one quadlet of the app, optionally followed by ":" and options
    QUADLET_REFERENCE_DIRECTIVES = {"Network", "Pod", "Volume"}

    # Directives naming systemd units, as a space-separated list
    UNIT_LIST_DIRECTIVES = {
        # Names the unit a .timer activates. Not valid in any quadlet type, so it only ever
        # matches in a plain systemd unit.
        "Unit",
        # Systemd unit dependencies
        "Wants",
        "Requires",
        "Requisite",
        "BindsTo",
        "PartOf",
        "Upholds",
        "Conflicts",
        "Before",
        "After",
        # Units enabled and disabled along with this one
        "Also",
    }

    # Directives to process for Rule 1
    RESOURCE_DIRECTIVES = QUADLET_REFERENCE_DIRECTIVES | UNIT_LIST_DIRECTIVES

    def __init__(self, app_name: str, volume_owners: Dict[str, str] | None = None):
        """
        Initialize preprocessor.

        Args:
            app_name: Application name to use for prefixing
            volume_owners: Maps volume stem -> "uid:gid" for volumes declaring X-Owner
        """
        self.app_name = app_name
        self.volume_owners = volume_owners or {}
        # Quadlet stem -> extension, so a unit naming a quadlet can be pointed at the service
        # podman will actually generate for it
        self.quadlet_types: Dict[str, str] = {}
        # Every file name in the app's quadlets/ and units/. Only names the app defines get its
        # prefix: anything else belongs to the host or to another app.
        self.app_files: Set[str] = set()

    def preprocess_quadlet_file(self, content: str, filename: str) -> str:
        """
        Apply preprocessing rules to quadlet file content.

        Args:
            content: File content (already templated by Ansible)
            filename: Quadlet filename (e.g., "main.container")

        Returns:
            Preprocessed content
        """
        file_ext = Path(filename).suffix

        # Which volumes this container mounts has to be read before Rule 1 rewrites the
        # references into their prefixed form
        mounted_volumes = self.referenced_volumes(content)

        # Apply Rule 3 first (inject resource names) - operates on full content
        content = self._apply_rule3_inject_names(content, filename)

        # Determine which rules apply
        needs_rule1 = file_ext in self.RULE1_EXTENSIONS
        needs_rule2 = file_ext in self.RULE2_EXTENSIONS

        # Extract container/pod name from filename (without extension)
        container_name = Path(filename).stem

        # Process line by line (Rules 1 and 2)
        lines = content.split("\n")
        processed_lines = []

        for line in lines:
            # Skip empty lines and comments
            if not line.strip() or line.strip().startswith("#"):
                processed_lines.append(line)
                continue

            # Apply preprocessing rules
            if needs_rule1:
                line = self._apply_rule1_prefix_resources(line)

            if needs_rule2:
                line = self._apply_rule2_replace_paths(line, container_name)

            if file_ext == ".container":
                line = self._normalize_userns(line)

            processed_lines.append(line)

        content = "\n".join(processed_lines)

        # X-Owner is ours, not quadlet's - it never reaches the deployed file
        if file_ext == ".volume":
            declares_owner = self._has_directive(content, "X-Owner")
            content = self._strip_directive(content, "X-Owner")
            if declares_owner:
                content = self._inject_volume_owner(content, Path(filename).stem)

        # Injected last, and deliberately after Rule 1: these values are already app-prefixed, and
        # Rule 1 would prefix them a second time
        if file_ext == ".container":
            content = self._inject_unit_directives(content, mounted_volumes)
            content = self._inject_default_userns(content)

        return content

    def preprocess_unit_file(self, content: str, filename: str) -> str:
        """
        Apply preprocessing rules to a plain systemd unit file.

        Only Rule 1 applies. The rest of the quadlet rules describe things a systemd unit does not
        have: no %init%/%config% mounts, no resource to name, no user namespace, no image store to
        order against.

        Args:
            content: File content (already templated by Ansible)
            filename: Unit filename (e.g., "backup.timer")

        Returns:
            Preprocessed content
        """
        file_ext = Path(filename).suffix

        processed_lines = []
        for line in content.split("\n"):
            if not line.strip() or line.strip().startswith("#"):
                processed_lines.append(line)
                continue

            # Suffix first, prefix second: Unit=backup.pod has to become backup-pod.service before
            # Rule 1 turns it into myapp--backup-pod.service
            if file_ext == ".timer":
                line = self._resolve_timer_unit(line)

            processed_lines.append(self._apply_rule1_prefix_resources(line))

        content = "\n".join(processed_lines)

        # Injected last, and deliberately after Rule 1: these values are already app-prefixed, and
        # Rule 1 would prefix them a second time
        if file_ext == ".timer":
            content = self._inject_timer_defaults(content, filename)

        return content

    def _resolve_timer_unit(self, line: str) -> str:
        """
        Point a timer's Unit= at the service podman generates, rather than at the quadlet.

        Writing Unit=backup.container is the natural thing to do, but no unit by that name ever
        exists - the generator emits backup.service from it.

        Args:
            line: Single line from a .timer file

        Returns:
            Line with the quadlet suffix swapped for the generated service's
        """
        parsed = parse_directive(line)
        if parsed is None or parsed[1] != "Unit":
            return line
        leading_ws, _directive, separator, value = parsed

        value = value.strip()
        suffix = self.QUADLET_SERVICE_SUFFIXES.get(Path(value).suffix)
        if suffix is None:
            return line

        return f"{leading_ws}Unit{separator}{Path(value).stem}{suffix}"

    def _inject_timer_defaults(self, content: str, filename: str) -> str:
        """
        Add the directives a timer needs to do anything at all.

        Without a WantedBy, `systemctl enable` is a no-op and the timer never arms on boot. Unit=
        is injected only for readability - systemd would default to the same basename anyway - but
        spelling it out makes the deployed file say what it triggers.

        Args:
            content: File content, after Rule 1
            filename: Timer filename (e.g., "backup.timer")

        Returns:
            Content with the defaults in place
        """
        # A timer with no [Timer] section is broken whatever we do, and opening one here would
        # only hide that behind a file that still never fires
        if self._has_section(content, "Timer") and not self._has_directive(content, "Unit"):
            target = self._timer_target_unit(filename)
            content = self._append_to_section(content, "Timer", [f"Unit={target}"])

        if not self._has_directive(content, "WantedBy"):
            content = self._append_to_section(
                content, "Install", [f"WantedBy={self.DEFAULT_TIMER_TARGET}"], at_end=True
            )

        return content

    def _timer_target_unit(self, filename: str) -> str:
        """
        Name the service a timer triggers when it does not say so itself.

        Args:
            filename: Timer filename (e.g., "backup.timer")

        Returns:
            Prefixed service name (e.g. "myapp--backup.service")
        """
        stem = Path(filename).stem
        quadlet_ext = self.quadlet_types.get(stem)
        suffix = self.QUADLET_SERVICE_SUFFIXES.get(quadlet_ext, ".service")
        return f"{self.app_name}--{stem}{suffix}"

    @staticmethod
    def _has_section(content: str, section: str) -> bool:
        """
        Check whether a section header appears.

        Args:
            content: File content
            section: Section name without brackets (e.g. "Timer")

        Returns:
            True if the section is present
        """
        header = f"[{section}]"
        return any(line.strip() == header for line in content.split("\n"))

    def _inject_volume_owner(self, content: str, stem: str) -> str:
        """
        Have podman create the volume already owned by its declared owner.

        Podman's local driver re-chowns an *empty* volume to the container's user every time a
        container starts - which would undo the ownership the bootstrap unit set, while its marker
        keeps it from ever running again. A volume created with an owner of its own is left alone.
        The ids are the declared ones unchanged: volumes are mounted idmapped, so on-disk ids are
        the container's.

        Only volumes created after this is in place benefit; podman does not change an existing
        volume's options. With a volume driver, quadlet passes these on to the driver as an `o=`
        option, which a driver may or may not accept.

        Args:
            content: .volume file content, X-Owner already stripped
            stem: Volume stem (e.g. "data")

        Returns:
            Content with User= and Group= in [Volume], unless either is already set
        """
        owner = self.volume_owners.get(stem)
        if owner is None or self._has_directive(content, "User") or self._has_directive(content, "Group"):
            return content
        uid, gid = owner.split(":")
        return self._append_to_section(content, "Volume", [f"User={uid}", f"Group={gid}"])

    def _inject_default_userns(self, content: str) -> str:
        """
        Give a container a user namespace unless it asked for something else.

        Rootful podman puts a container in the host's namespace by default, so its root is the
        host's root. Defaulting to `auto` gives each container its own unprivileged range instead.
        Volumes are unaffected: they are mounted idmapped, so their on-disk ids are the container's
        own either way.

        A container that genuinely needs host identity - reading the host journal, or another
        process's /proc entries - must say so with an explicit UserNS=host. Inside a namespace
        those files read as nobody, and, worse, world-readable ones keep working, so such a
        container half-works rather than failing outright.

        Args:
            content: File content, after Rules 1-3

        Returns:
            Content with a default UserNS if none was declared
        """
        if self._has_directive(content, "UserNS"):
            return content

        # A container joining a pod lives in the pod's user namespace, and podman refuses to start
        # one that asks for its own
        if self._has_directive(content, "Pod"):
            return content

        return self._append_to_section(content, "Container", [
            "# Default from quadlet_app. Set UserNS= explicitly to override; UserNS=host is what a",
            "# container needs if it reads the host journal or other processes' /proc entries.",
            f"UserNS=auto:size={self.DEFAULT_USERNS_SIZE}",
        ])

    @staticmethod
    def _has_directive(content: str, directive: str) -> bool:
        """
        Check whether a directive appears at all, ignoring comments.

        Args:
            content: File content
            directive: Directive name, without the '='

        Returns:
            True if the directive is set
        """
        return any(
            parsed is not None and parsed[1] == directive
            for parsed in (parse_directive(line) for line in content.split("\n"))
        )

    def referenced_volumes(self, content: str) -> List[str]:
        """
        Find the named .volume resources a quadlet file mounts.

        Args:
            content: File content, before Rule 1 has prefixed anything

        Returns:
            Volume stems in first-seen order (e.g. ["data"] for Volume=data.volume:/x)
        """
        stems = []
        for line in content.split("\n"):
            parsed = parse_directive(line)
            if parsed is None or parsed[1] != "Volume":
                continue
            source = parsed[3].strip().split(":", 1)[0]
            if source.startswith("/") or not source.endswith(".volume"):
                continue
            stem = Path(source).stem
            if stem not in stems:
                stems.append(stem)
        return stems

    @staticmethod
    def _strip_directive(content: str, directive: str) -> str:
        """
        Remove every occurrence of a directive from file content.

        Args:
            content: File content
            directive: Directive name, without the '='

        Returns:
            Content with those lines dropped
        """
        return "\n".join(
            line
            for line in content.split("\n")
            if (parse_directive(line) or (None, None))[1] != directive
        )

    def _normalize_userns(self, line: str) -> str:
        """
        Force an explicit range size onto a bare UserNS=auto.

        Podman sizes an unqualified `auto` range from the image's highest uid, which is routinely
        far smaller than the uids the app actually writes with. Anything above the range is
        unmappable: it reads back as 65534 and is not writable even by container root.

        Explicit uidmapping= or size=, and every non-auto mode, are left untouched.

        Args:
            line: Single line from a .container file

        Returns:
            Line with size= added when it was missing
        """
        parsed = parse_directive(line)
        if parsed is None or parsed[1] != "UserNS":
            return line
        leading_ws, _directive, equals, value = parsed

        value = value.strip()
        if not value.startswith("auto"):
            return line
        if "size=" in value or "uidmapping=" in value:
            return line

        separator = "," if value.startswith("auto:") else ":"
        return f"{leading_ws}UserNS{equals}{value}{separator}size={self.DEFAULT_USERNS_SIZE}"

    def _inject_unit_directives(self, content: str, mounted_volumes: List[str]) -> str:
        """
        Add the [Unit] directives every container gets by default.

        Two of them:
        - RequiresMountsFor on the podman image store, so a container never starts before the
          storage it lives on is mounted. Declaring it here keeps it out of every app's quadlet.
        - Ordering against the ownership bootstrap unit of each volume that declares an owner.

        Args:
            content: File content, after Rules 1-3
            mounted_volumes: Volume stems this container mounts

        Returns:
            Content with the directives injected
        """
        directives = []

        storage_mount = f"RequiresMountsFor={QUADLET_SYSTEMD_STORAGE_DIR}"
        # RequiresMountsFor is additive in systemd and an app may legitimately declare its own for
        # a different path, so match on the path rather than on the directive
        if not self._has_directive_value(content, "RequiresMountsFor", QUADLET_SYSTEMD_STORAGE_DIR):
            directives.append(storage_mount)

        for stem in mounted_volumes:
            if stem not in self.volume_owners:
                continue
            unit = f"{self.app_name}--{stem}-volume-init.service"
            directives.extend([f"Requires={unit}", f"After={unit}"])

        if not directives:
            return content

        return self._append_to_section(content, "Unit", directives)

    @staticmethod
    def _has_directive_value(content: str, directive: str, value: str) -> bool:
        """Check whether a directive already carries a given value."""
        for line in content.split("\n"):
            parsed = parse_directive(line)
            if parsed is not None and parsed[1] == directive and value in parsed[3].split():
                return True
        return False

    @staticmethod
    def _is_section_header(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("[") and stripped.endswith("]")

    @staticmethod
    def _append_to_section(
        content: str, section: str, directives: List[str], at_end: bool = False
    ) -> str:
        """
        Append directives to a section, creating the section when it is absent.

        Args:
            content: File content
            section: Section name without brackets (e.g. "Unit")
            directives: Lines to add
            at_end: Open a missing section at the end of the file rather than the top. [Install]
                reads as an afterthought and belongs last; [Unit] belongs first.

        Returns:
            Content with the directives in place
        """
        lines = content.split("\n")
        header = f"[{section}]"

        start = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                start = i
                break

        if start is None:
            if at_end:
                # Trailing blank lines belong before the new section, not inside it
                while lines and not lines[-1].strip():
                    lines.pop()
                separator = [""] if lines else []
                return "\n".join(lines + separator + [header] + directives + [""])

            # No such section - open one ahead of every other, but below the comments that open
            # the file. A comment with no blank line between it and the first header is about that
            # section, so it stays with it: the new section goes after the last blank line.
            first_header = next(
                (i for i, line in enumerate(lines) if QuadletPreprocessor._is_section_header(line)),
                len(lines),
            )
            split = 0
            for i in range(first_header):
                if not lines[i].strip():
                    split = i + 1
            if not any(line.strip() for line in lines[:split]):
                split = 0
            return "\n".join(lines[:split] + [header] + directives + [""] + lines[split:])

        # Insert at the end of the section, before the next section header
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if QuadletPreprocessor._is_section_header(lines[i]):
                end = i
                break

        # Keep the blank line that separated this section from the next
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1

        return "\n".join(lines[:end] + directives + lines[end:])

    def _apply_rule1_prefix_resources(self, line: str) -> str:
        """
        Rule 1: Add the application name prefix to references to the app's own files and units.

        Transforms:
            Network=main.network -> Network=myapp--main.network
            Pod=backend.pod -> Pod=myapp--backend.pod
            Volume=data.volume:... -> Volume=myapp--data.volume:...
            Wants=db.service -> Wants=myapp--db.service
            After=db.service sshd.service -> After=myapp--db.service sshd.service

        A name the app does not define is left as written - sshd.service is the host's, and
        other--db.service another app's, spelled out in full.

        Args:
            line: Single line from quadlet file

        Returns:
            Processed line with prefixed resources
        """
        parsed = parse_directive(line)
        if parsed is None:
            return line
        leading_ws, directive, separator, value = parsed

        if directive in self.UNIT_LIST_DIRECTIVES:
            # Each name on its own; the spacing between them, and any \r a file saved with
            # Windows line endings leaves at the end, stay as they are
            units = self.app_units
            value = re.sub(
                r"\S+",
                lambda match: (
                    f"{self.app_name}--{match.group(0)}" if match.group(0) in units
                    else match.group(0)
                ),
                value,
            )
            return f"{leading_ws}{directive}{separator}{value}"

        if directive not in self.QUADLET_REFERENCE_DIRECTIVES:
            return line

        body = value.rstrip()
        trailing = value[len(body):]
        name, colon, rest = body.partition(":")
        if name not in self.app_files:
            return line

        if directive == "Volume" and name.endswith(".volume") and rest and ":" not in rest:
            # A destination but no options: give it the defaults. Without a destination there is
            # nowhere to put them - podman would read them as the destination.
            rest = f"{rest}:{self.DEFAULT_NAMED_VOLUME_OPTIONS}"

        return f"{leading_ws}{directive}{separator}{self.app_name}--{name}{colon}{rest}{trailing}"

    @property
    def app_units(self) -> Set[str]:
        """
        The unit names the app defines: its files, and the services podman generates from them.
        """
        units = set(self.app_files)
        for filename in self.app_files:
            suffix = self.QUADLET_SERVICE_SUFFIXES.get(Path(filename).suffix)
            if suffix is not None:
                units.add(f"{Path(filename).stem}{suffix}")
        return units

    def _apply_rule2_replace_paths(self, line: str, container_name: str) -> str:
        """
        Rule 2: Replace %init%/%config% volume paths.

        Transforms:
            Volume=%init%:...      -> Volume={QUADLET_APP_BASE_DIR}/myapp/init/container:...:ro,Z,U
            Volume=%config%/sub:...-> Volume={QUADLET_APP_BASE_DIR}/myapp/config/container/sub:...:ro,Z,U

        If no mount options are specified, ro,Z,U is applied automatically.
        Explicit options are always preserved as-is.

        Args:
            line: Single line from quadlet file
            container_name: Name of the container (without extension)

        Returns:
            Processed line with replaced paths
        """
        # Only process Volume= directives
        parsed = parse_directive(line)
        if parsed is None or parsed[1] != "Volume":
            return line
        leading_ws, _directive, separator, value = parsed
        # Trailing whitespace - such as the \r a file saved with Windows line endings leaves - stays
        # at the end of the line, rather than ending up inside the destination
        body = value.rstrip()
        trailing = value[len(body):]

        # Parse Volume directive: Volume=SOURCE:DEST[:OPTIONS]
        parts = body.split(":", 2)  # [source, dest, options?]
        source = parts[0]
        dest = parts[1] if len(parts) > 1 else ""
        options = parts[2] if len(parts) > 2 else None  # None = not specified

        # Skip if source is an absolute path
        if source.startswith("/"):
            return line

        # Process %init% paths
        if source.startswith("%init%"):
            subpath = source[6:]  # len("%init%") == 6
            # Remove leading slash if present
            if subpath.startswith("/"):
                subpath = subpath[1:]

            # Build replacement path
            new_source = f"{QUADLET_APP_BASE_DIR}/{self.app_name}/init/{container_name}"
            if subpath:
                new_source += f"/{subpath}"

            effective_options = options if options is not None else self.DEFAULT_INIT_CONFIG_VOLUME_OPTIONS
            return f"{leading_ws}Volume{separator}{new_source}:{dest}:{effective_options}{trailing}"

        # Process %config% paths
        if source.startswith("%config%"):
            subpath = source[8:]  # len("%config%") == 8
            # Remove leading slash if present
            if subpath.startswith("/"):
                subpath = subpath[1:]

            # Build replacement path
            new_source = f"{QUADLET_APP_BASE_DIR}/{self.app_name}/config/{container_name}"
            if subpath:
                new_source += f"/{subpath}"

            effective_options = options if options is not None else self.DEFAULT_INIT_CONFIG_VOLUME_OPTIONS
            return f"{leading_ws}Volume{separator}{new_source}:{dest}:{effective_options}{trailing}"

        return line

    def _apply_rule3_inject_names(self, content: str, filename: str) -> str:
        """
        Rule 3: Inject resource name directives if missing.

        Adds ContainerName, PodName, VolumeName, or NetworkName to the
        appropriate section if not already present.

        Args:
            content: File content
            filename: Quadlet filename (e.g., "main.container")

        Returns:
            Content with name directive injected if needed
        """
        file_ext = Path(filename).suffix

        # Check if this file type needs name injection
        if file_ext not in self.RESOURCE_NAME_DIRECTIVES:
            return content

        section_name, directive_name = self.RESOURCE_NAME_DIRECTIVES[file_ext]
        resource_name = Path(filename).stem
        name_value = f"{self.app_name}--{resource_name}"

        # Parse content into sections. Whatever comes before the first header - usually comments
        # about the file - is kept as a nameless section of its own.
        sections = []
        current_section = None
        current_lines = []

        for line in content.split("\n"):
            if self._is_section_header(line):
                sections.append((current_section, current_lines))
                current_section = line.strip()[1:-1]  # Remove brackets
                current_lines = [line]
            else:
                current_lines.append(line)

        sections.append((current_section, current_lines))

        # Find target section and inject directive if needed
        for i, (section, lines) in enumerate(sections):
            if section == section_name:
                # Check if directive already exists (ignore comments)
                has_directive = self._has_directive("\n".join(lines), directive_name)

                if not has_directive:
                    # Find section header and inject after it
                    for j, line in enumerate(lines):
                        if line.strip().startswith("["):
                            lines.insert(j + 1, f"{directive_name}={name_value}")
                            break

                sections[i] = (section, lines)
                break

        # Reconstruct content
        all_lines = []
        for _section_name, lines in sections:
            all_lines.extend(lines)

        return "\n".join(all_lines)


class QuadletIdempotency:
    """Handles idempotency checking using file checksums."""

    def __init__(self, app_name: str, force: bool):
        """
        Initialize idempotency checker.

        Args:
            app_name: Application name
            force: If True, always report changes needed
        """
        self.app_name = app_name
        self.force = force
        self.quadlet_dest = QUADLET_SYSTEMD_DIR
        self.srv_base = f"{QUADLET_APP_BASE_DIR}/{app_name}"

    def needs_deployment(self, processed_files: Dict[str, str]) -> bool:
        """
        Check if deployment is needed based on file changes.

        Args:
            processed_files: Dict mapping destination_path -> content

        Returns:
            True if deployment is needed, False otherwise
        """
        if self.force:
            return True

        for dest_path, new_content in processed_files.items():
            if self.file_changed(dest_path, new_content):
                return True

        return False

    def file_changed(self, dest_path: str, new_content: str) -> bool:
        """
        Check if a file has changed by comparing checksums.

        Args:
            dest_path: Destination file path
            new_content: New file content

        Returns:
            True if file doesn't exist or content differs
        """
        # Check if file exists
        if not os.path.exists(dest_path):
            return True

        # Check if it's a regular file
        if not os.path.isfile(dest_path):
            return True

        # Read existing content
        try:
            with open(dest_path, "r", encoding="utf-8") as f:
                existing_content = f.read()
        except (IOError, OSError, UnicodeDecodeError):
            # If we can't read it, assume it changed
            return True

        # Compare checksums
        existing_checksum = self._calculate_checksum(existing_content)
        new_checksum = self._calculate_checksum(new_content)

        return existing_checksum != new_checksum

    @staticmethod
    def _calculate_checksum(content: str) -> str:
        """
        Calculate SHA256 checksum of content.

        Args:
            content: String content

        Returns:
            Hex digest of SHA256 hash
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class QuadletAppModule:  # pylint: disable=too-few-public-methods
    """Main module class coordinating all components."""

    def __init__(self, module: AnsibleModule):
        """
        Initialize the module.

        Args:
            module: AnsibleModule instance
        """
        self.module = module
        self.params = module.params
        self.changed = False

        # Extract parameters
        self.src = self.params["src"]
        # normpath first: the basename of "apps/myapp/" is empty
        self.app_name = self.params.get("name") or os.path.basename(os.path.normpath(self.src))
        self.state = self.params["state"]
        self.force = self.params["force"]
        self.systemctl_timeout = self.params["systemctl_timeout"]
        self.settle_time = self.params["settle_time"]

        # Check if files were processed by action plugin on control node
        self.control_node_processed = self.params.get("_control_node_processed", False)
        # Files grouped as quadlets, units, init and config; built from _files/_contents in run()
        self.files_data: Dict[str, List[Dict]] | None = None

        # Paths
        self.quadlet_dest = QUADLET_SYSTEMD_DIR
        self.unit_dest = SYSTEMD_UNIT_DIR
        self.srv_base: str | None = None  # Set after app_name validation

        # Volume stem -> "uid:gid", from X-Owner. Populated while processing quadlets.
        self.volume_owners: Dict[str, str] = {}
        # Quadlet files this module derives rather than reads from the app directory
        self.generated_quadlets: List[str] = []
        # Deployed timer unit name -> whether its file content changed this run. Recorded before
        # deployment overwrites the files, so the systemd phase can still tell them apart.
        self.timer_changes: Dict[str, bool] = {}
        # Generated ownership bootstrap services whose volume has yet to get its declared owner
        self.pending_volume_inits: List[str] = []

        # Initialize components
        self.validator: QuadletValidator | None = None
        self.discovery: QuadletFileDiscovery | None = None
        self.preprocessor: QuadletPreprocessor | None = None
        self.idempotency: QuadletIdempotency | None = None

    def run(self):
        """Main execution flow."""
        try:
            # Phase 1: Validation (app name only if from action plugin)
            if self.control_node_processed:
                # Minimal validation - action plugin already validated structure
                self.app_name = self.app_name.lower()
                QuadletValidator(self.src, self.app_name).validate_app_name()
                self.files_data = self._files_data_from_params()
            else:
                # Full validation when running without action plugin
                self.validator = QuadletValidator(self.src, self.app_name)
                self.app_name = self.validator.validate_all()

            self.srv_base = f"{QUADLET_APP_BASE_DIR}/{self.app_name}"

            # Initialize remaining components after app_name validation
            self.preprocessor = QuadletPreprocessor(self.app_name)
            self.idempotency = QuadletIdempotency(self.app_name, self.force)

            # Phase 2 & 3: Get file content (either from action plugin or local)
            if self.control_node_processed:
                # Files already templated by action plugin
                processed_files = self._process_files_from_action_plugin()
                quadlet_names = [f["name"] for f in self.files_data["quadlets"]]
                unit_names = [f["name"] for f in self.files_data.get("units", [])]
            else:
                # Discover and read files locally (fallback mode)
                self.discovery = QuadletFileDiscovery(self.src)
                discovered_files = self.discovery.discover_all_files()
                processed_files = self._process_files(discovered_files)
                quadlet_names = [name for name, _ in discovered_files["quadlets"]]
                unit_names = [name for name, _ in discovered_files["units"]]

            deployed_quadlets = [
                f"{self.app_name}--{name}" for name in quadlet_names
            ] + self.generated_quadlets
            deployed_units = [f"{self.app_name}--{name}" for name in unit_names]

            # Which timers changed has to be read before deployment overwrites them
            self._record_timer_changes(processed_files)
            self.pending_volume_inits = self._pending_volume_inits()

            # Phase 4: Check idempotency
            needs_update = self.idempotency.needs_deployment(processed_files)

            # Also check if service state needs management
            service_name = f"{self.app_name}--main.service"
            needs_service_management = self._needs_service_management(service_name)

            if not needs_update and not needs_service_management:
                # No changes needed - files and service state are correct
                self.module.exit_json(
                    changed=False,
                    application_name=self.app_name,
                    service_name=service_name,
                    quadlet_files=deployed_quadlets,
                    unit_files=deployed_units,
                    msg="application already up to date",
                )

            # Phase 5: Deploy files (only if they changed), once podman's generator has accepted
            # them: a file it rejects must never reach the host
            if needs_update:
                self._validate_quadlet_syntax(processed_files)
                self._deploy_files(processed_files)
                self.changed = True

            # Phase 6: Manage systemd (if files changed OR service needs management)
            if needs_update or needs_service_management:
                service_changed = self._manage_systemd()
                if service_changed:
                    self.changed = True

            # Success
            self.module.exit_json(
                changed=self.changed,
                application_name=self.app_name,
                service_name=service_name,
                quadlet_files=deployed_quadlets,
                unit_files=deployed_units,
                msg=self._get_success_message(),
            )

        except ValidationError as e:
            self.module.fail_json(msg=str(e))
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.module.fail_json(msg=f"Unexpected error: {str(e)}")

    def _process_files(self, discovered_files: Dict) -> Dict[str, str]:
        """
        Process all discovered files.

        Args:
            discovered_files: Output from FileDiscovery.discover_all_files()

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.srv_base is not None

        quadlet_contents = {
            filename: self._read_file(full_path)
            for filename, full_path in discovered_files["quadlets"]
        }
        unit_contents = {
            filename: self._read_file(full_path)
            for filename, full_path in discovered_files["units"]
        }
        processed = self._process_quadlets(quadlet_contents, set(unit_contents))
        processed.update(self._process_units(unit_contents))

        # init and config files are deployed verbatim - no preprocessing
        for kind in ("init", "config"):
            for container_dir, rel_path, full_path in discovered_files[kind]:
                container_name = Path(container_dir).stem  # Remove .container extension
                dest_path = os.path.join(self.srv_base, kind, container_name, rel_path)
                processed[dest_path] = self._read_file(full_path)

        return processed

    def _files_data_from_params(self) -> Dict[str, List[Dict]]:
        """
        Put the files the action plugin sent back together, grouped by kind.

        Returns:
            Dict with 'quadlets', 'units', 'init' and 'config' lists, each entry carrying its
            content next to its name, or its container directory and path

        Raises:
            ValidationError: If names and contents do not match up
        """
        files = self.params.get("_files") or []
        contents = self.params.get("_contents") or []
        if len(files) != len(contents):
            raise ValidationError(
                f"_files and _contents must have the same length ({len(files)} and {len(contents)})"
            )

        groups = {"quadlet": "quadlets", "unit": "units", "init": "init", "config": "config"}
        files_data: Dict[str, List[Dict]] = {group: [] for group in groups.values()}
        for entry, content in zip(files, contents):
            fields = {key: value for key, value in entry.items() if key != "kind"}
            files_data[groups[entry["kind"]]].append({**fields, "content": content})
        return files_data

    def _process_files_from_action_plugin(self) -> Dict[str, str]:
        """
        Process files that were already templated by the action plugin.

        Files are already templated - we only need to preprocess quadlets
        and map to destination paths.

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.srv_base is not None

        quadlet_contents = {f["name"]: f["content"] for f in self.files_data["quadlets"]}
        unit_contents = {f["name"]: f["content"] for f in self.files_data.get("units", [])}
        processed = self._process_quadlets(quadlet_contents, set(unit_contents))
        processed.update(self._process_units(unit_contents))

        # init and config files are deployed verbatim - no preprocessing
        for kind in ("init", "config"):
            for file_data in self.files_data[kind]:
                container_name = Path(file_data["container"]).stem
                dest_path = os.path.join(self.srv_base, kind, container_name, file_data["path"])
                processed[dest_path] = file_data["content"]

        return processed

    def _process_quadlets(
        self, quadlet_contents: Dict[str, str], unit_names: Set[str]
    ) -> Dict[str, str]:
        """
        Preprocess quadlet files and generate the units derived from them.

        Args:
            quadlet_contents: Maps quadlet filename -> raw (already templated) content
            unit_names: Filenames in units/, which a quadlet may name too

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.preprocessor is not None

        # Ownership has to be known before any container is preprocessed: it decides both the
        # mount options each volume reference gets and which bootstrap units exist to order against
        self.volume_owners = self._collect_volume_owners(quadlet_contents)
        self.preprocessor.volume_owners = self.volume_owners

        # Lets a unit in units/ be pointed at the service podman generates for a given quadlet
        self.preprocessor.quadlet_types = {
            Path(filename).stem: Path(filename).suffix for filename in quadlet_contents
        }
        self.preprocessor.app_files = set(quadlet_contents) | unit_names

        processed = {}

        for filename, content in quadlet_contents.items():
            dest_path = os.path.join(self.quadlet_dest, f"{self.app_name}--{filename}")
            processed[dest_path] = self.preprocessor.preprocess_quadlet_file(content, filename)

        for stem, owner in self.volume_owners.items():
            filename = f"{self.app_name}--{stem}-volume-init.container"
            self.generated_quadlets.append(filename)
            processed[os.path.join(self.quadlet_dest, filename)] = self._volume_init_unit(
                stem, owner
            )

        return processed

    def _process_units(self, unit_contents: Dict[str, str]) -> Dict[str, str]:
        """
        Preprocess plain systemd unit files.

        Must run after _process_quadlets, which tells the preprocessor what each quadlet is - a
        timer naming one has to be pointed at the service podman generates from it.

        Args:
            unit_contents: Maps unit filename -> raw (already templated) content

        Returns:
            Dict mapping destination_path -> content
        """
        assert self.preprocessor is not None

        return {
            os.path.join(self.unit_dest, f"{self.app_name}--{filename}"):
                self.preprocessor.preprocess_unit_file(content, filename)
            for filename, content in unit_contents.items()
        }

    def _record_timer_changes(self, processed_files: Dict[str, str]):
        """
        Note which timers have new content, before deployment overwrites them.

        A timer only needs restarting when its own file changed - restarting one because an
        unrelated container was edited would reset the countdown of every relative timer in the app
        on every deploy.

        Args:
            processed_files: Dict mapping destination_path -> content
        """
        assert self.idempotency is not None

        for dest_path, content in processed_files.items():
            if not dest_path.endswith(".timer"):
                continue
            self.timer_changes[os.path.basename(dest_path)] = self.force or (
                self.idempotency.file_changed(dest_path, content)
            )

    def _collect_volume_owners(self, quadlet_contents: Dict[str, str]) -> Dict[str, str]:
        """
        Determine the owner of every volume the app declares.

        A .volume without X-Owner falls back to container root, which is right for any image
        running as root. An image that drops privileges internally has to declare its own uid,
        or its data ends up owned by a user it no longer is.

        Args:
            quadlet_contents: Maps quadlet filename -> raw content

        Returns:
            Maps volume stem -> validated "uid:gid"

        Raises:
            ValidationError: On a malformed or unusable X-Owner value
        """
        owners = {}

        for filename, content in quadlet_contents.items():
            if not filename.endswith(".volume"):
                continue

            owner = DEFAULT_VOLUME_OWNER
            for line in content.split("\n"):
                parsed = parse_directive(line)
                if parsed is None or parsed[1] != "X-Owner":
                    continue
                owner = self._validate_owner(parsed[3].strip(), filename)

            owners[Path(filename).stem] = owner

        return owners

    @staticmethod
    def _validate_owner(value: str, filename: str) -> str:
        """
        Check an X-Owner value is a uid:gid pair the consuming container can actually see.

        Args:
            value: Raw directive value
            filename: Quadlet filename, for the error message

        Returns:
            Normalized "uid:gid"

        Raises:
            ValidationError: If malformed, or outside the consuming container's user namespace
        """
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValidationError(
                f"Invalid X-Owner in {filename}: '{value}'. "
                "Expected a numeric uid:gid pair, for example X-Owner=10000:10000"
            )

        limit = QuadletPreprocessor.DEFAULT_USERNS_SIZE
        if any(int(part) >= limit for part in parts):
            raise ValidationError(
                f"X-Owner in {filename} is out of range: '{value}'. Ids must be below {limit}, "
                "which is the size of the user namespace the consuming container gets. A higher "
                "id would be written to disk and then be invisible to the app that needs it."
            )

        return ":".join(parts)

    def _volume_init_unit(self, stem: str, owner: str) -> str:
        """
        Build the ownership bootstrap unit for one volume.

        It chowns the volume once and is then skipped: the marker filename carries the uid/gid, so
        changing X-Owner changes the path and the unit's condition lets it run again. When it does,
        it replaces the marker of the previous owner with its own, so the state directory always
        lists the owner each volume actually has.

        Deliberately no UserNS and no idmap on the mount - it has to run as real host root. An
        existing volume's files are owned by host ids outside any container's range, and a process
        in a user namespace cannot chown a file whose owner is unmapped, not even as container
        root. What it writes is the container-relative id, which is exactly what the app then sees
        through its own idmapped mount.

        Args:
            stem: Volume stem (e.g. "data")
            owner: Validated "uid:gid"

        Returns:
            Quadlet file content
        """
        volume = f"{self.app_name}--{stem}"
        marker = self._volume_marker(stem, owner)
        # Every marker of this volume, whatever owner it names, and nothing else: the dot after the
        # name keeps 'app--data' from matching 'app--data2', and the ids hold no dot, so the markers
        # of a volume named 'data.v2' do not match either. [.] stands for a literal dot, to keep
        # backslashes - which systemd reads as escapes of its own - out of the command line.
        previous_markers = f".*/{volume}[.][^./]+_[^./]+[.]done"

        # Children first and the root last, so the root's ownership is a true completion marker:
        # an interrupted run cannot leave a half-chowned tree behind a satisfied condition
        chown = (
            f"find {VOLUME_INIT_TARGET} -mindepth 1 -print0 | xargs -0r chown -h {owner}; "
            f"chown {owner} {VOLUME_INIT_TARGET}"
        )

        return "\n".join([
            "# Generated by kriansa.quadlet.quadlet_app - do not edit",
            "[Unit]",
            f"Description=Ownership bootstrap for {volume}",
            f"ConditionPathExists=!{marker}",
            "",
            "[Container]",
            f"ContainerName={volume}-volume-init",
            f"Image={VOLUME_INIT_IMAGE}",
            # Lowercase z: the consuming container's Z relabels the volume to its own private
            # category moments later, so taking a private one here would only be discarded
            f"Volume={volume}.volume:{VOLUME_INIT_TARGET}:rw,z",
            f"Exec=/bin/sh -c '{chown}'",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"TimeoutStartSec={VOLUME_INIT_TIMEOUT_SEC}",
            # These run only once the chown has succeeded
            f"ExecStartPost=/usr/bin/mkdir -p {QUADLET_APP_STATE_DIR}",
            f"ExecStartPost=/usr/bin/find {QUADLET_APP_STATE_DIR} -maxdepth 1 "
            f"-regex '{previous_markers}' -delete",
            f"ExecStartPost=/usr/bin/touch {marker}",
            "",
        ])

    def _volume_marker(self, stem: str, owner: str) -> str:
        """The file a volume's bootstrap unit leaves once it has given the volume this owner."""
        return f"{QUADLET_APP_STATE_DIR}/{self.app_name}--{stem}.{owner.replace(':', '_')}.done"

    def _deploy_files(self, processed_files: Dict[str, str]):
        """
        Deploy all processed files to their destinations.

        Args:
            processed_files: Dict mapping destination_path -> content
        """
        for dest_path, content in processed_files.items():
            # Create parent directory
            dest_dir = os.path.dirname(dest_path)
            try:
                os.makedirs(dest_dir, mode=0o755, exist_ok=True)
            except OSError as e:
                self.module.fail_json(msg=f"Failed to create directory {dest_dir}: {e}")

            # Written next to its destination under a temporary name and then renamed over it, so a
            # write that fails halfway leaves the previous version in place, not a truncated file.
            # The temporary name ends in random characters, so neither podman's generator nor
            # systemd would load it should it ever be left behind.
            try:
                fd, temp_path = tempfile.mkstemp(
                    dir=dest_dir, prefix=f".{os.path.basename(dest_path)}."
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.chmod(temp_path, 0o644)
                    os.replace(temp_path, dest_path)
                except BaseException:
                    os.unlink(temp_path)
                    raise
            except (OSError, UnicodeError, TypeError) as e:
                self.module.fail_json(msg=f"Failed to write file {dest_path}: {e}")

    def _manage_systemd(self):
        """
        Manage systemd: reload, enable, start/restart.

        Returns:
            bool: True if any systemd operations were performed
        """
        service_changed = False
        service_name = f"{self.app_name}--main.service"

        # Reload daemon if files changed
        if self.changed:
            self._systemctl(["daemon-reload"])
            service_changed = True

        # Timers are reconciled on every state, main service or not, and only after the reload
        # above - systemd has not seen a newly written unit file before that
        if self._reconcile_timers():
            service_changed = True

        # Which run of each service was current before anything is started, so the ones this run
        # starts - itself, or through systemd pulling them in - can be told apart afterwards
        watched = []
        if self.state != "installed" and self.settle_time > 0:
            watched = self._app_services()
        before = self._unit_properties(watched)

        # Determine if we need to restart dependencies and main service
        needs_restart = False
        if self.state == "restarted":
            # Always restart when restarted state is requested
            needs_restart = True
        elif self.state == "started" and self.changed:
            # Restart when files changed, whether main runs or not: a container main depends on may
            # still be running its old definition, and starting main alone would leave it so.
            # Restarting a stopped unit starts it.
            needs_restart = True

        if needs_restart:
            self._restart_app(service_name)
            service_changed = True
        else:
            # Main is not being restarted, but a volume waiting for its owner still gets it - and
            # before main mounts the volume, if it is about to be started below. Not under
            # installed, which leaves running containers alone: running a bootstrap unit again
            # restarts every container that mounts its volume.
            if self.state != "installed" and self._restart_volume_init_units():
                service_changed = True
            if self.state == "started" and not self._is_service_active(service_name):
                # Start service if not already active (no restart needed)
                self._systemctl(["start", service_name])
                service_changed = True

        self._check_services_stayed_up(before)

        return service_changed

    def _app_services(self) -> List[str]:
        """
        The services podman generates from the app's containers, pods and kube files.

        Volumes, networks and the ownership bootstrap units are left out: they run once and are
        done, and systemctl waits for that and reports their failure itself.
        """
        assert self.preprocessor is not None

        services = []
        for filename in sorted(self.preprocessor.app_files):
            suffix = Path(filename).suffix
            if suffix in (".container", ".pod", ".kube"):
                stem = Path(filename).stem
                services.append(
                    f"{self.app_name}--{stem}{QuadletPreprocessor.QUADLET_SERVICE_SUFFIXES[suffix]}"
                )
        return services

    def _unit_properties(self, units: List[str]) -> Dict[str, Dict[str, str]]:
        """
        Read the state of each unit: its type, whether it runs, and which run of it is current.

        Args:
            units: Unit names

        Returns:
            Unit name -> property name -> value
        """
        if not units:
            return {}

        cmd = ["systemctl", "show", "-p", "Id,Type,ActiveState,SubState,InvocationID"] + units
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        except subprocess.TimeoutExpired:
            self.module.fail_json(msg=f"{' '.join(cmd)} timed out")
            return {}  # unreachable, but for type checking
        if result.returncode != 0:
            self.module.fail_json(msg=f"{' '.join(cmd)} failed: {result.stderr.strip()}")

        # One block of Key=Value lines per unit, separated by a blank line
        properties = {}
        for block in result.stdout.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            properties[values.get("Id", "")] = values
        return properties

    def _check_services_stayed_up(self, before: Dict[str, Dict[str, str]]):
        """
        Fail the task if a service this run started did not stay up for settle_time seconds.

        Podman tells systemd a container is ready as soon as the container starts, so systemctl
        reports success even for one whose process dies a moment later. Each service is watched
        under the run of it this module started: if systemd has had to start it again by the end
        of the wait, it exited in between, however quickly it came back.

        Args:
            before: The state of the app's services before this run started anything
        """
        after = self._unit_properties(list(before))
        started = [
            unit for unit, state in after.items()
            if state.get("InvocationID") != before[unit].get("InvocationID")
            and state.get("Type") != "oneshot"
        ]
        if not started:
            return

        time.sleep(self.settle_time)

        failures = []
        for unit, state in self._unit_properties(started).items():
            if state.get("ActiveState") != "active":
                problem = f"it is {state.get('ActiveState')}"
                if state.get("SubState") not in (state.get("ActiveState"), None):
                    problem += f" ({state.get('SubState')})"
            elif state.get("InvocationID") != after[unit].get("InvocationID"):
                problem = "systemd had to start it again"
            else:
                continue

            message = f"{unit} did not stay running after it was started: {problem}."
            log = self._journal_tail(unit)
            if log:
                message += f" Its last log lines:\n{log}"
            failures.append(message)

        if failures:
            self.module.fail_json(msg="\n\n".join(failures))

    @staticmethod
    def _journal_tail(unit: str) -> str:
        """The last lines a unit logged, its container's output included; empty if unavailable."""
        cmd = ["journalctl", "--unit", unit, "--lines", "10", "--no-pager", "--output", "cat"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return result.stdout.strip()

    def _restart_app(self, service_name: str):
        """
        Restart the main service, every app unit it depends on, and the pending bootstrap units.

        All of them go to systemd in one call, so it builds a single transaction: each unit stops
        and starts once, in dependency order, and a bootstrap unit finishes before the containers
        mounting its volume start. Split into separate calls, a unit that requires one restarted
        earlier is restarted again by systemd on its own - which is how main used to go round two
        or three times per deploy.

        The bootstrap units main depends on whose owner is already applied are left out. Restarting
        one does nothing useful - its marker makes the condition fail and the unit is skipped - and
        it would still take down every container that requires it.

        Args:
            service_name: Main service name
        """
        all_init_units = {f"{Path(name).stem}.service" for name in self.generated_quadlets}
        dependencies = [
            unit for unit in self._get_app_dependencies(service_name) if unit not in all_init_units
        ]
        units = self.pending_volume_inits + dependencies + [service_name]
        self._systemctl(["restart"] + list(dict.fromkeys(units)))

    def _reconcile_timers(self) -> bool:
        """
        Bring every deployed timer to enabled and running.

        This is the one place the module runs `systemctl enable`, and the reason is that a timer is
        not a quadlet. Podman's generator applies the [Install] section of the units it generates
        itself - they are transient and cannot be enabled at all - so the module never has to. A
        timer is an ordinary unit file that systemd will not arm unless somebody enables it, and
        nobody else is going to.

        Returns:
            True if any timer was touched
        """
        changed = False

        for timer in sorted(self.timer_changes):
            if not self._is_unit_enabled(timer):
                self._systemctl(["enable", timer])
                changed = True

            # Covers both reasons to run it: it is not running at all, or its schedule moved and
            # systemd is still holding the elapse computed from the old one. Enabling alone would
            # not do it - timers.target was reached long before this runs, so a newly enabled
            # timer sits dead until the next boot.
            if self.timer_changes[timer] or not self._is_service_active(timer):
                self._systemctl(["restart", timer])
                changed = True

        return changed

    def _pending_volume_inits(self) -> List[str]:
        """
        Find the ownership bootstrap units that have yet to give their volume its declared owner.

        That is the case when the marker for the declared owner is missing from the host: the
        volume is new, its X-Owner changed, or its marker was deleted to run the chown again.
        Asking the host, rather than which files changed this run, is what lets an owner change
        deployed under `installed` - which runs no bootstrap - be applied by the next run allowed
        to restart the app.

        Returns:
            Their service names
        """
        return [
            f"{self.app_name}--{stem}-volume-init.service"
            for stem, owner in self.volume_owners.items()
            if not os.path.exists(self._volume_marker(stem, owner))
        ]

    def _restart_volume_init_units(self) -> bool:
        """
        Run the pending ownership bootstrap units.

        They are Type=oneshot with RemainAfterExit=yes, so once one has run it stays active and
        systemd never re-checks its condition. Without an explicit restart, a new owner would not
        be applied until the host next booted.

        These cannot be picked up by the dependency walk in _get_app_dependencies: that starts from
        the main service, and a volume mounted only by a secondary container is not in its tree.

        Returns:
            True if any unit was run
        """
        if not self.pending_volume_inits:
            return False
        self._systemctl(["restart"] + self.pending_volume_inits)
        return True

    def _get_app_dependencies(self, service_name: str) -> List[str]:
        """
        Get list of app-prefixed service dependencies.

        Args:
            service_name: Main service name to query dependencies for

        Returns:
            List of app-prefixed dependency service names
        """
        try:
            # List all service dependencies, however indirect. Without --all, systemctl only expands
            # targets, so a container two Requires= away from main would be left out - and keep
            # running its old definition after a deploy that changed it.
            result = subprocess.run(
                ["systemctl", "list-dependencies", "--all", "--type", "service", service_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            if result.returncode != 0:
                # If service doesn't exist yet or has no dependencies, return empty list
                return []

            # Parse output and filter for app-prefixed services
            dependencies = []
            app_prefix = f"{self.app_name}--"

            for line in result.stdout.splitlines():
                # Remove leading whitespace (--type only adds spaces, no tree chars)
                line_stripped = line.lstrip()

                # Check if this is an app-prefixed service (but not the main service itself)
                if line_stripped.startswith(app_prefix) and line_stripped != service_name:
                    dependencies.append(line_stripped)

            return dependencies

        except subprocess.TimeoutExpired:
            # If listing dependencies times out, return empty list
            return []
        except FileNotFoundError as e:
            # systemctl not found - this is a fatal error
            self.module.fail_json(msg=f"systemctl command not found: {e}")
            return []  # unreachable, but for type checking

    def _validate_quadlet_syntax(self, processed_files: Dict[str, str]):
        """
        Check this app's quadlets with podman's own generator, before any of them is written.

        The generator runs over a staged copy holding only this app's quadlets. Pointed at the
        real directory instead, it would only see the files after they had been written - so a
        rejected one would stay on the host - and it would judge every other app's files too, so
        one app's broken file would fail every other app's deploy.

        Args:
            processed_files: Dict mapping destination_path -> content
        """
        generator = "/usr/lib/systemd/system-generators/podman-system-generator"
        cmd = [generator, "-dryrun", "-v"]
        with tempfile.TemporaryDirectory(prefix="quadlet-app-") as staging:
            staged = {
                os.path.basename(dest_path): content
                for dest_path, content in processed_files.items()
                if os.path.dirname(dest_path) == self.quadlet_dest
            }
            staged.update(self._quadlets_of_other_apps_named_in(staged))
            for name, content in staged.items():
                with open(os.path.join(staging, name), "w", encoding="utf-8") as f:
                    f.write(content)

            env = {**os.environ, "QUADLET_UNIT_DIRS": staging}
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=False, timeout=30, env=env
                )
            except subprocess.TimeoutExpired:
                self.module.fail_json(msg=f"{generator} -dryrun -v timed out")
                return
            except Exception as e:  # pylint: disable=broad-exception-caught
                self.module.fail_json(msg=f"Failed to execute quadlet syntax validation: {e}")
                return

        if result.returncode != 0:
            self.module.fail_json(
                msg=result.stderr.strip(),
                cmd=" ".join(cmd),
                rc=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    def _quadlets_of_other_apps_named_in(self, quadlets: Dict[str, str]) -> Dict[str, str]:
        """
        Read the deployed quadlets of other apps that these quadlets name.

        A container can join another app's network by naming its file, e.g.
        Network=other--main.network, and the generator cannot resolve that name without the file.
        What a staged file of another app names in turn is read too, since the generator has to
        make sense of that file as well.

        Args:
            quadlets: Filename -> content of the quadlets being validated

        Returns:
            Filename -> content of each named quadlet found on the host. A name not found is left
            out, for the generator to report.
        """
        directives = QuadletPreprocessor.QUADLET_REFERENCE_DIRECTIVES
        found: Dict[str, str] = {}
        pending = list(quadlets.values())
        while pending:
            for line in pending.pop().split("\n"):
                parsed = parse_directive(line)
                if parsed is None or parsed[1] not in directives:
                    continue
                name = parsed[3].strip().partition(":")[0]
                # Only a quadlet's filename - a path, or a plain podman network or volume name,
                # is not one, and a path must not be read as one
                if "/" in name or not name.endswith((".network", ".pod", ".volume")):
                    continue
                if name in quadlets or name in found:
                    continue
                path = os.path.join(self.quadlet_dest, name)
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    found[name] = f.read()
                pending.append(found[name])
        return found

    def _systemctl(self, args: List[str]):
        """
        Execute systemctl command.

        Args:
            args: Command arguments (e.g., ['daemon-reload'])
        """
        cmd = ["systemctl"] + args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=self.systemctl_timeout)
            if result.returncode != 0:
                self.module.fail_json(
                    msg=f"systemctl {' '.join(args)} failed: {result.stderr.strip()}",
                    cmd=" ".join(cmd),
                    rc=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        except subprocess.TimeoutExpired:
            self.module.fail_json(msg=f"systemctl {' '.join(args)} timed out after {self.systemctl_timeout}s")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.module.fail_json(msg=f"Failed to execute systemctl: {e}")

    def _needs_service_management(self, service_name: str) -> bool:
        """
        Check if service needs management based on desired state.

        Args:
            service_name: Service name to check

        Returns:
            True if service state doesn't match desired state
        """
        # Checked before the state branches: a timer someone stopped or disabled by hand has to
        # bring the module past its early exit, whatever state the main service is in
        if self._has_stale_timer():
            return True

        if self.state == "started":
            # Need to start if not currently active, or to give a volume its declared owner
            return not self._is_service_active(service_name) or bool(self.pending_volume_inits)
        if self.state == "restarted":
            # Always restart when restarted state is requested
            return True

        return False

    def _has_stale_timer(self) -> bool:
        """
        Check whether any deployed timer is not enabled or not running.

        Returns:
            True if at least one timer needs reconciling
        """
        return any(
            not self._is_unit_enabled(timer) or not self._is_service_active(timer)
            for timer in self.timer_changes
        )

    def _is_service_active(self, service_name: str) -> bool:
        """
        Check if service is currently active.

        Args:
            service_name: Service name to check

        Returns:
            True if service is active
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() == "active"
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def _is_unit_enabled(self, unit_name: str) -> bool:
        """
        Check if a unit is enabled for auto-start.

        Args:
            unit_name: Unit name to check

        Returns:
            True if the unit is enabled
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", unit_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() == "enabled"
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def _read_file(self, path: str) -> str:
        """
        Read file content.

        Args:
            path: File path

        Returns:
            File content as string
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (IOError, OSError) as e:
            raise ValidationError(f"Failed to read file {path}: {e}") from e
        except UnicodeDecodeError as e:
            raise ValidationError(f"File {path} is not valid UTF-8: {e}") from e

    def _get_quadlet_filenames(self, discovered_files: Dict) -> List[str]:
        """Get list of deployed quadlet filenames with app prefix."""
        return [f"{self.app_name}--{filename}" for filename, _ in discovered_files["quadlets"]]

    def _get_success_message(self) -> str:
        """Get success message based on state."""
        if self.state == "installed":
            return "quadlet files deployed"
        if self.state == "started":
            return "quadlet files deployed and service started"
        if self.state == "restarted":
            return "quadlet files deployed and service restarted"
        return "quadlet files deployed"


def main():
    """Ansible module entry point."""
    module = AnsibleModule(
        argument_spec={
            "src": {"type": "path", "required": True},
            "name": {"type": "str", "required": False},
            "state": {
                "type": "str",
                "default": "installed",
                "choices": ["installed", "started", "restarted"],
            },
            "force": {"type": "bool", "default": False},
            "systemctl_timeout": {"type": "int", "default": 120},
            "settle_time": {"type": "int", "default": 5},
            # Internal parameters used by action plugin
            "_control_node_processed": {"type": "bool", "default": False, "required": False},
            # Which files the app has: kind, and name or container directory and path. Logged.
            "_files": {"type": "list", "elements": "dict", "required": False},
            # The rendered content of each file in _files, at the same position, secrets included.
            # Without no_log, Ansible writes it into the managed host's journal on every run. Kept
            # apart from the names because no_log masks every one of its strings in all output.
            "_contents": {"type": "list", "elements": "str", "required": False, "no_log": True},
        },
        supports_check_mode=False,
    )

    # Run the module
    app_module = QuadletAppModule(module)
    app_module.run()


if __name__ == "__main__":
    main()
