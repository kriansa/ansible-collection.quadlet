# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""What the collection's action plugins share: the variables they render with, and finding src."""

import os

# pylint: disable=import-error
from ansible.errors import AnsibleFileNotFound  # type: ignore[import-not-found]
from ansible.plugins.action import ActionBase  # type: ignore[import-not-found]

# pylint: enable=import-error


class QuadletAction(ActionBase):  # pylint: disable=abstract-method
    """An action plugin that reads app directories on the control node and renders their files."""

    def _use_task_vars(self, task_vars: dict) -> None:
        """
        Render files with every variable the task sees, its own `vars:` block included.

        The task's own variables are templated here, so one written as "{{ other }}" holds the
        value of `other` by the time a file uses it.
        """
        all_vars = task_vars.copy()
        if hasattr(self._task, "vars") and self._task.vars:
            for var_name, var_value in self._task.vars.items():
                all_vars[var_name] = self._templar.template(var_value)
        self._templar.available_variables = all_vars

    def _resolve_src_path(self, src: str) -> str:
        """Resolve a source path the way Ansible finds files: files/ first, then the playbook's."""
        try:
            return self._find_needle("files", src)
        except AnsibleFileNotFound:
            if not os.path.isabs(src):
                playbook_dir = self._loader.get_basedir()
                return os.path.join(playbook_dir, src)
            return src
