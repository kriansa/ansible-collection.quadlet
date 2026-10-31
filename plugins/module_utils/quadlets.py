# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The quadlets of an app, each type with the rules that rewrite it before it is deployed."""

import os
from pathlib import Path

from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import Directive, UnitFile

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

# What the default user namespace comes with, so a reader of the deployed file knows where it came
# from and when to set their own
DEFAULT_USERNS_COMMENT = (
    "# Default from quadlet_app. Set UserNS= explicitly to override; UserNS=host is what a",
    "# container needs if it reads the host journal or other processes' /proc entries.",
)


class Quadlet(HostFile):
    """
    A quadlet of the app: a file podman's generator turns into a systemd service.

    It is deployed under the app's prefix, so the files of different apps never collide. This class
    deploys a quadlet as it is written; each type the module knows has a subclass with its rules.
    """

    # The section and directive naming what podman creates from the quadlet. Rule 3 sets it to the
    # app's prefixed name when the file does not choose a name itself. None for types without one.
    NAME_DIRECTIVE: tuple[str, str] | None = None

    def __init__(self, app, filename: str, source: str):
        """
        Args:
            app: The QuadletApp the quadlet belongs to
            filename: Its name in quadlets/ (e.g. "main.container")
            source: Its content, already templated by Ansible
        """
        self.app = app
        self.filename = filename
        self.source = source

    @staticmethod
    def create(app, filename: str, source: str) -> "Quadlet":
        """A quadlet of the type its filename names."""
        quadlet_type = QUADLET_TYPES.get(Path(filename).suffix, Quadlet)
        return quadlet_type(app, filename, source)

    @property
    def stem(self) -> str:
        return Path(self.filename).stem

    @property
    def kind(self) -> str:
        """The quadlet type, as its file extension (e.g. ".container")."""
        return Path(self.filename).suffix

    @property
    def deployed_name(self) -> str:
        return f"{self.app.name}--{self.filename}"

    @property
    def dest_path(self) -> str:
        return os.path.join(constants.QUADLET_SYSTEMD_DIR, self.deployed_name)

    @property
    def service_name(self) -> str:
        """The service podman's generator makes of this quadlet."""
        return f"{self.app.name}--{self.stem}{constants.QUADLET_SERVICE_SUFFIXES[self.kind]}"

    def render(self) -> str:
        unit = UnitFile(self.source)
        self._inject_name(unit)
        return unit.text

    def _inject_name(self, unit: UnitFile) -> None:
        """
        Rule 3: Name what podman creates after the app, unless the file names it.

        Adds ContainerName, PodName, VolumeName or NetworkName (e.g. ContainerName=myapp--main).
        """
        if self.NAME_DIRECTIVE is None:
            return
        section, key = self.NAME_DIRECTIVE
        unit.ensure_directive(section, key, f"{self.app.name}--{self.stem}")


