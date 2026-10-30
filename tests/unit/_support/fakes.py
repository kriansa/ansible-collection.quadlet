"""
Stand-ins for AnsibleModule and for the processes the module runs.

The exit and fail exceptions derive from BaseException on purpose. The module wraps its whole run
in `except Exception`, so an ordinary exception raised from exit_json would be caught and turned
into a fail_json("Unexpected error: ...") - the test would then see a failure that never happens
for real.
"""

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


class AnsibleExitJson(BaseException):
    """Raised where the real module would exit successfully."""

    def __init__(self, kwargs: dict[str, Any]):
        super().__init__(kwargs)
        self.result = kwargs


class AnsibleFailJson(BaseException):
    """Raised where the real module would exit with a failure."""

    def __init__(self, kwargs: dict[str, Any]):
        super().__init__(kwargs)
        self.result = kwargs


class FakeModule:
    """The slice of AnsibleModule the quadlet modules use."""

    def __init__(self, params: dict[str, Any]):
        self.params = params

    def exit_json(self, **kwargs):
        raise AnsibleExitJson(kwargs)

    def fail_json(self, **kwargs):
        raise AnsibleFailJson(kwargs)


GENERATOR = "/usr/lib/systemd/system-generators/podman-system-generator"


@dataclass
class Call:
    """One process the module started."""

    argv: list[str]
    timeout: float | None
    # For a generator run: the directories it was pointed at, and the files in them at that moment
    unit_dirs: str | None = None
    staged: dict[str, str] | None = None

    @property
    def systemctl(self) -> list[str] | None:
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
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # A command prefix (e.g. ("start", "app--main.service")) -> (returncode, stderr)
    failures: dict[tuple, tuple] = field(default_factory=dict)
    generator_result: tuple = (0, "", "")
    # Called after each state-changing systemctl command, to model side effects
    on_command: Callable[["FakeSystemd", list[str]], None] | None = None
    # Unit -> the answers its next `is-active` queries get, in order, before falling back to
    # `active`. Models a unit changing state on its own between two queries.
    scripted_states: dict[str, list[str]] = field(default_factory=dict)
    # Where the module wrote its quadlets. When set, starting an ownership bootstrap unit does what
    # it would on a host - see run_bootstrap_unit
    quadlet_dir: str | None = None
    # Unit -> its Type= as `systemctl show` reports it; "notify", as for a container, unless listed
    types: dict[str, str] = field(default_factory=dict)
    # Units whose container exits right after it starts, leaving the unit failed
    exits_after_start: set = field(default_factory=set)
    # Units whose container keeps exiting and being started again by systemd: every pause of the
    # module finds them on a new run
    crash_loops: set = field(default_factory=set)
    # Unit -> what `journalctl` prints for it
    journal: dict[str, str] = field(default_factory=dict)
    # Unit -> how many times it has been started, which stands in for its InvocationID
    invocations: dict[str, int] = field(default_factory=dict)
    failed: set = field(default_factory=set)
    # The pauses the module took, in seconds
    sleeps: list[float] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def run(self, argv, capture_output=False, text=False, check=False, timeout=None, env=None):
        del capture_output, text, check
        argv = list(argv)
        call = Call(argv, timeout)
        self.calls.append(call)

        if argv[0] == GENERATOR:
            call.unit_dirs = (env or {}).get("QUADLET_UNIT_DIRS")
            call.staged = {}
            for directory in (call.unit_dirs or "").split(":"):
                if directory and os.path.isdir(directory):
                    for name in sorted(os.listdir(directory)):
                        with open(os.path.join(directory, name), encoding="utf-8") as handle:
                            call.staged[name] = handle.read()
            returncode, stdout, stderr = self.generator_result
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        if argv[0] == "journalctl":
            unit = argv[argv.index("--unit") + 1]
            return subprocess.CompletedProcess(argv, 0, self.journal.get(unit, ""), "")

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
        if verb == "show":
            blocks = [self._show(unit) for unit in args[3:]]
            return subprocess.CompletedProcess(argv, 0, "\n\n".join(blocks) + "\n", "")

        for prefix, (returncode, stderr) in self.failures.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, returncode, "", stderr)

        if verb in ("start", "restart"):
            self.active.update(args[1:])
            for unit in args[1:]:
                self.invocations[unit] = self.invocations.get(unit, 0) + 1
                self.failed.discard(unit)
                if unit in self.exits_after_start:
                    self.active.discard(unit)
                    self.failed.add(unit)
                self._run_if_bootstrap(unit)
        elif verb == "enable":
            self.enabled.update(args[1:])
        if self.on_command is not None:
            self.on_command(self, args)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _show(self, unit: str) -> str:
        """`systemctl show` for one unit, with the properties the module asks for."""
        if unit in self.active:
            state, substate = "active", "running"
        elif unit in self.failed:
            state, substate = "failed", "failed"
        else:
            state, substate = "inactive", "dead"
        count = self.invocations.get(unit, 0)
        return "\n".join([
            f"Id={unit}",
            f"Type={self.types.get(unit, 'notify')}",
            f"ActiveState={state}",
            f"SubState={substate}",
            f"InvocationID={f'invocation-{count}' if count else ''}",
        ])

    def _sleep(self, seconds: float):
        self.sleeps.append(seconds)
        for unit in self.crash_loops:
            self.invocations[unit] = self.invocations.get(unit, 0) + 1

    def as_time_module(self) -> SimpleNamespace:
        """A drop-in for the `time` name inside the module under test: pausing takes no time."""
        return SimpleNamespace(sleep=self._sleep)

    def _run_if_bootstrap(self, unit: str):
        if self.quadlet_dir is None or not unit.endswith("-volume-init.service"):
            return
        path = os.path.join(self.quadlet_dir, unit.removesuffix(".service") + ".container")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                run_bootstrap_unit(handle.read())

    def as_subprocess_module(self) -> SimpleNamespace:
        """A drop-in for the `subprocess` name inside the module under test."""
        return SimpleNamespace(
            run=self.run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
        )

    @property
    def generator_calls(self) -> list[Call]:
        return [call for call in self.calls if call.argv[0] == GENERATOR]

    @property
    def log(self) -> list[str]:
        """
        Every call as one readable line: systemctl's arguments, "generator" for a dry run, or the
        whole command for anything else.
        """
        return [
            " ".join(call.systemctl) if call.systemctl is not None
            else "generator" if call.argv[0] == GENERATOR
            else " ".join(call.argv)
            for call in self.calls
        ]

    @property
    def systemctl_calls(self) -> list[list[str]]:
        """The arguments of every systemctl call, in order."""
        return [call.systemctl for call in self.calls if call.systemctl is not None]

    @property
    def state_changes(self) -> list[list[str]]:
        """systemctl calls that change something, leaving out the read-only queries."""
        queries = ("is-active", "is-enabled", "list-dependencies", "show")
        return [args for args in self.systemctl_calls if args[0] not in queries]


