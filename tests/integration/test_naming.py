"""K. The app name: where it comes from, how it is normalised, which names are refused."""

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import IDLE, IMAGE

pytestmark = pytest.mark.integration

MAIN = f"[Container]\n{IMAGE}\n{IDLE}\n"


def test_name_argument_is_lowercased(make_app, host):
    app = make_app("custom")
    app.write("quadlets/main.container", MAIN)
    custom = f"{app.name}-X".upper()
    try:
        step = app.deploy("started", args={"name": custom})
        lowered = custom.lower()
        assert step.result.result["application_name"] == lowered
        assert host.active_state(f"{lowered}--main.service") == "active"
        assert host.exists(f"/etc/containers/systemd/{lowered}--main.container")
    finally:
        from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import remove_from_host
        remove_from_host(host, custom.lower())


@pytest.mark.parametrize("name", ["1bad", "bad-", "Bad_Name-"])
def test_invalid_names_deploy_nothing(make_app, host, name):
    app = make_app("badname")
    app.write("quadlets/main.container", MAIN)

    step = app.deploy("started", args={"name": name})

    assert step.failed
    assert step.msg.startswith(f"Invalid application name: {name.lower()}.")
    assert host.listdir("/etc/containers/systemd") == [
        entry for entry in host.listdir("/etc/containers/systemd") if not entry.startswith(name.lower())
    ]


def test_directory_name_is_lowercased(make_app, host):
    app = make_app("dirname")
    app.write("quadlets/main.container", MAIN)
    upper = app.directory.with_name(app.name.upper())
    app.directory.rename(upper)

    step = app.deploy("installed", src=f"apps/{upper.name}")

    assert step.result.result["application_name"] == app.name
    assert host.exists(app.quadlet_path("main.container"))


def test_trailing_slash_without_name(make_app, host):
    app = make_app("slash")
    app.write("quadlets/main.container", MAIN + "Environment=APP={{ quadlet_app_name }}\n")

    step = app.deploy("installed", src=f"apps/{app.name}/")

    assert not step.failed, step.msg
    assert step.result.result["application_name"] == app.name
    assert f"Environment=APP={app.name}\n" in host.read(app.quadlet_path("main.container"))