class Container(Quadlet):
    """A .container quadlet. Every rule applies to it."""

    NAME_DIRECTIVE = ("Container", "ContainerName")

    def render(self) -> str:
        # Which volumes this container mounts has to be read before Rule 1 rewrites the references
        # into their prefixed form
        mounted_volumes = self.mounted_volumes()

        unit = UnitFile(self.source)
        self._inject_name(unit)
        unit.map_directives(self._rewrite)

        # Injected last, and deliberately after Rule 1: these values are already app-prefixed, and
        # Rule 1 would prefix them a second time
        self._inject_unit_directives(unit, mounted_volumes)
        self._inject_default_userns(unit)
        return unit.text

    def mounted_volumes(self) -> list[str]:
        """
        The named .volume resources the container mounts, as written, whichever app they are of.

        Returns:
            Volume stems in first-seen order (e.g. ["data"] for Volume=data.volume:/x)
        """
        stems = []
        for directive in UnitFile(self.source).directives("Volume"):
            source = directive.value.strip().split(":", 1)[0]
            if source.startswith("/") or not source.endswith(".volume"):
                continue
            stem = Path(source).stem
            if stem not in stems:
                stems.append(stem)
        return stems

    def _rewrite(self, directive: Directive) -> Directive:
        directive = self.app.prefix_reference(directive)
        directive = self.app.resolve_data_mount(directive, self.stem)
        return self._normalize_userns(directive)

    @staticmethod
    def _normalize_userns(directive: Directive) -> Directive:
        """
        Force an explicit range size onto a bare UserNS=auto.

        Podman sizes an unqualified `auto` range from the image's highest uid, which is routinely
        far smaller than the uids the app actually writes with. Anything above the range is
        unmappable: it reads back as 65534 and is not writable even by container root.

        Explicit uidmapping= or size=, and every non-auto mode, are left untouched.
        """
        if directive.key != "UserNS":
            return directive

        value = directive.value.strip()
        if not value.startswith("auto"):
            return directive
        if "size=" in value or "uidmapping=" in value:
            return directive

        separator = "," if value.startswith("auto:") else ":"
        return directive.with_value(f"{value}{separator}size={constants.DEFAULT_USERNS_SIZE}")

    def _inject_unit_directives(self, unit: UnitFile, mounted_volumes: list[str]) -> None:
        """
        Add the [Unit] directives every container gets by default.

        Two of them:
        - RequiresMountsFor on the podman image store, so a container never starts before the
          storage it lives on is mounted. Declaring it here keeps it out of every app's quadlet.
        - Ordering against the ownership bootstrap unit of each of the app's volumes it mounts.
        """
        lines = []

        # RequiresMountsFor is additive in systemd and an app may legitimately declare its own for
        # a different path, so match on the path rather than on the directive
        storage_dir = constants.QUADLET_SYSTEMD_STORAGE_DIR
        if not unit.has_directive_value("RequiresMountsFor", storage_dir):
            lines.append(f"RequiresMountsFor={storage_dir}")

        for stem in mounted_volumes:
            volume = self.app.volume(stem)
            if volume is None:
                continue
            lines.extend(
                [f"Requires={volume.init_service_name}", f"After={volume.init_service_name}"]
            )

        if lines:
            unit.append_to_section("Unit", lines)

    @staticmethod
    def _inject_default_userns(unit: UnitFile) -> None:
        """
        Give the container a user namespace unless it asked for something else.

        Rootful podman puts a container in the host's namespace by default, so its root is the
        host's root. Defaulting to `auto` gives each container its own unprivileged range instead.
        Volumes are unaffected: they are mounted idmapped, so their on-disk ids are the container's
        own either way.

        A container that genuinely needs host identity - reading the host journal, or another
        process's /proc entries - must say so with an explicit UserNS=host. Inside a namespace
        those files read as nobody, and, worse, world-readable ones keep working, so such a
        container half-works rather than failing outright.
        """
        if unit.has_directive("UserNS"):
            return

        # A container joining a pod lives in the pod's user namespace, and podman refuses to start
        # one that asks for its own
        if unit.has_directive("Pod"):
            return

        unit.append_to_section(
            "Container",
            [*DEFAULT_USERNS_COMMENT, f"UserNS=auto:size={constants.DEFAULT_USERNS_SIZE}"],
        )


class Pod(Quadlet):
    """A .pod quadlet: named, its references prefixed, and its %init%/%config% mounts placed."""

    NAME_DIRECTIVE = ("Pod", "PodName")

    def render(self) -> str:
        unit = UnitFile(self.source)
        self._inject_name(unit)
        unit.map_directives(self._rewrite)
        return unit.text

    def _rewrite(self, directive: Directive) -> Directive:
        directive = self.app.prefix_reference(directive)
        return self.app.resolve_data_mount(directive, self.stem)


class Kube(Quadlet):
    """A .kube quadlet. Only Rule 1 applies: its references to the app's files are prefixed."""

    def render(self) -> str:
        unit = UnitFile(self.source)
        unit.map_directives(self.app.prefix_reference)
        return unit.text


class Network(Quadlet):
    """A .network quadlet. Only Rule 3 applies: it is named after the app."""

    NAME_DIRECTIVE = ("Network", "NetworkName")

    @property
    def network_name(self) -> str:
        """
        The name podman gives the network.

        That is the NetworkName= the deployed file's [Network] section sets, which Rule 3 makes
        the app's prefixed name unless the file chose one of its own. It is read the way podman
        reads it - quotes removed, continued lines joined - and the last one counts, as it does for
        podman. Without one, or with an empty one, podman falls back to its own default, systemd-
        and the deployed file's name.

        The name is what the module asks podman about, to tell whether the network has to be
        created again. Read any other way, podman never has it, and the network - with every
        container joined to it, other apps' included - would be restarted on every deploy.

        Two forms are still read differently, and the docs ask for a plain name instead: a
        comment line inside a continued value, which podman skips, and a systemd specifier such as
        %N, which systemd expands only when the unit runs. Asking podman or systemd for the name
        would avoid both, but this is also read on the control node, which has neither.
        """
        return self.network_name_in(self.content)

    def network_name_in(self, text: str) -> str:
        """The name podman gives the network a deployed file of this quadlet defines."""
        names = UnitFile(text).section_values("Network", "NetworkName")
        if names and names[-1]:
            return names[-1]
        return f"systemd-{Path(self.deployed_name).stem}"


