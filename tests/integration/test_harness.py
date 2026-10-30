"""
The test host itself. If one of these fails, no other integration result can be trusted.
"""

import pytest

pytestmark = pytest.mark.integration


def test_selinux_is_enforcing(host):
    assert host.output("getenforce") == "Enforcing"


def test_user_namespace_range_is_configured(host):
    # What the podman role sets up, and what UserNS=auto allocates from
    assert "containers:1000000:4294836225" in host.read("/etc/subuid").splitlines()
    assert "containers:1000000:4294836225" in host.read("/etc/subgid").splitlines()


def test_images_are_present_offline(host):
    assert (
        host.run("podman image exists docker.io/library/alpine:3.24", check=False).returncode == 0
    )


def test_no_internet(host):
    # Tests must not depend on the network: a missing image has to fail, not be pulled
    result = host.run("curl -sS -m 5 -o /dev/null https://repo.almalinux.org", check=False)
    assert result.returncode != 0


def test_quadlet_generator_is_installed(host):
    assert host.exists("/usr/lib/systemd/system-generators/podman-system-generator")


def test_collection_resolves_to_this_checkout(runner, tmp_path):
    run = runner.run_plays(
        [
            {
                "hosts": "testhost",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": "where",
                        "ansible.builtin.debug": {
                            "msg": "{{ lookup('ansible.builtin.config', 'COLLECTIONS_PATHS') }}"
                        },
                    }
                ],
            }
        ],
        tmp_path,
    )
    assert run.returncode == 0, run.output
    paths = run.task("where").result["msg"]
    assert len(paths) == 1, paths


def test_report_versions(host, record_property):
    """Not an assertion: records what the suite ran against, for the report."""
    record_property("podman", host.output("podman --version"))
    record_property("systemd", host.output("systemctl --version | head -1"))
    record_property("kernel", host.output("uname -r"))
