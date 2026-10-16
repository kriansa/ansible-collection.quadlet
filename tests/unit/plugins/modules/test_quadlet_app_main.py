"""
The module entry point, through a real AnsibleModule: argument handling, check mode, and what
no_log does to the logs and to the result.
"""

import json

import pytest
import yaml
from ansible.module_utils import basic
from ansible.module_utils.testing import patch_module_args

from ansible_collections.kriansa.commons.plugins.modules import quadlet_app
from ansible_collections.kriansa.commons.tests.unit._support.quadlet_app import make_files_data

SECRET = "s3cr3t-value-that-must-not-be-logged"


def call_main(capsys, args):
    """Run main() and return the JSON it printed."""
    with patch_module_args(args):
        with pytest.raises(SystemExit):
            quadlet_app.main()
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def captured_params(monkeypatch):
    """Stop run() from doing anything; record the parameters it was given."""
    seen = {}

    def run(self):
        seen.update(self.module.params)
        self.module.exit_json(changed=False)

    monkeypatch.setattr(quadlet_app.QuadletAppModule, "run", run)
    return seen


@pytest.fixture
def logged(monkeypatch):
    """Capture what AnsibleModule would send to the journal or syslog."""
    messages = []
    monkeypatch.setattr(basic.AnsibleModule, "log", lambda self, msg, log_args=None: messages.append(msg))
    return messages


def test_defaults(capsys, captured_params):
    call_main(capsys, {"src": "/apps/app"})
    assert captured_params == {
        "src": "/apps/app",
        "name": None,
        "state": "installed",
        "force": False,
        "systemctl_timeout": 120,
        "_control_node_processed": False,
        "_files_data": None,
    }


def test_src_is_required(capsys):
    result = call_main(capsys, {})
    assert result["failed"] is True
    assert result["msg"] == "missing required arguments: src"


def test_state_choices(capsys):
    result = call_main(capsys, {"src": "/apps/app", "state": "running"})
    assert result["failed"] is True
    assert result["msg"] == (
        "value of state must be one of: installed, started, restarted, got: running"
    )


def test_check_mode_is_not_supported(capsys, captured_params):
    result = call_main(capsys, {"src": "/apps/app", "_ansible_check_mode": True})
    assert result["skipped"] is True
    assert "does not support check mode" in result["msg"]
    assert captured_params == {}


def test_files_data_is_never_logged(capsys, captured_params, logged):
    files = make_files_data(quadlets={"main.container": f"Environment=PW={SECRET}\n"})
    call_main(capsys, {"src": "/apps/app", "_control_node_processed": True, "_files_data": files})

    invocation = [message for message in logged if message.startswith("Invoked with")]
    assert invocation, logged
    assert SECRET not in invocation[0]
    assert "_files_data=NOT_LOGGING_PARAMETER" in invocation[0]


def test_file_names_are_masked_in_the_result(capsys, fake_host, systemd):
    """
    Pinned bug: no_log hides every string inside _files_data wherever it appears in the output,
    file names included. "main.container" is in every app, so this field is always mangled.
    """
    files = make_files_data(quadlets={"main.container": "[Container]\nImage=alpine\n"},
                            config={"main": {"app.conf": "x=1\n"}})
    result = call_main(
        capsys,
        {"src": "/apps/app", "_control_node_processed": True, "_files_data": files, "state": "installed"},
    )
    assert result["quadlet_files"] == ["app--********"]
    # The config directory is named after the container, so "main" is hidden too
    assert result["service_name"] == "app--********.service"


def test_service_name_is_readable_without_a_main_config_dir(capsys, fake_host, systemd):
    files = make_files_data(quadlets={"main.container": "[Container]\nImage=alpine\n"})
    result = call_main(
        capsys,
        {"src": "/apps/app", "_control_node_processed": True, "_files_data": files, "state": "installed"},
    )
    assert result["service_name"] == "app--main.service"


def test_documentation_matches_the_argument_spec(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, argument_spec, supports_check_mode):
            seen["spec"] = argument_spec
            seen["check_mode"] = supports_check_mode
            raise SystemExit(0)

    monkeypatch.setattr(quadlet_app, "AnsibleModule", Recorder)
    with pytest.raises(SystemExit):
        quadlet_app.main()

    documented = yaml.safe_load(quadlet_app.DOCUMENTATION)["options"]
    public = {name: spec for name, spec in seen["spec"].items() if not name.startswith("_")}
    assert documented.keys() == public.keys()
    for name, spec in public.items():
        doc = documented[name]
        assert doc["type"] == spec["type"], name
        assert doc.get("required", False) == spec.get("required", False), name
        assert doc.get("default") == spec.get("default"), name
        assert doc.get("choices") == spec.get("choices"), name
    assert seen["check_mode"] is False


def test_examples_and_return_are_valid_yaml():
    assert isinstance(yaml.safe_load(quadlet_app.EXAMPLES), list)
    assert set(yaml.safe_load(quadlet_app.RETURN)) == {
        "changed", "application_name", "service_name", "quadlet_files", "unit_files", "msg"
    }
