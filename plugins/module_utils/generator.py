# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""Podman's own check of the app's quadlets, made before any of them reaches the host."""

import os
import subprocess
import tempfile

from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.quadlets import Quadlet
from ansible_collections.kriansa.quadlet.plugins.module_utils.unit_file import UnitFile

# The program systemd runs to turn quadlets into services
PODMAN_GENERATOR = "/usr/lib/systemd/system-generators/podman-system-generator"

# The quadlet types one quadlet can name another of by filename
REFERABLE_QUADLET_EXTENSIONS = (".network", ".pod", ".volume")


class QuadletGenerator:
    """
    Podman's quadlet generator, run as a dry run over the quadlets about to be deployed.

    It runs over a staged copy holding only this app's quadlets. Pointed at the real directory
    instead, it would only see the files after they had been written - so a rejected one would
    stay on the host - and it would judge every other app's files too, so one app's broken file
    would fail every other app's deploy.
    """

    def validate(self, files: list[HostFile]) -> None:
        """
        Check the quadlets among these files, before any of them is written.

        Raises:
            QuadletAppError: If the generator rejects them, or cannot be run at all
        """
        cmd = [PODMAN_GENERATOR, "-dryrun", "-v"]
        with tempfile.TemporaryDirectory(prefix="quadlet-app-") as staging:
            staged = {
                file.deployed_name: file.content for file in files if isinstance(file, Quadlet)
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
                raise QuadletAppError(f"{PODMAN_GENERATOR} -dryrun -v timed out") from None
            except Exception as e:  # pylint: disable=broad-exception-caught
                raise QuadletAppError(f"Failed to execute quadlet syntax validation: {e}") from e

        if result.returncode != 0:
            raise QuadletAppError(
                result.stderr.strip(),
                cmd=" ".join(cmd),
                rc=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    @staticmethod
    def _quadlets_of_other_apps_named_in(quadlets: dict[str, str]) -> dict[str, str]:
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
        found: dict[str, str] = {}
        pending = list(quadlets.values())
        while pending:
            for directive in UnitFile(pending.pop()).directives():
                if directive.key not in constants.QUADLET_REFERENCE_DIRECTIVES:
                    continue
                name = directive.value.strip().partition(":")[0]
                # Only a quadlet's filename - a path, or a plain podman network or volume name,
                # is not one, and a path must not be read as one
                if "/" in name or not name.endswith(REFERABLE_QUADLET_EXTENSIONS):
                    continue
                if name in quadlets or name in found:
                    continue
                path = os.path.join(constants.QUADLET_SYSTEMD_DIR, name)
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    found[name] = f.read()
                pending.append(found[name])
        return found
