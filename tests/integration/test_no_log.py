"""
H. Secrets in app files must not reach the managed host's logs.

Rendered files carry resolved secrets, and the module receives them as a parameter. Without
no_log, Ansible would write that parameter into the host's journal on every run.
"""

import uuid

import pytest

from ansible_collections.kriansa.commons.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration


def main(extra: str = "") -> str:
    return f"[Container]\n{IMAGE}\n{IDLE}\n{extra}"


def secret() -> str:
    return f"QA_SECRET_{uuid.uuid4().hex}"


def test_rendered_files_never_reach_the_journal(make_app, host):
    value = secret()
    app = make_app("nolog")
    app.write("quadlets/main.container", main())
    app.write("config/main/secret.env", f"TOKEN={value}\n")

    step = app.deploy("installed")

    journal = host.journal(step.since)
    # Positive control: the invocation was logged, so its absence below means something
    assert "Invoked with" in journal
    assert "_contents=NOT_LOGGING_PARAMETER" in journal
    assert value not in journal
    # In the result, the contents are hidden and the names are not
    module_args = step.result.result["invocation"]["module_args"]
    assert module_args["_contents"] == ["VALUE_SPECIFIED_IN_NO_LOG_PARAMETER"] * 2
    assert {"kind": "config", "container": "main", "path": "secret.env"} in module_args["_files"]
    assert value not in step.run.output


def test_secret_in_a_container_environment(make_app, host):
    """Characterises whether a secret in Environment= shows up anywhere in the journal."""
    value = secret()
    app = make_app("envsecret")
    app.write("quadlets/main.container", main(f"Environment=TOKEN={value}\n"))

    step = app.deploy("started")

    assert host.active_state(app.service("main")) == "active"
    assert value not in host.journal(step.since)


def test_failed_deploy_does_not_leak(make_app, host):
    value = secret()
    app = make_app("nologfail")
    app.write("quadlets/main.container", main("BogusKey=1\n"))
    app.write("config/main/secret.env", f"TOKEN={value}\n")

    step = app.deploy("started")

    assert step.failed
    assert value not in step.run.output
    assert value not in host.journal(step.since)


def test_file_names_are_readable_in_the_result(make_app, host):
    """
    no_log masks every string of the parameter it protects, wherever it appears. File names are
    kept out of it, so "main.container" and "main" (a config directory) still read as written.
    """
    app = make_app("masking")
    app.write("quadlets/main.container", main())
    app.write("config/main/app.conf", "x=1\n")

    result = app.deploy("installed").result.result

    assert result["quadlet_files"] == [f"{app.name}--main.container"]
    assert result["service_name"] == f"{app.name}--main.service"


def test_service_name_is_readable_without_a_main_config_directory(make_app, host):
    app = make_app("nomask")
    app.write("quadlets/main.container", main())

    result = app.deploy("installed").result.result

    assert result["service_name"] == f"{app.name}--main.service"
