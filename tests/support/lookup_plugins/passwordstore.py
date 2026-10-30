# Copyright (c) 2026 Daniel Pereira
#
# SPDX-License-Identifier: Apache-2.0

"""
Stand-in for community.general.passwordstore in tests.

The real lookup reads a password store on this machine, and anything it returned could end up in a
golden file committed to this public repository. This one never reads anything: every answer is
derived from the lookup's own arguments, so it is the same on every run and every machine.

It mimics the parts of the real lookup that templates commonly rely on:
- a plain entry returns a single-line password
- `subkey=hash` returns something shaped like a salted password hash (`$7$...`)
- any other subkey returns digits, so a template can pipe it through `| int`
- `returnall=true` returns several lines, the way a multi-line entry would
- `missing=empty` behaves as if the entry exists

Every call is recorded in `LookupModule.calls`, so a test can assert which secrets a deploy asked
for.
"""

import hashlib

# pylint: disable=import-error
from ansible.plugins.lookup import LookupBase  # type: ignore[import-not-found]

# pylint: enable=import-error

DOCUMENTATION = r"""
name: passwordstore
short_description: Deterministic test double for community.general.passwordstore
description:
  - Returns values derived from the lookup arguments. Never reads a password store.
options:
  _terms:
    description: Entry path, optionally followed by space-separated key=value options
    required: true
"""


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def stub_value(term: str) -> str:
    """
    Compute what the stub returns for one lookup term.

    Args:
        term: The lookup argument, e.g. "test/secret-1 subkey=hash missing_subkey=error"

    Returns:
        The deterministic value standing in for the secret
    """
    path, *option_words = term.split()
    options = dict(word.split("=", 1) for word in option_words if "=" in word)

    password = f"stub-{_digest(path)[:16]}"
    subkey = options.get("subkey")

    if subkey == "hash":
        digest = _digest(path, "hash")
        return f"$7$101${digest[:16]}${digest[16:48]}"
    if subkey:
        return str(int(_digest(path, subkey)[:8], 16) % 65536)
    if options.get("returnall", "").lower() == "true":
        return "\n".join([password, f"user: stub-{_digest(path, 'user')[:8]}", "notes: stub"])
    return password


class LookupModule(LookupBase):  # pylint: disable=too-few-public-methods
    """Deterministic passwordstore lookup."""

    # Every term looked up in this process, in order. Kept on the class rather than on an instance,
    # so it gathers the lookups of every instance Ansible creates.
    calls = []  # noqa: RUF012

    def run(self, terms, variables=None, **kwargs):
        values = []
        for term in terms:
            LookupModule.calls.append(term)
            values.append(stub_value(term))
        return values
