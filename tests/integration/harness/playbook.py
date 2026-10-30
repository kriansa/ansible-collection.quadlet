"""
Run real playbooks against the test VM and read back each task's result.

ansible-playbook runs as a subprocess with an environment built from scratch: only this checkout
of the collection is visible, `passwordstore` is the test stub, and nothing from the machine's own
Ansible configuration applies.
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TESTS_DIR = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = TESTS_DIR / "integration"


@dataclass
class TaskResult:
    """One task's outcome on one host, as reported by the qa_capture callback."""

    task: str
    action: str
    status: str
    result: dict[str, Any]

    @property
    def changed(self) -> bool:
        return bool(self.result.get("changed"))

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def msg(self) -> str:
        return self.result.get("msg", "")


@dataclass
class PlaybookRun:
    """What one ansible-playbook invocation did."""

    returncode: int
    output: str
    tasks: list[TaskResult] = field(default_factory=list)

    def task(self, name: str) -> TaskResult:
        matches = [t for t in self.tasks if t.task == name]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one result for task {name!r}, got {len(matches)}; output:\n{self.output}"
            )
        return matches[0]


class PlaybookRunner:
    """Runs playbooks against one inventory, keeping each run's log for failure reports."""

    def __init__(self, inventory: Path, private_key: Path, workdir: Path):
        self.inventory = inventory
        self.private_key = private_key
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.ansible_home = Path(tempfile.mkdtemp(prefix="ansible-home-", dir=self.workdir))
        self.history: list[PlaybookRun] = []

    def environment(self, capture_file: Path) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")}
        env.update(
            {
                "ANSIBLE_CONFIG": str(INTEGRATION_DIR / "ansible.cfg"),
                # Set by tests/conftest.py to a root holding only this checkout
                "ANSIBLE_COLLECTIONS_PATH": os.environ["ANSIBLE_COLLECTIONS_PATH"],
                "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False",
                "ANSIBLE_HOME": str(self.ansible_home),
                "ANSIBLE_LOOKUP_PLUGINS": str(TESTS_DIR / "support" / "lookup_plugins"),
                "ANSIBLE_CALLBACK_PLUGINS": str(INTEGRATION_DIR / "callback_plugins"),
                "ANSIBLE_PRIVATE_KEY_FILE": str(self.private_key),
                "ANSIBLE_FORCE_COLOR": "0",
                "PASSWORD_STORE_DIR": "/nonexistent",
                "QA_CAPTURE_FILE": str(capture_file),
            }
        )
        return env

    def run(
        self, playbook: Path, *, extra_args: list[str] | None = None, timeout: float = 900
    ) -> PlaybookRun:
        """Run a playbook file and collect its results."""
        capture = Path(tempfile.mkstemp(prefix="results-", suffix=".jsonl", dir=self.workdir)[1])
        argv = [
            str(Path(sys.executable).parent / "ansible-playbook"),
            "-i",
            str(self.inventory),
            str(playbook),
            *(extra_args or []),
        ]
        process = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=self.environment(capture),
            cwd=playbook.parent,
            check=False,
            timeout=timeout,
        )
        tasks = [
            TaskResult(entry["task"], entry["action"], entry["status"], entry["result"])
            for entry in map(json.loads, capture.read_text().splitlines())
        ]
        run = PlaybookRun(process.returncode, process.stdout + process.stderr, tasks)
        self.history.append(run)
        return run

    def run_plays(
        self, plays: list[dict[str, Any]], directory: Path, *, name: str = "step", **kwargs
    ) -> PlaybookRun:
        """Write plays to a playbook file in `directory` and run it."""
        directory.mkdir(parents=True, exist_ok=True)
        playbook = directory / f"{name}.yml"
        playbook.write_text(yaml.safe_dump(plays, sort_keys=False))
        return self.run(playbook, **kwargs)


def write_inventory(path: Path, port: int) -> Path:
    path.write_text(
        f"[test]\ntesthost ansible_host=127.0.0.1 ansible_port={port} ansible_user=qa\n"
    )
    return path
