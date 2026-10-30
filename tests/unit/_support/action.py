"""
Run an action plugin in-process, with Ansible's real loader and templating engine.

Only the parts that would reach a remote host are faked: the connection, and the call that
ships the module there, which is captured instead.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from ansible.parsing.dataloader import DataLoader
from ansible.playbook.play_context import PlayContext
from ansible.template import Templar


@dataclass
class ActionRun:
    """What an action plugin produced."""

    result: dict[str, Any]
    # The module it asked to run and the arguments it sent, or None if it never got that far
    module_name: str | None
    module_args: dict[str, Any] | None
    module_task_vars: dict[str, Any] | None


def make_action(action_class, *, basedir: Path, args: dict[str, Any],
                task_vars: dict[str, Any] | None = None,
                task_level_vars: dict[str, Any] | None = None,
                check_mode: bool = False, async_val: int = 0,
                module_result: dict[str, Any] | None = None):
    """
    Build an action plugin instance wired to real Ansible objects.

    Args:
        action_class: The ActionModule class under test
        basedir: Directory relative paths resolve against, i.e. the playbook directory
        args: The task's arguments
        task_vars: Every variable the task sees (host, play, extra vars...)
        task_level_vars: The task's own `vars:` block, which the plugin templates itself
        check_mode: Whether the task runs in check mode
        async_val: The task's async setting
        module_result: What the module would have returned

    Returns:
        (action, captured) - captured fills in when the plugin calls its module
    """
    loader = DataLoader()
    loader.set_basedir(str(basedir))
    templar = Templar(loader=loader, variables=dict(task_vars or {}))

    task = MagicMock(name="task")
    task.action = "kriansa.quadlet.quadlet_app"
    task.args = dict(args)
    task.vars = dict(task_level_vars or {})
    task.async_val = async_val
    task.check_mode = check_mode
    task.get_search_path.return_value = [str(basedir)]

    connection = MagicMock(name="connection")
    connection._shell.tmpdir = None  # pylint: disable=protected-access

    action = action_class(task, connection, PlayContext(), loader, templar, None)

    captured: dict[str, Any] = {}

    def execute_module(module_name=None, module_args=None, task_vars=None, **_kwargs):
        captured.update(module_name=module_name, module_args=module_args, task_vars=task_vars)
        return dict(module_result or {"changed": False})

    action._execute_module = execute_module  # pylint: disable=protected-access
    return action, captured


def run_action(action_class, *, task_vars: dict[str, Any] | None = None, **kwargs) -> ActionRun:
    """Build the plugin with make_action, run it, and report what happened."""
    action, captured = make_action(action_class, task_vars=task_vars, **kwargs)
    result = action.run(task_vars=dict(task_vars or {}))
    return ActionRun(
        result=result,
        module_name=captured.get("module_name"),
        module_args=captured.get("module_args"),
        module_task_vars=captured.get("task_vars"),
    )
