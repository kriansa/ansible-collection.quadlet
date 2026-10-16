"""
Golden files: snapshots of what the code produced, compared byte for byte.

A golden tree is a directory of files. Comparing one reports every missing, extra and different
file at once, with a diff, so a single run shows the whole effect of a change.

Goldens are only ever written with `pytest --update-goldens`, and only from the baseline
quadlet_app code: a golden captured from modified code would pin the modification instead of
the behaviour it is meant to protect.
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"

# The commit the quadlet_app goldens were captured from
BASELINE_COMMIT = "9678cea"
BASELINE_FILES = ("plugins/action/quadlet_app.py", "plugins/modules/quadlet_app.py")


def ensure_baseline_code() -> None:
    """Refuse to write goldens unless quadlet_app is exactly as it was at the baseline commit."""
    result = subprocess.run(
        ["git", "diff", "--quiet", BASELINE_COMMIT, "--", *BASELINE_FILES],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"quadlet_app differs from {BASELINE_COMMIT}; goldens must be captured from the "
            "baseline code, never from a modified copy"
        )


def read_tree(root: Path) -> Dict[str, str]:
    """Every file under root, keyed by its path relative to root."""
    if not root.is_dir():
        return {}
    tree = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with open(path, encoding="utf-8", newline="") as handle:
                tree[path.relative_to(root).as_posix()] = handle.read()
    return tree


def write_tree(root: Path, files: Dict[str, str]) -> None:
    """Replace root with exactly these files."""
    if root.exists():
        shutil.rmtree(root)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)


def _describe(expected: Dict[str, str], actual: Dict[str, str], label: str) -> List[str]:
    problems = []
    for rel in sorted(expected.keys() - actual.keys()):
        problems.append(f"{label}/{rel}: missing (golden has it, the code no longer produces it)")
    for rel in sorted(actual.keys() - expected.keys()):
        problems.append(f"{label}/{rel}: unexpected (the code produces it, the golden lacks it)")
    for rel in sorted(expected.keys() & actual.keys()):
        if expected[rel] != actual[rel]:
            diff = "".join(
                difflib.unified_diff(
                    expected[rel].splitlines(keepends=True),
                    actual[rel].splitlines(keepends=True),
                    fromfile=f"golden/{rel}",
                    tofile=f"actual/{rel}",
                )
            )
            problems.append(f"{label}/{rel}: differs\n{diff}")
    return problems


def check_tree(request, root: Path, actual: Dict[str, str]) -> None:
    """
    Compare files against a golden tree, or rewrite the tree under --update-goldens.

    Args:
        request: The pytest request of the calling test
        root: The golden tree directory
        actual: What the code produced, keyed by relative path
    """
    if request.config.getoption("--update-goldens"):
        ensure_baseline_code()
        write_tree(root, actual)
        return

    if not root.is_dir():
        pytest.fail(f"no golden at {root.relative_to(REPO_ROOT)}; run with --update-goldens")
    problems = _describe(read_tree(root), actual, root.relative_to(GOLDEN_DIR).as_posix())
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


def check_json(request, path: Path, actual) -> None:
    """Compare a JSON-serialisable value against a golden JSON file."""
    rendered = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if request.config.getoption("--update-goldens"):
        ensure_baseline_code()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    if not path.is_file():
        pytest.fail(f"no golden at {path.relative_to(REPO_ROOT)}; run with --update-goldens")
    problems = _describe({path.name: path.read_text(encoding="utf-8")}, {path.name: rendered},
                         path.parent.relative_to(GOLDEN_DIR).as_posix())
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)
