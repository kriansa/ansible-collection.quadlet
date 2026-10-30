# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The plain systemd units of an app: files from units/, which podman's generator never sees."""

import os
from pathlib import Path

from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import Directive, UnitFile

# Target a timer activates when it does not name one itself
DEFAULT_TIMER_TARGET = "timers.target"


class SystemdUnit(HostFile):
    """
    A plain systemd unit of the app, deployed as itself to the directory systemd reads.

    Only Rule 1 applies. The rest of the quadlet rules describe things a systemd unit does not
    have: no %init%/%config% mounts, no resource to name, no user namespace, no image store to
    order against.
    """

    def __init__(self, app, filename: str, source: str):
        """
        Args:
            app: The QuadletApp the unit belongs to
            filename: Its name in units/ (e.g. "backup.timer")
            source: Its content, already templated by Ansible
        """
        self.app = app
        self.filename = filename
        self.source = source

    @staticmethod
    def create(app, filename: str, source: str) -> "SystemdUnit":
        """A unit of the type its filename names."""
        if Path(filename).suffix == ".timer":
            return Timer(app, filename, source)
        return SystemdUnit(app, filename, source)

    @property
    def deployed_name(self) -> str:
        return f"{self.app.name}--{self.filename}"

    @property
    def dest_path(self) -> str:
        return os.path.join(constants.SYSTEMD_UNIT_DIR, self.deployed_name)

    def render(self) -> str:
        unit = UnitFile(self.source)
        unit.map_directives(self.app.prefix_reference)
        return unit.text


class Timer(SystemdUnit):
    """
    A timer of the app, which runs one of its services on a schedule.

    Pointed at the service podman generates when it names a quadlet, and given what it needs to
    fire at all when it leaves that out.
    """

    def render(self) -> str:
        unit = UnitFile(self.source)
        unit.map_directives(self._rewrite)

        # Injected last, and deliberately after Rule 1: these values are already app-prefixed, and
        # Rule 1 would prefix them a second time
        self._inject_defaults(unit)
        return unit.text

    def _rewrite(self, directive: Directive) -> Directive:
        # Suffix first, prefix second: Unit=backup.pod has to become backup-pod.service before
        # Rule 1 turns it into myapp--backup-pod.service
        return self.app.prefix_reference(self._resolve_target(directive))

    @staticmethod
    def _resolve_target(directive: Directive) -> Directive:
        """
        Point Unit= at the service podman generates, rather than at the quadlet.

        Writing Unit=backup.container is the natural thing to do, but no unit by that name ever
        exists - the generator emits backup.service from it.
        """
        if directive.key != "Unit":
            return directive

        value = directive.value.strip()
        suffix = constants.QUADLET_SERVICE_SUFFIXES.get(Path(value).suffix)
        if suffix is None:
            return directive

        return directive.with_value(f"{Path(value).stem}{suffix}")

    def _inject_defaults(self, unit: UnitFile) -> None:
        """
        Add the directives a timer needs to do anything at all.

        Without a WantedBy, `systemctl enable` is a no-op and the timer never arms on boot. Unit=
        is injected only for readability - systemd would default to the same basename anyway - but
        spelling it out makes the deployed file say what it triggers.
        """
        # A timer with no [Timer] section is broken whatever we do, and opening one here would
        # only hide that behind a file that still never fires
        if unit.has_section("Timer") and not unit.has_directive("Unit"):
            unit.append_to_section("Timer", [f"Unit={self._target_service()}"])

        if not unit.has_directive("WantedBy"):
            unit.append_to_section("Install", [f"WantedBy={DEFAULT_TIMER_TARGET}"], at_end=True)

    def _target_service(self) -> str:
        """
        The service the timer triggers when it does not say so itself: the one of the same name.

        Returns:
            Prefixed service name (e.g. "myapp--backup.service")
        """
        stem = Path(self.filename).stem
        quadlet = self.app.quadlet(stem)
        kind = quadlet.kind if quadlet is not None else None
        suffix = constants.QUADLET_SERVICE_SUFFIXES.get(kind, ".service")
        return f"{self.app.name}--{stem}{suffix}"
