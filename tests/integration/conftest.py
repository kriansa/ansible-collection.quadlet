"""
Fixtures for the integration tests: one AlmaLinux VM per session (per xdist worker), and apps
deployed to it with real playbooks.

Run with `pytest -m integration`. The first run downloads the base image and provisions the test
image, which takes a few minutes; later sessions boot a throwaway copy in seconds.

Options:
  --keep-vm           leave the VM running after the session and print how to reach it
  --vm-attach=PORT    use a VM already listening for ssh on PORT instead of booting one
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ansible_collections.kriansa.quadlet.tests.integration.harness.apps import (
    App,
    leftover_apps,
    new_app_name,
    remove_from_host,
)
from ansible_collections.kriansa.quadlet.tests.integration.harness.host import Host
from ansible_collections.kriansa.quadlet.tests.integration.harness.image import CACHE_DIR
from ansible_collections.kriansa.quadlet.tests.integration.harness.playbook import (
    PlaybookRunner,
    write_inventory,
)
from ansible_collections.kriansa.quadlet.tests.integration.harness.provision import (
    create_overlay,
    ensure_test_image,
)
from ansible_collections.kriansa.quadlet.tests.integration.harness.qemu import Vm
from ansible_collections.kriansa.quadlet.tests.integration.harness.seed import ensure_ssh_key

REQUIRED_TOOLS = ("qemu-system-x86_64", "qemu-img", "xorriso", "ssh", "ssh-keygen")


def pytest_addoption(parser):
    parser.addoption("--keep-vm", action="store_true", default=False,
                     help="leave the test VM running after the session")
    parser.addoption("--vm-attach", type=int, default=None, metavar="PORT",
                     help="use a running test VM listening for ssh on this localhost port")


def missing_prerequisites() -> list:
    problems = [f"{tool} is not installed" for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        problems.append("/dev/kvm is not usable")
    if os.environ.get("QA_SKIP_INTEGRATION"):
        problems.append("QA_SKIP_INTEGRATION is set")
    return problems


def pytest_collection_modifyitems(config, items):
    problems = missing_prerequisites()
    if not problems and not config.getoption("--vm-attach"):
        return
    if problems and not config.getoption("--vm-attach"):
        skip = pytest.mark.skip(reason="integration tests need: " + "; ".join(problems))
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def vm(request, tmp_path_factory):
    private_key = ensure_ssh_key(CACHE_DIR / "ssh")[0]
    attach = request.config.getoption("--vm-attach")
    if attach:
        yield Vm(disk=Path("/dev/null"), workdir=tmp_path_factory.mktemp("vm"), private_key=private_key,
                 port=attach)
        return

    keep = request.config.getoption("--keep-vm")
    workdir = tmp_path_factory.mktemp("vm")
    disk = create_overlay(ensure_test_image(), workdir / "disk.qcow2")
    machine = Vm(disk=disk, workdir=workdir, private_key=private_key, outlive_parent=keep).start()
    try:
        machine.wait_for_ssh()
    except Exception:
        machine.kill()
        raise
    yield machine
    if keep:
        print(f"\nThe test VM is still running: {' '.join(machine.ssh_argv())}\n"
              f"Attach to it with --vm-attach={machine.port}")
    else:
        machine.kill()


@pytest.fixture(scope="session")
def host(vm) -> Host:
    test_host = Host(vm)
    # Every SELinux-related assertion would be meaningless otherwise
    assert test_host.output("getenforce") == "Enforcing"
    return test_host


@pytest.fixture(scope="session")
def runner(vm, tmp_path_factory) -> PlaybookRunner:
    workdir = tmp_path_factory.mktemp("ansible")
    return PlaybookRunner(write_inventory(workdir / "inventory", vm.port), vm.private_key, workdir)


@pytest.fixture
def clean_host(host):
    """Remove whatever a crashed earlier test left behind, and make sure it worked."""
    for name in leftover_apps(host):
        remove_from_host(host, name)
    assert leftover_apps(host) == [], "the VM could not be cleaned up"


@pytest.fixture
def make_app(request, host, runner, clean_host, tmp_path):
    """
    Factory for apps under test: make_app("tag") returns a new App with a unique name.

    Every app it made is removed from the VM when the test ends, unless QA_KEEP_APPS is set.
    """
    del clean_host
    playbook_dir = tmp_path / "playbook"
    made = []
    since = host.cursor()

    def factory(tag: str = "app") -> App:
        app = App(new_app_name(tag), playbook_dir, host, runner)
        made.append(app)
        return app

    yield factory

    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        for app in made:
            request.node.add_report_section("call", f"state of {app.name}", diagnose(host, app, since))
    if not os.environ.get("QA_KEEP_APPS"):
        for app in made:
            remove_from_host(host, app.name)


def diagnose(host: Host, app: App, since: str) -> str:
    """What a failing test's app looks like on the VM, for the failure report."""
    sections = {
        "units": host.output(f"systemctl list-units --all --no-pager '{app.name}--*'", check=False),
        "deployed files": host.output(
            f"ls -la /etc/containers/systemd/ /etc/systemd/system/ | grep {app.name}", check=False),
        "journal": host.journal(since)[-6000:],
        "last playbook output": app.steps[-1].run.output[-6000:] if app.steps else "(no deploy ran)",
    }
    return "\n\n".join(f"--- {title} ---\n{body}" for title, body in sections.items())


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.rep_call = report
    return report
