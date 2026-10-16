"""
Stand-ins for AnsibleModule and for the processes the module runs.

The exit and fail exceptions derive from BaseException on purpose. The module wraps its whole run
in `except Exception`, so an ordinary exception raised from exit_json would be caught and turned
into a fail_json("Unexpected error: ...") - the test would then see a failure that never happens
for real.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional


class AnsibleExitJson(BaseException):
    """Raised where the real module would exit successfully."""

    def __init__(self, kwargs: Dict[str, Any]):
        super().__init__(kwargs)
        self.result = kwargs


class AnsibleFailJson(BaseException):
    """Raised where the real module would exit with a failure."""

    def __init__(self, kwargs: Dict[str, Any]):
        super().__init__(kwargs)
        self.result = kwargs


class FakeModule:
    """The slice of AnsibleModule the quadlet modules use."""

    def __init__(self, params: Dict[str, Any]):
        self.params = params

    def exit_json(self, **kwargs):
        raise AnsibleExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise AnsibleFailJson(kwargs)


GENERATOR = "/usr/lib/systemd/system-generators/podman-system-generator"


@dataclass
class Call:
    """One process the module started."""

    argv: List[str]
    timeout: Optional[float]

    @property
    def systemctl(self) -> Optional[List[str]]:
        """The systemctl arguments, or None when this was not a systemctl call."""
        return self.argv[1:] if self.argv[0] == "systemctl" else None


@dataclass
class FakeSystemd:
    """
    Answers the systemctl and quadlet generator calls the module makes, and records them.

    Units are inactive and disabled unless listed otherwise. Every state-changing command
    succeeds unless `failures` names it.
    """

    active: set = field(default_factory=set)
    enabled: set = field(default_factory=set)
    # Main service -> the lines `systemctl list-dependencies --type service` prints for it
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    # A command prefix (e.g. ("start", "app--main.service")) -> (returncode, stderr)
    failures: Dict[tuple, tuple] = field(default_factory=dict)
    generator_result: tuple = (0, "", "")
    # Called after each state-changing systemctl command, to model side effects
    on_command: Optional[Callable[["FakeSystemd", List[str]], None]] = None
    # Unit -> the answers its next `is-active` queries get, in order, before falling back to
    # `active`. Models a unit changing state on its own between two queries.
    scripted_states: Dict[str, List[str]] = field(default_factory=dict)
    calls: List[Call] = field(default_factory=list)

    def run(self, argv, capture_output=False, text=False, check=False, timeout=None):
        del capture_output, text, check
        argv = list(argv)
        self.calls.append(Call(argv, timeout))

        if argv[0] == GENERATOR:
            returncode, stdout, stderr = self.generator_result
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        if argv[0] != "systemctl":
            raise AssertionError(f"unexpected process: {argv}")

        args = argv[1:]
        verb = args[0]
        if verb == "is-active":
            unit = args[1]
            if self.scripted_states.get(unit):
                state = self.scripted_states[unit].pop(0)
            else:
                state = "active" if unit in self.active else "inactive"
            return subprocess.CompletedProcess(argv, 0 if state == "active" else 3, state + "\n", "")
        if verb == "is-enabled":
            unit = args[1]
            state = "enabled" if unit in self.enabled else "disabled"
            return subprocess.CompletedProcess(argv, 0 if state == "enabled" else 1, state + "\n", "")
        if verb == "list-dependencies":
            main = args[-1]
            lines = [main] + [f"  {dep}" for dep in self.dependencies.get(main, [])]
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

        for prefix, (returncode, stderr) in self.failures.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, returncode, "", stderr)

        if verb in ("start", "restart"):
            self.active.update(args[1:])
        elif verb == "enable":
            self.enabled.update(args[1:])
        if self.on_command is not None:
            self.on_command(self, args)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def as_subprocess_module(self) -> SimpleNamespace:
        """A drop-in for the `subprocess` name inside the module under test."""
        return SimpleNamespace(
            run=self.run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
        )

    @property
    def log(self) -> List[str]:
        """Every call as one readable line: systemctl arguments, or "generator" for a dry run."""
        return [
            " ".join(call.systemctl) if call.systemctl is not None else "generator"
            for call in self.calls
        ]

    @property
    def systemctl_calls(self) -> List[List[str]]:
        """The arguments of every systemctl call, in order."""
        return [call.systemctl for call in self.calls if call.systemctl is not None]

    @property
    def state_changes(self) -> List[List[str]]:
        """systemctl calls that change something, leaving out the read-only queries."""
        queries = ("is-active", "is-enabled", "list-dependencies")
        return [args for args in self.systemctl_calls if args[0] not in queries]


def raiser(exc: BaseException) -> Callable[..., Any]:
    """A function that raises exc whatever it is called with, to stand in for a failing call."""

    def raise_it(*_args, **_kwargs):
        raise exc

    return raise_it
