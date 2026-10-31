# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""podman, as the module asks it what exists on the host."""

import subprocess

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError


class Podman:
    """The podman queries the module runs."""

    @staticmethod
    def network_exists(name: str) -> bool:
        """
        Whether podman has a network by this name.

        Raises:
            QuadletAppError: If podman cannot be asked
        """
        cmd = ["podman", "network", "exists", name]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            raise QuadletAppError(f"{' '.join(cmd)} timed out") from None
        except OSError as e:
            raise QuadletAppError(f"Failed to execute podman: {e}") from e

        # 0 when it exists, 1 when it does not; anything else is podman failing to answer
        if result.returncode not in (0, 1):
            raise QuadletAppError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
        return result.returncode == 0
