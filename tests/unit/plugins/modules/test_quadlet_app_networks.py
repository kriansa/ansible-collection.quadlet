"""
The module's networks-only run: the pass the quadlet_stack role makes over every app, creating its
missing networks before any app is deployed.
"""

from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    make_files_data,
    run_module,
)

NETWORK = "[Network]\nSubnet=10.0.0.0/24\n"


def test_creates_the_networks_and_nothing_else(fake_host, systemd):
    outcome = run_module(make_files_data({"main.network": NETWORK}), networks_only=True)

    assert outcome.exit == {
        "changed": True,
        "application_name": "app",
        "network_files": ["app--main.network"],
        "msg": "networks created",
    }
    assert sorted(fake_host.files()) == ["etc/containers/systemd/app--main.network"]
    assert systemd.state_changes == [["daemon-reload"], ["start", "app--main-network.service"]]


def test_already_present(fake_host, systemd):
    files = make_files_data({"main.network": NETWORK})
    run_module(files, networks_only=True)
    systemd.calls.clear()

    outcome = run_module(files, networks_only=True)

    assert outcome.exit["changed"] is False
    assert outcome.msg == "networks already present"
    assert systemd.state_changes == []


def test_the_main_quadlet_is_not_required(fake_host, systemd):
    # The action plugin sends only the networks in this pass
    outcome = run_module(make_files_data(), networks_only=True)
    assert outcome.exit["changed"] is False
    assert outcome.exit["network_files"] == []


def test_any_other_file_sent_is_left_alone(fake_host, systemd):
    files = make_files_data(
        {"main.container": "[Container]\nImage=alpine\n", "main.network": NETWORK},
        units={"job.timer": "[Timer]\nOnCalendar=daily\n"},
        config={"main": {"a.conf": "a"}},
    )
    run_module(files, networks_only=True, state="started")
    assert sorted(fake_host.files()) == ["etc/containers/systemd/app--main.network"]
    assert systemd.state_changes == [["daemon-reload"], ["start", "app--main-network.service"]]


def test_uses_the_apps_timeout(fake_host, systemd):
    run_module(make_files_data({"main.network": NETWORK}), networks_only=True, systemctl_timeout=7)
    assert {call.timeout for call in systemd.calls if call.systemctl} == {7, 10}
