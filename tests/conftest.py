"""
Session setup shared by every test layer.

This file is imported before any test module, and it has to run before anything imports Ansible:
Ansible reads its configuration once, at import time.

It makes sure every test runs against the code in this repository and nothing else. This machine
has other installed copies of this collection (under ~/.ansible, and one created by ansible-lint
under ./.ansible) that Ansible would otherwise prefer. It also has the real passwordstore lookup
installed, and a test that resolved real secrets could end up committing them into a golden file
of this public repository. So the only collection Ansible can see is a symlink to this checkout,
and `passwordstore` resolves to a deterministic stub.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
STUB_LOOKUP_DIR = TESTS_DIR / "support" / "lookup_plugins"


def _collection_finder_installed() -> bool:
    """Whether a test runner (ansible-test units) already set up the collection importer."""
    return any(type(finder).__name__ == "_AnsibleCollectionFinder" for finder in sys.meta_path)


def _isolate_ansible() -> Path:
    """
    Build a collections root holding only this checkout, and point Ansible at it.

    Returns:
        The directory containing `ansible_collections/`
    """
    # When the checkout already sits at <root>/ansible_collections/kriansa/commons - as it does
    # under ansible-test - that root can be used as it is
    if REPO_ROOT.parent.name == "kriansa" and REPO_ROOT.parent.parent.name == "ansible_collections":
        root = REPO_ROOT.parent.parent.parent
    else:
        root = Path(tempfile.mkdtemp(prefix="kriansa-commons-tests-"))
        namespace_dir = root / "ansible_collections" / "kriansa"
        namespace_dir.mkdir(parents=True)
        (namespace_dir / "commons").symlink_to(REPO_ROOT)
        atexit.register(shutil.rmtree, root, ignore_errors=True)

    ansible_home = Path(tempfile.mkdtemp(prefix="kriansa-commons-ansible-home-"))
    atexit.register(shutil.rmtree, ansible_home, ignore_errors=True)

    os.environ.update(
        {
            "ANSIBLE_COLLECTIONS_PATH": str(root),
            # A pip-installed `ansible` package ships its own ansible_collections on sys.path
            "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False",
            # Keeps /etc/ansible/ansible.cfg and ~/.ansible.cfg out of the picture
            "ANSIBLE_CONFIG": str(TESTS_DIR / "ansible.cfg"),
            "ANSIBLE_HOME": str(ansible_home),
            "ANSIBLE_LOOKUP_PLUGINS": str(STUB_LOOKUP_DIR),
            # Second line of defence: even a real passwordstore lookup finds no store
            "PASSWORD_STORE_DIR": "/nonexistent",
        }
    )
    return root


COLLECTIONS_ROOT = _isolate_ansible()

if not _collection_finder_installed():
    # pylint: disable-next=wrong-import-position
    from ansible.plugins.loader import init_plugin_loader

    init_plugin_loader()


def pytest_configure(config):
    # Registered here rather than in pyproject.toml so they also exist under ansible-test, which
    # runs pytest with a configuration of its own and --strict-markers
    for marker in (
        "integration: boots an AlmaLinux VM and deploys to it",
        "quadlet: needs the podman quadlet generator on this machine",
        "slow: waits on real timeouts or timers",
    ):
        config.addinivalue_line("markers", marker)


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Rewrite golden files from the current code instead of comparing against them",
    )
