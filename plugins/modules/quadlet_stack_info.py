# Copyright (c) 2025 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

# Documentation only. quadlet_stack_info is an action plugin that runs on the control node alone
# (plugins/action/quadlet_stack_info.py); nothing of it runs on the managed host.

DOCUMENTATION = r"""
---
module: quadlet_stack_info
short_description: Read every app of a host, their manifests and the resource claims they make
version_added: "1.0.0"
description:
    - Reads every app under O(src) on the control node - one directory per app, laid out as
      M(kriansa.quadlet.quadlet_app) expects - and reports what they make up together.
    - Each app's manifest, C(application.yml) or C(application.yaml), is rendered with the task's
      variables and read. An app without one gets the defaults and claims nothing.
    - Each app's containers, pods, kube files and networks are rendered too, to report the
      networks the app joins and to check its main quadlet exists.
    - Every app is read, always. One that cannot be read fails the task, whichever apps are about
      to be deployed.
    - The M(kriansa.quadlet.quadlet_stack) role runs this first, and hands what it returns to the
      providers that satisfy the claims.
options:
    src:
        description:
            - The directory holding one directory per app.
            - Relative to the playbook's directory, or found under its C(files/), as
              M(kriansa.quadlet.quadlet_app) finds O(kriansa.quadlet.quadlet_app#module:src).
            - Every directory in it that does not start with C(.) is an app, and must have a
              C(quadlets/) directory.
        type: path
        default: quadlet-apps
attributes:
    check_mode:
        support: full
        description: It only reads files on the control node.
author:
    - Daniel Pereira (@kriansa)
notes:
    - This runs on the control node only, and never connects to the managed host.
    - "Template manifest values as C({{ var | to_json }}), unquoted. The file is read as YAML after
      rendering, so a value written plainly is read as whatever YAML makes of it, and one put in
      quotes loses its backslashes, or becomes text where a number was meant."
"""

EXAMPLES = r"""
- name: Read the apps
  kriansa.quadlet.quadlet_stack_info:
    src: quadlet-apps
  register: stack

- name: Show the networks each app joins
  ansible.builtin.debug:
    msg: "{{ item.key }}: {{ item.value.networks | join(', ') }}"
  loop: "{{ stack.quadlet_stack.apps | dict2items }}"
"""

RETURN = r"""
quadlet_stack:
    description: Everything read, as plain data.
    returned: success
    type: dict
    contains:
        src:
            description: The apps directory, as an absolute path on the control node.
            type: str
            sample: /home/me/playbooks/quadlet-apps
        apps_list:
            description: Every app, by name, sorted.
            type: list
            elements: str
            sample: ["app_one", "consumer_app"]
        apps:
            description: Each app by name, with what its manifest says and what its files show.
            type: dict
            sample:
                app_one:
                    version: v1
                    systemctl_timeout: 300
                    main_container: main
                    quadlets: ["db", "main"]
                    networks: ["app_one--main", "consumer_app--main"]
                    config_dir:
                        main: /srv/app_one/config/main
        claims_by_role:
            description:
                - Every claim of every app, grouped by the role it is addressed to, sorted by
                  role. Within a role, claims come in app order, then in the order the manifest
                  lists them.
            type: dict
            sample:
                example_dns:
                    - app: app_one
                      claim:
                        name: app-one.example
                        value: 10.0.0.1
msg:
    description: How many apps and claims were read.
    returned: always
    type: str
    sample: "3 apps, 2 claims for 1 role"
"""
