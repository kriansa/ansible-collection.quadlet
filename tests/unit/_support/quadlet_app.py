"""
Run the quadlet_app module without a host.

Only module-side code is imported here: ansible-test runs module unit tests where controller code
(the action plugin, ansible.errors, the templating engine) cannot be imported. Rendering through
the action plugin lives in quadlet_app_render.py.
"""

from dataclasses import dataclass, field
from typing import Any

from ansible_collections.kriansa.quadlet.plugins.module_utils import generator, systemctl
from ansible_collections.kriansa.quadlet.plugins.module_utils.host_file import HostFile
from ansible_collections.kriansa.quadlet.plugins.module_utils.quadlets import Volume
from ansible_collections.kriansa.quadlet.plugins.modules import quadlet_app as module
from ansible_collections.kriansa.quadlet.tests.unit._support.fakes import (
    AnsibleExitJson,
    AnsibleFailJson,
    FakeModule,
    FakeSystemd,
)

FILE_KINDS = ("quadlets", "units", "init", "config")

# How each group of files is labelled in the module's `_files` argument
KIND_OF_GROUP = {"quadlets": "quadlet", "units": "unit", "init": "init", "config": "config"}


def to_module_args(files_data: dict[str, Any] | None) -> dict[str, Any]:
    """
    Turn a set of app files into the module's arguments.

    Names travel in `_files`, which is logged; contents in `_contents`, which is no_log and is
    matched to `_files` by position.

    Args:
        files_data: Files grouped as quadlets, units, init and config (see make_files_data)
    """
    if files_data is None:
        return {"_files": None, "_contents": None}
    files, contents = [], []
    for group in FILE_KINDS:
        for entry in files_data.get(group, []):
            files.append(
                {
                    "kind": KIND_OF_GROUP[group],
                    **{key: value for key, value in entry.items() if key != "content"},
                }
            )
            contents.append(entry["content"])
    return {"_files": files, "_contents": contents}


