# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

# pylint: disable=import-error
from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-not-found]

from ansible_collections.kriansa.quadlet.plugins.module_utils.app_module import (  # type: ignore[import-not-found]
    QuadletAppModule,
)

# pylint: enable=import-error

DOCUMENTATION = r"""
---
module: quadlet_app
short_description: Deploy Podman Quadlet applications with automatic templating and preprocessing
version_added: "1.0.0"
description:
    - Deploys Podman Quadlet applications with automatic resource prefixing
    - Templates all files with Jinja2 variables (handled by Ansible)
    - Preprocesses Quadlet files to add application name prefixes
    - Deploys plain systemd units from units/, currently .timer files, and enables them
    - Manages init and config directories
    - Handles systemd service lifecycle (daemon-reload, start, enable)
    - Ensures idempotent deployments (only updates when files change)

options:
    src:
        description:
            - Path to the application directory containing quadlets/, units/, init/, config/
            - Can be relative to playbook directory or absolute path
            - Must contain quadlets/main.container (required)
        required: true
        type: path
    name:
        description:
            - Application name used for prefixing files and services
            - If not provided, extracted from basename of src
            - Must start with a letter (a-z)
            - Can end with a letter or number (a-z, 0-9)
            - Can contain lowercase letters, numbers, hyphens, underscores
            - Automatically converted to lowercase
        required: false
        type: str
    state:
        description:
            - Desired deployment state
            - C(installed) - Deploy files and reload systemd (no service management)
            - C(started) - Deploy files, reload systemd, start main service
            - C(restarted) - Deploy files, reload systemd, restart main service
        required: false
        type: str
        choices: ['installed', 'started', 'restarted']
        default: installed
    force:
        description:
            - Force overwrite files even if content hasn't changed
            - When false, uses checksum comparison for idempotency
        required: false
        type: bool
        default: false
    systemctl_timeout:
        description:
            - Timeout in seconds for systemctl commands (start, stop, restart, daemon-reload)
            - Increase this for services that take a long time to start (e.g., large image pulls)
        required: false
        type: int
        default: 120
    settle_time:
        description:
            - Seconds the services this run started have to stay up before the task succeeds
            - Podman reports a container as started as soon as it starts, so without this check a
              container that exits right away, or keeps crashing, would still pass
            - A service that stops, or that systemd has to start again, fails the task with its
              last log lines
            - Services that run once and finish (C(Type=oneshot)) are not watched
            - Set to 0 to turn the check off
        required: false
        type: int
        default: 5

author:
    - Daniel Pereira (@kriansa)

requirements:
    - python >= 3.12
    - podman >= 4.4

notes:
    - This module requires root privileges or appropriate systemd user permissions
    - Quadlet files must be in quadlets/ subdirectory
    - At least quadlets/main.container must exist
    - init and config subdirectories must end with .container or .pod
    - All files are automatically templated by Ansible before module receives them
    - Named volumes are mounted idmapped and chowned once by a generated bootstrap unit, rather
      than being recursively chowned to container root on every mount
    - A .volume may declare X-Owner=uid:gid to set that ownership in container-relative terms. It
      defaults to 0:0, which is correct for any image running as root; an image that drops
      privileges internally has to declare the uid it drops to.
    - Containers default to UserNS=auto:size=65536, except those joining a pod, which share the
      pod's. A container that reads the host journal or other processes' /proc entries must
      declare UserNS=host, or it will half-work - those files read as nobody while world-readable
      ones keep succeeding.
    - A .timer is not a Quadlet type. It belongs in units/, is deployed to /etc/systemd/system,
      and is the one unit this module enables - nothing generates it, so nothing else would apply
      its [Install] section.
    - Timers are enabled and started on every state, including installed, and one whose file
      changed is restarted so a new schedule takes effect immediately.
"""

EXAMPLES = r"""
# Deploy application with default settings
- name: Deploy simple web app
  kriansa.quadlet.quadlet_app:
    src: podman-apps/nginx
    state: installed

# Deploy and start application with custom name
- name: Deploy backend application
  kriansa.quadlet.quadlet_app:
    src: podman-apps/backend
    name: prod-backend
    state: started

# Deploy with variables for templating
- name: Deploy database
  kriansa.quadlet.quadlet_app:
    src: podman-apps/postgres
    state: started
  vars:
    db_version: "16"
    db_password: "{{ vault_db_password }}"

# Force restart application
- name: Update and restart application
  kriansa.quadlet.quadlet_app:
    src: podman-apps/webapp
    state: restarted
    force: true

# Deploy service with extended timeout for slow image pulls
- name: Deploy application with custom timeout
  kriansa.quadlet.quadlet_app:
    src: podman-apps/unifi-network
    state: started
    systemctl_timeout: 300

# An app with a scheduled job. quadlets/backup.container runs it, units/backup.timer schedules it,
# and the timer is enabled and started regardless of the state requested here.
- name: Deploy application with a nightly backup
  kriansa.quadlet.quadlet_app:
    src: podman-apps/postgres
    state: started
"""

RETURN = r"""
changed:
    description: Whether any changes were made
    type: bool
    returned: always
    sample: true
application_name:
    description: The application name used for deployment
    type: str
    returned: always
    sample: "my-backend-app"
service_name:
    description: The main systemd service name
    type: str
    returned: always
    sample: "my-backend-app--main.service"
quadlet_files:
    description: List of quadlet files deployed to /etc/containers/systemd
    type: list
    returned: always
    sample: ["my-app--main.container", "my-app--db.container", "my-app--data.volume"]
unit_files:
    description: List of plain systemd units deployed to /etc/systemd/system
    type: list
    returned: always
    sample: ["my-app--backup.timer"]
msg:
    description: Human-readable message about the operation
    type: str
    returned: always
    sample: "quadlet files deployed and service started"
"""


def main():
    """Ansible module entry point."""
    module = AnsibleModule(
        argument_spec={
            "src": {"type": "path", "required": True},
            "name": {"type": "str", "required": False},
            "state": {
                "type": "str",
                "default": "installed",
                "choices": ["installed", "started", "restarted"],
            },
            "force": {"type": "bool", "default": False},
            "systemctl_timeout": {"type": "int", "default": 120},
            "settle_time": {"type": "int", "default": 5},
            # Internal parameters used by action plugin
            "_control_node_processed": {"type": "bool", "default": False, "required": False},
            # Which files the app has: kind, and name or container directory and path. Logged.
            "_files": {"type": "list", "elements": "dict", "required": False},
            # The rendered content of each file in _files, at the same position, secrets included.
            # Without no_log, Ansible writes it into the managed host's journal on every run. Kept
            # apart from the names because no_log masks every one of its strings in all output.
            "_contents": {"type": "list", "elements": "str", "required": False, "no_log": True},
        },
        supports_check_mode=False,
    )

    # Run the module
    app_module = QuadletAppModule(module)
    app_module.run()


if __name__ == "__main__":
    main()
