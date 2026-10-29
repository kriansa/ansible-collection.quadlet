"""G. Templating through a real playbook: variable sources, lookups, Jinja features, failures."""

import pytest
from ansible.parsing.vault import VaultLib, VaultSecret

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def config_of(host, app, rel="main/out"):
    return host.read(f"/srv/{app.name}/config/{rel}")


def test_variable_sources_and_precedence(make_app, host):
    app = make_app("vars")
    app.write("quadlets/main.container", main())
    app.write("config/main/out", "{{ play_only }}|{{ overridden }}|{{ derived }}|{{ extra }}\n")

    app.deploy(
        "installed",
        play_vars={"play_only": "p", "overridden": "from-play", "base": "b", "extra": "from-play"},
        task_vars={"overridden": "from-task", "derived": "{{ base }}-x"},
        extra_args=["-e", "extra=from-extra"],
    )

    assert config_of(host, app) == "p|from-task|b-x|from-extra\n"


def test_app_name_variable(make_app, host):
    app = make_app("appname")
    app.write("quadlets/main.container", main("Environment=SELF={{ quadlet_app_name }}--main\n"))
    app.write("config/main/out", "{{ quadlet_app_name }}\n")

    # A variable of the same name is ignored in favour of the real app name
    app.deploy("started", play_vars={"quadlet_app_name": "wrong"})

    assert config_of(host, app) == f"{app.name}\n"
    assert f"SELF={app.name}--main" in host.inspect(app.container("main"))["Config"]["Env"]


def test_lookups(make_app, host, monkeypatch):
    monkeypatch.setenv("QA_LOOKUP_VALUE", "from-env")
    app = make_app("lookups")
    app.write("quadlets/main.container", main())
    (app.playbook_dir / "files").mkdir(parents=True, exist_ok=True)
    (app.playbook_dir / "files" / "note.txt").write_text("from-file\n")
    app.write("config/main/out",
              "E={{ lookup('env', 'QA_LOOKUP_VALUE') }}\n"
              "F={{ lookup('file', 'note.txt') }}\n"
              "P={{ lookup('passwordstore', 'test/secret-1') }}\n"
              "M=\n  {{ lookup('passwordstore', 'test/secret-2 returnall=true') | indent(2) }}\n")

    app.deploy("installed")

    out = config_of(host, app).splitlines()
    assert out[:2] == ["E=from-env", "F=from-file"]
    assert out[2].startswith("P=stub-")
    assert out[4].startswith("  stub-") and out[5].startswith("  user: ")


def test_jinja_features(make_app, host):
    app = make_app("jinja")
    app.write("quadlets/main.container", main())
    app.write("config/main/out",
              "{# a comment that disappears #}\n"
              "{% raw %}{{ kept }}{% endraw %}\n"
              "{{ '{{' }} escaped\n"
              "{% for n in [1, 2] -%}\n"
              "n={{ n }}\n"
              "{% endfor -%}\n"
              "end\n")

    app.deploy("installed")

    # Pinned: Ansible trims the newline after every block tag, comments and {% endraw %}
    # included - so a comment line and a raw line ending in {% endraw %} lose their line break
    assert config_of(host, app) == "{{ kept }}{{ escaped\nn=1\nn=2\nend\n"


def test_facts_gathered_earlier_in_the_play(make_app, host):
    app = make_app("facts")
    app.write("quadlets/main.container",
              main("User=1000:1000\nGroupAdd={{ ansible_facts.getent_group['wheel'][1] }}\n"))

    app.deploy("started", pre_tasks=[
        {"name": "look up the wheel group", "ansible.builtin.getent": {"database": "group", "key": "wheel"}}
    ])

    assert "GroupAdd=10\n" in host.read(app.quadlet_path("main.container"))
    groups = host.container_exec(app.container("main"), "id -G").stdout.split()
    assert "10" in groups


def test_undefined_variable_deploys_nothing(make_app, host):
    app = make_app("undef")
    app.write("quadlets/main.container", main("Environment=X={{ nope }}\n"))

    step = app.deploy("started")

    path = app.directory / "quadlets" / "main.container"
    assert step.failed
    assert step.msg.startswith(f"Failed to process files on control node: Failed to template file {path}: ")
    assert "'nope' is undefined" in step.msg
    assert not host.exists(app.quadlet_path("main.container"))


def test_vault_encrypted_file(make_app, host, tmp_path):
    secret = [("default", VaultSecret(b"vault-password"))]
    app = make_app("vault")
    app.write("quadlets/main.container", main())
    app.write_bytes("config/main/out", VaultLib(secret).encrypt("TOKEN={{ token }}\n"))
    password_file = tmp_path / "vault-password"
    password_file.write_text("vault-password\n")

    app.deploy("installed", play_vars={"token": "abc"},
               extra_args=["--vault-password-file", str(password_file)])

    assert config_of(host, app) == "TOKEN=abc\n"


def test_non_utf8_file(make_app, host):
    """Rejected on the controller, before anything reaches the host."""
    app = make_app("binary")
    app.write("quadlets/main.container", main())
    app.write_bytes("config/main/out", b"a\xffb\n")

    step = app.deploy("installed")

    assert step.failed
    assert step.msg.startswith("Failed to process files on control node: File "), step.msg
    assert step.msg.endswith("/config/main/out is not valid UTF-8: 'utf-8' codec can't decode "
                             "byte 0xff in position 1: invalid start byte"), step.msg
    assert not host.exists(app.quadlet_path("main.container"))
    assert not host.exists(f"/srv/{app.name}")


def test_bare_expression_file(make_app, host):
    """A file that is only `{{ value }}` is written as that value's text, as the template module does."""
    app = make_app("bare")
    app.write("quadlets/main.container", main())
    app.write("config/main/out", "{{ 42 }}")

    step = app.deploy("installed")

    assert not step.failed, step.msg
    assert host.read(f"/srv/{app.name}/config/main/out") == "42"


def test_backslashes_and_crlf_survive_byte_for_byte(make_app, host):
    app = make_app("bytes")
    app.write("quadlets/main.container", main())
    content = "path=C:\\dir\\file\r\nregex=\\d+\\.\\w*\r\ntrailing=\\\n"
    app.write_bytes("config/main/out", content.encode())

    app.deploy("installed")

    assert config_of(host, app) == content
