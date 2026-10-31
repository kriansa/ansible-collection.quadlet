"""
A stack under test: apps under one directory, deployed together by the quadlet_stack role, with a
provider role that records what it was given.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import App
from ansible_collections.kriansa.quadlet.tests.integration.harness.host import Host
from ansible_collections.kriansa.quadlet.tests.integration.harness.playbook import (
    PlaybookRun,
    PlaybookRunner,
)

# Where the stack's apps are, under the playbook's directory: the role's default
STACK_DIR = "quadlet-apps"

STACK_TASK = "deploy the stack"

# The test provider. On each side of the deploy it writes one line per claim it is given: the
# hook, the claiming app, the claim, how many claims it got, and whether the claiming app's main
# service was running at that moment. Its handler writes "reload" once, at the end of the play.
PROVIDER_TASKS = [
    {
        "name": "qa provider",
        "when": "quadlet_hook in ['before', 'after']",
        "block": [
            {
                "name": "record each claim",
                "ansible.builtin.shell": (
                    'echo "{{ quadlet_hook }} {{ item.app }} {{ item.claim }} '
                    "{{ quadlet_claims | length }} "
                    '$(systemctl is-active {{ item.app }}--main.service)" >> {{ qa_provider_log }}'
                ),
                "loop": "{{ quadlet_claims }}",
                "changed_when": True,
                "notify": "qa provider reload",
            }
        ],
    }
]
PROVIDER_HANDLERS = [
    {
        "name": "qa provider reload",
        "ansible.builtin.shell": "echo reload >> {{ qa_provider_log }}",
    }
]


@dataclass
class StackRun:
    """One run of the role, and what it did."""

    run: PlaybookRun

    @property
    def failed(self) -> bool:
        return self.run.returncode != 0

    @property
    def msg(self) -> str:
        """The message of the task that failed the run."""
        failures = [task for task in self.run.tasks if task.failed]
        return failures[-1].msg if failures else ""

    def results(self, action: str) -> list:
        """The results of every task of this action, in the order they ran."""
        return [task for task in self.run.tasks if task.action == action]


class Stack:
    """Apps deployed together through the quadlet_stack role."""

    def __init__(self, playbook_dir: Path, host: Host, runner: PlaybookRunner, make_app):
        """
        Args:
            make_app: The make_app fixture, which also removes the apps when the test ends
        """
        self.playbook_dir = playbook_dir
        self.host = host
        self.runner = runner
        self._make_app = make_app
        self.runs: list[StackRun] = []
        # Unique to the test, so tests sharing the VM never read each other's lines
        self.provider_log = f"/var/tmp/qa-provider-{uuid.uuid4().hex}.log"

    def app(self, tag: str) -> App:
        """A new app of the stack."""
        return self._make_app(tag, apps_dir=STACK_DIR)

    def add_provider(self, name: str = "qa_provider") -> None:
        """Put the test provider on the roles path, under this name."""
        role = self.playbook_dir / "roles" / name
        for subdir, content in (("tasks", PROVIDER_TASKS), ("handlers", PROVIDER_HANDLERS)):
            (role / subdir).mkdir(parents=True, exist_ok=True)
            (role / subdir / "main.yml").write_text(yaml.safe_dump(content, sort_keys=False))

    def deploy(
        self,
        deploy_only: list[str] | None = None,
        state: str = "started",
        tags: list[str] | None = None,
    ) -> StackRun:
        """
        Run the role over the stack.

        Args:
            deploy_only: The apps to deploy, by name; all of them when None
            tags: Run with --tags, given to the role's import as a playbook would
        """
        task: dict[str, Any] = {
            "name": STACK_TASK,
            "ansible.builtin.import_role": {"name": "kriansa.quadlet.quadlet_stack"},
            "vars": {"quadlet_stack_deploy_only": deploy_only or [], "quadlet_stack_state": state},
        }
        extra_args = None
        if tags:
            task["tags"] = tags
            extra_args = ["--tags", ",".join(tags)]

        play = {
            "name": "deploy the stack",
            "hosts": "testhost",
            "gather_facts": False,
            "become": True,
            "vars": {"qa_provider_log": self.provider_log},
            "tasks": [task],
        }
        run = self.runner.run_plays(
            [play], self.playbook_dir, name=f"stack-{len(self.runs) + 1}", extra_args=extra_args
        )
        stack_run = StackRun(run)
        self.runs.append(stack_run)
        return stack_run

    def provider_calls(self) -> list[str]:
        """What the test provider recorded, one line per claim per hook, in order."""
        return self.host.output(f"cat {self.provider_log}", check=False).splitlines()
