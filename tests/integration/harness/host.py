"""
Read the test VM's state over ssh: units, journal, files, containers.

Assertions about "how many times did this restart" are made from journal events, never from
sleeps: systemd logs a message with a fixed id each time a unit starts or stops, and counting
those after a cursor is exact.
"""

import json
import shlex
import subprocess
from dataclasses import dataclass

from ansible_collections.kriansa.quadlet.tests.integration.harness.qemu import Vm

# systemd's catalog ids for "unit started" and "unit stopped"
UNIT_STARTED = "39f53479d3a045ac8e11786248231fbf"
UNIT_STOPPED = "9d1aaa27d60140bd96365438aad20286"


class HostCommandError(AssertionError):
    """A command on the VM failed when the test needed it to succeed."""


@dataclass
class FileInfo:
    owner: str  # "uid:gid"
    mode: str  # octal, e.g. "644"
    context: str  # SELinux label

    @property
    def selinux_type(self) -> str:
        return self.context.split(":")[2]


class Host:
    """Runs commands on the VM as root."""

    def __init__(self, vm: Vm):
        self.vm = vm

    def run(
        self, command: str, *, check: bool = True, timeout: float = 120
    ) -> subprocess.CompletedProcess:
        """Run a shell command as root on the VM."""
        result = subprocess.run(
            self.vm.ssh_argv("sudo", "bash", "-c", shlex.quote(command)),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise HostCommandError(
                f"`{command}` failed ({result.returncode}):\n{result.stdout}{result.stderr}"
            )
        return result

    def output(self, command: str, **kwargs) -> str:
        return self.run(command, **kwargs).stdout.strip()

    # --- units -------------------------------------------------------------------------------

    def unit(self, name: str, *properties: str) -> dict[str, str]:
        """Selected `systemctl show` properties of a unit."""
        args = " ".join(f"-p {shlex.quote(p)}" for p in properties)
        lines = self.output(f"systemctl show {args} {shlex.quote(name)}").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def invocation_id(self, name: str) -> str:
        """Changes every time the unit is started; empty while it has never run."""
        return self.unit(name, "InvocationID")["InvocationID"]

    def active_state(self, name: str) -> str:
        return self.unit(name, "ActiveState")["ActiveState"]

    def is_enabled(self, name: str) -> bool:
        return (
            self.run(f"systemctl is-enabled {shlex.quote(name)}", check=False).stdout.strip()
            == "enabled"
        )

    def unit_exists(self, name: str) -> bool:
        return self.unit(name, "LoadState")["LoadState"] == "loaded"

    # --- journal -----------------------------------------------------------------------------

    def cursor(self) -> str:
        """A position in the journal; events after it belong to whatever happens next."""
        out = self.output("journalctl --quiet -n 0 --show-cursor")
        return out.rsplit("-- cursor: ", 1)[1].strip()

    def _events(self, message_id: str, unit: str, since: str) -> list[dict]:
        out = self.output(
            f"journalctl --quiet -o json --after-cursor={shlex.quote(since)} "
            f"MESSAGE_ID={message_id} UNIT={shlex.quote(unit)}",
            check=False,
        )
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def starts(self, unit: str, since: str) -> int:
        """How many times a unit finished starting after the cursor."""
        return len(self._events(UNIT_STARTED, unit, since))

    def stops(self, unit: str, since: str) -> int:
        return len(self._events(UNIT_STOPPED, unit, since))

    def journal(self, since: str, unit: str | None = None) -> str:
        unit_filter = f"-u {shlex.quote(unit)}" if unit else ""
        return self.output(
            f"journalctl --quiet --no-pager -o short-precise "
            f"--after-cursor={shlex.quote(since)} {unit_filter}",
            check=False,
        )

    # --- files -------------------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {shlex.quote(path)}", check=False).returncode == 0

    def read(self, path: str) -> str:
        """A file's exact content. Read as bytes: text mode would turn \\r\\n into \\n."""
        result = subprocess.run(
            self.vm.ssh_argv("sudo", "cat", shlex.quote(path)),
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise HostCommandError(f"cannot read {path}: {result.stderr.decode(errors='replace')}")
        return result.stdout.decode("utf-8")

    def stat(self, path: str) -> FileInfo:
        owner, mode, context = self.output(f"stat -c '%u:%g %a %C' {shlex.quote(path)}").split(" ")
        return FileInfo(owner, mode, context)

    def listdir(self, path: str) -> list[str]:
        out = self.output(f"ls -A {shlex.quote(path)}", check=False)
        return sorted(out.splitlines()) if out else []

    def mtime(self, path: str) -> str:
        return self.output(f"stat -c %Y.%y {shlex.quote(path)}")

    # --- containers --------------------------------------------------------------------------

    def inspect(self, container: str) -> dict:
        return json.loads(self.output(f"podman inspect {shlex.quote(container)}"))[0]

    def container_exec(self, container: str, command: str, **kwargs) -> subprocess.CompletedProcess:
        return self.run(
            f"podman exec {shlex.quote(container)} sh -c {shlex.quote(command)}", **kwargs
        )

    def uid_map(self, container: str) -> list[list[int]]:
        """The container's uid map: [inside, outside, count] per line."""
        pid = self.inspect(container)["State"]["Pid"]
        return [
            [int(n) for n in line.split()]
            for line in self.output(f"cat /proc/{pid}/uid_map").splitlines()
        ]

    def volume_mountpoint(self, volume: str) -> str:
        return self.output(
            f"podman volume inspect --format '{{{{.Mountpoint}}}}' {shlex.quote(volume)}"
        )

    def seed_volume(self, volume: str, owner: str = "0:0") -> str:
        """
        Create a volume holding one file, ahead of the deploy that would create it empty.

        Seeding stands in for a volume that already holds data, and lets a test plant files with
        specific ownership before the bootstrap unit runs.

        Returns:
            The volume's mount point on the host
        """
        self.run(f"podman volume create {shlex.quote(volume)}")
        mountpoint = self.volume_mountpoint(volume)
        self.run(f"touch {mountpoint}/seed && chown -R {owner} {mountpoint}")
        return mountpoint

    # --- SELinux -----------------------------------------------------------------------------

    def now(self) -> str:
        """The VM's clock, in the form ausearch accepts."""
        return self.output("date '+%m/%d/%Y %H:%M:%S'")

    def denials_since(self, timestamp: str) -> str:
        """SELinux denials logged since a `now()` timestamp; empty if none."""
        date, time = timestamp.split(" ")
        out = self.run(f"ausearch -m avc -i -ts {date} {time}", check=False)
        # ausearch exits 1 with "<no matches>" when there is nothing to report
        return "" if out.returncode == 1 else out.stdout
