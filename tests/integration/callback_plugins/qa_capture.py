# Copyright (c) 2026 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Record every task result of a playbook run as JSON lines, for the integration tests to read.

The normal stdout callback keeps printing as usual, so a run's log stays readable. Results go
through Ansible's own result serialisation, so no_log censoring applies exactly as it would in
any other output.
"""

import json
import os

# pylint: disable=import-error
from ansible.plugins.callback import CallbackBase  # type: ignore[import-not-found]

# pylint: enable=import-error

DOCUMENTATION = r"""
name: qa_capture
type: notification
short_description: Writes task results as JSON lines to $QA_CAPTURE_FILE
description:
  - Test harness callback. Each line holds the task name, action, host, status and result.
"""


class CallbackModule(CallbackBase):
    """Appends one JSON line per task result."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "qa_capture"
    CALLBACK_NEEDS_ENABLED = True

    def _record(self, result, status):
        path = os.environ.get("QA_CAPTURE_FILE")
        if not path:
            return
        entry = {
            "task": result.task_name,
            "action": result._task.action,  # pylint: disable=protected-access
            "host": result._host.get_name(),  # pylint: disable=protected-access
            "status": status,
            # keep_invocation: the module arguments, with no_log ones censored, are part of what the
            # tests check
            "result": json.loads(
                self._dump_results(result._result, indent=0, keep_invocation=True)  # pylint: disable=protected-access
            ),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def v2_runner_on_ok(self, result):
        self._record(result, "ok")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._record(result, "failed")

    def v2_runner_on_skipped(self, result):
        self._record(result, "skipped")

    def v2_runner_on_unreachable(self, result):
        self._record(result, "unreachable")
