# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""An app: its name, its files, and how they refer to each other."""

import re
from pathlib import Path

from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import DataFile, HostFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.quadlets import (
    Network,
    Quadlet,
    Volume,
)
from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import Directive, UnitFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.units import SystemdUnit, Timer

# Application names: a single letter, or a letter followed by letters, numbers, hyphens and
# underscores, ending in a letter or number
APP_NAME_PATTERN_SINGLE = r"^[a-z]$"
APP_NAME_PATTERN_MULTI = r"^[a-z][a-z0-9_-]*[a-z0-9]$"

# Default mount options for %init%/%config% volume mounts. idmap rather than U: the kernel
# translates ids at the mount, so the container sees these files as its own without the host-side
# tree being chowned out from under Ansible.
DEFAULT_INIT_CONFIG_VOLUME_OPTIONS = "ro,Z,idmap"

# Default mount options for named .volume references. Their on-disk ids are container-relative,
# set once by the volume's bootstrap unit, so idmap presents them correctly whatever range podman
# hands out.
DEFAULT_NAMED_VOLUME_OPTIONS = "rw,Z,idmap"

# The kinds of quadlet whose service runs for as long as the app does
LONG_RUNNING_KINDS = (".container", ".pod", ".kube")

# Directives naming systemd units, as a space-separated list
UNIT_LIST_DIRECTIVES = {
    # Names the unit a .timer activates. Not valid in any quadlet type, so it only ever matches in
    # a plain systemd unit.
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


class QuadletApp:
    """
    An app made of quadlets, plain systemd units, and the files its containers mount.

    Everything it deploys carries its name as a prefix ("myapp--main.container"), so apps never
    collide on a host. Its files only render once all of them have been added: the rules need to
    know every name the app defines.
    """

    def __init__(self, name: str, main_container: str = constants.DEFAULT_MAIN_CONTAINER):
        """
        Args:
            name: The application name, in any case
            main_container: The name of the quadlet whose service is the app's main one, without
                its extension (e.g. "main" for main.container)

        Raises:
            QuadletAppError: If the name is not one an app can have
        """
        self.name = name.lower()
        self._validate_name()
        self.main_container = main_container
        self.quadlets: list[Quadlet] = []
        self.units: list[SystemdUnit] = []
        self.data_files: list[DataFile] = []

    def add_quadlet(self, filename: str, source: str) -> Quadlet:
        """
        Add a file from quadlets/.

        Raises:
            QuadletAppError: If the file's name is not one the app can deploy, see
                _check_filename. A template quadlet ("worker@.container") is not supported:
                deploying the app restarts and checks the services its quadlets become, and the
                running instances of a template are services of another name.
        """
        self._check_filename("quadlets", filename)
        if "@" in filename:
            raise QuadletAppError(
                f"The app {self.name} cannot deploy quadlets/{filename}: template quadlets, whose "
                "name has an @, are not supported. Write one quadlet for each instance instead."
            )
        quadlet = Quadlet.create(self, filename, source)
        self.quadlets.append(quadlet)
        return quadlet

    def add_unit(self, filename: str, source: str) -> SystemdUnit:
        """
        Add a file from units/.

        Raises:
            QuadletAppError: If the file's name is not one the app can deploy, see _check_filename
        """
        self._check_filename("units", filename)
        unit = SystemdUnit.create(self, filename, source)
        self.units.append(unit)
        return unit

    def add_data_file(self, kind: str, container: str, path: str, source: str) -> DataFile:
        """Add a file from init/ or config/."""
        data_file = DataFile(self, kind, container, path, source)
        self.data_files.append(data_file)
        return data_file

    @property
    def base_dir(self) -> str:
        """Where the app's init and config files go on the host."""
        return f"{constants.QUADLET_APP_BASE_DIR}/{self.name}"

    @property
    def main_service(self) -> str:
        """
        The service the app's state is judged by: started, restarted, or left alone.

        Raises:
            QuadletAppError: If the app has no main quadlet, see main_quadlet
        """
        return self.main_quadlet().service_name

    def main_quadlet(self) -> Quadlet:
        """
        The quadlet named by main_container, of one of the types whose service keeps running.

        A pod or a kube file can be the main one as well as a container: the service podman makes
        of it is named after its type, so a pod's is "<app>--<name>-pod.service".

        Raises:
            QuadletAppError: If there is no such quadlet, or more than one
        """
        stem = self.main_container
        found = [
            quadlet
            for quadlet in self.quadlets
            if quadlet.stem == stem and quadlet.kind in LONG_RUNNING_KINDS
        ]
        if not found:
            expected = ", ".join(f"quadlets/{stem}{kind}" for kind in LONG_RUNNING_KINDS)
            raise QuadletAppError(
                f"The app {self.name} has no main quadlet: expected one of {expected}. "
                "Which quadlet is the main one is set by main_container in the app's manifest, "
                f"and defaults to '{constants.DEFAULT_MAIN_CONTAINER}'."
            )
        if len(found) > 1:
            raise QuadletAppError(
                f"The app {self.name} has more than one main quadlet: "
                f"{', '.join(quadlet.filename for quadlet in found)}. Keep only one of them."
            )
        return found[0]

    def check_main(self) -> None:
        """
        Check the app has its main quadlet.

        Raises:
            QuadletAppError: If it has none, or more than one
        """
        self.main_quadlet()

    @property
    def volumes(self) -> list[Volume]:
        return [quadlet for quadlet in self.quadlets if isinstance(quadlet, Volume)]

    @property
    def networks(self) -> list[Network]:
        return [quadlet for quadlet in self.quadlets if isinstance(quadlet, Network)]

    @property
    def timers(self) -> list[Timer]:
        return [unit for unit in self.units if isinstance(unit, Timer)]

    @property
    def deployed_quadlets(self) -> list[str]:
        """The names the app's quadlets are deployed under, the generated ones last."""
        return [quadlet.deployed_name for quadlet in self.quadlets] + [
            volume.bootstrap.deployed_name for volume in self.volumes
        ]

    @property
    def deployed_units(self) -> list[str]:
        """The names the app's plain systemd units are deployed under."""
        return [unit.deployed_name for unit in self.units]

    def quadlet(self, stem: str) -> Quadlet | None:
        """The quadlet with this name, whatever its type; the last one added if there are several."""
        found = None
        for quadlet in self.quadlets:
            if quadlet.stem == stem:
                found = quadlet
        return found

    def volume(self, stem: str) -> Volume | None:
        """The volume with this name, if the app has one."""
        found = None
        for volume in self.volumes:
            if volume.stem == stem:
                found = volume
        return found

    def files(self) -> list[HostFile]:
        """
        Every file the app puts on the host, in the order they are deployed.

        A generated file takes the place of one of the app's own deployed under the same name.
        """
        files = {}
        for file in [
            *self.quadlets,
            *(volume.bootstrap for volume in self.volumes),
            *self.units,
            *self.data_files,
        ]:
            files[file.dest_path] = file
        return list(files.values())

    def services(self) -> list[str]:
        """
        The services podman generates from the app's containers, pods and kube files.

        Volumes, networks and the ownership bootstrap units are left out: they run once and are
        done, and systemctl waits for that and reports their failure itself.
        """
        return [
            quadlet.service_name
            for quadlet in sorted(self.quadlets, key=lambda quadlet: quadlet.filename)
            if quadlet.kind in LONG_RUNNING_KINDS
        ]

    def volume_init_services(self) -> list[str]:
        """The services of every ownership bootstrap unit of the app."""
        return [volume.init_service_name for volume in self.volumes]

    def own_services(self) -> set[str]:
        """
        Every service the app's quadlets become, the generated bootstrap units included.

        Matched by name, not by the app's prefix: an app called "web--x" deploys services that
        start with "web--" too, and they are not this app's.
        """
        return {quadlet.service_name for quadlet in self.quadlets} | set(
            self.volume_init_services()
        )

    def timer_targets(self) -> set[str]:
        """The services the app's timers run."""
        return {timer.target_service for timer in self.timers}

    def pending_volume_inits(self) -> list[str]:
        """The services of the bootstrap units whose volume has yet to get its declared owner."""
        return [
            volume.init_service_name for volume in self.volumes if not volume.ownership_applied()
        ]

    def network_references(self) -> list[str]:
        """
        The networks the app's containers, pods and kube files join, as they are deployed.

        Each is a Network= value with its options dropped: a network quadlet's filename - the app's
        own carrying its prefix, as Rule 1 writes it - or the name of a network podman already
        has. The modes podman can run networking in instead (host, none, another container's...)
        join no network, and are left out.

        Returns:
            Each reference once, in the order the files name them (e.g. ["myapp--main.network",
            "other--main"])
        """
        references = []
        for quadlet in sorted(self.quadlets, key=lambda quadlet: quadlet.filename):
            if quadlet.kind not in LONG_RUNNING_KINDS:
                continue
            for name in self._networks_joined_by(quadlet):
                if name not in references:
                    references.append(name)
        return references

    def _networks_joined_by(self, quadlet: Quadlet) -> list[str]:
        """The networks one quadlet joins, as network_references describes them."""
        joined: list[str] = []
        for directive in UnitFile(quadlet.source).directives("Network"):
            name = self.prefix_reference(directive).value.strip().partition(":")[0]
            if not name:
                # An empty Network= clears the ones before it, as an empty value does for any
                # list in a unit file - and is what Network={{ x | default('') }} renders to
                joined = []
                continue
            # Network=other.container shares that container's networking: quadlet's spelling of
            # the container: mode
            if name in constants.PODMAN_NETWORK_MODES or name.endswith(".container"):
                continue
            joined.append(name)
        return joined

    def prefix_reference(self, directive: Directive) -> Directive:
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
        """
        if directive.key in UNIT_LIST_DIRECTIVES:
            # Each name on its own; the spacing between them, and any \r a file saved with Windows
            # line endings leaves at the end, stay as they are
            units = self._unit_names()
            return directive.with_value(
                re.sub(
                    r"\S+",
                    lambda match: (
                        f"{self.name}--{match.group(0)}"
                        if match.group(0) in units
                        else match.group(0)
                    ),
                    directive.value,
                )
            )

        if directive.key not in constants.QUADLET_REFERENCE_DIRECTIVES:
            return directive

        body = directive.value.rstrip()
        trailing = directive.value[len(body) :]
        name, colon, rest = body.partition(":")
        if name not in self._file_names():
            return directive

        if directive.key == "Volume" and name.endswith(".volume") and rest and ":" not in rest:
            # A destination but no options: give it the defaults. Without a destination there is
            # nowhere to put them - podman would read them as the destination.
            rest = f"{rest}:{DEFAULT_NAMED_VOLUME_OPTIONS}"

        return directive.with_value(f"{self.name}--{name}{colon}{rest}{trailing}")

    def resolve_data_mount(self, directive: Directive, stem: str) -> Directive:
        """
        Rule 2: Point a %init% or %config% volume at where the app's files are on the host.

        Transforms, for the quadlet named "main":
            Volume=%init%:/x        -> Volume=/srv/myapp/init/main:/x:ro,Z,idmap
            Volume=%config%/sub:/x  -> Volume=/srv/myapp/config/main/sub:/x:ro,Z,idmap

        Without mount options, the defaults are applied. Explicit options are kept as they are.

        Args:
            directive: A directive of the quadlet
            stem: The quadlet's name, which names its directory under init/ and config/
        """
        if directive.key != "Volume":
            return directive

        # Trailing whitespace - such as the \r a file saved with Windows line endings leaves - stays
        # at the end of the line, rather than ending up inside the destination
        body = directive.value.rstrip()
        trailing = directive.value[len(body) :]

        # Volume=SOURCE:DEST[:OPTIONS]
        parts = body.split(":", 2)
        source = parts[0]
        dest = parts[1] if len(parts) > 1 else ""
        options = parts[2] if len(parts) > 2 else None  # None = not specified

        for kind in ("init", "config"):
            placeholder = f"%{kind}%"
            if not source.startswith(placeholder):
                continue

            subpath = source.removeprefix(placeholder).removeprefix("/")
            new_source = f"{self.base_dir}/{kind}/{stem}"
            if subpath:
                new_source += f"/{subpath}"

            if options is None:
                options = DEFAULT_INIT_CONFIG_VOLUME_OPTIONS
            return directive.with_value(f"{new_source}:{dest}:{options}{trailing}")

        return directive

    def _file_names(self) -> set[str]:
        """
        Every file name in the app's quadlets/ and units/.

        Only names the app defines get its prefix: anything else belongs to the host or to another
        app. The generated bootstrap units are not among them.
        """
        return {quadlet.filename for quadlet in self.quadlets} | {
            unit.filename for unit in self.units
        }

    def _unit_names(self) -> set[str]:
        """The unit names the app defines: its files, and the services podman generates from them."""
        files = self._file_names()
        units = set(files)
        for filename in files:
            suffix = constants.QUADLET_SERVICE_SUFFIXES.get(Path(filename).suffix)
            if suffix is not None:
                units.add(f"{Path(filename).stem}{suffix}")
        return units

    def _check_filename(self, directory: str, filename: str) -> None:
        """
        Refuse a file name holding "--", which separates the app's name from its files' names.

        A file is deployed as "<app>--<file>", so app "web" deploying "x--main.network" would
        write "web--x--main.network" - the file app "web--x" deploys for its "main.network". The
        two apps would overwrite each other's file on every deploy.
        """
        if "--" in filename:
            raise QuadletAppError(
                f"The app {self.name} cannot deploy {directory}/{filename}: a file name cannot "
                f"contain '--'. Deployed as {self.name}--{filename}, it could take the name of "
                "another app's file, since '--' separates an app's name from its files' names."
            )

    def _validate_name(self) -> None:
        if len(self.name) == 1:
            pattern = APP_NAME_PATTERN_SINGLE
        else:
            pattern = APP_NAME_PATTERN_MULTI

        # The whole name has to match: re.match would accept a trailing newline, since $ also
        # matches just before one
        if not re.fullmatch(pattern, self.name):
            raise QuadletAppError(
                f"Invalid application name: {self.name}. "
                "Must start with a letter (a-z), end with a letter or number, "
                "and contain only lowercase letters, numbers, hyphens, and underscores."
            )
