# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""The error the quadlet_app module reports to the user as a failed task."""


class QuadletAppError(Exception):
    """
    A failure of the task, with the message the user sees.

    The objects doing the work raise this rather than ending the module themselves, so only the
    module's entry point talks to Ansible. Anything passed besides the message - the command that
    failed, its exit code and its output - is reported alongside it.
    """

    def __init__(self, msg: str, **details):
        super().__init__(msg)
        self.msg = msg
        self.details = details
