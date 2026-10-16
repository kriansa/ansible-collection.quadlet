"""One QEMU virtual machine: start it, wait until it can be logged into, stop it."""

from __future__ import annotations

import ctypes
import json
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ansible_collections.kriansa.commons.tests.integration.harness.seed import USER

_PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """Runs in the QEMU child: if pytest dies, so does the VM, instead of lingering."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Vm:
    """
    A VM booted from `disk`, reachable over ssh on a localhost port.

    With `internet` False the VM has no route anywhere except back to the host's forwarded ssh
    port. Tests run that way: anything that tries to pull an image fails at once instead of
    depending on the network.
    """

    disk: Path
    workdir: Path
    private_key: Path
    seed: Optional[Path] = None
    internet: bool = False
    memory_mb: int = 3072
    cpus: int = 2
    port: int = field(default_factory=free_port)
    # Normally the VM is killed when the process that started it dies; --keep-vm needs it not to be
    outlive_parent: bool = False
    process: Optional[subprocess.Popen] = None

    @property
    def serial_log(self) -> Path:
        return self.workdir / "serial.log"

    @property
    def qmp_socket(self) -> Path:
        return self.workdir / "qmp.sock"

    def start(self) -> "Vm":
        self.workdir.mkdir(parents=True, exist_ok=True)
        restrict = "" if self.internet else ",restrict=on"
        argv = [
            "qemu-system-x86_64",
            "-enable-kvm", "-cpu", "host", "-machine", "q35",
            "-smp", str(self.cpus), "-m", str(self.memory_mb),
            # cache=unsafe: the disk is a throwaway overlay, and losing it on a host crash is fine
            "-drive", f"file={self.disk},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
            "-nic", f"user,model=virtio-net-pci{restrict},hostfwd=tcp:127.0.0.1:{self.port}-:22",
            "-device", "virtio-rng-pci",
            "-serial", f"file:{self.serial_log}",
            "-qmp", f"unix:{self.qmp_socket},server=on,wait=off",
            "-display", "none",
        ]
        if self.seed is not None:
            argv += ["-drive", f"file={self.seed},if=virtio,format=raw,readonly=on"]
        # A file rather than a pipe: nothing reads QEMU's stderr while it runs, and a full pipe
        # would freeze the VM
        with open(self.workdir / "qemu.stderr", "wb") as stderr:
            # pylint: disable-next=consider-using-with,subprocess-popen-preexec-fn
            self.process = subprocess.Popen(
                argv,
                preexec_fn=None if self.outlive_parent else _die_with_parent,
                start_new_session=self.outlive_parent,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        return self

    def ssh_argv(self, *command: str) -> List[str]:
        return [
            "ssh", "-p", str(self.port), "-i", str(self.private_key),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            f"{USER}@127.0.0.1", *command,
        ]

    def wait_for_ssh(self, timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = (self.workdir / "qemu.stderr").read_text(errors="replace")
                raise RuntimeError(f"QEMU exited ({self.process.returncode}): {stderr}")
            result = subprocess.run(self.ssh_argv("true"), capture_output=True, check=False, timeout=30)
            if result.returncode == 0:
                return
            time.sleep(1)
        raise TimeoutError(f"no ssh after {timeout}s; last console output:\n{self.console_tail()}")

    def console_tail(self, lines: int = 40) -> str:
        if not self.serial_log.exists():
            return "(no console output)"
        return "\n".join(self.serial_log.read_text(errors="replace").splitlines()[-lines:])

    def _qmp(self, command: str) -> None:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(self.qmp_socket))
            reader = sock.makefile("r")
            reader.readline()  # greeting
            for payload in ({"execute": "qmp_capabilities"}, {"execute": command}):
                sock.sendall(json.dumps(payload).encode() + b"\n")
                reader.readline()

    def power_off(self, timeout: float = 120) -> None:
        """Shut the guest down cleanly, so everything it wrote reaches the disk."""
        subprocess.run(self.ssh_argv("sudo", "systemctl", "poweroff"), capture_output=True,
                       check=False, timeout=30)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise

    def kill(self) -> None:
        """Stop the VM at once. Fine for a throwaway overlay."""
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self._qmp("quit")
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=10)