class Volume(Quadlet):
    """
    A .volume quadlet, and the ownership its data is given.

    A volume is mounted idmapped, so the ids on disk are the container's own. They are set once, by
    a bootstrap unit generated for every volume, to the owner the volume declares with X-Owner -
    container root when it declares none.
    """

    NAME_DIRECTIVE = ("Volume", "VolumeName")

    def __init__(self, app, filename: str, source: str):
        super().__init__(app, filename, source)
        # Read now rather than when the file is rendered: an unusable owner has to fail the run
        # before anything is asked of the host
        self.owner = self._read_owner()
        self.bootstrap = VolumeInit(self)

    @property
    def init_service_name(self) -> str:
        """The service of the bootstrap unit giving this volume its owner."""
        return self.bootstrap.service_name

    @property
    def marker_path(self) -> str:
        """The file the bootstrap unit leaves once it has given the volume its current owner."""
        owner = self.owner.replace(":", "_")
        return f"{constants.QUADLET_APP_STATE_DIR}/{self.app.name}--{self.stem}.{owner}.done"

    def ownership_applied(self) -> bool:
        """
        Whether the host's copy of the volume already has its declared owner.

        It does not when the volume is new, its X-Owner changed, or its marker was deleted to run
        the chown again. Asking the host, rather than which files changed this run, is what lets an
        owner change deployed under `installed` - which runs no bootstrap - be applied by the next
        run allowed to restart the app.
        """
        return os.path.exists(self.marker_path)

    def render(self) -> str:
        unit = UnitFile(self.source)
        self._inject_name(unit)

        # X-Owner is ours, not quadlet's - it never reaches the deployed file
        declares_owner = unit.has_directive("X-Owner")
        unit.strip_directive("X-Owner")
        if declares_owner:
            self._inject_owner(unit)

        return unit.text

    def _read_owner(self) -> str:
        """
        The owner the volume declares, or container root if it declares none.

        Root is right for any image running as root. An image that drops privileges internally has
        to declare its own uid, or its data ends up owned by a user it no longer is.

        Raises:
            QuadletAppError: On a malformed or unusable X-Owner value
        """
        owner = DEFAULT_VOLUME_OWNER
        for directive in UnitFile(self.source).directives("X-Owner"):
            owner = self._validate_owner(directive.value.strip(), self.filename)
        return owner

    @staticmethod
    def _validate_owner(value: str, filename: str) -> str:
        """
        Check an X-Owner value is a uid:gid pair the consuming container can actually see.

        Raises:
            QuadletAppError: If malformed, or outside the consuming container's user namespace
        """
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise QuadletAppError(
                f"Invalid X-Owner in {filename}: '{value}'. "
                "Expected a numeric uid:gid pair, for example X-Owner=10000:10000"
            )

        limit = constants.DEFAULT_USERNS_SIZE
        if any(int(part) >= limit for part in parts):
            raise QuadletAppError(
                f"X-Owner in {filename} is out of range: '{value}'. Ids must be below {limit}, "
                "which is the size of the user namespace the consuming container gets. A higher "
                "id would be written to disk and then be invisible to the app that needs it."
            )

        return ":".join(parts)

    def _inject_owner(self, unit: UnitFile) -> None:
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
        """
        if unit.has_directive("User") or unit.has_directive("Group"):
            return
        uid, gid = self.owner.split(":")
        unit.append_to_section("Volume", [f"User={uid}", f"Group={gid}"])


class VolumeInit(Quadlet):
    """
    The ownership bootstrap unit of a volume, generated rather than read from the app.

    It chowns the volume once and is then skipped: the marker filename carries the uid/gid, so
    changing X-Owner changes the path and the unit's condition lets it run again. When it does, it
    replaces the marker of the previous owner with its own, so the state directory always lists the
    owner each volume actually has.

    Deliberately no UserNS and no idmap on the mount - it has to run as real host root. An existing
    volume's files are owned by host ids outside any container's range, and a process in a user
    namespace cannot chown a file whose owner is unmapped, not even as container root. What it
    writes is the container-relative id, which is exactly what the app then sees through its own
    idmapped mount.
    """

    def __init__(self, volume: Volume):
        super().__init__(volume.app, f"{volume.stem}-volume-init.container", "")
        self.volume = volume

    def render(self) -> str:
        volume = f"{self.app.name}--{self.volume.stem}"
        owner = self.volume.owner
        marker = self.volume.marker_path
        state_dir = constants.QUADLET_APP_STATE_DIR
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

        return "\n".join(
            [
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
                f"ExecStartPost=/usr/bin/mkdir -p {state_dir}",
                (
                    f"ExecStartPost=/usr/bin/find {state_dir} -maxdepth 1 "
                    f"-regex '{previous_markers}' -delete"
                ),
                f"ExecStartPost=/usr/bin/touch {marker}",
                "",
            ]
        )


# The class of each quadlet type the module has rules for. Any other is deployed as written.
QUADLET_TYPES = {
    ".container": Container,
    ".pod": Pod,
    ".kube": Kube,
    ".network": Network,
    ".volume": Volume,
}
