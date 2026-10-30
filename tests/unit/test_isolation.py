"""
Guards for the test environment itself.

If any of these fail, every other result is suspect: tests would be exercising some other copy of
the collection, or would be able to reach real secrets.
"""

import os
from pathlib import Path

from ansible.plugins.loader import lookup_loader

from ansible_collections.kriansa.quadlet.plugins.action import quadlet_app as action_plugin
from ansible_collections.kriansa.quadlet.plugins.module_utils import constants
from ansible_collections.kriansa.quadlet.plugins.modules import quadlet_app as module

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collection_code_comes_from_this_checkout():
    for imported in (module, constants, action_plugin):
        real_path = Path(imported.__file__).resolve()
        assert real_path.is_relative_to(REPO_ROOT), real_path
        # A copy installed by ansible-galaxy or ansible-lint lives under some .ansible directory
        assert ".ansible" not in real_path.relative_to(REPO_ROOT).parts


def test_passwordstore_resolves_to_the_stub():
    plugin = lookup_loader.find_plugin("passwordstore")
    assert plugin is not None
    assert Path(plugin).resolve() == REPO_ROOT / "tests/support/lookup_plugins/passwordstore.py"


def test_real_passwordstore_cannot_be_resolved():
    assert lookup_loader.find_plugin("community.general.passwordstore") is None


def test_password_store_directory_does_not_exist():
    assert not Path(os.environ["PASSWORD_STORE_DIR"]).exists()


def test_only_the_isolated_collections_root_is_configured():
    roots = os.environ["ANSIBLE_COLLECTIONS_PATH"].split(os.pathsep)
    assert len(roots) == 1
    linked = Path(roots[0]) / "ansible_collections" / "kriansa" / "quadlet"
    assert linked.resolve() == REPO_ROOT
