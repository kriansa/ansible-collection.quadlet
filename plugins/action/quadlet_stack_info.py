# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Action plugin for quadlet_stack_info.

It runs on the control node alone: the apps, their manifests and their quadlets are all on the
control node, and nothing is asked of the managed host.
"""

# pylint: disable=import-error
from ansible.errors import AnsibleError  # type: ignore[import-not-found]
from ansible.utils.display import Display  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.plugin_utils.action import (  # type: ignore[import-not-found]
    QuadletAction,
)
from ansible_collections.kriansa.quadlet.plugins.plugin_utils.stack import (  # type: ignore[import-not-found]
    QuadletStack,
)

# pylint: enable=import-error

display = Display()

# Where the apps are when the task does not say
DEFAULT_SRC = "quadlet-apps"


class ActionModule(QuadletAction):  # pylint: disable=too-few-public-methods
    """Read every app under src, and report the stack they make."""

    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset(("src",))

    def run(self, tmp=None, task_vars=None):
        """Execute action plugin on control node."""
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        self._use_task_vars(task_vars)
        path = self._resolve_src_path(self._task.args.get("src") or DEFAULT_SRC)

        try:
            collected = QuadletStack(path, self._loader, self._templar).collect()
        # A directory that cannot be read is named in the error, and with it the app
        except (AnsibleError, OSError) as e:
            result["failed"] = True
            result["msg"] = str(e)
            return result

        for warning in collected.warnings:
            display.warning(warning)

        result.update(changed=False, msg=collected.summary, quadlet_stack=collected.payload)
        return result
