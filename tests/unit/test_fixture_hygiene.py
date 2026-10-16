"""
The fixtures may only carry test values.

This repository is public. The example apps are made up, and these checks keep them that way: a
real address, e-mail or secret-store path added to a fixture - or a golden regenerated from one -
fails here before it can be published.
"""

import ipaddress
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.]*\d)")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# The first argument of a passwordstore lookup, when it is written as a literal string
SECRET_PATH_RE = re.compile(r"""passwordstore['"]\s*,\s*['"]([^'"\s]+)""")

ALLOWED_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "192.0.2.0/24",  # TEST-NET-1
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "198.18.0.0/15",  # benchmarking
        "127.0.0.0/8",
        "0.0.0.0/8",
        "255.0.0.0/8",  # netmasks
    )
]


def find_problems(text):
    """Every value in text that is not a test value."""
    problems = []

    for match in IPV4_RE.finditer(text):
        if any(int(part) > 255 for part in match.groups()):
            continue
        address = ipaddress.ip_address(match.group(0))
        if not any(address in network for network in ALLOWED_NETWORKS):
            problems.append(f"IPv4 address outside the documentation ranges: {address}")

    for match in EMAIL_RE.finditer(text):
        if not match.group(0).endswith("@example.test"):
            problems.append(f"e-mail address outside example.test: {match.group(0)}")

    for match in SECRET_PATH_RE.finditer(text):
        if not match.group(1).startswith("test/"):
            problems.append(f"secret-store path outside test/: {match.group(1)}")

    return problems


FILES = sorted(path for path in FIXTURES_DIR.rglob("*") if path.is_file())


@pytest.mark.parametrize("path", FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in FILES])
def test_only_test_values(path):
    problems = find_problems(path.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)


def test_real_looking_values_are_caught():
    """Negative control: the check above has to be able to fail."""
    text = (
        "Environment=HOST=10.1.2.3\n"
        "Environment=MAIL=someone@mail.invalid\n"
        "Environment=PW={{ lookup('passwordstore', 'Infra/Db/Password') }}\n"
    )
    assert len(find_problems(text)) == 3
