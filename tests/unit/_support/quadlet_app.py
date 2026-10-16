"""
Run the quadlet_app module without a host.

Only module-side code is imported here: ansible-test runs module unit tests where controller code
(the action plugin, ansible.errors, the templating engine) cannot be imported. Rendering through
the action plugin lives in quadlet_app_render.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ansible_collections.kriansa.commons.plugins.modules import quadlet_app as module
from ansible_collections.kriansa.commons.tests.unit._support.fakes import (
    AnsibleExitJson,
    AnsibleFailJson,
    FakeModule,
    FakeSystemd,
)

FILE_KINDS = ("quadlets", "units", "init", "config")


def files_data_to_tree(files_data: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    """Flatten `_files_data` into relative path -> content."""
    tree = {}
    for kind in ("quadlets", "units"):
        for entry in files_data.get(kind, []):
            tree[f"{kind}/{entry['name']}"] = entry["content"]
    for kind in ("init", "config"):
        for entry in files_data.get(kind, []):
            tree[f"{kind}/{entry['container']}/{entry['path']}"] = entry["content"]
    return tree


def tree_to_files_data(tree: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    """The inverse of files_data_to_tree, in the order the action plugin produces."""
    files_data: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in FILE_KINDS}
    for rel in sorted(tree):
        kind, rest = rel.split("/", 1)
        if kind in ("quadlets", "units"):
            files_data[kind].append({"name": rest, "content": tree[rel]})
        else:
            container, path = rest.split("/", 1)
            files_data[kind].append({"container": container, "path": path, "content": tree[rel]})
    return files_data


@dataclass
class Deployment:
    """What one module run did."""

    # Absolute destination path -> content, for every file it wrote
    files: Dict[str, str] = field(default_factory=dict)
    exit: Optional[Dict[str, Any]] = None
    failure: Optional[Dict[str, Any]] = None
    systemd: Optional[FakeSystemd] = None

    @property
    def tree(self) -> Dict[str, str]:
        """files, keyed relative to / so it can be stored as a golden tree."""
        return {path.lstrip("/"): content for path, content in self.files.items()}

    def summary(self) -> Dict[str, Any]:
        """Everything but the file contents, for a JSON golden."""
        return {
            "exit": self.exit,
            "failure": self.failure,
            "calls": [
                {"argv": call.argv, "timeout": call.timeout} for call in self.systemd.calls
            ],
        }


def make_files_data(quadlets: Optional[Dict[str, str]] = None,
                    units: Optional[Dict[str, str]] = None,
                    init: Optional[Dict[str, Dict[str, str]]] = None,
                    config: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
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

    exit: Optional[Dict[str, Any]] = None
    failure: Optional[Dict[str, Any]] = None

    @property
    def changed(self) -> bool:
        assert self.failure is None, self.failure
        return self.exit["changed"]

    @property
    def msg(self) -> str:
        return (self.exit or self.failure)["msg"]


def run_module(files_data: Optional[Dict[str, Any]], *, src: str = "/apps/app",
               state: str = "installed", force: bool = False, systemctl_timeout: int = 120,
               name: Optional[str] = None, control_node_processed: bool = True) -> Outcome:
    """Run the real module with these parameters and report how it ended."""
    fake = FakeModule(
        {
            "src": src,
            "name": name,
            "state": state,
            "force": force,
            "systemctl_timeout": systemctl_timeout,
            "_control_node_processed": control_node_processed,
            "_files_data": files_data,
        }
    )
    try:
        module.QuadletAppModule(fake).run()
    except AnsibleExitJson as e:
        return Outcome(exit=e.result)
    except AnsibleFailJson as e:
        return Outcome(failure=e.result)
    raise AssertionError("the module returned without calling exit_json or fail_json")


def deploy_fresh(monkeypatch, files_data: Dict[str, Any], app_name: str, *,
                 systemd: Optional[FakeSystemd] = None, **params) -> Deployment:
    """
    Stage B: run the module as if on a host that has never seen this app.

    Every file counts as changed, nothing is written to disk (the files the module would write
    are captured instead), and systemd is faked.
    """
    systemd = systemd or FakeSystemd()
    deployment = Deployment(systemd=systemd)

    monkeypatch.setattr(module, "subprocess", systemd.as_subprocess_module())
    monkeypatch.setattr(module.QuadletIdempotency, "file_changed", lambda self, path, content: True)
    monkeypatch.setattr(
        module.QuadletAppModule,
        "_deploy_files",
        lambda self, processed: deployment.files.update(processed),
    )

    fake = FakeModule(
        {
            "src": app_name,
            "name": None,
            "state": "started",
            "force": False,
            "systemctl_timeout": 120,
            "_control_node_processed": True,
            "_files_data": files_data,
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
