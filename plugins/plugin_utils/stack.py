# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""Every app of a host, read together: what quadlet_stack_info reports and the role acts on."""

import os
from dataclasses import dataclass

# pylint: disable=import-error
from ansible.errors import AnsibleError  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.module_utils.app import (  # type: ignore[import-not-found]
    LONG_RUNNING_KINDS,
    QuadletApp,
)
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import (  # type: ignore[import-not-found]
    QuadletAppError,
)
from ansible_collections.kriansa.quadlet.plugins.plugin_utils.control_node_app import (  # type: ignore[import-not-found]
    ControlNodeApp,
)
from ansible_collections.kriansa.quadlet.plugins.plugin_utils.manifest import (  # type: ignore[import-not-found]
    Manifest,
)

# pylint: enable=import-error


@dataclass
class CollectedApp:
    """One app of the stack, as read on the control node."""

    name: str
    manifest: Manifest
    model: QuadletApp
    config_dirs: list[str]


@dataclass
class Collected:
    """What reading the stack found."""

    payload: dict
    warnings: list[str]

    @property
    def summary(self) -> str:
        apps = len(self.payload["apps_list"])
        roles = self.payload["claims_by_role"]
        claims = sum(len(role_claims) for role_claims in roles.values())
        return f"{_count(apps, 'app')}, {_count(claims, 'claim')} for {_count(len(roles), 'role')}"


class QuadletStack:
    """
    The apps of one host: every directory under the stack's directory is one.

    They are always read all together, whichever of them a run deploys. A provider that removes
    what nothing claims any more, shown only the apps being deployed, would remove everything the
    others claim.
    """

    def __init__(self, path: str, loader, templar):
        """
        Args:
            path: The directory holding one directory per app
            loader: Ansible's DataLoader, which reads (and decrypts) the files
            templar: A Templar holding every variable the task sees
        """
        self.path = path
        self._loader = loader
        self._templar = templar

    def app_names(self) -> list[str]:
        """
        The apps, by directory name, sorted. Hidden directories and plain files are not apps.

        Raises:
            AnsibleError: If the stack's directory is missing, or a directory is not a usable app
        """
        if not os.path.isdir(self.path):
            raise AnsibleError(f"The apps directory was not found on the control node: {self.path}")

        names = sorted(
            entry
            for entry in os.listdir(self.path)
            if not entry.startswith(".") and os.path.isdir(os.path.join(self.path, entry))
        )
        for name in names:
            self._check_app_directory(name)
        return names

    def collect(self) -> Collected:
        """
        Read every app: its manifest, its shape, and the claims it makes.

        Any app failing to read fails the whole stack, however many apps are to be deployed: a
        partial view of the claims is the one thing a provider must never be given.

        Raises:
            AnsibleError: If any app cannot be read
        """
        apps = [self._collect_app(name) for name in self.app_names()]

        # The name podman gives each network of the stack, by the file another app names it by
        network_names = {
            network.deployed_name: network.network_name
            for app in apps
            for network in app.model.networks
        }

        claims_by_role: dict[str, list[dict]] = {}
        for app in apps:
            for role, claims in app.manifest.resource_claims.items():
                claims_by_role.setdefault(role, []).extend(
                    {"app": app.name, "claim": claim} for claim in claims
                )

        payload = {
            "src": self.path,
            "apps_list": [app.name for app in apps],
            "apps": {app.name: self._describe(app, network_names) for app in apps},
            "claims_by_role": dict(sorted(claims_by_role.items())),
        }
        warnings = [warning for app in apps for warning in app.manifest.warnings]
        return Collected(payload, warnings)

    def _collect_app(self, name: str) -> CollectedApp:
        app = ControlNodeApp(os.path.join(self.path, name), name, self._loader, self._templar)
        manifest = app.manifest()
        try:
            model = app.model(manifest.main_container)
            model.check_main()
        except QuadletAppError as e:
            raise AnsibleError(e.msg) from None
        return CollectedApp(name, manifest, model, app.config_dirs())

    @staticmethod
    def _describe(app: CollectedApp, network_names: dict[str, str]) -> dict:
        """What the stack tells providers about one app."""
        networks: list[str] = []
        for reference in app.model.network_references():
            network = _network_name(reference, network_names)
            if network not in networks:
                networks.append(network)

        services = sorted(app.model.quadlets, key=lambda quadlet: quadlet.filename)
        return {
            "version": app.manifest.version,
            "systemctl_timeout": app.manifest.systemctl_timeout,
            "main_container": app.manifest.main_container,
            # Each name once: a container and a pod can share one
            "quadlets": list(
                dict.fromkeys(
                    quadlet.stem for quadlet in services if quadlet.kind in LONG_RUNNING_KINDS
                )
            ),
            "networks": networks,
            # Where the module puts each config/<quadlet>/ directory on the host, keyed by the
            # quadlet's name the way the module names the destination
            "config_dir": {
                _stem(directory): f"{app.model.base_dir}/config/{_stem(directory)}"
                for directory in app.config_dirs
            },
        }

    def _check_app_directory(self, name: str) -> None:
        # The name is used as it is, to find the directory again when the app is deployed - so it
        # has to be valid without the lowercasing quadlet_app applies to a name it is given
        try:
            QuadletApp(name)
        except QuadletAppError as e:
            raise AnsibleError(f"{os.path.join(self.path, name)} is not an app: {e.msg}") from None
        if name != name.lower():
            raise AnsibleError(
                f"{os.path.join(self.path, name)} is not an app: its name must be lowercase, "
                f"as {name.lower()}"
            )

        if not os.path.isdir(os.path.join(self.path, name, "quadlets")):
            raise AnsibleError(
                f"{os.path.join(self.path, name)} is not an app: it has no quadlets/ directory. "
                "Every directory here is deployed as an app."
            )


def _network_name(reference: str, network_names: dict[str, str]) -> str:
    """
    The name podman knows a network by, from how a quadlet names it.

    A network quadlet's filename becomes the NetworkName= of that file, if an app of the stack has
    it. One no app of the stack has can only be guessed, from what would name it by default:
    quadlet_app names a network it deploys after its file, "<app>--<name>", and quadlet names one
    of the host's own "systemd-<name>". Either may have set a NetworkName= of its own, which this
    cannot see.
    """
    if not reference.endswith(".network"):
        return reference
    if reference in network_names:
        return network_names[reference]
    stem = reference.removesuffix(".network")
    return stem if "--" in stem else f"systemd-{stem}"


def _stem(directory: str) -> str:
    """A config/ directory's quadlet name: the module drops any extension the directory has."""
    return os.path.splitext(directory)[0]


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
