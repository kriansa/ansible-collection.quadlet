"""
The quadlet_app action plugin: what it reads on the controller, how it templates it, and what it
sends to the module.

The plugin runs with Ansible's real loader and templating engine; only the module call is
captured instead of executed.
"""

from pathlib import Path

import pytest
from ansible.errors import AnsibleActionFail
from ansible.parsing.vault import VaultLib, VaultSecret
from ansible.template import trust_as_template

from ansible_collections.kriansa.quadlet.plugins.action import quadlet_app as action_plugin
from ansible_collections.kriansa.quadlet.tests.unit._support.action import make_action, run_action
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    files_data_to_tree,
    from_module_args,
)

ActionModule = action_plugin.ActionModule


def write(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_bytes(content.encode("utf-8"))
    return root


@pytest.fixture
def playbook_dir(tmp_path):
    return tmp_path / "playbook"


def deploy(playbook_dir, files, *, src="myapp", task_vars=None, **kwargs):
    """Write an app under the playbook directory and run the plugin over it."""
    write(playbook_dir / "myapp", files)
    return run_action(
        ActionModule,
        basedir=playbook_dir,
        args={"src": src, **kwargs.pop("args", {})},
        task_vars=task_vars,
        **kwargs,
    )


def rendered(run):
    assert run.module_args is not None, run.result
    return files_data_to_tree(from_module_args(run.module_args))


MAIN = {"quadlets/main.container": "[Container]\nImage=alpine\n"}


class TestRun:
    def test_module_call(self, playbook_dir):
        run = deploy(playbook_dir, MAIN, args={"state": "started", "systemctl_timeout": 5},
                     task_vars={"inventory_hostname": "h"})
        assert run.module_name == "kriansa.quadlet.quadlet_app"
        assert run.module_args == {
            "src": "myapp",
            "state": "started",
            "systemctl_timeout": 5,
            "_control_node_processed": True,
            # Names are logged; contents are no_log and matched to the names by position
            "_files": [{"kind": "quadlet", "name": "main.container"}],
            "_contents": ["[Container]\nImage=alpine\n"],
        }

    def test_module_gets_the_original_task_vars(self, playbook_dir):
        # Pinned: the task's own vars: block is only used for templating files
        run = deploy(playbook_dir, MAIN, task_vars={"a": 1},
                     task_level_vars={"b": trust_as_template("{{ a }}")})
        assert run.module_task_vars == {"a": 1}

    def test_module_result_is_returned(self, playbook_dir):
        run = deploy(playbook_dir, MAIN, module_result={"changed": True, "msg": "deployed"})
        assert run.result == {"changed": True, "msg": "deployed"}

    def test_without_task_vars(self, playbook_dir):
        write(playbook_dir / "myapp", MAIN)
        action, captured = make_action(ActionModule, basedir=playbook_dir, args={"src": "myapp"})
        action.run(task_vars=None)
        assert from_module_args(captured["module_args"])["quadlets"][0]["name"] == "main.container"

    @pytest.mark.parametrize("args", [{}, {"src": ""}, {"src": None}])
    def test_src_is_required(self, playbook_dir, args):
        run = run_action(ActionModule, basedir=playbook_dir, args=args)
        assert run.result == {"failed": True, "msg": "src parameter is required"}
        assert run.module_args is None

    def test_src_not_found(self, playbook_dir):
        run = run_action(ActionModule, basedir=playbook_dir, args={"src": "nope"})
        assert run.result == {
            "failed": True,
            "msg": f"Source directory not found on control node: {playbook_dir / 'nope'}",
        }

    def test_src_is_a_file(self, playbook_dir):
        write(playbook_dir, {"afile": "x"})
        run = run_action(ActionModule, basedir=playbook_dir, args={"src": "afile"})
        assert run.result == {
            "failed": True,
            "msg": f"Source path is not a directory: {playbook_dir / 'afile'}",
        }

    def test_processing_errors_are_reported(self, playbook_dir):
        run = deploy(playbook_dir, {**MAIN, "quadlets/x.timer": "[Timer]\n"})
        assert run.result == {
            "failed": True,
            "msg": "Failed to process files on control node: "
                   "x.timer is a systemd unit, not a quadlet. Move it to units/.",
        }
        assert run.module_args is None

    def test_check_mode_still_reaches_the_module(self, playbook_dir):
        run = deploy(playbook_dir, MAIN, check_mode=True)
        assert run.module_args is not None

    def test_async_is_rejected(self, playbook_dir):
        write(playbook_dir / "myapp", MAIN)
        action = make_action(ActionModule, basedir=playbook_dir, args={"src": "myapp"}, async_val=10)[0]
        with pytest.raises(AnsibleActionFail, match="does not support async"):
            action.run(task_vars={})


class TestResolveSrc:
    def resolve(self, playbook_dir, src):
        action = make_action(ActionModule, basedir=playbook_dir, args={})[0]
        return action._resolve_src_path(src)

    def test_under_files(self, playbook_dir):
        (playbook_dir / "files/app").mkdir(parents=True)
        assert self.resolve(playbook_dir, "app") == str(playbook_dir / "files/app")

    def test_next_to_the_playbook(self, playbook_dir):
        (playbook_dir / "app").mkdir(parents=True)
        assert self.resolve(playbook_dir, "app") == str(playbook_dir / "app")

    def test_files_takes_precedence(self, playbook_dir):
        (playbook_dir / "files/app").mkdir(parents=True)
        (playbook_dir / "app").mkdir(parents=True)
        assert self.resolve(playbook_dir, "app") == str(playbook_dir / "files/app")

    def test_relative_and_missing(self, playbook_dir):
        playbook_dir.mkdir()
        assert self.resolve(playbook_dir, "apps/x") == str(playbook_dir / "apps/x")

    def test_absolute(self, playbook_dir, tmp_path):
        playbook_dir.mkdir()
        assert self.resolve(playbook_dir, "/abs/missing") == "/abs/missing"
        assert self.resolve(playbook_dir, str(tmp_path)) == str(tmp_path)


class TestName:
    def test_from_src_basename(self, playbook_dir):
        run = deploy(playbook_dir, {"quadlets/main.container": "N={{ quadlet_app_name }}\n"})
        assert rendered(run)["quadlets/main.container"] == "N=myapp\n"

    def test_from_name_argument_lowercased(self, playbook_dir):
        run = deploy(playbook_dir, {"quadlets/main.container": "N={{ quadlet_app_name }}\n"},
                     args={"name": "Prod-DB"})
        assert rendered(run)["quadlets/main.container"] == "N=prod-db\n"

    def test_from_src_with_a_trailing_slash(self, playbook_dir):
        run = deploy(playbook_dir, {"quadlets/main.container": "N={{ quadlet_app_name }}\n"},
                     src="myapp/")
        assert rendered(run)["quadlets/main.container"] == "N=myapp\n"

    def test_name_beats_a_variable_of_the_same_name(self, playbook_dir):
        run = deploy(playbook_dir, {"quadlets/main.container": "N={{ quadlet_app_name }}\n"},
                     task_vars={"quadlet_app_name": "other"})
        assert rendered(run)["quadlets/main.container"] == "N=myapp\n"


class TestDiscovery:
    def test_absent_directories(self, playbook_dir):
        run = deploy(playbook_dir, MAIN)
        assert from_module_args(run.module_args) == {
            "quadlets": [{"name": "main.container", "content": "[Container]\nImage=alpine\n"}],
            "units": [], "init": [], "config": [],
        }

    def test_quadlets_sorted_and_strays_skipped(self, playbook_dir):
        files = {f"quadlets/{name}": "" for name in
                 ["main.container", "a.network", "b.volume", "c.pod", "d.kube", "README.md"]}
        files["quadlets/sub.container/x"] = ""
        run = deploy(playbook_dir, files)
        assert [f["name"] for f in from_module_args(run.module_args)["quadlets"]] == [
            "a.network", "b.volume", "c.pod", "d.kube", "main.container"
        ]

    def test_units_sorted(self, playbook_dir):
        run = deploy(playbook_dir, {**MAIN, "units/b.timer": "", "units/a.timer": ""})
        assert [f["name"] for f in from_module_args(run.module_args)["units"]] == ["a.timer", "b.timer"]

    def test_stray_unit_is_rejected_with_the_modules_message(self, playbook_dir):
        run = deploy(playbook_dir, {**MAIN, "units/x.service": ""})
        assert run.result["msg"] == (
            "Failed to process files on control node: "
            "Unsupported file in units/: x.service. Only .timer files are supported."
        )

    @pytest.mark.parametrize("kind", ["init", "config"])
    def test_nested_files(self, playbook_dir, kind):
        run = deploy(playbook_dir, {**MAIN, f"{kind}/README": "top level, ignored",
                                    f"{kind}/main/a": "1", f"{kind}/main/sub/b": "2",
                                    f"{kind}/main.container/c": "3"})
        entries = from_module_args(run.module_args)[kind]
        assert sorted((e["container"], e["path"], e["content"]) for e in entries) == [
            ("main", "a", "1"), ("main", "sub/b", "2"),
            # Raw directory name: the module strips the suffix later
            ("main.container", "c", "3"),
        ]


class TestTemplating:
    def render_one(self, playbook_dir, content, **kwargs):
        run = deploy(playbook_dir, {"quadlets/main.container": content}, **kwargs)
        return rendered(run)["quadlets/main.container"]

    def test_variables(self, playbook_dir):
        assert self.render_one(playbook_dir, "Image=x:{{ v }}\n", task_vars={"v": "1.2"}) == "Image=x:1.2\n"

    def test_task_level_vars_win_and_are_templated(self, playbook_dir):
        out = self.render_one(
            playbook_dir, "G={{ greeting }}\n",
            task_vars={"who": "bob", "greeting": "overridden"},
            task_level_vars={"greeting": trust_as_template("{{ who }} says hi")},
        )
        assert out == "G=bob says hi\n"

    def test_untrusted_task_level_vars_are_not_templated(self, playbook_dir):
        # Values written in a playbook are trusted; a plain string built in Python is not
        out = self.render_one(playbook_dir, "G={{ g }}\n", task_vars={"n": 1},
                              task_level_vars={"g": "{{ n }}"})
        assert out == "G={{ n }}\n"

    def test_raw_comments_and_escaped_braces(self, playbook_dir):
        out = self.render_one(playbook_dir, "{% raw %}{{ x }}{% endraw %}|{{ '{{' }}|{# gone #}end\n")
        assert out == "{{ x }}|{{|end\n"

    def test_block_tags_leave_no_blank_lines(self, playbook_dir):
        assert self.render_one(playbook_dir, "{% if true %}\nX\n{% endif %}\nY\n") == "X\nY\n"

    @pytest.mark.parametrize("content", ["a", "a\n", "a\n\n"])
    def test_trailing_newlines_are_preserved(self, playbook_dir, content):
        assert self.render_one(playbook_dir, content) == content

    def test_backslashes_and_crlf_are_preserved(self, playbook_dir):
        assert self.render_one(playbook_dir, "a=b\\c\r\nd\r\n") == "a=b\\c\r\nd\r\n"

    def test_undefined_variable(self, playbook_dir):
        run = deploy(playbook_dir, {"quadlets/main.container": "{{ nope }}"})
        path = playbook_dir / "myapp/quadlets/main.container"
        assert run.result["failed"] is True
        assert run.result["msg"].startswith(
            f"Failed to process files on control node: Failed to template file {path}: "
        )
        assert "'nope' is undefined" in run.result["msg"]

    def test_lookups(self, playbook_dir, monkeypatch):
        monkeypatch.setenv("QA_LOOKUP", "from-env")
        write(playbook_dir, {"files/note.txt": "from-file\n"})
        out = self.render_one(
            playbook_dir,
            "E={{ lookup('env', 'QA_LOOKUP') }}\n"
            "F={{ lookup('file', 'note.txt') }}\n"
            "P={{ lookup('passwordstore', 'test/secret-1') }}\n",
        )
        assert out.startswith("E=from-env\nF=from-file\nP=stub-")

    def test_vault_encrypted_file(self, tmp_path):
        secret = [("default", VaultSecret(b"vault-password"))]
        playbook_dir = tmp_path / "pb"
        write(playbook_dir / "myapp", {
            **MAIN,
            "config/main/secret.env": VaultLib(secret).encrypt("TOKEN={{ v }}\n"),
        })
        action, captured = make_action(ActionModule, basedir=playbook_dir, args={"src": "myapp"},
                                       task_vars={"v": "abc"})
        action._loader.set_vault_secrets(secret)
        action.run(task_vars={"v": "abc"})
        assert from_module_args(captured["module_args"])["config"] == [
            {"container": "main", "path": "secret.env", "content": "TOKEN=abc\n"}
        ]

    def test_invalid_utf8_is_rejected(self, playbook_dir):
        # ansible-core reads it anyway, with the bad bytes escaped, and only warns
        run = deploy(playbook_dir, {"quadlets/main.container": b"[Container]\na\xffb\n"})
        path = playbook_dir / "myapp/quadlets/main.container"
        assert run.result["failed"] is True
        assert run.result["msg"] == (
            f"Failed to process files on control node: File {path} is not valid UTF-8: "
            "'utf-8' codec can't decode byte 0xff in position 13: invalid start byte"
        )
        assert run.module_args is None

    @pytest.mark.parametrize("content, text", [
        ("{{ n }}", "42"),
        # Lost, as with Ansible's template module: the file renders to the number alone
        ("{{ n }}\n", "42"),
        ("{{ l }}", "[1, 2]"),
        ("{{ none }}", ""),
    ])
    def test_a_bare_expression_is_written_as_text(self, playbook_dir, content, text):
        # A file that is one expression renders to its value, which is turned into text the way
        # Ansible's template module does it
        out = self.render_one(playbook_dir, content, task_vars={"n": 42, "l": [1, 2], "none": None})
        assert out == text


def test_app_without_quadlets_directory_is_sent_anyway(playbook_dir):
    # Pinned: the plugin requires nothing; the module then reports "already up to date"
    run = deploy(playbook_dir, {"config/main/a": "x"})
    assert from_module_args(run.module_args)["quadlets"] == []


def test_subdirectories_in_units_are_skipped(playbook_dir):
    run = deploy(playbook_dir, {**MAIN, "units/a.timer": "", "units/nested/b.timer": ""})
    assert [f["name"] for f in from_module_args(run.module_args)["units"]] == ["a.timer"]


def test_decode_error_message(playbook_dir, monkeypatch):
    """The plugin's own message for a decode error, should the loader ever raise one again."""
    write(playbook_dir / "myapp", MAIN)
    action = make_action(ActionModule, basedir=playbook_dir, args={"src": "myapp"})[0]

    def raise_decode_error(path):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(action._loader, "get_text_file_contents", raise_decode_error)
    result = action.run(task_vars={})
    path = playbook_dir / "myapp/quadlets/main.container"
    assert result["msg"] == (
        "Failed to process files on control node: File "
        f"{path} is not valid UTF-8: 'utf-8' codec can't decode byte 0xff in position 0: "
        "invalid start byte"
    )