def from_module_args(args: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The inverse of to_module_args: the files a module call carries, grouped again."""
    group_of = {kind: group for group, kind in KIND_OF_GROUP.items()}
    files_data: dict[str, list[dict[str, Any]]] = {group: [] for group in FILE_KINDS}
    for entry, content in zip(args["_files"], args["_contents"]):
        files_data[group_of[entry["kind"]]].append(
            {**{key: value for key, value in entry.items() if key != "kind"}, "content": content}
        )
    return files_data


def files_data_to_tree(files_data: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Flatten app files, grouped as make_files_data groups them, into relative path -> content."""
    tree = {}
    for kind in ("quadlets", "units"):
        for entry in files_data.get(kind, []):
            tree[f"{kind}/{entry['name']}"] = entry["content"]
    for kind in ("init", "config"):
        for entry in files_data.get(kind, []):
            tree[f"{kind}/{entry['container']}/{entry['path']}"] = entry["content"]
    return tree


def tree_to_files_data(tree: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """The inverse of files_data_to_tree, in the order the action plugin produces."""
    files_data: dict[str, list[dict[str, Any]]] = {kind: [] for kind in FILE_KINDS}
    for rel in sorted(tree):
        kind, rest = rel.split("/", 1)
        if kind in ("quadlets", "units"):
            files_data[kind].append({"name": rest, "content": tree[rel]})
        else:
            container, path = rest.split("/", 1)
            files_data[kind].append({"container": container, "path": path, "content": tree[rel]})
    return files_data


def install_processes(monkeypatch, subprocess_module, time_module=None):
    """
    Stand in for every process the module starts - systemctl, journalctl and podman's generator -
    and, if given, for its pauses.

    Args:
        subprocess_module: Takes the place of `subprocess` wherever the module starts a process
        time_module: Takes the place of `time` where the module waits
    """
    for user in (systemctl, generator):
        monkeypatch.setattr(user, "subprocess", subprocess_module)
    if time_module is not None:
        monkeypatch.setattr(systemctl, "time", time_module)


@dataclass
class Deployment:
    """What one module run did."""

    # Absolute destination path -> content, for every file it wrote
    files: dict[str, str] = field(default_factory=dict)
    exit: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    systemd: FakeSystemd | None = None

    @property
    def tree(self) -> dict[str, str]:
        """files, keyed relative to / so it can be stored as a golden tree."""
        return {path.lstrip("/"): content for path, content in self.files.items()}

    def summary(self) -> dict[str, Any]:
        """Everything but the file contents, for a JSON golden."""
        return {
            "exit": self.exit,
            "failure": self.failure,
            "calls": [{"argv": call.argv, "timeout": call.timeout} for call in self.systemd.calls],
        }


def make_files_data(
    quadlets: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    init: dict[str, dict[str, str]] | None = None,
    config: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Build the `_files_data` the action plugin would send.

    Args:
        quadlets: filename -> content
        units: filename -> content
        init: container directory -> {relative path -> content}
        config: same as init
    """

    def nested(tree):
        return [
            {"container": container, "path": path, "content": content}
            for container, files in sorted((tree or {}).items())
            for path, content in sorted(files.items())
        ]

    return {
        "quadlets": [{"name": n, "content": c} for n, c in sorted((quadlets or {}).items())],
        "units": [{"name": n, "content": c} for n, c in sorted((units or {}).items())],
        "init": nested(init),
        "config": nested(config),
    }


@dataclass
class Outcome:
    """How one module run ended."""

    exit: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None

    @property
    def changed(self) -> bool:
        assert self.failure is None, self.failure
        return self.exit["changed"]

    @property
    def msg(self) -> str:
        return (self.exit or self.failure)["msg"]


def run_module(
    files_data: dict[str, Any] | None,
    *,
    src: str = "/apps/app",
    state: str = "installed",
    force: bool = False,
    systemctl_timeout: int = 120,
    settle_time: int = 0,
    name: str | None = None,
    control_node_processed: bool = True,
) -> Outcome:
    """
    Run the real module with these parameters and report how it ended.

    The settle check is off unless asked for: it has tests of its own, and every other test would
    otherwise have its queries in the way.
    """
    fake = FakeModule(
        {
            "src": src,
            "name": name,
            "state": state,
            "force": force,
            "systemctl_timeout": systemctl_timeout,
            "settle_time": settle_time,
            "_control_node_processed": control_node_processed,
            **to_module_args(files_data),
        }
    )
    try:
        module.QuadletAppModule(fake).run()
    except AnsibleExitJson as e:
        return Outcome(exit=e.result)
    except AnsibleFailJson as e:
        return Outcome(failure=e.result)
    raise AssertionError("the module returned without calling exit_json or fail_json")


def deploy_fresh(
    monkeypatch,
    files_data: dict[str, Any],
    app_name: str,
    *,
    systemd: FakeSystemd | None = None,
    **params,
) -> Deployment:
    """
    Stage B: run the module as if on a host that has never seen this app.

    Every file counts as changed, nothing is written to disk (the files the module would write
    are captured instead), and systemd is faked.
    """
    systemd = systemd or FakeSystemd()
    deployment = Deployment(systemd=systemd)

    install_processes(monkeypatch, systemd.as_subprocess_module(), systemd.as_time_module())
    monkeypatch.setattr(HostFile, "differs_from_host", lambda self: True)
    # Nor any ownership marker, so every bootstrap unit is still to run
    monkeypatch.setattr(Volume, "ownership_applied", lambda self: False)
    monkeypatch.setattr(
        HostFile, "write", lambda self: deployment.files.update({self.dest_path: self.content})
    )

    fake = FakeModule(
        {
            "src": app_name,
            "name": None,
            "state": "started",
            "force": False,
            "systemctl_timeout": 120,
            "settle_time": 5,
            "_control_node_processed": True,
            **to_module_args(files_data),
            **params,
        }
    )
    try:
        module.QuadletAppModule(fake).run()
    except AnsibleExitJson as e:
        deployment.exit = e.result
    except AnsibleFailJson as e:
        deployment.failure = e.result
    return deployment
