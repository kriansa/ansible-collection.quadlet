"""
Render an app through the real quadlet_app action plugin (golden Stage A).

Kept apart from quadlet_app.py because it imports controller code, which module unit tests
cannot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ansible_collections.kriansa.commons.plugins.action import quadlet_app as action_plugin
from ansible_collections.kriansa.commons.tests.unit._support.action import run_action


def render_app(app_dir: Path, task_vars: Dict[str, Any], **args) -> Dict[str, List[Dict[str, Any]]]:
    """
    Stage A: run the action plugin over an app and return the `_files_data` it sends.

    The app is referenced relative to its parent directory, the way a playbook names
    `podman-apps/<app>`, so path resolution is exercised too.
    """
    run = run_action(
        action_plugin.ActionModule,
        basedir=app_dir.parent,
        args={"src": app_dir.name, "state": "started", **args},
        task_vars=task_vars,
    )
    if run.module_args is None:
        raise AssertionError(f"the action plugin never called the module: {run.result}")
    return run.module_args["_files_data"]
