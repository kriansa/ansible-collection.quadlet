# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""systemd, as the module asks it to run the app's units and to report on them."""

import subprocess
import time

from ansible_collections.kriansa.quadlet.plugins.module_utils.errors import QuadletAppError


class Systemd:
    """The systemctl and journalctl commands the module runs."""

    def __init__(self, timeout: int):
        """
        Args:
            timeout: Seconds a command changing units may take (start, restart, daemon-reload).
                Queries have a short fixed timeout of their own.
        """
        self.timeout = timeout

    def run(self, *args: str) -> None:
        """
        Run a systemctl command (e.g. "restart", "x.service").

        Raises:
            QuadletAppError: If it fails, times out, or cannot be run
        """
        cmd = ["systemctl", *args]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            raise QuadletAppError(
                f"systemctl {' '.join(args)} timed out after {self.timeout}s"
            ) from None
        except Exception as e:  # pylint: disable=broad-exception-caught
            raise QuadletAppError(f"Failed to execute systemctl: {e}") from e

        if result.returncode != 0:
            raise QuadletAppError(
                f"systemctl {' '.join(args)} failed: {result.stderr.strip()}",
                cmd=" ".join(cmd),
                rc=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    @staticmethod
    def is_active(unit: str) -> bool:
        """Whether a unit is running. A query that fails counts as no."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() == "active"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def is_enabled(unit: str) -> bool:
        """Whether a unit is enabled to start on boot. A query that fails counts as no."""
        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", unit],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.returncode == 0 and result.stdout.strip() == "enabled"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def properties(units: list[str]) -> dict[str, dict[str, str]]:
        """
        Read the state of each unit: its type, whether it runs, and which run of it is current.

        Returns:
            Unit name -> property name -> value

        Raises:
            QuadletAppError: If the query fails or times out
        """
        if not units:
            return {}

        cmd = ["systemctl", "show", "-p", "Id,Type,ActiveState,SubState,InvocationID", *units]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        except subprocess.TimeoutExpired:
            raise QuadletAppError(f"{' '.join(cmd)} timed out") from None
        if result.returncode != 0:
            raise QuadletAppError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")

        # One block of Key=Value lines per unit, separated by a blank line
        properties = {}
        for block in result.stdout.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            properties[values.get("Id", "")] = values
        return properties

    @staticmethod
    def dependencies(service: str) -> list[str] | None:
        """
        The services a service depends on, however indirectly.

        Returns:
            Their names, the service itself left out. None when systemd could not say: it does
            not have the service, or a query failed or timed out.

        Raises:
            QuadletAppError: If systemctl is not there to ask
        """
        # Listing the dependencies of a unit systemd does not have succeeds, and shows a unit with
        # none - which would read as a service that depends on nothing. So whether systemd has it
        # is asked first.
        load_state = Systemd._query("show", "-p", "LoadState", service)
        if load_state is None or "LoadState=loaded" not in load_state.stdout.splitlines():
            return None

        # Without --all, systemctl only expands targets, so a container two Requires= away from
        # main would be left out - and keep running its old definition after a deploy that
        # changed it.
        result = Systemd._query("list-dependencies", "--all", "--type", "service", service)
        if result is None:
            return None

        dependencies = []
        for line in result.stdout.splitlines():
            # --type only indents, it draws no tree
            name = line.strip()
            if name.endswith(".service") and name != service and name not in dependencies:
                dependencies.append(name)
        return dependencies

    @staticmethod
    def _query(*args: str) -> subprocess.CompletedProcess | None:
        """
        Run a systemctl query; None if it failed or timed out.

        Raises:
            QuadletAppError: If systemctl is not there to ask
        """
        try:
            result = subprocess.run(
                ["systemctl", *args], capture_output=True, text=True, check=False, timeout=10
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError as e:
            raise QuadletAppError(f"systemctl command not found: {e}") from e
        return result if result.returncode == 0 else None

    @staticmethod
    def journal_tail(unit: str) -> str:
        """The last lines a unit logged, its container's output included; empty if unavailable."""
        cmd = ["journalctl", "--unit", unit, "--lines", "10", "--no-pager", "--output", "cat"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return result.stdout.strip()


class ServiceWatch:
    """
    Services watched while a run starts them, to check they stay up.

    Podman tells systemd a container is ready as soon as the container starts, so systemctl
    reports success even for one whose process dies a moment later. Each service is watched under
    the run of it the module started: if systemd has had to start it again by the end of the wait,
    it exited in between, however quickly it came back.
    """

    def __init__(self, systemd: Systemd, units: list[str], settle_time: int):
        """
        Note which run of each unit is current, before anything is started.

        Args:
            systemd: Where to ask
            units: The services to watch
            settle_time: Seconds a service this run started has to stay up
        """
        self.systemd = systemd
        self.settle_time = settle_time
        self.before = systemd.properties(units)

    def check(self) -> None:
        """
        Wait, then check every service started since the watch began is still on that run.

        Raises:
            QuadletAppError: Naming each one that did not stay up, with its last log lines
        """
        after = self.systemd.properties(list(self.before))
        started = [
            unit
            for unit, state in after.items()
            if state.get("InvocationID") != self.before[unit].get("InvocationID")
            and state.get("Type") != "oneshot"
        ]
        if not started:
            return

        time.sleep(self.settle_time)

        failures = []
        for unit, state in self.systemd.properties(started).items():
            if state.get("ActiveState") != "active":
                problem = f"it is {state.get('ActiveState')}"
                if state.get("SubState") not in (state.get("ActiveState"), None):
                    problem += f" ({state.get('SubState')})"
            elif state.get("InvocationID") != after[unit].get("InvocationID"):
                problem = "systemd had to start it again"
            else:
                continue

            message = f"{unit} did not stay running after it was started: {problem}."
            log = self.systemd.journal_tail(unit)
            if log:
                message += f" Its last log lines:\n{log}"
            failures.append(message)

        if failures:
            raise QuadletAppError("\n\n".join(failures))