def run_bootstrap_unit(text: str) -> bool:
    """
    Do on this machine what systemd does on a host when an ownership bootstrap unit starts.

    The unit is skipped when the marker its condition names already exists. Otherwise its
    ExecStartPost= commands run - the chown before them happens inside a container, and is left
    out. Only the commands the module writes there are understood; anything else is an error, so a
    change to the unit cannot slip past the tests unmodelled.

    Args:
        text: The unit file

    Returns:
        Whether the unit ran
    """
    lines = text.splitlines()
    condition = "ConditionPathExists=!"
    marker = next(line.removeprefix(condition) for line in lines if line.startswith(condition))
    if os.path.exists(marker):
        return False

    for line in lines:
        if not line.startswith("ExecStartPost="):
            continue
        argv = shlex.split(line.split("=", 1)[1])
        if argv[:2] == ["/usr/bin/mkdir", "-p"] and len(argv) == 3:
            os.makedirs(argv[2], exist_ok=True)
        elif (argv[0] == "/usr/bin/find" and argv[2:5] == ["-maxdepth", "1", "-regex"]
              and argv[6:] == ["-delete"]):
            # find matches its -regex against the whole path it prints, directory included
            for entry in os.listdir(argv[1]):
                path = os.path.join(argv[1], entry)
                if re.fullmatch(argv[5], path):
                    os.unlink(path)
        elif argv[0] == "/usr/bin/touch" and len(argv) == 2:
            with open(argv[1], "a", encoding="utf-8"):
                pass
        else:
            raise AssertionError(f"a bootstrap unit runs a command the fake does not know: {argv}")
    return True


def raiser(exc: BaseException) -> Callable[..., Any]:
    """A function that raises exc whatever it is called with, to stand in for a failing call."""

    def raise_it(*_args, **_kwargs):
        raise exc

    return raise_it
