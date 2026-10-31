"""
The quadlet_stack_info action plugin: what it reads of every app, and what it reports.

The plugin runs with Ansible's real loader and templating engine. It never calls a module.
"""

import os
from pathlib import Path

import pytest
from ansible.errors import AnsibleActionFail

from ansible_collections.kriansa.quadlet.plugins.action import quadlet_stack_info as action_plugin
from ansible_collections.kriansa.quadlet.tests.unit._support.action import run_action

ActionModule = action_plugin.ActionModule

MAIN = {"quadlets/main.container": "[Container]\nImage=alpine\n"}


def write(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.fixture
def playbook_dir(tmp_path):
    path = tmp_path / "playbook"
    path.mkdir()
    return path


def collect(playbook_dir, apps, *, src=None, task_vars=None):
    """Write the apps under quadlet-apps/ and run the plugin over them."""
    for name, files in apps.items():
        write(playbook_dir / "quadlet-apps" / name, files)
    args = {} if src is None else {"src": src}
    run = run_action(ActionModule, basedir=playbook_dir, args=args, task_vars=task_vars)
    assert run.module_name is None, "quadlet_stack_info must never run a module"
    return run.result


def stack(playbook_dir, apps, **kwargs):
    result = collect(playbook_dir, apps, **kwargs)
    assert "failed" not in result, result
    return result["quadlet_stack"]


def failure(playbook_dir, apps, **kwargs) -> str:
    result = collect(playbook_dir, apps, **kwargs)
    assert result["failed"] is True, result
    return result["msg"]


def test_report(playbook_dir, fake_host):
    result = collect(
        playbook_dir,
        {
            "web": {
                **MAIN,
                "quadlets/db.container": "[Container]\nNetwork=main.network\n",
                "quadlets/main.network": "[Network]\n",
                "config/main/a.conf": "",
                "application.yml": (
                    'version: "v1"\n'
                    "systemctl_timeout: 900\n"
                    "resource_claims:\n"
                    "  dns:\n"
                    '    - name: "{{ quadlet_app_name }}.{{ domain }}"\n'
                ),
            },
            "ingress": MAIN,
        },
        task_vars={"domain": "example"},
    )
    assert result == {
        "changed": False,
        "msg": "2 apps, 1 claim for 1 role",
        "quadlet_stack": {
            "src": str(playbook_dir / "quadlet-apps"),
            "apps_list": ["ingress", "web"],
            "apps": {
                "ingress": {
                    "version": None,
                    "systemctl_timeout": 300,
                    "main_container": "main",
                    "quadlets": ["main"],
                    "networks": [],
                    "config_dir": {},
                },
                "web": {
                    "version": "v1",
                    "systemctl_timeout": 900,
                    "main_container": "main",
                    "quadlets": ["db", "main"],
                    "networks": ["web--main"],
                    "config_dir": {"main": f"{fake_host.srv_dir}/web/config/main"},
                },
            },
            "claims_by_role": {"dns": [{"app": "web", "claim": {"name": "web.example"}}]},
        },
    }


def test_quadlets_are_named_once(playbook_dir):
    # A pod and a container can share a name
    files = {**MAIN, "quadlets/db.pod": "[Pod]\n", "quadlets/db.container": "[Container]\n"}
    assert stack(playbook_dir, {"web": files})["apps"]["web"]["quadlets"] == ["db", "main"]


class TestDiscovery:
    def test_hidden_directories_and_files_are_not_apps(self, playbook_dir):
        write(playbook_dir / "quadlet-apps", {"README.md": "", ".git/HEAD": ""})
        assert stack(playbook_dir, {"b": MAIN, "a": MAIN})["apps_list"] == ["a", "b"]

    def test_src_elsewhere(self, playbook_dir):
        write(playbook_dir / "elsewhere/one", MAIN)
        result = stack(playbook_dir, {}, src="elsewhere")
        assert result["apps_list"] == ["one"]
        assert result["src"] == str(playbook_dir / "elsewhere")

    def test_src_under_files(self, playbook_dir):
        write(playbook_dir / "files/quadlet-apps/one", MAIN)
        assert stack(playbook_dir, {})["src"] == str(playbook_dir / "files/quadlet-apps")

    def test_src_missing(self, playbook_dir):
        assert failure(playbook_dir, {}) == (
            f"The apps directory was not found on the control node: {playbook_dir / 'quadlet-apps'}"
        )

    def test_no_apps(self, playbook_dir):
        (playbook_dir / "quadlet-apps").mkdir()
        result = collect(playbook_dir, {})
        assert result["msg"] == "0 apps, 0 claims for 0 roles"
        assert result["quadlet_stack"]["apps_list"] == []

    def test_an_app_without_quadlets(self, playbook_dir):
        apps_dir = playbook_dir / "quadlet-apps"
        assert failure(playbook_dir, {"web": MAIN, "docs": {"README.md": ""}}) == (
            f"{apps_dir / 'docs'} is not an app: it has no quadlets/ directory. "
            "Every directory here is deployed as an app."
        )

    def test_an_uppercase_name(self, playbook_dir):
        apps_dir = playbook_dir / "quadlet-apps"
        assert failure(playbook_dir, {"Web": MAIN}) == (
            f"{apps_dir / 'Web'} is not an app: its name must be lowercase, as web"
        )

    @pytest.mark.parametrize("name", ["9web", "web-", "we b"])
    def test_an_invalid_name(self, playbook_dir, name):
        message = failure(playbook_dir, {name: MAIN})
        assert message.startswith(f"{playbook_dir / 'quadlet-apps' / name} is not an app: ")
        assert "Invalid application name" in message

    def test_unknown_options_are_refused(self, playbook_dir):
        write(playbook_dir / "quadlet-apps/web", MAIN)
        # Ansible turns this into a failed task
        with pytest.raises(AnsibleActionFail, match="Invalid options for .*: deploy_only"):
            run_action(ActionModule, basedir=playbook_dir, args={"deploy_only": ["web"]})


class TestEveryAppIsRead:
    def test_one_broken_app_fails_the_whole_stack(self, playbook_dir):
        message = failure(
            playbook_dir,
            {"good": MAIN, "bad": {**MAIN, "application.yml": "version: 2\n"}},
        )
        assert "The version in" in message
        assert "/bad/application.yml must be a string" in message

    def test_a_quadlet_that_does_not_render(self, playbook_dir):
        message = failure(
            playbook_dir, {"web": {"quadlets/main.container": "Image={{ undefined_var }}\n"}}
        )
        assert message.startswith("Failed to template file ")
        assert "'undefined_var' is undefined" in message

    def test_files_that_do_not_shape_the_app_are_not_rendered(self, playbook_dir):
        # Volumes, timers and init/config files say nothing about networks or the main quadlet
        broken = "{{ undefined_var }}"
        result = stack(
            playbook_dir,
            {
                "web": {
                    **MAIN,
                    "quadlets/data.volume": broken,
                    "units/job.timer": broken,
                    "init/main/a.sql": broken,
                    "config/main/b.conf": broken,
                }
            },
        )
        assert result["apps_list"] == ["web"]

    @pytest.mark.parametrize(
        "files, message",
        [
            (
                {"quadlets/db.container": "[Container]\n"},
                (
                    "The app web has no main quadlet: expected one of quadlets/main.container, "
                    "quadlets/main.pod, quadlets/main.kube."
                ),
            ),
            (
                {**MAIN, "quadlets/main.pod": "[Pod]\n"},
                "The app web has more than one main quadlet: main.container, main.pod.",
            ),
            (
                {**MAIN, "application.yml": 'version: "v1"\nmain_container: api\n'},
                "The app web has no main quadlet: expected one of quadlets/api.container, ",
            ),
        ],
    )
    def test_every_main_quadlet_is_checked(self, playbook_dir, files, message):
        assert failure(playbook_dir, {"ok": MAIN, "web": files}).startswith(message)

    def test_a_file_name_another_app_could_deploy(self, playbook_dir):
        # web's x--main.network would be deployed under the name web--x's main.network has
        network = {"quadlets/x--main.network": "[Network]\n"}
        message = failure(playbook_dir, {"web": {**MAIN, **network}, "web--x": MAIN})
        assert message.startswith(
            "The app web cannot deploy quadlets/x--main.network: a file name cannot contain '--'."
        )

    def test_a_template_quadlet(self, playbook_dir):
        message = failure(
            playbook_dir, {"web": {**MAIN, "quadlets/worker@.container": "[Container]\n"}}
        )
        assert message.startswith("The app web cannot deploy quadlets/worker@.container: ")

    def test_an_unreadable_directory(self, playbook_dir, monkeypatch):
        quadlets = write(playbook_dir / "quadlet-apps" / "web", MAIN) / "quadlets"
        real_listdir = os.listdir

        def guarded_listdir(path):
            if str(path) == str(quadlets):
                raise PermissionError(13, "Permission denied", str(path))
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", guarded_listdir)
        assert failure(playbook_dir, {}) == f"[Errno 13] Permission denied: '{quadlets}'"

    def test_both_manifest_names(self, playbook_dir):
        manifest = 'version: "v1"\n'
        message = failure(
            playbook_dir,
            {"web": {**MAIN, "application.yml": manifest, "application.yaml": manifest}},
        )
        assert message.startswith("The app web has two manifests, ")

    def test_warnings_are_shown(self, playbook_dir, monkeypatch):
        warnings = []
        monkeypatch.setattr(action_plugin.display, "warning", warnings.append)
        stack(playbook_dir, {"web": {**MAIN, "application.yml": 'version: "v1"\ntypo: 1\n'}})
        assert len(warnings) == 1
        assert "/web/application.yml: unknown key 'typo' is ignored" in warnings[0]


class TestClaims:
    def test_grouped_by_role_in_app_then_file_order(self, playbook_dir):
        def manifest(claims):
            return {**MAIN, "application.yml": f'version: "v1"\nresource_claims:\n{claims}'}

        result = stack(
            playbook_dir,
            {
                "beta": manifest("  zeta:\n    - b1\n  dns:\n    - b2\n    - b3\n"),
                "alpha": manifest("  dns:\n    - a1\n"),
                "gamma": MAIN,
            },
        )
        claims = result["claims_by_role"]
        assert list(claims) == ["dns", "zeta"]
        assert claims["dns"] == [
            {"app": "alpha", "claim": "a1"},
            {"app": "beta", "claim": "b2"},
            {"app": "beta", "claim": "b3"},
        ]
        assert claims["zeta"] == [{"app": "beta", "claim": "b1"}]

    def test_rendered_once_with_the_tasks_variables(self, playbook_dir):
        manifest = (
            'version: "v1"\n'
            "resource_claims:\n"
            "  ingress:\n"
            '    - upstream: "{{ quadlet_app_name }}--main:{{ port }}"\n'
            "{% for name in extra_names %}\n"
            '    - upstream: "{{ name }}"\n'
            "{% endfor %}\n"
        )
        result = stack(
            playbook_dir,
            {"web": {**MAIN, "application.yml": manifest}},
            task_vars={"port": 8080, "extra_names": ["a", "b"]},
        )
        assert [entry["claim"] for entry in result["claims_by_role"]["ingress"]] == [
            {"upstream": "web--main:8080"},
            {"upstream": "a"},
            {"upstream": "b"},
        ]


class TestNetworks:
    def networks(self, playbook_dir, apps):
        return {name: app["networks"] for name, app in stack(playbook_dir, apps)["apps"].items()}

    def test_the_apps_own_and_another_apps(self, playbook_dir):
        joiner = (
            "[Container]\n"
            "Network=main.network\n"
            "Network=ingress--edge.network:ip=10.0.0.5\n"
            "Network=agent--main\n"
            "Network=host\n"
        )
        networks = self.networks(
            playbook_dir,
            {
                "ingress": {**MAIN, "quadlets/edge.network": "[Network]\n"},
                "web": {"quadlets/main.container": joiner, "quadlets/main.network": "[Network]\n"},
            },
        )
        assert networks == {
            "ingress": [],
            "web": ["web--main", "ingress--edge", "agent--main"],
        }

    def test_network_names_are_honoured_across_the_stack(self, playbook_dir):
        networks = self.networks(
            playbook_dir,
            {
                "ingress": {
                    **MAIN,
                    "quadlets/edge.network": "[Network]\nNetworkName=public\n",
                },
                "web": {
                    "quadlets/main.container": (
                        "[Container]\nNetwork=ingress--edge.network\nNetwork=public\n"
                    ),
                },
            },
        )
        # Two ways of naming the same network
        assert networks["web"] == ["public"]

    def test_a_network_file_no_app_of_the_stack_defines(self, playbook_dir):
        main = "[Container]\nNetwork=shared.network\nNetwork=legacy--main.network\n"
        networks = self.networks(playbook_dir, {"web": {"quadlets/main.container": main}})
        # Guessed from what would name it by default: quadlet for one of the host's own, and
        # quadlet_app for one it deployed outside this stack
        assert networks["web"] == ["systemd-shared", "legacy--main"]

    def test_podmans_default_name(self, playbook_dir):
        main = "[Container]\nNetwork=edge.network\n"
        networks = self.networks(
            playbook_dir,
            {
                "web": {
                    "quadlets/main.container": main,
                    "quadlets/edge.network": "[Network]\nNetworkName=\n",
                }
            },
        )
        assert networks["web"] == ["systemd-web--edge"]

    def test_pods_and_kube_files_join_networks_too(self, playbook_dir):
        networks = self.networks(
            playbook_dir,
            {
                "web": {
                    "quadlets/main.pod": "[Pod]\nNetwork=a\n",
                    "quadlets/job.kube": "[Kube]\nNetwork=b\n",
                    "quadlets/sidecar.container": "[Container]\nPod=main.pod\n",
                },
            },
        )
        assert networks["web"] == ["b", "a"]

    def test_a_templated_network_line(self, playbook_dir):
        main = (
            "[Container]\n"
            "{% if public %}\n"
            "Network={{ edge_network }}\n"
            "{% endif %}\n"
            "{% if not public %}\n"
            "Network=never\n"
            "{% endif %}\n"
        )
        result = stack(
            playbook_dir,
            {"web": {"quadlets/main.container": main}},
            task_vars={"public": True, "edge_network": "edge--main"},
        )
        assert result["apps"]["web"]["networks"] == ["edge--main"]


class TestConfigDir:
    def test_only_the_quadlets_that_have_one(self, playbook_dir, fake_host):
        result = stack(
            playbook_dir,
            {
                "web": {
                    **MAIN,
                    "quadlets/db.container": "[Container]\n",
                    "quadlets/api.pod": "[Pod]\n",
                    "config/main/a.conf": "",
                    "config/api/sub/b.conf": "",
                    # A file directly in config/ belongs to no quadlet
                    "config/README": "",
                },
            },
        )
        # An empty directory of db's would never exist on the host: the module creates them by
        # writing their files
        (playbook_dir / "quadlet-apps/web/config/db/empty").mkdir(parents=True)
        result = stack(playbook_dir, {})
        assert result["apps"]["web"]["config_dir"] == {
            "api": f"{fake_host.srv_dir}/web/config/api",
            "main": f"{fake_host.srv_dir}/web/config/main",
        }

    def test_named_the_way_the_module_names_the_destination(self, playbook_dir, fake_host):
        # The module drops the extension of a directory named like a quadlet file
        result = stack(playbook_dir, {"web": {**MAIN, "config/main.container/a.conf": ""}})
        assert result["apps"]["web"]["config_dir"] == {
            "main": f"{fake_host.srv_dir}/web/config/main"
        }


def test_check_mode_reads_the_same(playbook_dir):
    write(playbook_dir / "quadlet-apps/web", MAIN)
    run = run_action(ActionModule, basedir=playbook_dir, args={}, check_mode=True)
    assert run.result["quadlet_stack"]["apps_list"] == ["web"]
