"""
Apps under test: written to a playbook directory on the controller, deployed with a real playbook,
and removed from the VM afterwards.
"""

import itertools
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ansible_collections.kriansa.quadlet.tests.integration.harness.host import Host
from ansible_collections.kriansa.quadlet.tests.integration.harness.playbook import (
    PlaybookRun,
    PlaybookRunner,
    TaskResult,
)

DEPLOY_TASK = "deploy the app"

# A container that idles until stopped, and stops at once: TERM ends it without the 10 second
# wait for a SIGKILL that a plain `sleep` would cost on every restart
IDLE = "Exec=/bin/sh -c 'trap \"exit 0\" TERM; while :; do sleep 1; done'"
IMAGE = "Image=docker.io/library/alpine:3.24"

_counter = itertools.count(1)


def _worker_tag() -> str:
    """Distinguishes app names between pytest-xdist workers sharing nothing but the naming."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return worker.replace("gw", "")


@dataclass
class Step:
    """One deploy and what it did."""

    run: PlaybookRun
    host: Host
    # The journal position just before the deploy; events after it are this step's doing
    since: str

    @property
    def result(self) -> TaskResult:
        return self.run.task(DEPLOY_TASK)

    @property
    def changed(self) -> bool:
        return self.result.changed

    @property
    def failed(self) -> bool:
        return self.result.failed

    @property
    def msg(self) -> str:
        return self.result.msg

    def starts(self, unit: str) -> int:
        return self.host.starts(unit, self.since)

    def stops(self, unit: str) -> int:
        return self.host.stops(unit, self.since)


class App:
    """An app directory plus the means to deploy it."""

    def __init__(
        self,
        name: str,
        playbook_dir: Path,
        host: Host,
        runner: PlaybookRunner,
        apps_dir: str = "apps",
    ):
        """
        Args:
            apps_dir: The directory under the playbook's the app is written in. A stack's apps
                go in one of their own, since the stack deploys every app it finds there.
        """
        self.name = name
        self.playbook_dir = playbook_dir
        self.directory = playbook_dir / apps_dir / name
        self.directory.mkdir(parents=True)
        self.host = host
        self.runner = runner
        self.steps: list[Step] = []

    # --- the app's files ---------------------------------------------------------------------

    def write(self, rel: str, content: str) -> "App":
        path = self.directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return self

    def write_bytes(self, rel: str, content: bytes) -> "App":
        path = self.directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return self

    def delete(self, rel: str) -> "App":
        path = self.directory / rel
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return self

    def read(self, rel: str) -> str:
        return (self.directory / rel).read_text()

    # --- names on the host -------------------------------------------------------------------

    def service(self, stem: str) -> str:
        return f"{self.name}--{stem}.service"

    def container(self, stem: str) -> str:
        return f"{self.name}--{stem}"

    def quadlet_path(self, filename: str) -> str:
        return f"/etc/containers/systemd/{self.name}--{filename}"

    # --- deploying ---------------------------------------------------------------------------

    def deploy(
        self,
        state: str = "started",
        *,
        module: str = "kriansa.quadlet.quadlet_app",
        args: dict[str, Any] | None = None,
        task_vars: dict[str, Any] | None = None,
        play_vars: dict[str, Any] | None = None,
        pre_tasks: list[dict] | None = None,
        become: bool = True,
        src: str | None = None,
        extra_args: list[str] | None = None,
    ) -> Step:
        """
        Deploy the app with a real playbook and return what happened.

        The playbook lives next to apps/, so `src` is given relative to it, the way a real
        playbook refers to its app directories.
        """
        task: dict[str, Any] = {
            "name": DEPLOY_TASK,
            module: {"src": src or f"apps/{self.name}", "state": state, **(args or {})},
        }
        if task_vars:
            task["vars"] = task_vars
        play = {
            "name": "deploy",
            "hosts": "testhost",
            "gather_facts": False,
            "become": become,
            "tasks": list(pre_tasks or []) + [task],
        }
        if play_vars:
            play["vars"] = play_vars

        since = self.host.cursor()
        run = self.runner.run_plays(
            [play],
            self.playbook_dir,
            name=f"{self.name}-{len(self.steps) + 1}",
            extra_args=extra_args,
        )
        step = Step(run, self.host, since)
        self.steps.append(step)
        return step


def new_app_name(tag: str) -> str:
    """A name unique within the session that still passes the module's naming rules."""
    return f"qa{_worker_tag()}{next(_counter):03d}-{tag}"


def remove_from_host(host: Host, name: str) -> None:
    """Undo everything a deploy of this app could have left on the VM."""
    prefix = shlex.quote(f"{name}--")
    host.run(
        "set +e\n"
        f"units=$(systemctl list-units --all --plain --no-legend {prefix}'*' | awk '{{print $1}}')\n"
        f"timers=$(systemctl list-unit-files --plain --no-legend {prefix}'*.timer' | awk '{{print $1}}')\n"
        '[ -n "$timers" ] && systemctl disable --now $timers\n'
        '[ -n "$units" ] && systemctl stop $units\n'
        f"rm -f /etc/containers/systemd/{prefix}* /etc/systemd/system/{prefix}*\n"
        f"rm -rf /srv/{shlex.quote(name)}\n"
        f"rm -f /var/lib/quadlet-app/{prefix}*\n"
        "systemctl daemon-reload\n"
        f"systemctl reset-failed {prefix}'*' 2>/dev/null\n"
        f"podman ps -aq --filter name=^{prefix} | xargs -r podman rm -f\n"
        f"podman pod ps -q --filter name=^{prefix} | xargs -r podman pod rm -f\n"
        f"podman volume ls -q | grep ^{prefix} | xargs -r podman volume rm -f\n"
        f"podman network ls -q | grep ^{prefix} | xargs -r podman network rm -f\n"
        "true\n",
        timeout=300,
    )


def leftover_apps(host: Host) -> list[str]:
    """Names of test apps that still have something on the VM."""
    out = host.output(
        "ls /etc/containers/systemd /etc/systemd/system /srv 2>/dev/null | grep -o '^qa[0-9a-z_-]*' "
        "| sed 's/--.*//' | sort -u",
        check=False,
    )
    return [name for name in out.splitlines() if name.startswith("qa")]
