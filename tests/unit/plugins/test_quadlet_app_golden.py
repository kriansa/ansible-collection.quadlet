"""
Characterization tests: quadlet_app over every example app, compared against golden files.

These do not say the output is correct. They say it has not changed - including any bugs the
baseline has - which is exactly the guarantee a refactor needs.

Goldens live in tests/fixtures/golden/quadlet_app/<app>/:
- rendered/  what the action plugin sends to the host (Stage A)
- deployed/  what the module writes on a fresh host, at its real paths (Stage B)
- result.json  the module's result and every command it ran (Stage B)
"""

import re

import pytest

from ansible_collections.kriansa.quadlet.tests.unit._support.examples import (
    example_apps,
    example_vars,
)
from ansible_collections.kriansa.quadlet.tests.unit._support.golden import (
    GOLDEN_DIR,
    check_json,
    check_tree,
    read_tree,
)
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app import (
    deploy_fresh,
    files_data_to_tree,
    tree_to_files_data,
)
from ansible_collections.kriansa.quadlet.tests.unit._support.quadlet_app_render import render_app

APPS = example_apps()
GOLDEN = GOLDEN_DIR / "quadlet_app"


@pytest.mark.parametrize("app", APPS, ids=[app.id for app in APPS])
def test_rendered(request, app):
    files_data = render_app(app.path, example_vars())
    check_tree(request, GOLDEN / app.id / "rendered", files_data_to_tree(files_data))


@pytest.mark.parametrize("app", APPS, ids=[app.id for app in APPS])
def test_deployed(request, monkeypatch, app):
    rendered = read_tree(GOLDEN / app.id / "rendered")
    if not rendered:
        pytest.fail(f"no rendered golden for {app.id}; run with --update-goldens first")

    deployment = deploy_fresh(monkeypatch, tree_to_files_data(rendered), app.name)

    assert deployment.failure is None, deployment.failure
    check_tree(request, GOLDEN / app.id / "deployed", deployment.tree)
    check_json(request, GOLDEN / app.id / "result.json", deployment.summary())


# Each feature the example apps exist to exercise, as a pattern one of their files must match.
# Removing the last example of a feature makes this fail, instead of quietly shrinking coverage.
FEATURES = {
    "a .container": r"\.container$",
    "a .volume": r"\.volume$",
    "a .network": r"\.network$",
    "a .pod": r"\.pod$",
    "a .kube": r"\.kube$",
    "a .timer": r"\.timer$",
    "an init/ file": r"^init/",
    "a nested config/ file": r"^config/[^/]+/.+",
}
CONTENT_FEATURES = {
    "a declared volume owner": r"^X-Owner=",
    "a volume without a declared owner": r"^\[Volume\]\n(?!.*X-Owner)",
    "UserNS=host": r"^UserNS=host$",
    "a bare UserNS=auto": r"^UserNS=auto$",
    "an explicit UserNS size": r"^UserNS=auto:size=",
    "a %config% mount": r"%config%",
    "an %init% mount": r"%init%",
    "a named volume mount": r"^Volume=\w[\w-]*\.volume:",
    "a pod membership": r"^Pod=",
    "a timer naming its unit": r"^Unit=",
    "a cross-app network": r"^Network=\w[\w-]*--",
    "a passwordstore lookup": r"lookup\('passwordstore'",
    "the app name variable": r"quadlet_app_name",
    "a Jinja block statement": r"\{%",
    "a Jinja comment": r"\{#",
}


def test_examples_cover_every_feature():
    files = {}
    for app in APPS:
        for path in app.path.rglob("*"):
            if path.is_file():
                files[f"{app.name}/{path.relative_to(app.path).as_posix()}"] = path.read_text()

    missing = [
        feature for feature, pattern in FEATURES.items()
        if not any(re.search(pattern, rel.split("/", 1)[1]) for rel in files)
    ] + [
        feature for feature, pattern in CONTENT_FEATURES.items()
        if not any(re.search(pattern, text, re.MULTILINE | re.DOTALL) for text in files.values())
    ]
    assert not missing, f"no example app exercises: {', '.join(missing)}"
